"""FastAPI API server — REST + WebSocket.

Run via:  nvcurve serve [--host 127.0.0.1 --port 8042]
Or:       uvicorn nvcurve.server:app

Requires root (NvAPI needs it).
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import Config, default_config
from .hal.gpu import get_gpu, discover_gpus
from .hal.monitoring import (
    get_driver_version,
    get_vram_total,
    init_nvml,
    poll,
    shutdown_nvml,
)
from .hal.ranges import get_clock_ranges
from .hal.snapshot import (
    list_snapshots,
    restore as snapshot_restore,
    save as snapshot_save,
)
from .hal.limits import (
    get_power_limit,
    set_power_limit,
    get_clock_offsets,
    set_clock_offsets,
    get_mem_offset_range,
)
from .profiles.native import (
    ProfileData,
    save_profile,
    load_profile,
    list_profiles,
    delete_profile,
    rename_profile,
)
from .hal.vfcurve import (
    read_clock_offsets,
    read_curve,
    read_vfp_curve,
    reset_offsets,
    write_global_offset,
    write_offsets,
)
from .safety import validate_write, check_negative_freq_warnings

log = logging.getLogger("nvcurve.server")


def _open_browser_as_user(url: str) -> None:
    """Open URL as the original (non-root) user when running under sudo."""
    import os
    import subprocess
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            subprocess.Popen(
                ["runuser", "-u", sudo_user, "--", "xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    import webbrowser
    webbrowser.open(url)

# ── Shared app state ──────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "gpus": {}, # dict[int, dict] mapping gpu_index -> gpu state
    "config": default_config,
}

def _get_gpu_state(gpu_index: int) -> dict:
    if gpu_index not in _state["gpus"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"GPU {gpu_index} not found")
    return _state["gpus"][gpu_index]


# ── Serialization helpers ─────────────────────────────────────────────────────

def _vfpoint_dict(p) -> dict:
    return {
        "index": p.index,
        "freq_khz": p.freq_khz,
        "freq_mhz": p.freq_mhz,
        "volt_uv": p.volt_uv,
        "volt_mv": p.volt_mv,
        "delta_khz": p.delta_khz,
        "delta_mhz": p.delta_mhz,
        "effective_freq_khz": p.effective_freq_khz,
        "effective_freq_mhz": p.effective_freq_mhz,
        "domain": p.domain,
    }


def _curve_state_dict(state) -> dict:
    return {
        "gpu_name": state.gpu_name,
        "timestamp": state.timestamp,
        "points": [_vfpoint_dict(p) for p in state.points],
    }


def _sample_dict(s) -> dict:
    return {
        "timestamp": s.timestamp,
        "voltage_uv": s.voltage_uv,
        "voltage_mv": s.voltage_uv / 1000.0 if s.voltage_uv is not None else None,
        "clock_mhz": s.clock_mhz,
        "mem_clock_mhz": s.mem_clock_mhz,
        "temp_c": s.temp_c,
        "power_w": s.power_w,
        "fan_pct": s.fan_pct,
        "pstate": s.pstate,
        "pstate_label": f"P{s.pstate}" if s.pstate is not None else None,
        "mem_used_bytes": s.mem_used_bytes,
        "mem_total_bytes": s.mem_total_bytes,
        "mem_used_mib": round(s.mem_used_bytes / (1024 ** 2), 1) if s.mem_used_bytes is not None else None,
        "mem_total_mib": round(s.mem_total_bytes / (1024 ** 2), 1) if s.mem_total_bytes is not None else None,
        "gpu_util_pct": s.gpu_util_pct,
        "mem_util_pct": s.mem_util_pct,
    }


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def _broadcast(clients: set, payload: dict) -> None:
    """Send JSON payload to all connected WebSocket clients, evict dead ones."""
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    clients -= dead


# ── Background monitoring poller ─────────────────────────────────────────────

async def _monitor_poller(gpu_index: int) -> None:
    """Continuously poll GPU state and push to connected monitor WebSocket clients."""
    cfg: Config = _state["config"]
    while True:
        try:
            g_state = _state["gpus"].get(gpu_index)
            if g_state and g_state["gpu"] is not None and g_state["monitor_clients"]:
                loop = asyncio.get_running_loop()
                sample = await loop.run_in_executor(
                    None, poll, g_state["gpu"], gpu_index
                )
                await _broadcast(g_state["monitor_clients"], _sample_dict(sample))
        except Exception as exc:
            log.warning("Monitor poller error for GPU %d: %s", gpu_index, exc)
        await asyncio.sleep(cfg.poll_interval_s)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    # Initialize NVML (best-effort)
    await loop.run_in_executor(None, init_nvml)

    gpu_infos = await loop.run_in_executor(None, discover_gpus)
    if not gpu_infos:
        log.warning("No GPUs discovered.")
    
    poller_tasks = []

    for info in gpu_infos:
        idx = info.index
        g_state = {
            "gpu": None,
            "gpu_name": info.name,
            "uuid": info.uuid,
            "pci_bus_id": info.pci_bus_id,
            "write_lock": asyncio.Lock(),
            "last_offsets": None,
            "active_profile": None,
            "monitor_clients": set(),
            "curve_clients": set(),
        }
        _state["gpus"][idx] = g_state
        
        try:
            gpu, name = await loop.run_in_executor(None, get_gpu, idx)
            g_state["gpu"] = gpu
            g_state["gpu_name"] = name
            log.info("GPU %d: %s", idx, name)
            
            # Read initial offsets for reconciliation baseline
            offsets, err = await loop.run_in_executor(None, read_clock_offsets, gpu)
            if offsets:
                g_state["last_offsets"] = offsets
                
            poller_tasks.append(asyncio.create_task(_monitor_poller(idx)))
        except Exception as exc:
            log.error("Failed to initialize GPU %d: %s", idx, exc)

    # ── Backward Compatibility Bridge ──────────────────────────────────────────
    # NOTE: This auto-load path is for users running the server directly (e.g.
    # via an old systemd unit file that lacks the new daemon mode).
    # In the future, this will be removed and auto-loading will be the
    # responsibility of daemon.py only.
    cfg: Config = _state["config"]
    if cfg.auto_load_profiles:
        # Build a reverse map: stable_key → current gpu_index
        key_to_idx = {_gpu_stable_key(idx): idx for idx in _state["gpus"]}
        for gpu_key, profile_name in cfg.auto_load_profiles.items():
            if not profile_name:
                continue
            gpu_idx = key_to_idx.get(gpu_key)
            if gpu_idx is None:
                log.warning("Auto-load: no GPU found with key %r — skipping", gpu_key)
                continue
            log.info("Auto-loading profile %r on GPU %d (%s) [compat path]",
                     profile_name, gpu_idx, gpu_key)
            try:
                await _auto_apply_profile_with_retry(profile_name, gpu_idx)
            except FileNotFoundError:
                log.warning("Auto-load profile %r not found in %s — skipping GPU %d",
                            profile_name, cfg.profile_dir, gpu_idx)
            except Exception as exc:
                log.warning("Auto-load profile %r failed on GPU %d: %s — skipping",
                            profile_name, gpu_idx, exc)
    # ──────────────────────────────────────────────────────────────────────────

    yield  # server is running

    for task in poller_tasks:
        task.cancel()
    for task in poller_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

    await loop.run_in_executor(None, shutdown_nvml)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="nvcurve", version="0.5.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    deltas: dict[int, int]          # {point_index: delta_kHz}
    max_delta_khz: int | None = None  # per-request safety limit override


class GlobalOffsetRequest(BaseModel):
    delta_khz: int
    max_delta_khz: int | None = None  # per-request safety limit override


class VerifyRequest(BaseModel):
    deltas: dict[int, int]          # {point_index: delta_kHz} — pre-expanded by CLI


class SnapshotRestoreRequest(BaseModel):
    filepath: str | None = None


class LimitsRequest(BaseModel):
    power_limit_w: int | None = None
    mem_offset_mhz: int | None = None


class ProfileSaveRequest(BaseModel):
    name: str


class ProfileRenameRequest(BaseModel):
    new_name: str


class ConfigUpdateRequest(BaseModel):
    auto_load_profile: str | None = None
    gpu_index: int = 0


# ── Helper: run blocking HAL call in thread pool ──────────────────────────────

async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def _require_gpu(gpu_index: int = 0):
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]
    if gpu is None:
        raise HTTPException(status_code=503, detail=f"GPU {gpu_index} not initialized")
    return gpu, g_state


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/gpus")
async def api_gpus():
    """List all discovered GPUs."""
    from .hal.gpu import discover_gpus
    gpu_infos = await _run(discover_gpus)
    return [
        {
            "index": info.index,
            "name": info.name,
            "uuid": info.uuid,
            "pci_bus_id": info.pci_bus_id,
        }
        for info in gpu_infos
    ]

@app.get("/api/gpu")
async def api_gpu(gpu_index: int = 0):
    """GPU info: name, driver version, VRAM."""
    gpu, g_state = _require_gpu(gpu_index)
    driver = get_driver_version()
    vram = get_vram_total(gpu_index)
    return {
        "name": g_state["gpu_name"],
        "index": gpu_index,
        "driver_version": driver,
        "vram_bytes": vram,
        "vram_gib": round(vram / (1024 ** 3), 2) if vram else None,
    }


@app.get("/api/curve")
async def api_curve(gpu_index: int = 0):
    """Full CurveState: all V/F points with base freq, voltage, delta, effective freq."""
    gpu, g_state = _require_gpu(gpu_index)
    state, err = await _run(read_curve, gpu, g_state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")

    # Update reconciliation baseline
    g_state["last_offsets"] = [p.delta_khz for p in state.points]
    return _curve_state_dict(state)


@app.get("/api/curve/{point}")
async def api_curve_point(point: int, gpu_index: int = 0):
    """Single V/F point detail."""
    gpu, g_state = _require_gpu(gpu_index)
    state, err = await _run(read_curve, gpu, g_state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")
    if point < 0 or point >= len(state.points):
        raise HTTPException(status_code=400, detail=f"Point index must be 0–{len(state.points)-1}")
    return _vfpoint_dict(state.points[point])


@app.get("/api/ranges")
async def api_ranges(gpu_index: int = 0):
    """Clock boost domain ranges (min/max offset per domain)."""
    gpu, g_state = _require_gpu(gpu_index)
    ranges, err = await _run(get_clock_ranges, gpu)
    if ranges is None:
        raise HTTPException(status_code=500, detail=f"Failed to read ranges: {err}")
    return ranges


@app.get("/api/voltage")
async def api_voltage(gpu_index: int = 0):
    """Current GPU core voltage."""
    from .hal.monitoring import read_voltage
    gpu, g_state = _require_gpu(gpu_index)
    voltage_uv, err = await _run(read_voltage, gpu)
    if voltage_uv is None:
        raise HTTPException(status_code=500, detail=f"Failed to read voltage: {err}")
    return {"voltage_uv": voltage_uv, "voltage_mv": voltage_uv / 1000.0}


@app.get("/api/monitor")
async def api_monitor(gpu_index: int = 0):
    """One-shot monitoring snapshot: voltage, clock, temp, power, fan, p-state, VRAM, utilization."""
    gpu, g_state = _require_gpu(gpu_index)
    sample = await _run(poll, gpu, gpu_index)
    return _sample_dict(sample)


@app.get("/api/snapshots")
async def api_snapshots():
    """List saved ClockBoostTable snapshots."""
    cfg: Config = _state["config"]
    snapshots = await _run(list_snapshots, cfg.snapshot_dir)
    return [
        {
            "filepath": s.filepath,
            "timestamp": s.timestamp,
            "gpu": s.gpu,
            "nonzero_offsets": s.nonzero_offsets,
            "size": s.size,
        }
        for s in snapshots
    ]


def _persist_config_field(key: str, value) -> None:
    """Write a single key into /etc/nvcurve/config.json if the file exists.

    The file is created by `service install`. If it doesn't exist (e.g. the
    service was never installed), config changes are in-memory only for the
    current server session. Silently ignores errors.
    """
    import json as _json
    import os as _os
    config_path = "/etc/nvcurve/config.json"
    if not _os.path.exists(config_path):
        return
    try:
        with open(config_path) as f:
            data = _json.load(f)
    except Exception:
        data = {}
    if value is not None:
        data[key] = value
    else:
        data.pop(key, None)
    try:
        with open(config_path, "w") as f:
            _json.dump(data, f)
    except Exception as exc:
        log.warning("Failed to persist config field %r: %s", key, exc)


def _gpu_stable_key(gpu_index: int) -> str:
    """Return a stable identifier for a GPU suitable for use as a config key.

    Preference order: NVML UUID → PCI bus ID → 'idx:{n}' fallback.
    UUID is the most stable across reboots and GPU slot changes.
    """
    g_state = _state["gpus"].get(gpu_index, {})
    uuid = g_state.get("uuid")
    if uuid:
        return uuid
    pci = g_state.get("pci_bus_id")
    if pci is not None:
        return f"pci:{pci:04x}"
    return f"idx:{gpu_index}"


def _persist_auto_load_profiles(profiles: dict[str, str]) -> None:
    """Persist auto_load_profiles dict to config.json."""
    _persist_config_field("auto_load_profiles", profiles if profiles else None)


@app.get("/api/profiles")
async def api_profiles(gpu_index: int = 0):
    """List saved native profiles, the active profile name, and the auto-load profile name."""
    cfg: Config = _state["config"]
    profiles = await _run(list_profiles, cfg.profile_dir)
    g_state = _state["gpus"].get(gpu_index)
    active = g_state["active_profile"] if g_state else None
    return {
        "profiles": profiles,
        "active": active,
        "auto_load": cfg.auto_load_profiles.get(_gpu_stable_key(gpu_index)),
    }


@app.post("/api/profiles")
async def api_profile_save(req: ProfileSaveRequest, gpu_index: int = 0):
    """Save current GPU state (curve deltas + limits) as a named profile."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    state, err = await _run(read_curve, gpu, g_state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")

    curve_deltas = {str(p.index): p.delta_khz for p in state.points if p.delta_khz != 0}

    try:
        power_info = await _run(get_power_limit, gpu_index)
        offsets = await _run(get_clock_offsets, gpu_index)
        power_limit_w = power_info.get("power_limit_w")
        mem_offset_mhz = offsets.get("mem_offset_mhz")
    except Exception:
        power_limit_w = None
        mem_offset_mhz = None

    data = ProfileData(
        name=req.name,
        gpu_name=g_state["gpu_name"],
        curve_deltas=curve_deltas,
        mem_offset_mhz=mem_offset_mhz,
        power_limit_w=power_limit_w,
    )
    filepath = await _run(save_profile, cfg.profile_dir, data)
    g_state["active_profile"] = req.name
    return {"ok": True, "filepath": filepath}


