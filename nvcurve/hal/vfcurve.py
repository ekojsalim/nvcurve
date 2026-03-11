"""Read and write the GPU V/F curve via NvAPI."""

import ctypes
import struct
from typing import Optional

from ..nvapi.bootstrap import nvcall, nvcall_raw
from ..nvapi.constants import (
    FUNC,
    VFP_SIZE, VFP_BASE, VFP_STRIDE, VFP_POINTS,
    CT_SIZE, CT_BASE, CT_STRIDE, CT_DELTA_OFF, CT_POINTS,
)
from ..nvapi.types import VFPoint, CurveState


# ── Mask helpers ─────────────────────────────────────────────────────────────

def fill_mask_128(buf, offset: int = 4, nbytes: int = 16) -> None:
    """Set all 128 bits in the mask field (request all points)."""
    for i in range(offset, offset + nbytes):
        buf[i] = 0xFF


def set_mask_bit(buf, point: int, offset: int = 4) -> None:
    """Set a single bit in the 128-bit mask for one point."""
    byte_idx = offset + (point // 8)
    bit_idx = point % 8
    buf[byte_idx] = int.from_bytes(buf[byte_idx:byte_idx + 1], "little") | (1 << bit_idx)


def set_mask_bits(buf, points: set[int], offset: int = 4) -> None:
    """Set mask bits for a set of points."""
    for p in points:
        set_mask_bit(buf, p, offset)


# ── Readers ───────────────────────────────────────────────────────────────────

def read_vfp_curve(gpu) -> tuple[Optional[list[tuple[int, int]]], str]:
    """Read the 128-point base V/F curve (frequency + voltage pairs).

    Returns ([(freq_kHz, volt_uV), ...], "OK") or (None, error).
    """
    def fill(buf):
        fill_mask_128(buf)
        struct.pack_into("<I", buf, 0x14, 15)

    d, err = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill)
    if not d:
        return None, err

    points = []
    for i in range(VFP_POINTS):
        off = VFP_BASE + i * VFP_STRIDE
        freq = struct.unpack_from("<I", d, off)[0]
        volt = struct.unpack_from("<I", d, off + 4)[0]
        points.append((freq, volt))
    return points, "OK"


def read_clock_table_raw(gpu) -> tuple[Optional[bytes], str]:
    """Read the raw ClockBoostTable buffer.

    Used for snapshots, inspection, and as the baseline for writes.
    Returns (bytes, "OK") or (None, error).
    """
    def fill(buf):
        fill_mask_128(buf)

    return nvcall(FUNC["GetClockBoostTable"], gpu, CT_SIZE, ver=1, pre_fill=fill)


def read_clock_offsets(gpu) -> tuple[Optional[list[int]], str]:
    """Read per-point frequency offsets (kHz, signed) from the ClockBoostTable.

    Returns a list of CT_POINTS integers, or (None, error).
    """
    d, err = read_clock_table_raw(gpu)
    if not d:
        return None, err

    offsets = []
    max_entries = min(CT_POINTS, (len(d) - CT_BASE) // CT_STRIDE)
    for i in range(max_entries):
        off = CT_BASE + i * CT_STRIDE + CT_DELTA_OFF
        delta = struct.unpack_from("<i", d, off)[0]
        offsets.append(delta)

    while len(offsets) < CT_POINTS:
        offsets.append(0)

    return offsets, "OK"


def read_clock_entry_full(data: bytes, point: int) -> dict:
    """Extract all 9 raw fields from a single ClockBoostTable entry.

    Useful for diagnostics and verifying unknown fields.
    """
    base = CT_BASE + point * CT_STRIDE
    fields = {}
    for j in range(9):
        off = base + j * 4
        if j == 5:  # freqDelta is signed
            fields[f"field_{j:02d}_0x{j * 4:02X}"] = struct.unpack_from("<i", data, off)[0]
        else:
            fields[f"field_{j:02d}_0x{j * 4:02X}"] = struct.unpack_from("<I", data, off)[0]
    fields["freqDelta_kHz"] = fields["field_05_0x14"]
    return fields


def read_curve(gpu, gpu_name: str = "") -> tuple[Optional[CurveState], str]:
    """Read both the VFP curve and ClockBoostTable and merge into CurveState.

    Returns (CurveState, "OK") or (None, error).
    """
    import time
    vfp_points, vfp_err = read_vfp_curve(gpu)
    if not vfp_points:
        return None, vfp_err

    offsets, ct_err = read_clock_offsets(gpu)
    if not offsets:
        return None, ct_err

    points = []
    for i, (freq_khz, volt_uv) in enumerate(vfp_points):
        points.append(VFPoint(
            index=i,
            freq_khz=freq_khz,
            volt_uv=volt_uv,
            delta_khz=offsets[i],
            is_idle=(i == CT_POINTS - 1 and freq_khz < 1_000_000),
        ))

    return CurveState(points=points, timestamp=time.time(), gpu_name=gpu_name), "OK"


# ── Writers ───────────────────────────────────────────────────────────────────

def build_write_buffer(
    gpu,
    point_deltas: dict[int, int],
) -> tuple[Optional[ctypes.Array], str]:
    """Build a SetClockBoostTable buffer with specified per-point deltas.

    Strategy: read the current ClockBoostTable, modify only the targeted
    entries' freqDelta fields, set only the targeted mask bits (single-bit per
    write to avoid touching neighbouring points).

    Returns (mutable_buffer, "OK") or (None, error).
    """
    current_raw, err = read_clock_table_raw(gpu)
    if not current_raw:
        return None, f"Cannot read current ClockBoostTable: {err}"

    buf = ctypes.create_string_buffer(CT_SIZE)
    ctypes.memmove(buf, current_raw, CT_SIZE)

    # Rewrite version word explicitly
    struct.pack_into("<I", buf, 0, (1 << 16) | CT_SIZE)

    # Clear mask — set only bits for points we're writing
    for i in range(4, 4 + 16):
        buf[i] = 0x00

    set_mask_bits(buf, set(point_deltas.keys()))

    for point, delta_khz in point_deltas.items():
        off = CT_BASE + point * CT_STRIDE + CT_DELTA_OFF
        struct.pack_into("<i", buf, off, delta_khz)

    return buf, "OK"


def write_offsets(
    gpu,
    point_deltas: dict[int, int],
    dry_run: bool = False,
) -> tuple[int, str]:
    """Write per-point frequency offsets via SetClockBoostTable.

    Args:
        gpu: NvAPI GPU handle
        point_deltas: {point_index: delta_kHz} — only these points are written
        dry_run: if True, build the buffer but don't call the driver

    Returns (return_code, description).
    """
    buf, err = build_write_buffer(gpu, point_deltas)
    if buf is None:
        return -999, err

    if dry_run:
        return 0, "DRY RUN — buffer built but not sent to driver"

    return nvcall_raw(FUNC["SetClockBoostTable"], gpu, buf)


def write_global_offset(gpu, delta_khz: int, dry_run: bool = False) -> tuple[int, str]:
    """Apply a uniform frequency offset to all non-idle points."""
    from ..nvapi.constants import IDLE_POINT
    point_deltas = {i: delta_khz for i in range(CT_POINTS) if i != IDLE_POINT}
    return write_offsets(gpu, point_deltas, dry_run=dry_run)


def reset_offsets(gpu, dry_run: bool = False) -> tuple[int, str]:
    """Zero all frequency offsets (all non-idle points)."""
    from ..nvapi.constants import IDLE_POINT
    point_deltas = {i: 0 for i in range(CT_POINTS) if i != IDLE_POINT}
    return write_offsets(gpu, point_deltas, dry_run=dry_run)
