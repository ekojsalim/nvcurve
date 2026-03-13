"""Save and restore ClockBoostTable snapshots to/from disk."""

import ctypes
import json
import os
import struct
from datetime import datetime
from typing import Optional

from ..nvapi.bootstrap import nvcall_raw
from ..nvapi.constants import FUNC, CT_SIZE, CT_BASE, CT_STRIDE, CT_DELTA_OFF, CT_POINTS
from ..nvapi.types import SnapshotInfo
from .vfcurve import read_clock_table_raw, get_boost_mask


def save(gpu, gpu_name: str, snapshot_dir: str, max_snapshots: int = 0) -> Optional[str]:
    """Save the current ClockBoostTable to disk.

    Writes both a binary .bin file and a human-readable .json metadata file.
    If max_snapshots > 0, deletes the oldest snapshots to stay within the limit.
    Returns the binary filepath on success, or None on failure.
    """
    raw, err = read_clock_table_raw(gpu)
    if not raw:
        print(f"Failed to read ClockBoostTable: {err}")
        return None

    os.makedirs(snapshot_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bin_path = os.path.join(snapshot_dir, f"clock_boost_table_{ts}.bin")
    meta_path = os.path.join(snapshot_dir, f"clock_boost_table_{ts}.json")

    with open(bin_path, "wb") as f:
        f.write(raw)

    offsets = []
    max_entries = (len(raw) - CT_BASE) // CT_STRIDE
    for i in range(max_entries):
        off = CT_BASE + i * CT_STRIDE + CT_DELTA_OFF
        delta = struct.unpack_from("<i", raw, off)[0]
        offsets.append(delta)

    meta = {
        "gpu": gpu_name,
        "timestamp": datetime.now().isoformat(),
        "file": bin_path,
        "size": len(raw),
        "offsets_kHz": offsets,
        "nonzero_offsets": sum(1 for o in offsets if o != 0),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Snapshot saved:")
    print(f"  Binary:   {bin_path}")
    print(f"  Metadata: {meta_path}")
    print(f"  Size:     {len(raw)} bytes")
    print(f"  Non-zero offsets: {meta['nonzero_offsets']}")

    if max_snapshots > 0:
        _prune_snapshots(snapshot_dir, max_snapshots)

    return bin_path


def _prune_snapshots(snapshot_dir: str, max_snapshots: int) -> None:
    """Delete oldest snapshots (both .bin and .json) to stay within max_snapshots."""
    bins = sorted(
        f for f in os.listdir(snapshot_dir) if f.endswith(".bin")
    )  # oldest first (lexicographic = chronological for our timestamp format)
    excess = len(bins) - max_snapshots
    for fname in bins[:excess]:
        stem = fname[:-4]  # strip .bin
        for ext in (".bin", ".json"):
            try:
                os.remove(os.path.join(snapshot_dir, stem + ext))
            except OSError:
                pass


def restore(gpu, snapshot_dir: str, filepath: str = None) -> bool:
    """Restore a ClockBoostTable snapshot from disk.

    If no filepath is given, uses the most recent snapshot in snapshot_dir.
    Returns True on success.
    """
    if filepath is None:
        if not os.path.isdir(snapshot_dir):
            print(f"No snapshots found in {snapshot_dir}")
            return False
        bins = sorted(
            [f for f in os.listdir(snapshot_dir) if f.endswith(".bin")],
            reverse=True,
        )
        if not bins:
            print(f"No snapshot .bin files in {snapshot_dir}")
            return False
        filepath = os.path.join(snapshot_dir, bins[0])

    if not os.path.isfile(filepath):
        print(f"Snapshot file not found: {filepath}")
        return False

    with open(filepath, "rb") as f:
        raw = f.read()

    if len(raw) != CT_SIZE:
        print(f"Snapshot size mismatch: expected {CT_SIZE}, got {len(raw)}")
        return False

    vw = struct.unpack_from("<I", raw, 0)[0]
    expected_vw = (1 << 16) | CT_SIZE
    if vw != expected_vw:
        print(f"Version word mismatch: 0x{vw:08X} (expected 0x{expected_vw:08X})")
        return False

    buf = ctypes.create_string_buffer(CT_SIZE)
    ctypes.memmove(buf, raw, CT_SIZE)

    # Full mask for restore — write all points
    mask, _ = get_boost_mask(gpu)
    if mask:
        for i in range(32):
            buf[4 + i] = mask[i]

    print(f"Restoring from: {filepath}")
    ret, desc = nvcall_raw(FUNC["SetClockBoostTable"], gpu, buf)
    print(f"SetClockBoostTable returned: {ret} ({desc})")
    return ret == 0


def list_snapshots(snapshot_dir: str) -> list[SnapshotInfo]:
    """Return metadata for all snapshots in snapshot_dir, newest first."""
    if not os.path.isdir(snapshot_dir):
        return []

    results = []
    for fname in sorted(os.listdir(snapshot_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        meta_path = os.path.join(snapshot_dir, fname)
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            bin_path = meta.get("file", meta_path.replace(".json", ".bin"))
            results.append(SnapshotInfo(
                filepath=bin_path,
                timestamp=meta.get("timestamp", ""),
                gpu=meta.get("gpu", ""),
                nonzero_offsets=meta.get("nonzero_offsets", 0),
                size=meta.get("size", 0),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    return results