async def _auto_apply_profile_with_retry(
    name: str, gpu_index: int = 0, max_retries: int = 3
) -> None:
    """Apply profile on startup with read-back verification and exponential backoff retry.

    Raises FileNotFoundError if the profile does not exist (no point retrying).
    Logs a warning and gives up after max_retries failed attempts.
    """
    import os as _os
    cfg: Config = _state["config"]
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = _os.path.join(cfg.profile_dir, f"{safe_name}.json")
    profile = await _run(load_profile, filepath)  # raises FileNotFoundError if missing

    expected: dict[int, int] = (
        {int(k): v for k, v in profile.curve_deltas.items()} if profile.curve_deltas else {}
    )

    for attempt in range(max_retries):
        errs = await _apply_profile(name, gpu_index)

        if errs:
            log.warning("Auto-load attempt %d/%d had errors: %s",
                        attempt + 1, max_retries, "; ".join(errs))
        elif expected:
            offsets, err = await _run(read_clock_offsets, gpu)
            if offsets is None:
                log.warning("Auto-load attempt %d/%d: read-back failed: %s",
                            attempt + 1, max_retries, err)
            else:
                mismatches = [
                    f"pt{idx}: expected {val/1000:+.0f}MHz got {offsets[idx]/1000:+.0f}MHz"
                    for idx, val in expected.items()
                    if idx < len(offsets) and offsets[idx] != val
                ]
                if not mismatches:
                    log.info("Auto-load profile %r verified on GPU %d (attempt %d/%d)",
                             name, gpu_index, attempt + 1, max_retries)
                    return
                log.warning("Auto-load attempt %d/%d: read-back mismatch — %s",
                            attempt + 1, max_retries, "; ".join(mismatches))
        else:
            log.info("Auto-load profile %r applied on GPU %d (attempt %d/%d)",
                     name, gpu_index, attempt + 1, max_retries)
            return

        if attempt < max_retries - 1:
            delay = 2 ** attempt  # 1 s, 2 s, 4 s
            log.info("Retrying auto-load in %ds…", delay)
            await asyncio.sleep(delay)

    log.warning("Auto-load profile %r failed after %d attempts — giving up", name, max_retries)


