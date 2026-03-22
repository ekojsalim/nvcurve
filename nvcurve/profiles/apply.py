"""Apply saved profiles to hardware, with optional read-back verification."""

import json
import logging
import os
import sys
import time

log = logging.getLogger("nvcurve.profiles.apply")

_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"


def _gpu_stable_key(info) -> str:
    if info.uuid:
        return info.uuid
    if info.pci_bus_id is not None:
        return f"pci:{info.pci_bus_id:04x}"
    return f"idx:{info.index}"


def apply_profile(gpu_index: int, name: str, cfg) -> list[str]:
    """Apply a named profile to the given GPU. Returns a list of error strings."""
    from .native import load_profile
    from ..hal.gpu import get_gpu
    from ..hal.limits import set_clock_offsets, set_power_limit
    from ..hal.vfcurve import write_offsets, reset_offsets
    from ..hal.snapshot import save as snapshot_save
    from ..safety import validate_write

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = os.path.join(cfg.profile_dir, f"{safe_name}.json")

    profile = load_profile(filepath)  # raises FileNotFoundError if missing
    gpu, gpu_name = get_gpu(index=gpu_index)

    errs: list[str] = []

    # Apply mem offset first — driver may reset curve table as a side-effect.
    if profile.mem_offset_mhz is not None:
        ok, msg = set_clock_offsets(None, profile.mem_offset_mhz, gpu_index)
        if not ok:
            errs.append(f"Mem offset: {msg}")

    if profile.power_limit_w is not None:
        ok, msg = set_power_limit(profile.power_limit_w, gpu_index)
        if not ok:
            errs.append(f"Power limit: {msg}")

    if profile.curve_deltas:
        deltas = {int(k): v for k, v in profile.curve_deltas.items()}
        errors = validate_write(deltas, cfg.max_delta_khz)
        if errors:
            errs.append("Curve: " + "; ".join(errors))
        else:
            if cfg.auto_snapshot:
                try:
                    snapshot_save(gpu, gpu_name, cfg.snapshot_dir, cfg.max_snapshots)
                except Exception as exc:
                    log.warning("Auto-snapshot failed: %s", exc)
            ret, desc = write_offsets(gpu, deltas)
            if ret != 0:
                errs.append(f"Curve write failed ({ret}): {desc}")
    else:
        reset_offsets(gpu)

    return errs


def apply_with_retry(gpu_index: int, name: str, cfg, max_retries: int = 3) -> bool:
    """Apply a named profile with read-back verification, retrying on mismatch."""
    from .native import load_profile
    from ..hal.gpu import get_gpu
    from ..hal.vfcurve import read_clock_offsets

    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = os.path.join(cfg.profile_dir, f"{safe_name}.json")

    try:
        profile = load_profile(filepath)
    except FileNotFoundError:
        log.warning("Auto-load profile %r not found — skipping GPU %d", name, gpu_index)
        return False

    expected: dict[int, int] = (
        {int(k): v for k, v in profile.curve_deltas.items()}
        if profile.curve_deltas else {}
    )

    for attempt in range(max_retries):
        try:
            errs = apply_profile(gpu_index, name, cfg)
        except Exception as exc:
            log.warning("Auto-load attempt %d/%d exception: %s", attempt + 1, max_retries, exc)
            errs = [str(exc)]

        if errs:
            log.warning("Auto-load attempt %d/%d errors: %s",
                        attempt + 1, max_retries, "; ".join(errs))
        elif expected:
            gpu, _ = get_gpu(index=gpu_index)
            offsets, err = read_clock_offsets(gpu)
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
                    return True
                log.warning("Auto-load attempt %d/%d: read-back mismatch — %s",
                            attempt + 1, max_retries, "; ".join(mismatches))
        else:
            log.info("Auto-load profile %r applied on GPU %d (attempt %d/%d)",
                     name, gpu_index, attempt + 1, max_retries)
            return True

        if attempt < max_retries - 1:
            delay = 2 ** attempt  # 1s, 2s, 4s
            log.info("Retrying auto-load in %ds…", delay)
            time.sleep(delay)

    log.warning("Auto-load profile %r failed after %d attempts on GPU %d",
                name, max_retries, gpu_index)
    return False


def run_autoload() -> None:
    """Read config and apply all configured auto-load profiles. Requires root."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if os.geteuid() != 0:
        print("nvcurve autoload: must run as root", file=sys.stderr)
        sys.exit(1)

    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            cfg_data = json.load(f)
    except Exception:
        cfg_data = {}

    auto_load_profiles: dict = cfg_data.get("auto_load_profiles", {})
    if not auto_load_profiles:
        log.info("No auto-load profiles configured.")
        return

    from ..config import Config
    cfg = Config()
    for key in ("max_delta_khz", "auto_snapshot", "max_snapshots",
                "snapshot_dir", "profile_dir"):
        if key in cfg_data:
            setattr(cfg, key, cfg_data[key])

    from ..hal.gpu import init_nvapi, discover_gpus
    from ..hal.monitoring import init_nvml, shutdown_nvml

    try:
        init_nvapi()
    except Exception as exc:
        log.error("Failed to initialize NvAPI: %s", exc)
        sys.exit(1)

    init_nvml()  # best-effort
    gpus = discover_gpus()
    if not gpus:
        log.warning("No GPUs discovered.")

    key_to_idx = {_gpu_stable_key(info): info.index for info in gpus}
    for gpu_key, profile_name in auto_load_profiles.items():
        if not profile_name:
            continue
        gpu_idx = key_to_idx.get(gpu_key)
        if gpu_idx is None:
            log.warning("Auto-load: no GPU found with key %r — skipping", gpu_key)
            continue
        log.info("Auto-loading profile %r on GPU %d (%s)", profile_name, gpu_idx, gpu_key)
        apply_with_retry(gpu_idx, profile_name, cfg)

    shutdown_nvml()
