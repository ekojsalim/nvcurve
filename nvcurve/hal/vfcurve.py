"""Read and write the GPU V/F curve via NvAPI."""

import ctypes
import struct
from typing import Optional

from ..nvapi.bootstrap import nvcall, nvcall_raw
from ..nvapi.constants import (
    FUNC,
    VFP_SIZE, VFP_BASE, VFP_STRIDE,
    CT_SIZE, CT_BASE, CT_STRIDE, CT_DELTA_OFF, CT_POINTS,
)
from ..nvapi.types import VFPoint, CurveState


# ── Mask helpers ─────────────────────────────────────────────────────────────

def get_boost_mask(gpu) -> tuple[Optional[bytes], str]:
    """Read the canonical 32-byte clock boost mask from the driver.
    
    Returns (mask_bytes, "OK") or (None, error).
    """
    from ..nvapi.constants import MASK_SIZE
    def fill(b):
        for i in range(4, 4 + 32):
            b[i] = 0xFF
    d, err = nvcall(FUNC["GetClockBoostMask"], gpu, MASK_SIZE, ver=1, pre_fill=fill)
    if d and len(d) >= 36:
        return d[4:36], "OK"
    return None, err


def set_mask_bit(buf, point: int, offset: int = 4) -> None:
    """Set a single bit in the 256-bit mask for one point."""
    byte_idx = offset + (point // 8)
    bit_idx = point % 8
    buf[byte_idx] = int.from_bytes(buf[byte_idx:byte_idx + 1], "little") | (1 << bit_idx)


def set_mask_bits(buf, points: set[int], offset: int = 4) -> None:
    """Set mask bits for a set of points."""
    for p in points:
        set_mask_bit(buf, p, offset)


# ── Readers ───────────────────────────────────────────────────────────────────

def read_vfp_curve(gpu) -> tuple[Optional[list[tuple[int, int]]], str]:
    """Read the base V/F curve (frequency + voltage pairs).

    Returns ([(freq_kHz, volt_uV), ...], "OK") or (None, error).
    """
    mask, mask_err = get_boost_mask(gpu)
    if not mask:
        return None, f"GetClockBoostMask failed: {mask_err}"

    def fill(buf):
        for i in range(32):
            buf[4 + i] = mask[i]
        struct.pack_into("<I", buf, 0x14, 15)

    d, err = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill)
    if not d:
        return None, err

    points = []
    max_entries = (len(d) - VFP_BASE) // VFP_STRIDE
    for i in range(max_entries):
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
    mask, mask_err = get_boost_mask(gpu)
    if not mask:
        return None, f"GetClockBoostMask failed: {mask_err}"

    def fill(buf):
        for i in range(32):
            buf[4 + i] = mask[i]

    return nvcall(FUNC["GetClockBoostTable"], gpu, CT_SIZE, ver=1, pre_fill=fill)


def read_clock_table_parsed(gpu) -> tuple[Optional[list[tuple[int, int]]], str]:
    """Read per-point offsets and flags from the ClockBoostTable.

    Returns a list of (delta_kHz, flags) tuples, or (None, error).
    """
    d, err = read_clock_table_raw(gpu)
    if not d:
        return None, err

    entries = []
    max_entries = (len(d) - CT_BASE) // CT_STRIDE
    for i in range(max_entries):
        base_off = CT_BASE + i * CT_STRIDE
        flags = struct.unpack_from("<I", d, base_off)[0]
        delta = struct.unpack_from("<i", d, base_off + CT_DELTA_OFF)[0]
        entries.append((delta, flags))

    return entries, "OK"


def read_clock_offsets(gpu) -> tuple[Optional[list[int]], str]:
    """Read per-point frequency offsets (kHz, signed) from the ClockBoostTable.

    Returns a list of integers, or (None, error).
    """
    parsed, err = read_clock_table_parsed(gpu)
    if not parsed:
        return None, err

    offsets = [delta for delta, flags in parsed]
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

    ct_entries, ct_err = read_clock_table_parsed(gpu)
    if not ct_entries:
        return None, ct_err

    points = []
    for i, (freq_khz, volt_uv) in enumerate(vfp_points):
        delta_khz = ct_entries[i][0] if i < len(ct_entries) else 0
        flags = ct_entries[i][1] if i < len(ct_entries) else 0

        if flags == 1:
            # flags=1 marks the start of the memory clock domain. Stop here.
            break

        points.append(VFPoint(
            index=i,
            freq_khz=freq_khz,
            volt_uv=volt_uv,
            delta_khz=delta_khz,
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
    curve, err = read_curve(gpu)
    if not curve:
        return -999, f"Failed to read curve: {err}"
    
    point_deltas = {p.index: delta_khz for p in curve.points}
    return write_offsets(gpu, point_deltas, dry_run=dry_run)


def reset_offsets(gpu, dry_run: bool = False) -> tuple[int, str]:
    """Zero all frequency offsets."""
    curve, err = read_curve(gpu)
    if not curve:
        return -999, f"Failed to read curve: {err}"

    point_deltas = {p.index: 0 for p in curve.points}
    return write_offsets(gpu, point_deltas, dry_run=dry_run)