async def _apply_profile(name: str, gpu_index: int = 0) -> list[str]:
    """Load and apply a saved profile to hardware.

    Returns a list of error strings. An empty list means success.
    Raises FileNotFoundError if the profile file does not exist.
    Sets g_state["active_profile"] on full success.
    """
    import os as _os
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]
    cfg: Config = _state["config"]

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = _os.path.join(cfg.profile_dir, f"{safe_name}.json")

    # Let FileNotFoundError propagate so callers can map it to 404 or a warning.
    profile = await _run(load_profile, filepath)

    errs: list[str] = []

    # Apply mem offset first — driver may reset curve table as a side-effect.
    if profile.mem_offset_mhz is not None:
        ok, msg = await _run(set_clock_offsets, None, profile.mem_offset_mhz, gpu_index)
        if not ok:
            errs.append(f"Mem offset: {msg}")

    if profile.power_limit_w is not None:
        ok, msg = await _run(set_power_limit, profile.power_limit_w, gpu_index)
        if not ok:
            errs.append(f"Power limit: {msg}")

    # Apply curve deltas (after mem offset which may have wiped them).
    async with g_state["write_lock"]:
        if profile.curve_deltas:
            deltas = {int(k): v for k, v in profile.curve_deltas.items()}
            errors = validate_write(deltas, cfg.max_delta_khz)
            if errors:
                errs.append("Curve: " + "; ".join(errors))
            else:
                if cfg.auto_snapshot:
                    await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)
                ret, desc = await _run(write_offsets, gpu, deltas)
                if ret != 0:
                    errs.append(f"Curve write failed ({ret}): {desc}")
        else:
            await _run(reset_offsets, gpu)

        await _update_offsets_and_broadcast(gpu_index)

    if not errs:
        g_state["active_profile"] = name
    return errs


