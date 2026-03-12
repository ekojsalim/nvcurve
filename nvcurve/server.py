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
from .hal.gpu import get_gpu
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
    "gpu": None,
    "gpu_name": "",
    "gpu_index": 0,
    "write_lock": None,
    "last_offsets": None,       # list[int] — for reconciliation
    "active_profile": None,     # str | None — last applied profile name
    "config": default_config,
    "monitor_clients": set(),   # connected WS clients for /ws/monitor
    "curve_clients": set(),     # connected WS clients for /ws/curve
}


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
        "is_idle": p.is_idle,
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

async def _monitor_poller() -> None:
    """Continuously poll GPU state and push to connected monitor WebSocket clients."""
    cfg: Config = _state["config"]
    while True:
        try:
            gpu = _state["gpu"]
            if gpu is not None and _state["monitor_clients"]:
                loop = asyncio.get_event_loop()
                sample = await loop.run_in_executor(
                    None, poll, gpu, _state["gpu_index"]
                )
                await _broadcast(_state["monitor_clients"], _sample_dict(sample))
        except Exception as exc:
            log.warning("Monitor poller error: %s", exc)
        await asyncio.sleep(cfg.poll_interval_s)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["write_lock"] = asyncio.Lock()
    loop = asyncio.get_running_loop()

    # Initialize GPU (blocking)
    try:
        gpu, name = await loop.run_in_executor(None, get_gpu, _state["gpu_index"])
        _state["gpu"] = gpu
        _state["gpu_name"] = name
        log.info("GPU: %s", name)
    except SystemExit:
        log.error("Failed to initialize GPU — is the driver loaded?")
        raise RuntimeError("GPU initialization failed")

    # Initialize NVML (best-effort)
    await loop.run_in_executor(None, init_nvml)

    # Read initial offsets for reconciliation baseline
    offsets, err = await loop.run_in_executor(None, read_clock_offsets, gpu)
    if offsets:
        _state["last_offsets"] = offsets

    # Start background monitor poller
    poller_task = asyncio.create_task(_monitor_poller())

    yield  # server is running

    poller_task.cancel()
    try:
        await poller_task
    except asyncio.CancelledError:
        pass

    await loop.run_in_executor(None, shutdown_nvml)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="nvcurve", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    deltas: dict[int, int]          # {point_index: delta_kHz}
    force_idle: bool = False
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


# ── Helper: run blocking HAL call in thread pool ──────────────────────────────

async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def _require_gpu():
    gpu = _state["gpu"]
    if gpu is None:
        raise HTTPException(status_code=503, detail="GPU not initialized")
    return gpu


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/gpu")
async def api_gpu():
    """GPU info: name, driver version, VRAM."""
    gpu = _require_gpu()
    driver = get_driver_version()
    vram = get_vram_total(_state["gpu_index"])
    return {
        "name": _state["gpu_name"],
        "index": _state["gpu_index"],
        "driver_version": driver,
        "vram_bytes": vram,
        "vram_gib": round(vram / (1024 ** 3), 2) if vram else None,
    }