@app.post("/api/profiles/{name}/apply")
async def api_profile_apply(name: str, gpu_index: int = 0):
    """Apply a saved profile to hardware (curve deltas + limits)."""
    _require_gpu(gpu_index)
    try:
        errs = await _apply_profile(name, gpu_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load profile: {e}")
    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    return {"ok": True}


@app.delete("/api/profiles/{name}")
async def api_profile_delete(name: str):
    """Delete a saved profile by name."""
    cfg: Config = _state["config"]
    ok = await _run(delete_profile, cfg.profile_dir, name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    for g_state in _state["gpus"].values():
        if g_state["active_profile"] == name:
            g_state["active_profile"] = None
    changed = any(v == name for v in cfg.auto_load_profiles.values())
    if changed:
        cfg.auto_load_profiles = {k: v for k, v in cfg.auto_load_profiles.items() if v != name}
        _persist_auto_load_profiles(cfg.auto_load_profiles)
    return {"ok": True}


@app.post("/api/profiles/{name}/rename")
async def api_profile_rename(name: str, req: ProfileRenameRequest):
    """Rename a profile."""
    cfg: Config = _state["config"]
    if not req.new_name.strip():
        raise HTTPException(status_code=400, detail="New name cannot be empty")
    ok = await _run(rename_profile, cfg.profile_dir, name, req.new_name.strip())
    if not ok:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    for g_state in _state["gpus"].values():
        if g_state["active_profile"] == name:
            g_state["active_profile"] = req.new_name.strip()
    changed = any(v == name for v in cfg.auto_load_profiles.values())
    if changed:
        cfg.auto_load_profiles = {
            k: (req.new_name.strip() if v == name else v)
            for k, v in cfg.auto_load_profiles.items()
        }
        _persist_auto_load_profiles(cfg.auto_load_profiles)
    return {"ok": True}


@app.get("/api/config")
async def api_config_get(gpu_index: int = 0):
    """Get mutable server configuration for a specific GPU."""
    cfg: Config = _state["config"]
    return {"auto_load_profile": cfg.auto_load_profiles.get(_gpu_stable_key(gpu_index))}


@app.post("/api/config")
async def api_config_update(req: ConfigUpdateRequest):
    """Update mutable server configuration. Changes persist to /etc/nvcurve/config.json if present."""
    if req.gpu_index not in _state["gpus"]:
        raise HTTPException(status_code=404, detail=f"GPU {req.gpu_index} not found")
    cfg: Config = _state["config"]
    key = _gpu_stable_key(req.gpu_index)
    if req.auto_load_profile:
        cfg.auto_load_profiles[key] = req.auto_load_profile
    else:
        cfg.auto_load_profiles.pop(key, None)
    _persist_auto_load_profiles(cfg.auto_load_profiles)
    return {"ok": True, "auto_load_profile": cfg.auto_load_profiles.get(key)}


@app.get("/api/limits")
async def api_limits(gpu_index: int = 0):
    """Current performance limits: power and clock offsets."""
    power = await _run(get_power_limit, gpu_index)
    offsets = await _run(get_clock_offsets, gpu_index)
    mem_off_range = await _run(get_mem_offset_range, gpu_index)
    return {
        **power,
        **offsets,           # gpc_offset_mhz, mem_offset_mhz
        **mem_off_range,     # min_mem_offset_mhz, max_mem_offset_mhz
    }


@app.post("/api/limits")
async def api_limits_update(req: LimitsRequest, gpu_index: int = 0):
    """Update performance limits."""
    g_state = _get_gpu_state(gpu_index)
    errs = []

    if req.power_limit_w is not None:
        ok, msg = await _run(set_power_limit, req.power_limit_w, gpu_index)
        if not ok:
            errs.append(f"Power Limit: {msg}")

    if req.mem_offset_mhz is not None:
        ok, msg = await _run(set_clock_offsets, None, req.mem_offset_mhz, gpu_index)
        if not ok:
            errs.append(f"Mem Offset: {msg}")
        else:
            # Setting mem offset may reset the GPC/curve table as a driver side-effect.
            # Re-apply the last known curve offsets to restore them.
            await _reapply_curve(gpu_index)

    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))

    g_state["active_profile"] = None

    return {"ok": True}


async def _reapply_curve(gpu_index: int) -> None:
    """Re-write the last known V/F curve offsets to hardware and notify WS clients."""
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]
    last = g_state["last_offsets"]
    if gpu is None or not last:
        return
    deltas = {i: off for i, off in enumerate(last) if off != 0}
    if not deltas:
        return
    try:
        await _run(write_offsets, gpu, deltas)
        await _update_offsets_and_broadcast(gpu_index)
    except Exception as exc:
        log.warning("_reapply_curve: %s", exc)


async def _update_offsets_and_broadcast(gpu_index: int) -> None:
    """Re-read curve offsets, update the reconciliation baseline, and push to WS clients.

    When curve WS clients are connected, a single read_curve call covers both
    updating the baseline and the broadcast payload — avoiding a redundant
    read_clock_offsets (ClockBoostTable) call that would otherwise happen first.
    """
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]
    if gpu is None:
        return
    if g_state["curve_clients"]:
        state, _ = await _run(read_curve, gpu, g_state["gpu_name"])
        if state:
            g_state["last_offsets"] = [p.delta_khz for p in state.points]
            await _broadcast(g_state["curve_clients"], _curve_state_dict(state))
    else:
        offsets, _ = await _run(read_clock_offsets, gpu)
        g_state["last_offsets"] = offsets