@app.get("/api/curve")
async def api_curve():
    """Full CurveState: all 128 V/F points with base freq, voltage, delta, effective freq."""
    gpu = _require_gpu()
    state, err = await _run(read_curve, gpu, _state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")

    # Update reconciliation baseline
    _state["last_offsets"] = [p.delta_khz for p in state.points]
    return _curve_state_dict(state)


@app.get("/api/curve/{point}")
async def api_curve_point(point: int):
    """Single V/F point detail."""
    if point < 0 or point > 127:
        raise HTTPException(status_code=400, detail="Point index must be 0–127")
    gpu = _require_gpu()
    state, err = await _run(read_curve, gpu, _state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")
    return _vfpoint_dict(state.points[point])


@app.get("/api/ranges")
async def api_ranges():
    """Clock boost domain ranges (min/max offset per domain)."""
    gpu = _require_gpu()
    ranges, err = await _run(get_clock_ranges, gpu)
    if ranges is None:
        raise HTTPException(status_code=500, detail=f"Failed to read ranges: {err}")
    return ranges


@app.get("/api/voltage")
async def api_voltage():
    """Current GPU core voltage."""
    from .hal.monitoring import read_voltage
    gpu = _require_gpu()
    voltage_uv, err = await _run(read_voltage, gpu)
    if voltage_uv is None:
        raise HTTPException(status_code=500, detail=f"Failed to read voltage: {err}")
    return {"voltage_uv": voltage_uv, "voltage_mv": voltage_uv / 1000.0}


@app.get("/api/monitor")
async def api_monitor():
    """One-shot monitoring snapshot: voltage, clock, temp, power, fan, p-state, VRAM, utilization."""
    gpu = _require_gpu()
    sample = await _run(poll, gpu, _state["gpu_index"])
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


@app.get("/api/profiles")
async def api_profiles():
    """List saved native profiles and the currently active profile name."""
    cfg: Config = _state["config"]
    profiles = await _run(list_profiles, cfg.profile_dir)
    return {"profiles": profiles, "active": _state["active_profile"]}


@app.post("/api/profiles")
async def api_profile_save(req: ProfileSaveRequest):
    """Save current GPU state (curve deltas + limits) as a named profile."""
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    state, err = await _run(read_curve, gpu, _state["gpu_name"])
    if state is None:
        raise HTTPException(status_code=500, detail=f"Failed to read curve: {err}")

    curve_deltas = {str(p.index): p.delta_khz for p in state.points if p.delta_khz != 0}

    try:
        power_info = await _run(get_power_limit, _state["gpu_index"])
        offsets = await _run(get_clock_offsets, _state["gpu_index"])
        power_limit_w = power_info.get("power_limit_w")
        mem_offset_mhz = offsets.get("mem_offset_mhz")
    except Exception:
        power_limit_w = None
        mem_offset_mhz = None

    data = ProfileData(
        name=req.name,
        gpu_name=_state["gpu_name"],
        curve_deltas=curve_deltas,
        mem_offset_mhz=mem_offset_mhz,
        power_limit_w=power_limit_w,
    )
    filepath = await _run(save_profile, cfg.profile_dir, data)
    _state["active_profile"] = req.name
    return {"ok": True, "filepath": filepath}


@app.post("/api/profiles/{name}/apply")
async def api_profile_apply(name: str):
    """Apply a saved profile to hardware (curve deltas + limits)."""
    import os as _os
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = _os.path.join(cfg.profile_dir, f"{safe_name}.json")
    try:
        profile = await _run(load_profile, filepath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load profile: {e}")

    errs = []

    # Apply mem offset first — driver may reset curve table as a side-effect.
    if profile.mem_offset_mhz is not None:
        ok, msg = await _run(set_clock_offsets, None, profile.mem_offset_mhz, _state["gpu_index"])
        if not ok:
            errs.append(f"Mem offset: {msg}")

    if profile.power_limit_w is not None:
        ok, msg = await _run(set_power_limit, profile.power_limit_w, _state["gpu_index"])
        if not ok:
            errs.append(f"Power limit: {msg}")

    # Apply curve deltas (after mem offset which may have wiped them).
    async with _state["write_lock"]:
        if profile.curve_deltas:
            deltas = {int(k): v for k, v in profile.curve_deltas.items()}
            errors = validate_write(deltas, cfg.max_delta_khz)
            if errors:
                errs.append("Curve: " + "; ".join(errors))
            else:
                if cfg.auto_snapshot:
                    await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)
                ret, desc = await _run(write_offsets, gpu, deltas)
                if ret != 0:
                    errs.append(f"Curve write failed ({ret}): {desc}")
                else:
                    offsets_read, _ = await _run(read_clock_offsets, gpu)
                    _state["last_offsets"] = offsets_read
        else:
            await _run(reset_offsets, gpu)
            offsets_read, _ = await _run(read_clock_offsets, gpu)
            _state["last_offsets"] = offsets_read

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    _state["active_profile"] = name
    return {"ok": True}


@app.delete("/api/profiles/{name}")
async def api_profile_delete(name: str):
    """Delete a saved profile by name."""
    cfg: Config = _state["config"]
    ok = await _run(delete_profile, cfg.profile_dir, name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    if _state["active_profile"] == name:
        _state["active_profile"] = None
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
    if _state["active_profile"] == name:
        _state["active_profile"] = req.new_name.strip()
    return {"ok": True}


@app.get("/api/limits")
async def api_limits():
    """Current performance limits: power and clock offsets."""
    gpu_index = _state["gpu_index"]
    power = await _run(get_power_limit, gpu_index)
    offsets = await _run(get_clock_offsets, gpu_index)
    mem_off_range = await _run(get_mem_offset_range, gpu_index)
    return {
        **power,
        **offsets,           # gpc_offset_mhz, mem_offset_mhz
        **mem_off_range,     # min_mem_offset_mhz, max_mem_offset_mhz
    }


@app.post("/api/limits")
async def api_limits_update(req: LimitsRequest):
    """Update performance limits."""
    gpu_index = _state["gpu_index"]
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
            await _reapply_curve()

    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))

    _state["active_profile"] = None

    return {"ok": True}


async def _reapply_curve() -> None:
    """Re-write the last known V/F curve offsets to hardware and notify WS clients."""
    gpu = _state["gpu"]
    last = _state["last_offsets"]
    if gpu is None or not last:
        return
    deltas = {i: off for i, off in enumerate(last) if off != 0}
    if not deltas:
        return
    try:
        await _run(write_offsets, gpu, deltas)
        offsets, _ = await _run(read_clock_offsets, gpu)
        _state["last_offsets"] = offsets
        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))
    except Exception as exc:
        log.warning("_reapply_curve: %s", exc)