@app.post("/api/limits/reset")
async def api_limits_reset(gpu_index: int = 0):
    """Reset power limit to hardware default and memory clock offset to 0."""
    g_state = _get_gpu_state(gpu_index)
    errs = []

    power = await _run(get_power_limit, gpu_index)
    default_w = power.get("default_power_limit_w")
    if default_w is not None:
        ok, msg = await _run(set_power_limit, default_w, gpu_index)
        if not ok:
            errs.append(f"Power Limit: {msg}")

    ok, msg = await _run(set_clock_offsets, None, 0, gpu_index)
    if not ok:
        errs.append(f"Mem Offset: {msg}")
    else:
        await _reapply_curve(gpu_index)

    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))

    g_state["active_profile"] = None

    return {"ok": True}


# ── Write endpoints ────────────────────────────────────────────────────────────

async def _reconcile_check(gpu_index: int) -> dict | None:
    """Re-read current offsets and return a warning dict if they differ from our last known state.

    Returns None if no external change detected (or no baseline).
    """
    g_state = _get_gpu_state(gpu_index)
    gpu = g_state["gpu"]
    last = g_state["last_offsets"]
    if last is None:
        return None

    current, err = await _run(read_clock_offsets, gpu)
    if current is None:
        return None  # Can't read — let the write attempt proceed

    changed = [i for i, (a, b) in enumerate(zip(last, current)) if a != b]
    if not changed:
        return None

    # External tool changed the curve — active profile is no longer current.
    g_state["active_profile"] = None

    return {
        "warning": "external_change_detected",
        "message": (
            f"{len(changed)} point(s) changed since last read "
            f"(e.g. by LACT, nvidia-smi, or another tool). "
            "The write will proceed using the current hardware state."
        ),
        "changed_points": changed[:20],  # cap list for readability
    }


@app.post("/api/curve/write")
async def api_curve_write(req: WriteRequest, gpu_index: int = 0):
    """Write per-point frequency offsets. {deltas: {point_index: delta_kHz}}"""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    vfp_state, _ = await _run(read_curve, gpu, g_state["gpu_name"])

    effective_limit = req.max_delta_khz if req.max_delta_khz is not None else cfg.max_delta_khz
    errors = validate_write(req.deltas, effective_limit)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # Check for negative-freq warnings before writing (best-effort, non-blocking)
    freq_warnings: list[str] = []
    if vfp_state:
        vfp_freqs = [p.freq_khz for p in vfp_state.points]
        freq_warnings = check_negative_freq_warnings(
            req.deltas, vfp_freqs, g_state["last_offsets"] or []
        )

    async with g_state["write_lock"]:
        warning = await _reconcile_check(gpu_index)

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)

        ret, desc = await _run(write_offsets, gpu, req.deltas)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        # Update baseline and push curve update to WS clients
        await _update_offsets_and_broadcast(gpu_index)
        g_state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    if freq_warnings:
        result["freq_warnings"] = freq_warnings
    return result


@app.post("/api/curve/write/global")
async def api_curve_write_global(req: GlobalOffsetRequest, gpu_index: int = 0):
    """Apply a uniform frequency offset to all curve points."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    vfp_state, _ = await _run(read_curve, gpu, g_state["gpu_name"])
    if not vfp_state:
        raise HTTPException(status_code=500, detail="Failed to read curve")

    all_deltas = {p.index: req.delta_khz for p in vfp_state.points if p.domain == "gpu"}
    effective_limit = req.max_delta_khz if req.max_delta_khz is not None else cfg.max_delta_khz
    errors = validate_write(all_deltas, effective_limit)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    freq_warnings: list[str] = []
    if vfp_state:
        vfp_freqs = [p.freq_khz for p in vfp_state.points]
        freq_warnings = check_negative_freq_warnings(
            all_deltas, vfp_freqs, g_state["last_offsets"] or []
        )

    async with g_state["write_lock"]:
        warning = await _reconcile_check(gpu_index)

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)

        ret, desc = await _run(write_global_offset, gpu, req.delta_khz)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        await _update_offsets_and_broadcast(gpu_index)
        g_state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    if freq_warnings:
        result["freq_warnings"] = freq_warnings
    return result


@app.post("/api/curve/reset")
async def api_curve_reset(gpu_index: int = 0):
    """Reset all frequency offsets to zero."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    async with g_state["write_lock"]:
        warning = await _reconcile_check(gpu_index)

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)

        ret, desc = await _run(reset_offsets, gpu)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Reset failed ({ret}): {desc}")

        await _update_offsets_and_broadcast(gpu_index)
        g_state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    return result


@app.post("/api/curve/verify")
async def api_curve_verify(req: VerifyRequest, gpu_index: int = 0):
    """Write-verify-read cycle. Returns per-point match results and collateral changes."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    errors = validate_write(req.deltas, cfg.max_delta_khz)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    before_offsets, err = await _run(read_clock_offsets, gpu)
    if before_offsets is None:
        raise HTTPException(status_code=500, detail=f"Failed to read current state: {err}")

    # Always snapshot before verify — it's a testing operation
    await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)

    async with g_state["write_lock"]:
        ret, desc = await _run(write_offsets, gpu, req.deltas)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        await asyncio.sleep(0.2)

        after_offsets, err = await _run(read_clock_offsets, gpu)
        if after_offsets is None:
            raise HTTPException(status_code=500, detail=f"Verification read failed: {err}")

        g_state["active_profile"] = None
        await _update_offsets_and_broadcast(gpu_index)

    points_result = []
    all_matched = True
    for point, expected in sorted(req.deltas.items()):
        actual = after_offsets[point]
        match = actual == expected
        if not match:
            all_matched = False
        points_result.append({
            "point": point,
            "expected_khz": expected,
            "actual_khz": actual,
            "match": match,
        })

    collateral = [
        {"point": i, "before_khz": before_offsets[i], "after_khz": after_offsets[i]}
        for i in range(len(before_offsets))
        if i not in req.deltas and before_offsets[i] != after_offsets[i]
    ]

    return {
        "ok": all_matched and not collateral,
        "all_matched": all_matched,
        "no_side_effects": not collateral,
        "return_code": ret,
        "description": desc,
        "points": points_result,
        "collateral_changes": collateral,
    }


@app.post("/api/shutdown")
async def api_shutdown():
    """Gracefully shut down the server process."""
    import os
    import signal
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True}


@app.post("/api/snapshot/save")
async def api_snapshot_save(gpu_index: int = 0):
    """Save a ClockBoostTable snapshot."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]
    path = await _run(snapshot_save, gpu, g_state["gpu_name"], cfg.snapshot_dir, cfg.max_snapshots)
    if path is None:
        raise HTTPException(status_code=500, detail="Failed to save snapshot")
    return {"ok": True, "filepath": path}


@app.post("/api/snapshot/restore")
async def api_snapshot_restore(req: SnapshotRestoreRequest, gpu_index: int = 0):
    """Restore a ClockBoostTable snapshot. Uses most recent if filepath not specified."""
    gpu, g_state = _require_gpu(gpu_index)
    cfg: Config = _state["config"]

    async with g_state["write_lock"]:
        ok = await _run(snapshot_restore, gpu, cfg.snapshot_dir, req.filepath)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to restore snapshot")

        await _update_offsets_and_broadcast(gpu_index)
        g_state["active_profile"] = None

    return {"ok": True}


# ── WebSocket endpoints ───────────────────────────────────────────────────────