@app.post("/api/limits/reset")
async def api_limits_reset():
    """Reset power limit to hardware default and memory clock offset to 0."""
    gpu_index = _state["gpu_index"]
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
        await _reapply_curve()

    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))

    _state["active_profile"] = None

    return {"ok": True}


# ── Write endpoints ────────────────────────────────────────────────────────────

async def _reconcile_check() -> dict | None:
    """Re-read current offsets and return a warning dict if they differ from our last known state.

    Returns None if no external change detected (or no baseline).
    """
    gpu = _state["gpu"]
    last = _state["last_offsets"]
    if last is None:
        return None

    current, err = await _run(read_clock_offsets, gpu)
    if current is None:
        return None  # Can't read — let the write attempt proceed

    changed = [i for i, (a, b) in enumerate(zip(last, current)) if a != b]
    if not changed:
        return None

    # External tool changed the curve — active profile is no longer current.
    _state["active_profile"] = None

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
async def api_curve_write(req: WriteRequest):
    """Write per-point frequency offsets. {deltas: {point_index: delta_kHz}}"""
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    effective_limit = req.max_delta_khz if req.max_delta_khz is not None else cfg.max_delta_khz
    errors = validate_write(req.deltas, effective_limit, allow_idle=req.force_idle)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # Check for negative-freq warnings before writing (best-effort, non-blocking)
    freq_warnings: list[str] = []
    vfp_points, _ = await _run(read_vfp_curve, gpu)
    if vfp_points:
        vfp_freqs = [f for f, _v in vfp_points]
        freq_warnings = check_negative_freq_warnings(
            req.deltas, vfp_freqs, _state["last_offsets"] or []
        )

    async with _state["write_lock"]:
        warning = await _reconcile_check()

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)

        ret, desc = await _run(write_offsets, gpu, req.deltas)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        # Update baseline and push curve update to WS clients
        offsets, _ = await _run(read_clock_offsets, gpu)
        _state["last_offsets"] = offsets

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

        _state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    if freq_warnings:
        result["freq_warnings"] = freq_warnings
    return result


@app.post("/api/curve/write/global")
async def api_curve_write_global(req: GlobalOffsetRequest):
    """Apply a uniform frequency offset to all non-idle points."""
    from .nvapi.constants import CT_POINTS, IDLE_POINT
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    all_deltas = {i: req.delta_khz for i in range(CT_POINTS) if i != IDLE_POINT}
    effective_limit = req.max_delta_khz if req.max_delta_khz is not None else cfg.max_delta_khz
    errors = validate_write(all_deltas, effective_limit)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    freq_warnings: list[str] = []
    vfp_points, _ = await _run(read_vfp_curve, gpu)
    if vfp_points:
        vfp_freqs = [f for f, _v in vfp_points]
        freq_warnings = check_negative_freq_warnings(
            all_deltas, vfp_freqs, _state["last_offsets"] or []
        )

    async with _state["write_lock"]:
        warning = await _reconcile_check()

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)

        ret, desc = await _run(write_global_offset, gpu, req.delta_khz)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        offsets, _ = await _run(read_clock_offsets, gpu)
        _state["last_offsets"] = offsets

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

        _state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    if freq_warnings:
        result["freq_warnings"] = freq_warnings
    return result