@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    """Stream MonitoringSample at poll_interval_s. Clients receive JSON objects."""
    await ws.accept()
    try:
        data = await ws.receive_json()
        if data.get("action") != "subscribe":
            await ws.close()
            return
        gpu_index = data.get("gpu_index", 0)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()
        return

    g_state = _state["gpus"].get(gpu_index)
    if not g_state:
        await ws.close()
        return

    g_state["monitor_clients"].add(ws)
    try:
        gpu = g_state["gpu"]
        if gpu is not None:
            sample = await _run(poll, gpu, gpu_index)
            await ws.send_json(_sample_dict(sample))

        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        g_state["monitor_clients"].discard(ws)


@app.websocket("/ws/curve")
async def ws_curve(ws: WebSocket):
    """Push CurveState whenever the curve changes (after writes)."""
    await ws.accept()
    try:
        data = await ws.receive_json()
        if data.get("action") != "subscribe":
            await ws.close()
            return
        gpu_index = data.get("gpu_index", 0)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()
        return

    g_state = _state["gpus"].get(gpu_index)
    if not g_state:
        await ws.close()
        return

    g_state["curve_clients"].add(ws)
    try:
        gpu = g_state["gpu"]
        if gpu is not None:
            state, _ = await _run(read_curve, gpu, g_state["gpu_name"])
            if state:
                await ws.send_json(_curve_state_dict(state))

        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        g_state["curve_clients"].discard(ws)


# ── Frontend SPA ──────────────────────────────────────────────────────────────

import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

# When set, suppresses the auto-open browser behaviour so the dev can open
# the Vite dev server (pnpm dev) manually instead.
_DEV_PORT = os.environ.get("NVCURVE_DEV_PORT")

# Robust asset resolution using importlib.resources
try:
    from importlib.resources import files as _resource_files
    # In a packaged installation, frontend/dist is inside the package
    _dist_dir = _resource_files("nvcurve") / "frontend" / "dist"
    
    # Fallback for local development where frontend/dist might be at project root
    if not _dist_dir.is_dir():
        _here = Path(__file__).parent
        _dist_dir = _here.parent / "frontend" / "dist"
except (ImportError, TypeError):
    # Legacy fallback for older Python or environments without importlib.resources.files
    _here = Path(__file__).parent
    _dist_dir = _here / "frontend" / "dist"
    if not _dist_dir.is_dir():
        _dist_dir = _here.parent / "frontend" / "dist"

_dist_dir = str(_dist_dir)

if os.path.isdir(os.path.join(_dist_dir, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(_dist_dir, "assets")), name="assets")

@app.get("/{catchall:path}")
async def serve_spa(catchall: str):
    if catchall.startswith("api/") or catchall.startswith("ws/"):
        raise HTTPException(status_code=404, detail="Not Found")

    if not os.path.isdir(_dist_dir):
        return {"error": "Frontend not built. Run pnpm build in frontend/."}

    path = os.path.join(_dist_dir, catchall)
    if os.path.isfile(path) and catchall:
        return FileResponse(path)

    index = os.path.join(_dist_dir, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)

    raise HTTPException(status_code=404, detail="Not Found")


# ── Factory for configured app ────────────────────────────────────────────────

def create_app(config: Config = default_config) -> FastAPI:
    """Create a server app with a custom config (e.g. different gpu_index)."""
    _state["config"] = config
    return app


def run(
    host: str = "127.0.0.1",
    port: int = 8042,
    gpu_index: int = 0,
    config: Config = default_config,
    open_browser: bool = False,
) -> None:
    """Start the uvicorn server. Blocking."""
    import socket
    import threading
    import uvicorn

    _state["config"] = config

    # Suppress noisy websockets keepalive ping-timeout tracebacks — these are
    # normal disconnection events (browser tab closed, network hiccup) and
    # logging them at ERROR level creates false alarm noise.
    logging.getLogger("websockets").setLevel(logging.CRITICAL)

    # Fail fast if the port is already in use — silently shifting ports breaks
    # client discovery. Users should configure a different port explicitly.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            print(f"Error: port {port} is already in use.")
            print(f"Use --port N to specify a different port, or free port {port} first.")
            return

    url = f"http://{host}:{port}"

    # Print banner *before* uvicorn starts so it appears above uvicorn's own output.
    # GPU name is populated by the lifespan; we omit it here since the server
    # hasn't started yet, and the lifespan logs it via log.info.
    print("\033[1;36m" + "─" * 60 + "\033[0m")
    print("\033[1;32m" + "  NVCurve".center(60) + "\033[0m")
    print(f"  {url}".center(60))
    print("\033[1;36m" + "─" * 60 + "\033[0m")
    print("  Press Ctrl+C to stop.")
    print()

    if open_browser and not _DEV_PORT:
        threading.Timer(1.2, lambda: _open_browser_as_user(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