@app.post("/api/curve/reset")
async def api_curve_reset():
    """Reset all frequency offsets to zero."""
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    async with _state["write_lock"]:
        warning = await _reconcile_check()

        if cfg.auto_snapshot:
            await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)

        ret, desc = await _run(reset_offsets, gpu)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Reset failed ({ret}): {desc}")

        offsets, _ = await _run(read_clock_offsets, gpu)
        _state["last_offsets"] = offsets

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

        _state["active_profile"] = None

    result = {"ok": True, "return_code": ret, "description": desc}
    if warning:
        result["warning"] = warning
    return result


@app.post("/api/curve/verify")
async def api_curve_verify(req: VerifyRequest):
    """Write-verify-read cycle. Returns per-point match results and collateral changes."""
    from .nvapi.constants import CT_POINTS
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    errors = validate_write(req.deltas, cfg.max_delta_khz)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    before_offsets, err = await _run(read_clock_offsets, gpu)
    if before_offsets is None:
        raise HTTPException(status_code=500, detail=f"Failed to read current state: {err}")

    # Always snapshot before verify — it's a testing operation
    await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)

    async with _state["write_lock"]:
        ret, desc = await _run(write_offsets, gpu, req.deltas)
        if ret != 0:
            raise HTTPException(status_code=500, detail=f"Write failed ({ret}): {desc}")

        await asyncio.sleep(0.2)

        after_offsets, err = await _run(read_clock_offsets, gpu)
        if after_offsets is None:
            raise HTTPException(status_code=500, detail=f"Verification read failed: {err}")

        _state["last_offsets"] = after_offsets
        _state["active_profile"] = None

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

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
        for i in range(CT_POINTS)
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
    loop = asyncio.get_event_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True}


@app.post("/api/snapshot/save")
async def api_snapshot_save():
    """Save a ClockBoostTable snapshot."""
    gpu = _require_gpu()
    cfg: Config = _state["config"]
    path = await _run(snapshot_save, gpu, _state["gpu_name"], cfg.snapshot_dir)
    if path is None:
        raise HTTPException(status_code=500, detail="Failed to save snapshot")
    return {"ok": True, "filepath": path}


@app.post("/api/snapshot/restore")
async def api_snapshot_restore(req: SnapshotRestoreRequest):
    """Restore a ClockBoostTable snapshot. Uses most recent if filepath not specified."""
    gpu = _require_gpu()
    cfg: Config = _state["config"]

    async with _state["write_lock"]:
        ok = await _run(snapshot_restore, gpu, cfg.snapshot_dir, req.filepath)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to restore snapshot")

        offsets, _ = await _run(read_clock_offsets, gpu)
        _state["last_offsets"] = offsets

        if _state["curve_clients"]:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await _broadcast(_state["curve_clients"], _curve_state_dict(state))

        _state["active_profile"] = None

    return {"ok": True}


# ── WebSocket endpoints ───────────────────────────────────────────────────────

@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    """Stream MonitoringSample at poll_interval_s. Clients receive JSON objects."""
    await ws.accept()
    _state["monitor_clients"].add(ws)
    try:
        # Send an immediate first sample so the client doesn't wait
        gpu = _state["gpu"]
        if gpu is not None:
            sample = await _run(poll, gpu, _state["gpu_index"])
            await ws.send_json(_sample_dict(sample))

        # Keep connection open — poller handles subsequent pushes
        while True:
            await ws.receive_text()  # wait for client to disconnect or ping
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _state["monitor_clients"].discard(ws)


@app.websocket("/ws/curve")
async def ws_curve(ws: WebSocket):
    """Push CurveState whenever the curve changes (after writes)."""
    await ws.accept()
    _state["curve_clients"].add(ws)
    try:
        # Send current state immediately on connect
        gpu = _state["gpu"]
        if gpu is not None:
            state, _ = await _run(read_curve, gpu, _state["gpu_name"])
            if state:
                await ws.send_json(_curve_state_dict(state))

        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _state["curve_clients"].discard(ws)


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

    _state["gpu_index"] = gpu_index
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
