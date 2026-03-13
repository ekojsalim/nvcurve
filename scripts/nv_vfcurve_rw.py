#!/usr/bin/env python3
"""nv_vfcurve_rw.py — Read/Write NVIDIA GPU V/F curve via undocumented NvAPI (Linux).

Extended from nv_vfcurve.py with WRITE support for per-point frequency offsets.

┌─────────────────────────────────────────────────────────────────────┐
│  WARNING: Write operations modify GPU clocking behavior.           │
│  Excessive offsets can cause instability, crashes, or artifacts.   │
│  Use small deltas (+15 to +50 MHz) initially and verify.          │
│  The --dry-run flag lets you inspect what WOULD be written.       │
└─────────────────────────────────────────────────────────────────────┘

Write modes:
    sudo python3 nv_vfcurve_rw.py write --point 80 --delta 15
        Apply +15 MHz offset to point 80 only.

    sudo python3 nv_vfcurve_rw.py write --range 60-90 --delta 30
        Apply +30 MHz offset to points 60–90.

    sudo python3 nv_vfcurve_rw.py write --global --delta 50
        Apply +50 MHz offset to all GPU core points.

    sudo python3 nv_vfcurve_rw.py write --reset
        Reset all offsets to 0.

    Add --dry-run to any write command to preview without applying.

Read modes:
    sudo python3 nv_vfcurve_rw.py read              # Condensed V/F curve
    sudo python3 nv_vfcurve_rw.py read --full        # All points (GPU + mem)
    sudo python3 nv_vfcurve_rw.py read --json        # JSON output
    sudo python3 nv_vfcurve_rw.py read --raw         # Hex dumps
    sudo python3 nv_vfcurve_rw.py read --diag        # Probe all functions

Verify mode (read-verify-read cycle):
    sudo python3 nv_vfcurve_rw.py verify --point 80 --delta 15
        Write +15 MHz to point 80, then immediately re-read and confirm.

Snapshot mode (save/restore full ClockBoostTable):
    sudo python3 nv_vfcurve_rw.py snapshot save
    sudo python3 nv_vfcurve_rw.py snapshot restore

Struct layouts verified by hex-dump analysis on RTX 5090 (GB202, Blackwell),
driver 590.48.01. Cross-referenced with nvapioc (Demion), nvapi-rs (arcnmx),
and ccminer (tpruvot/cbuchner1).

Key findings:
  - Mask must come from GetClockBoostMask (all-0xFF fails on Pascal)
  - Table holds up to 255 entries: GPU core + memory
  - Point 127 (on RTX 5090) is the start of the memory domain (field_00=1), not idle
  - Points 128+ are memory V/F entries
  - freqDelta scaling is generation-dependent (÷2 on Pascal, ÷1 on Blackwell)
  - NVML SetGpcClkVfOffset and SetClockBoostTable share state (last writer wins)

See NvAPI_VF_Curve_Documentation.md for full technical details.
"""

import ctypes
import struct
import sys
import json
import os
import time
import argparse
from datetime import datetime
from typing import Optional, List, Tuple, Set, Dict

# ═══════════════════════════════════════════════════════════════════════════
# NvAPI bootstrap
# ═══════════════════════════════════════════════════════════════════════════

def load_nvapi():
    """Load libnvidia-api.so from the NVIDIA driver."""
    for name in ("libnvidia-api.so", "libnvidia-api.so.1"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("Error: Cannot load libnvidia-api.so")
    print("Ensure the NVIDIA proprietary driver is installed.")
    sys.exit(1)


nvapi = load_nvapi()
QI = nvapi.nvapi_QueryInterface
QI.restype = ctypes.c_void_p
QI.argtypes = [ctypes.c_uint32]

# NvAPI error codes (Linux uses negative integers, not 0x80000000+ range)
NVAPI_ERRORS = {
    0: "OK",
    -1: "GENERIC_ERROR",
    -5: "INVALID_ARGUMENT",
    -6: "NVIDIA_DEVICE_NOT_FOUND",
    -7: "END_ENUMERATION",
    -8: "INVALID_HANDLE",
    -9: "INCOMPATIBLE_STRUCT_VERSION",
    -10: "HANDLE_INVALIDATED",
    -14: "INVALID_POINTER",
}


def nvfunc(fid: int, nargs: int = 2):
    """Resolve an NvAPI function pointer by its ID."""
    ptr = QI(fid)
    if not ptr:
        return None
    return ctypes.CFUNCTYPE(ctypes.c_int32, *[ctypes.c_void_p] * nargs)(ptr)


def nvcall(fid: int, gpu, size: int, ver: int = 1, pre_fill=None):
    """Call an NvAPI function with a versioned struct buffer.

    Returns (bytes, error_string) tuple.
    """
    func = nvfunc(fid)
    if not func:
        return None, "function pointer not found (driver too old?)"
    buf = ctypes.create_string_buffer(size)
    struct.pack_into("<I", buf, 0, (ver << 16) | size)
    if pre_fill:
        pre_fill(buf)
    ret = func(gpu, buf)
    if ret != 0:
        return None, f"error {ret} ({NVAPI_ERRORS.get(ret, 'unknown')})"
    return bytes(buf), "OK"


def nvcall_raw(fid: int, gpu, buf: ctypes.Array):
    """Call an NvAPI function with a pre-built mutable buffer.

    Used for write operations where we need full control over buffer contents.
    Returns (return_code, error_string).
    """
    func = nvfunc(fid)
    if not func:
        return -999, "function pointer not found (driver too old?)"
    ret = func(gpu, buf)
    return ret, NVAPI_ERRORS.get(ret, f"unknown ({ret})")


# ═══════════════════════════════════════════════════════════════════════════
# NvAPI function IDs — confirmed across nvapioc, nvapi-rs, ccminer
# ═══════════════════════════════════════════════════════════════════════════

FUNC = {
    # Bootstrap
    "Initialize":          0x0150E828,
    "EnumPhysicalGPUs":    0xE5AC921F,
    "GetFullName":         0xCEEE8E9F,

    # V/F curve (read)
    "GetVFPCurve":         0x21537AD4,  # ClkVfPointsGetStatus
    "GetClockBoostMask":   0x507B4B59,  # ClkVfPointsGetInfo
    "GetClockBoostTable":  0x23F1B133,  # ClkVfPointsGetControl
    "GetCurrentVoltage":   0x465F9BCF,  # ClientVoltRailsGetStatus
    "GetClockBoostRanges": 0x64B43A6A,  # ClkDomainsGetInfo

    # Additional read
    "GetPerfLimits":       0xE440B867,  # PerfClientLimitsGetStatus
    "GetVoltBoostPercent": 0x9DF23CA1,  # ClientVoltRailsGetControl

    # Write
    "SetClockBoostTable":  0x0733E009,  # ClkVfPointsSetControl
}

# ═══════════════════════════════════════════════════════════════════════════
# Struct layout constants — verified by hex dump on RTX 5090
# Buffer sizes confirmed identical on Pascal (TITAN X) and Blackwell (5090).
# ═══════════════════════════════════════════════════════════════════════════

# GetVFPCurve (0x21537AD4)
VFP_SIZE     = 0x1C28
VFP_BASE     = 0x48
VFP_STRIDE   = 0x1C  # 28 bytes
VFP_MAX_ENTRIES = (VFP_SIZE - VFP_BASE) // VFP_STRIDE  # 255

# Get/SetClockBoostTable (0x23F1B133 / 0x0733E009)
CT_SIZE      = 0x2420
CT_BASE      = 0x44
CT_STRIDE    = 0x24  # 36 bytes
CT_DELTA_OFF = 0x14  # freqDelta offset within entry
CT_MAX_ENTRIES = (CT_SIZE - CT_BASE) // CT_STRIDE  # 255

# GetClockBoostMask (0x507B4B59)
MASK_SIZE    = 0x182C

# Other structs
VOLT_SIZE    = 0x004C
RANGES_SIZE  = 0x0928
PERF_SIZE    = 0x030C
VBOOST_SIZE  = 0x0028

# Mask location within VFP/CT structs
MASK_OFFSET  = 0x04
MASK_BYTES   = 32    # 256 bits — covers up to 256 points

# Safety constants
MAX_DELTA_KHZ = 300_000     # ±300 MHz hard cap for safety

SNAPSHOT_DIR = os.path.expanduser("~/.cache/nv_vfcurve")


# ═══════════════════════════════════════════════════════════════════════════
# GPU initialization
# ═══════════════════════════════════════════════════════════════════════════

def init_gpu() -> tuple:
    """Initialize NvAPI, enumerate GPUs, return (handle, name)."""
    init_fn = nvfunc(FUNC["Initialize"], 0)
    if not init_fn or init_fn() != 0:
        print("NvAPI_Initialize failed")
        sys.exit(1)

    gpus = (ctypes.c_void_p * 64)()
    ngpu = ctypes.c_int32()
    nvfunc(FUNC["EnumPhysicalGPUs"])(ctypes.byref(gpus), ctypes.byref(ngpu))
    if ngpu.value == 0:
        print("No NVIDIA GPUs found")
        sys.exit(1)

    gpu = gpus[0]
    name_buf = ctypes.create_string_buffer(256)
    nvfunc(FUNC["GetFullName"])(gpu, name_buf)
    return gpu, name_buf.value.decode(errors="replace")


# ═══════════════════════════════════════════════════════════════════════════
# Boost mask — the canonical source of which points are active
#
# Per nvapioc (Demion/nvapioc), the mask from GetClockBoostMask MUST be
# copied into GetVFPCurve and Get/SetClockBoostTable calls. Using all-0xFF
# works on Blackwell but fails on Pascal with GENERIC_ERROR (-1).
#
# The mask struct (0x182C bytes) contains per-entry enable/type info that
# also distinguishes GPU core vs memory clock domains.
# ═══════════════════════════════════════════════════════════════════════════

class BoostMask:
    """Parsed GetClockBoostMask data.

    Provides the raw mask bytes for copying into other calls, plus
    parsed per-entry enabled info for filtering.
    """
    def __init__(self, raw: bytes):
        self.raw = raw
        self.size = len(raw)

        # The mask field at offset 0x04, 16 bytes — same position as in VFP/CT structs
        self.mask_bytes = raw[MASK_OFFSET:MASK_OFFSET + MASK_BYTES]

        self.entries = []
        self._parse_entries()

    def _parse_entries(self):
        """Parse which points have their mask bit set."""
        for i in range(min(CT_MAX_ENTRIES, MASK_BYTES * 8)):
            byte_idx = i // 8
            bit_idx = i % 8
            enabled = bool(self.mask_bytes[byte_idx] & (1 << bit_idx))
            self.entries.append({"index": i, "enabled": enabled})

    def get_enabled_indices(self) -> List[int]:
        """Return list of point indices that are enabled in the mask."""
        return [e["index"] for e in self.entries if e["enabled"]]

    def count_enabled(self) -> int:
        return sum(1 for e in self.entries if e["enabled"])

    def copy_mask_into(self, buf, offset=MASK_OFFSET):
        """Copy the canonical mask bytes into a target buffer."""
        for i in range(MASK_BYTES):
            buf[offset + i] = self.mask_bytes[i]


def read_boost_mask(gpu) -> Tuple[Optional[BoostMask], str]:
    """Read the clock boost mask — the canonical source of active point info.

    Per nvapioc, this mask must be copied into VFP and ClockBoostTable calls.
    Using all-0xFF works on some GPUs (Blackwell) but fails on others (Pascal).
    """
    def fill(buf):
        for i in range(MASK_OFFSET, MASK_OFFSET + MASK_BYTES):
            buf[i] = 0xFF

    d, err = nvcall(FUNC["GetClockBoostMask"], gpu, MASK_SIZE, ver=1, pre_fill=fill)
    if not d:
        return None, err

    return BoostMask(d), "OK"


# ═══════════════════════════════════════════════════════════════════════════
# Point classification — GPU core vs memory
# ═══════════════════════════════════════════════════════════════════════════

class CurveInfo:
    """Holds classified point information for the GPU's V/F curve.

    Combines data from GetClockBoostMask, GetVFPCurve, and GetClockBoostTable
    to determine which points are GPU core and which are memory.
    """
    def __init__(self):
        self.gpu_points: List[int] = []       # GPU core V/F point indices
        self.mem_points: List[int] = []       # Memory V/F point indices
        self.total_points: int = 0            # Total populated entries
        self.mask: Optional[BoostMask] = None

    @staticmethod
    def build(gpu, mask: Optional[BoostMask] = None) -> 'CurveInfo':
        """Classify all points by reading CT field_00 and VFP data.

        field_00 == 0: GPU core data point
        field_00 == 1: start of memory domain (this point and subsequent are memory points)
        """
        info = CurveInfo()
        info.mask = mask

        # Read ClockBoostTable to check field_00
        ct_raw = _read_clock_table_raw_with_mask(gpu, mask)
        if not ct_raw:
            # Fallback: assume 128 GPU points
            info.gpu_points = list(range(128))
            info.total_points = 128
            return info

        # Read VFP curve for frequency data
        vfp_points = _read_vfp_with_mask(gpu, mask)

        # Scan all possible entries
        in_memory_domain = False
        for i in range(CT_MAX_ENTRIES):
            off = CT_BASE + i * CT_STRIDE
            if off + CT_STRIDE > len(ct_raw):
                break

            field_00 = struct.unpack_from("<I", ct_raw, off)[0]

            # Check if this entry has any data
            has_vfp_data = False
            if vfp_points and i < len(vfp_points):
                f, v = vfp_points[i]
                has_vfp_data = (f > 0 or v > 0)

            has_ct_data = False
            for j in range(9):
                val = struct.unpack_from("<I", ct_raw, off + j * 4)[0]
                if val != 0:
                    has_ct_data = True
                    break

            if not has_vfp_data and not has_ct_data:
                # Empty entry — end of data
                info.total_points = i
                break

            if field_00 == 1:
                in_memory_domain = True

            if in_memory_domain:
                info.mem_points.append(i)
            else:
                info.gpu_points.append(i)

            info.total_points = i + 1

        return info

    def describe(self) -> str:
        parts = [f"{len(self.gpu_points)} GPU core points"]
        if self.mem_points:
            parts.append(f"{len(self.mem_points)} memory points")
        parts.append(f"{self.total_points} total")
        return ", ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Data readers (mask-aware)
# ═══════════════════════════════════════════════════════════════════════════

def _fill_mask_from_boost(buf, mask: BoostMask):
    """Copy boost mask into buffer."""
    mask.copy_mask_into(buf)


def _read_vfp_with_mask(gpu, mask: Optional[BoostMask]) -> Optional[List[Tuple[int, int]]]:
    """Read VFP curve using the canonical boost mask."""
    def fill(buf):
        _fill_mask_from_boost(buf, mask)
        struct.pack_into("<I", buf, 0x14, 15)

    d, err = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill)
    if not d:
        return None

    points = []
    for i in range(VFP_MAX_ENTRIES):
        off = VFP_BASE + i * VFP_STRIDE
        if off + 8 > len(d):
            break
        freq = struct.unpack_from("<I", d, off)[0]
        volt = struct.unpack_from("<I", d, off + 4)[0]
        points.append((freq, volt))
    return points


def _read_clock_table_raw_with_mask(gpu, mask: Optional[BoostMask]) -> Optional[bytes]:
    """Read raw ClockBoostTable using the canonical boost mask."""
    def fill(buf):
        _fill_mask_from_boost(buf, mask)

    d, err = nvcall(FUNC["GetClockBoostTable"], gpu, CT_SIZE, ver=1, pre_fill=fill)
    return d if d else None


def read_vfp_curve(gpu, mask: Optional[BoostMask] = None,
                   curve_info: Optional[CurveInfo] = None
                   ) -> Tuple[Optional[List[Tuple[int, int]]], str]:
    """Read V/F curve (frequency + voltage pairs).

    Returns up to 255 entries. Use curve_info to determine which are GPU/mem.
    """
    def fill(buf):
        _fill_mask_from_boost(buf, mask)
        struct.pack_into("<I", buf, 0x14, 15)

    d, err = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill)
    if not d:
        return None, err

    max_entries = VFP_MAX_ENTRIES
    if curve_info and curve_info.total_points > 0:
        max_entries = curve_info.total_points

    points = []
    for i in range(max_entries):
        off = VFP_BASE + i * VFP_STRIDE
        if off + 8 > len(d):
            break
        freq = struct.unpack_from("<I", d, off)[0]
        volt = struct.unpack_from("<I", d, off + 4)[0]
        points.append((freq, volt))

    return points, "OK"


def read_clock_table_raw(gpu, mask: Optional[BoostMask] = None
                         ) -> Tuple[Optional[bytes], str]:
    """Read the raw ClockBoostTable buffer."""
    def fill(buf):
        _fill_mask_from_boost(buf, mask)

    return nvcall(FUNC["GetClockBoostTable"], gpu, CT_SIZE, ver=1, pre_fill=fill)


def read_clock_offsets(gpu, mask: Optional[BoostMask] = None,
                       curve_info: Optional[CurveInfo] = None
                       ) -> Tuple[Optional[List[int]], str]:
    """Read per-point frequency offsets from the ClockBoostTable."""
    d, err = read_clock_table_raw(gpu, mask)
    if not d:
        return None, err

    max_entries = CT_MAX_ENTRIES
    if curve_info and curve_info.total_points > 0:
        max_entries = curve_info.total_points

    offsets = []
    actual_max = min(max_entries, (len(d) - CT_BASE) // CT_STRIDE)
    for i in range(actual_max):
        off = CT_BASE + i * CT_STRIDE + CT_DELTA_OFF
        delta = struct.unpack_from("<i", d, off)[0]
        offsets.append(delta)

    return offsets, "OK"


def read_clock_entry_full(data: bytes, point: int) -> dict:
    """Extract all 9 fields from a single ClockBoostTable entry."""
    base = CT_BASE + point * CT_STRIDE
    if base + CT_STRIDE > len(data):
        return {}
    fields = {}
    for j in range(9):
        off = base + j * 4
        if j == 5:
            fields[f"field_{j:02d}_0x{j*4:02X}"] = struct.unpack_from("<i", data, off)[0]
        else:
            fields[f"field_{j:02d}_0x{j*4:02X}"] = struct.unpack_from("<I", data, off)[0]
    fields["freqDelta_kHz"] = fields["field_05_0x14"]
    return fields


def read_voltage(gpu) -> Tuple[Optional[int], str]:
    """Read current GPU core voltage in µV."""
    d, err = nvcall(FUNC["GetCurrentVoltage"], gpu, VOLT_SIZE, ver=1)
    if not d:
        return None, err
    return struct.unpack_from("<I", d, 0x28)[0], "OK"


def read_clock_ranges(gpu) -> Tuple[Optional[dict], str]:
    """Read clock domain min/max offset ranges."""
    d, err = nvcall(FUNC["GetClockBoostRanges"], gpu, RANGES_SIZE, ver=1)
    if not d:
        return None, err
    num = struct.unpack_from("<I", d, 4)[0]
    domains = []
    for i in range(min(num, 32)):
        base = 0x08 + i * 0x48
        if base + 0x48 > len(d):
            break
        words = [struct.unpack_from("<i", d, base + j)[0]
                 for j in range(0, 0x48, 4)]
        domains.append(words)
    return {"num_domains": num, "domains": domains}, "OK"


# ═══════════════════════════════════════════════════════════════════════════
# Mask bit helpers
# ═══════════════════════════════════════════════════════════════════════════

def set_mask_bit(buf, point: int, offset=MASK_OFFSET):
    """Set a single bit in the mask field."""
    byte_idx = offset + (point // 8)
    bit_idx = point % 8
    buf[byte_idx] = int.from_bytes(buf[byte_idx:byte_idx+1], 'little') | (1 << bit_idx)


def set_mask_bits(buf, points: Set[int], offset=MASK_OFFSET):
    """Set mask bits for a set of points."""
    for p in points:
        set_mask_bit(buf, p, offset)


# ═══════════════════════════════════════════════════════════════════════════
# Write operations
# ═══════════════════════════════════════════════════════════════════════════

def build_write_buffer(
    gpu,
    point_deltas: dict,
    mask: Optional[BoostMask] = None,
) -> Tuple[Optional[ctypes.Array], str]:
    """Build a SetClockBoostTable buffer with specified per-point deltas.

    Strategy: read the current ClockBoostTable (using canonical mask),
    modify only the targeted entries' freqDelta fields, set only the
    targeted mask bits for the write call.

    Returns (mutable_buffer, error_string).
    """
    current_raw, err = read_clock_table_raw(gpu, mask)
    if not current_raw:
        return None, f"Cannot read current ClockBoostTable: {err}"

    buf = ctypes.create_string_buffer(CT_SIZE)
    ctypes.memmove(buf, current_raw, CT_SIZE)

    # Rewrite version word
    struct.pack_into("<I", buf, 0, (1 << 16) | CT_SIZE)

    # Clear mask — we'll set only the bits we're writing
    for i in range(MASK_OFFSET, MASK_OFFSET + MASK_BYTES):
        buf[i] = 0x00

    # Set mask bits and write deltas
    target_points = set(point_deltas.keys())
    set_mask_bits(buf, target_points)

    for point, delta_khz in point_deltas.items():
        off = CT_BASE + point * CT_STRIDE + CT_DELTA_OFF
        struct.pack_into("<i", buf, off, delta_khz)

    return buf, "OK"


def write_clock_offsets(
    gpu,
    point_deltas: dict,
    mask: Optional[BoostMask] = None,
    dry_run: bool = False,
) -> Tuple[int, str]:
    """Write per-point frequency offsets via SetClockBoostTable."""
    buf, err = build_write_buffer(gpu, point_deltas, mask)
    if buf is None:
        return -999, err

    if dry_run:
        return 0, "DRY RUN — buffer built but not sent to driver"

    ret, desc = nvcall_raw(FUNC["SetClockBoostTable"], gpu, buf)
    return ret, desc


# ═══════════════════════════════════════════════════════════════════════════
# Safety checks
# ═══════════════════════════════════════════════════════════════════════════

def validate_write_request(point_deltas: dict,
                           curve_info: Optional[CurveInfo] = None
                           ) -> Optional[str]:
    """Return an error message if the write request is unsafe, else None."""
    mem_points = set()
    if curve_info:
        mem_points = set(curve_info.mem_points)

    for point, delta_khz in point_deltas.items():
        if point < 0 or point >= CT_MAX_ENTRIES:
            return f"Point {point} out of range (0–{CT_MAX_ENTRIES - 1})"

        if point in mem_points:
            return (f"Point {point} is a memory clock entry. "
                    "Memory offsets use a different mechanism (NVML). "
                    "Use --force if you really mean it.")

        if abs(delta_khz) > MAX_DELTA_KHZ:
            return (f"Delta {delta_khz/1000:+.0f} MHz for point {point} exceeds "
                    f"safety limit of ±{MAX_DELTA_KHZ/1000:.0f} MHz. "
                    "Use --max-delta to raise the limit if needed.")

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Hex dump utility
# ═══════════════════════════════════════════════════════════════════════════

def hexdump(data: bytes, start: int, length: int, cols: int = 16) -> str:
    lines = []
    end = min(start + length, len(data))
    for off in range(start, end, cols):
        chunk = data[off:off + cols]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {off:04x}: {hx:<{cols * 3}}  {asc}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Snapshot save/restore
# ═══════════════════════════════════════════════════════════════════════════

def snapshot_save(gpu, gpu_name: str, mask: Optional[BoostMask] = None):
    """Save the current ClockBoostTable to disk."""
    raw, err = read_clock_table_raw(gpu, mask)
    if not raw:
        print(f"Failed to read ClockBoostTable: {err}")
        return False

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(SNAPSHOT_DIR, f"clock_boost_table_{ts}.bin")
    meta_fname = os.path.join(SNAPSHOT_DIR, f"clock_boost_table_{ts}.json")

    with open(fname, "wb") as f:
        f.write(raw)

    # Save human-readable metadata — scan all possible entries
    offsets = []
    for i in range(CT_MAX_ENTRIES):
        off = CT_BASE + i * CT_STRIDE + CT_DELTA_OFF
        if off + 4 > len(raw):
            break
        delta = struct.unpack_from("<i", raw, off)[0]
        offsets.append(delta)

    meta = {
        "gpu": gpu_name,
        "timestamp": datetime.now().isoformat(),
        "file": fname,
        "size": len(raw),
        "offsets_kHz": offsets,
        "nonzero_offsets": sum(1 for o in offsets if o != 0),
    }
    with open(meta_fname, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Snapshot saved:")
    print(f"  Binary:   {fname}")
    print(f"  Metadata: {meta_fname}")
    print(f"  Size:     {len(raw)} bytes")
    print(f"  Non-zero offsets: {meta['nonzero_offsets']}")
    return True


def snapshot_restore(gpu, mask: Optional[BoostMask] = None, filepath: str = None):
    """Restore a ClockBoostTable snapshot from disk."""
    if filepath is None:
        if not os.path.isdir(SNAPSHOT_DIR):
            print(f"No snapshots found in {SNAPSHOT_DIR}")
            return False
        bins = sorted(
            [f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".bin")],
            reverse=True,
        )
        if not bins:
            print(f"No snapshot .bin files in {SNAPSHOT_DIR}")
            return False
        filepath = os.path.join(SNAPSHOT_DIR, bins[0])

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

    # Use boost mask for restore
    mask.copy_mask_into(buf)

    print(f"Restoring from: {filepath}")
    ret, desc = nvcall_raw(FUNC["SetClockBoostTable"], gpu, buf)
    print(f"SetClockBoostTable returned: {ret} ({desc})")
    return ret == 0


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics
# ═══════════════════════════════════════════════════════════════════════════

def run_diagnostics(gpu, gpu_name, mask: Optional[BoostMask] = None):
    """Probe all known functions and report results."""
    print(f"GPU: {gpu_name}")
    print()

    # Step 1: resolve function pointers
    print("=== Function probe ===")
    print()
    probes = [
        ("GetVFPCurve",         FUNC["GetVFPCurve"],         VFP_SIZE,    1),
        ("GetClockBoostMask",   FUNC["GetClockBoostMask"],   MASK_SIZE,   1),
        ("GetClockBoostTable",  FUNC["GetClockBoostTable"],  CT_SIZE,     1),
        ("GetCurrentVoltage",   FUNC["GetCurrentVoltage"],   VOLT_SIZE,   1),
        ("GetClockBoostRanges", FUNC["GetClockBoostRanges"], RANGES_SIZE, 1),
        ("GetPerfLimits",       FUNC["GetPerfLimits"],       PERF_SIZE,   2),
        ("GetVoltBoostPercent", FUNC["GetVoltBoostPercent"],  VBOOST_SIZE, 1),
        ("SetClockBoostTable",  FUNC["SetClockBoostTable"],  CT_SIZE,     1),
    ]
    for name, fid, size, ver in probes:
        ptr = QI(fid)
        resolved = "resolved" if ptr else "NOT FOUND"
        print(f"  {name:30s}  0x{fid:08X}  size=0x{size:04X}  ver={ver}  {resolved}")

    # Step 2: read GetClockBoostMask first
    print()
    print("=== Boost mask ===")
    mask_data, mask_err = read_boost_mask(gpu)
    if mask_data:
        enabled = mask_data.count_enabled()
        print(f"  GetClockBoostMask: OK — {enabled} enabled points in mask")
        print(f"  Mask bytes: {' '.join(f'{b:02x}' for b in mask_data.mask_bytes)}")
    else:
        print(f"  GetClockBoostMask: FAILED ({mask_err})")

    use_mask = mask_data or mask

    # Step 3: test reads with the proper mask
    needs_mask_fns = {
        FUNC["GetVFPCurve"], FUNC["GetClockBoostMask"], FUNC["GetClockBoostTable"]
    }

    print()
    print("=== Read function tests (using boost mask) ===")
    for name, fid, size, ver in probes:
        if name.startswith("Set") or name == "GetClockBoostMask":
            continue

        def fill(buf, _fid=fid, _mask=use_mask):
            if _fid in needs_mask_fns and _mask:
                _fill_mask_from_boost(buf, _mask)
            if _fid == FUNC["GetVFPCurve"]:
                struct.pack_into("<I", buf, 0x14, 15)

        d, err = nvcall(fid, gpu, size, ver=ver, pre_fill=fill)
        status = f"OK ({len(d)} bytes)" if d else f"FAILED: {err}"
        print(f"  {name:30s}  {status}")
        if d:
            vw = struct.unpack_from("<I", d, 0)[0]
            print(f"    version_word = 0x{vw:08X}")

    # Step 4: comparison with all-0xFF mask
    # if mask_data:
    #     print()
    #     print("=== Comparison: reads with all-0xFF mask (expected to fail on most architectures) ===")
    #     for name, fid, size, ver in probes:
    #         if fid not in needs_mask_fns or name.startswith("Set"):
    #             continue
    #         if name == "GetClockBoostMask":
    #             continue

    #         def fill_ff(buf, _fid=fid):
    #             for i in range(MASK_OFFSET, MASK_OFFSET + MASK_BYTES):
    #                 buf[i] = 0xFF
    #             if _fid == FUNC["GetVFPCurve"]:
    #                 struct.pack_into("<I", buf, 0x14, 15)

    #         d, err = nvcall(fid, gpu, size, ver=ver, pre_fill=fill_ff)
    #         status = f"OK ({len(d)} bytes)" if d else f"FAILED: {err}"
    #         print(f"  {name:30s}  {status}")

    # Step 5: curve info
    print()
    print("=== Curve classification ===")
    info = CurveInfo.build(gpu, use_mask)
    print(f"  {info.describe()}")


# ═══════════════════════════════════════════════════════════════════════════
# Output formatting
# ═══════════════════════════════════════════════════════════════════════════

def print_curve(points, offsets, voltage, curve_info: Optional[CurveInfo] = None,
                full=False):
    """Print formatted V/F curve table."""
    if voltage:
        print(f"Current voltage: {voltage / 1000:.1f} mV")

    if curve_info:
        print(f"Curve: {curve_info.describe()}")
    print()

    current_idx = None
    if voltage:
        for i, (f, v) in enumerate(points):
            if v > 0 and abs(v - voltage) < 10000:
                current_idx = i
                break

    gpu_set = set(curve_info.gpu_points) if curve_info else set()
    mem_set = set(curve_info.mem_points) if curve_info else set()

    if full:
        show = list(range(len(points)))
    else:
        show = []
        prev_freq = -1
        for i, (f, v) in enumerate(points):
            if f == 0 and v == 0:
                continue
            if i in mem_set:
                show.append(i)
            elif f != prev_freq or i == len(points) - 1:
                show.append(i)
            prev_freq = f

    print(f"{'#':>3s}  {'Freq':>8s}  {'Voltage':>8s}  {'Offset':>8s}  {'Domain'}")
    print("-" * 56)

    for i in show:
        if i >= len(points):
            break
        f, v = points[i]
        if f == 0 and v == 0:
            continue

        freq_s = f"{f / 1000:.0f} MHz"
        volt_s = f"{v / 1000:.0f} mV"

        offset_s = ""
        if offsets and i < len(offsets) and offsets[i] != 0:
            offset_s = f"{offsets[i] / 1000:+.0f} MHz"

        domain = ""
        if i in mem_set:
            domain = "memory"
        elif i in gpu_set:
            domain = "gpu"

        marker = ""
        if current_idx is not None and i == current_idx:
            marker = "  <-- current"

        print(f"{i:3d}  {freq_s:>8s}  {volt_s:>8s}  {offset_s:>8s}  {domain}{marker}")

    # Summary
    if curve_info and curve_info.gpu_points:
        gpu_data = [(points[i][0], points[i][1]) for i in curve_info.gpu_points
                     if i < len(points) and points[i][0] > 0]
        if gpu_data:
            freqs = [f for f, v in gpu_data]
            volts = [v for f, v in gpu_data]
            print()
            print(f"GPU core: {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz, "
                  f"{min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV "
                  f"({len(gpu_data)} points)")

    if curve_info and curve_info.mem_points:
        mem_data = [(points[i][0], points[i][1]) for i in curve_info.mem_points
                     if i < len(points) and points[i][0] > 0]
        if mem_data:
            freqs = [f for f, v in mem_data]
            volts = [v for f, v in mem_data]
            print(f"Memory:   {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz, "
                  f"{min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV "
                  f"({len(mem_data)} points)")

    if offsets:
        gpu_indices = set(curve_info.gpu_points) if curve_info else set(range(len(offsets)))
        gpu_offsets = [offsets[i] for i in gpu_indices
                       if i < len(offsets) and offsets[i] != 0]
        if gpu_offsets:
            vals = set(gpu_offsets)
            if len(vals) == 1:
                print(f"GPU offset: {next(iter(vals))/1000:+.0f} MHz "
                      f"(uniform across {len(gpu_offsets)} points)")
            else:
                print(f"GPU offsets: {len(gpu_offsets)} points active "
                      f"(range: {min(vals)/1000:+.0f} to {max(vals)/1000:+.0f} MHz)")


def output_json(gpu_name, points, offsets, voltage,
                curve_info: Optional[CurveInfo] = None):
    """Output JSON format."""
    data = {
        "gpu": gpu_name,
        "current_voltage_uV": voltage,
        "layout": {
            "vfp_curve": {"size": VFP_SIZE, "base": VFP_BASE,
                          "stride": VFP_STRIDE, "max_entries": VFP_MAX_ENTRIES},
            "clock_table": {"size": CT_SIZE, "base": CT_BASE,
                            "stride": CT_STRIDE, "delta_offset": CT_DELTA_OFF,
                            "max_entries": CT_MAX_ENTRIES},
        },
        "curve_info": {
            "gpu_points": curve_info.gpu_points if curve_info else [],
            "mem_points": curve_info.mem_points if curve_info else [],
            "total_points": curve_info.total_points if curve_info else len(points),
        },
        "vf_curve": [],
    }

    gpu_set = set(curve_info.gpu_points) if curve_info else set()
    mem_set = set(curve_info.mem_points) if curve_info else set()

    for i, (f, v) in enumerate(points):
        if f > 0 or v > 0:
            entry = {"index": i, "freq_kHz": f, "volt_uV": v}
            if offsets and i < len(offsets):
                entry["freq_offset_kHz"] = offsets[i]
            if i in mem_set:
                entry["domain"] = "memory"
            elif i in gpu_set:
                entry["domain"] = "gpu"
            data["vf_curve"].append(entry)

    print(json.dumps(data, indent=2))


# ═══════════════════════════════════════════════════════════════════════════
# Write command handler
# ═══════════════════════════════════════════════════════════════════════════

def cmd_write(gpu, gpu_name, args, mask, curve_info):
    """Handle write subcommand."""
    delta_khz = int(args.delta * 1000)
    point_deltas = {}

    # Determine GPU-only points for --global and --reset
    if curve_info and curve_info.gpu_points:
        gpu_points = curve_info.gpu_points
    else:
        gpu_points = list(range(127))

    if args.reset:
        delta_khz = 0
        for i in gpu_points:
            point_deltas[i] = 0
        print(f"Resetting offsets to 0 on {len(point_deltas)} GPU core points")

    elif args.point is not None:
        point_deltas[args.point] = delta_khz
        print(f"Target: point {args.point}, delta {args.delta:+.0f} MHz "
              f"({delta_khz:+d} kHz)")

    elif args.range:
        start, end = args.range
        for i in range(start, end + 1):
            point_deltas[i] = delta_khz
        print(f"Target: points {start}–{end} ({len(point_deltas)} points), "
              f"delta {args.delta:+.0f} MHz")

    elif args.glob:
        for i in gpu_points:
            point_deltas[i] = delta_khz
        print(f"Target: all {len(point_deltas)} GPU core points, "
              f"delta {args.delta:+.0f} MHz")

    else:
        print("Error: specify --point N, --range A-B, --global, or --reset")
        return

    # Safety check
    if not args.force:
        err = validate_write_request(point_deltas, curve_info)
        if err:
            print(f"\nSafety check FAILED: {err}")
            return

    # Read current state for comparison
    current_offsets, _ = read_clock_offsets(gpu, mask, curve_info)

    # Show what will change
    print()
    print("Changes to apply:")
    changed = 0
    for point in sorted(point_deltas.keys()):
        new = point_deltas[point]
        old = current_offsets[point] if current_offsets and point < len(current_offsets) else 0
        if old != new:
            changed += 1
            if changed <= 20:
                print(f"  Point {point:3d}: {old/1000:+8.0f} MHz → {new/1000:+8.0f} MHz")
    if changed > 20:
        print(f"  ... and {changed - 20} more points")
    if changed == 0:
        print("  (no changes — offsets already match)")
        return

    if args.dry_run:
        print()
        print("DRY RUN — no changes applied.")

        buf, err = build_write_buffer(gpu, point_deltas, mask)
        if buf:
            print()
            print("Buffer header (first 0x44 bytes):")
            print(hexdump(bytes(buf), 0x00, 0x44))

            first_pt = min(point_deltas.keys())
            entry_off = CT_BASE + first_pt * CT_STRIDE
            print(f"\nEntry for point {first_pt} (offset 0x{entry_off:04X}, "
                  f"stride 0x{CT_STRIDE:02X}):")
            print(hexdump(bytes(buf), entry_off, CT_STRIDE))
        return

    # Auto-save snapshot before write
    print()
    print("Saving pre-write snapshot...")
    snapshot_save(gpu, gpu_name, mask)

    # Execute write
    print()
    print("Writing to GPU...")
    ret, desc = write_clock_offsets(gpu, point_deltas, mask)
    print(f"SetClockBoostTable returned: {ret} ({desc})")

    if ret != 0:
        print("\nWrite FAILED. GPU state unchanged.")
        return

    # Verify by re-reading
    print()
    print("Verifying write...")
    time.sleep(0.1)
    new_offsets, err = read_clock_offsets(gpu, mask, curve_info)
    if not new_offsets:
        print(f"WARNING: Verification read failed: {err}")
        return

    mismatches = 0
    for point, expected in point_deltas.items():
        actual = new_offsets[point] if point < len(new_offsets) else 0
        if actual != expected:
            mismatches += 1
            print(f"  MISMATCH point {point}: expected {expected/1000:+.0f} MHz, "
                  f"got {actual/1000:+.0f} MHz")

    if mismatches == 0:
        print(f"Verified: all {len(point_deltas)} points match expected values.")
    else:
        print(f"\nWARNING: {mismatches} point(s) did not match!")


# ═══════════════════════════════════════════════════════════════════════════
# Verify command handler
# ═══════════════════════════════════════════════════════════════════════════

def cmd_verify(gpu, gpu_name, args, mask, curve_info):
    """Write-verify-read cycle for a single point or range."""
    delta_khz = int(args.delta * 1000)

    if args.point is not None:
        points = [args.point]
    elif args.range:
        points = list(range(args.range[0], args.range[1] + 1))
    else:
        print("Error: --point or --range required for verify mode")
        return

    point_deltas = {p: delta_khz for p in points}

    err = validate_write_request(point_deltas, curve_info)
    if err:
        print(f"Safety check FAILED: {err}")
        return

    print(f"=== Write-Verify Cycle ===")
    print(f"GPU: {gpu_name}")
    if curve_info:
        print(f"Curve: {curve_info.describe()}")
    print(f"Points: {points[0]}{'–' + str(points[-1]) if len(points) > 1 else ''}")
    print(f"Delta:  {args.delta:+.0f} MHz ({delta_khz:+d} kHz)")
    print()

    # Step 1: Read BEFORE state
    print("Step 1: Reading current state...")
    before_offsets, err = read_clock_offsets(gpu, mask, curve_info)
    if not before_offsets:
        print(f"  FAILED: {err}")
        return
    before_raw, _ = read_clock_table_raw(gpu, mask)

    for p in points[:5]:
        entry = read_clock_entry_full(before_raw, p) if before_raw else {}
        off_val = before_offsets[p] if p < len(before_offsets) else 0
        print(f"  Point {p:3d}: freqDelta = {off_val/1000:+8.0f} MHz")
        if entry:
            print(f"    All fields: {entry}")

    # Step 2: Save snapshot
    print()
    print("Step 2: Saving pre-write snapshot...")
    snapshot_save(gpu, gpu_name, mask)

    # Step 3: Write
    print()
    print("Step 3: Writing offsets...")
    ret, desc = write_clock_offsets(gpu, point_deltas, mask)
    print(f"  SetClockBoostTable returned: {ret} ({desc})")
    if ret != 0:
        print("  FAILED — aborting verify cycle.")
        return

    # Step 4: Read AFTER state
    print()
    print("Step 4: Reading back (verification)...")
    time.sleep(0.2)
    after_offsets, err = read_clock_offsets(gpu, mask, curve_info)
    if not after_offsets:
        print(f"  FAILED: {err}")
        return
    after_raw, _ = read_clock_table_raw(gpu, mask)

    all_ok = True
    for p in points:
        expected = delta_khz
        actual = after_offsets[p] if p < len(after_offsets) else 0
        match = "OK" if actual == expected else "MISMATCH"
        if actual != expected:
            all_ok = False
        print(f"  Point {p:3d}: expected {expected/1000:+8.0f} MHz, "
              f"got {actual/1000:+8.0f} MHz  [{match}]")

    # Step 5: Check for collateral damage
    print()
    print("Step 5: Checking for unintended side effects...")
    collateral = 0
    check_range = min(len(before_offsets), len(after_offsets))
    for i in range(check_range):
        if i in point_deltas:
            continue
        if before_offsets[i] != after_offsets[i]:
            collateral += 1
            print(f"  WARNING: Point {i} changed unexpectedly: "
                  f"{before_offsets[i]/1000:+.0f} → {after_offsets[i]/1000:+.0f} MHz")
    if collateral == 0:
        print("  No unintended changes detected.")

    # Step 6: Check if unknown fields changed
    if before_raw and after_raw:
        print()
        print("Step 6: Checking if driver modified any unknown fields...")
        field_changes = 0
        for p in points[:5]:
            before_entry = read_clock_entry_full(before_raw, p)
            after_entry = read_clock_entry_full(after_raw, p)
            for key in before_entry:
                if key == "freqDelta_kHz":
                    continue
                if before_entry[key] != after_entry[key]:
                    field_changes += 1
                    print(f"  Point {p}, {key}: {before_entry[key]} → {after_entry[key]}")
        if field_changes == 0:
            print("  No unknown fields changed.")

    # Step 7: Read voltage
    voltage, _ = read_voltage(gpu)
    if voltage:
        print(f"\nCurrent voltage after write: {voltage/1000:.1f} mV")

    # Summary
    print()
    print("=" * 50)
    if all_ok and collateral == 0:
        print("RESULT: Write verified successfully.")
    elif not all_ok:
        print("RESULT: Write verification FAILED — offsets don't match.")
    else:
        print("RESULT: Write applied but with unexpected side effects.")

    print()
    print("To undo this change, run:")
    print(f"  sudo python3 {sys.argv[0]} snapshot restore")


# ═══════════════════════════════════════════════════════════════════════════
# Inspect command
# ═══════════════════════════════════════════════════════════════════════════

def cmd_inspect(gpu, gpu_name, args, mask, curve_info):
    """Show detailed field-level data for specific points."""
    raw, err = read_clock_table_raw(gpu, mask)
    if not raw:
        print(f"Failed to read ClockBoostTable: {err}")
        return

    vfp_points, _ = read_vfp_curve(gpu, mask, curve_info)

    max_point = curve_info.total_points if curve_info else CT_MAX_ENTRIES

    if args.point is not None:
        indices = [args.point]
    elif args.range:
        indices = list(range(args.range[0], args.range[1] + 1))
    else:
        # Default: show interesting points
        defaults = [0, 1, 50, 51, 80, 126]
        if curve_info:
            if curve_info.mem_points:
                mp = curve_info.mem_points[0]
                defaults.extend([mp - 1, mp, mp + 1])
                defaults.append(curve_info.mem_points[-1])
        indices = sorted(set(defaults))

    gpu_set = set(curve_info.gpu_points) if curve_info else set()
    mem_set = set(curve_info.mem_points) if curve_info else set()

    print(f"GPU: {gpu_name}")
    if curve_info:
        print(f"Curve: {curve_info.describe()}")
    print(f"ClockBoostTable entry detail (stride=0x{CT_STRIDE:02X}, "
          f"9 fields × 4 bytes)")
    print()

    for p in indices:
        if p < 0 or p >= max_point:
            continue
        entry = read_clock_entry_full(raw, p)
        if not entry:
            continue
        off = CT_BASE + p * CT_STRIDE

        domain = ""
        if p in mem_set:
            domain = " [MEMORY]"
        elif p in gpu_set:
            domain = " [GPU]"

        freq_str = ""
        if vfp_points and p < len(vfp_points):
            f, v = vfp_points[p]
            freq_str = f"  (VFP: {f/1000:.0f} MHz @ {v/1000:.0f} mV)"

        print(f"Point {p:3d} — buffer offset 0x{off:04X}{domain}{freq_str}")
        for key, val in entry.items():
            if key == "freqDelta_kHz":
                continue
            marker = " ← freqDelta" if "0x14" in key else ""
            if "0x14" in key:
                print(f"  {key}: {val:12d}  (0x{val & 0xFFFFFFFF:08X})"
                      f"  = {val/1000:+.0f} MHz{marker}")
            else:
                print(f"  {key}: {val:12d}  (0x{val:08X})")
        print()


# ═══════════════════════════════════════════════════════════════════════════
# Read command handler
# ═══════════════════════════════════════════════════════════════════════════

def cmd_read(gpu, gpu_name, args, mask, curve_info):
    """Handle read subcommand."""
    if args.diag:
        run_diagnostics(gpu, gpu_name, mask)
        return

    points, vfp_err = read_vfp_curve(gpu, mask, curve_info)
    offsets, ct_err = read_clock_offsets(gpu, mask, curve_info)
    voltage, _ = read_voltage(gpu)

    if not points:
        print(f"GPU: {gpu_name}")
        print(f"Failed to read V/F curve: {vfp_err}")
        print("Run with 'read --diag' to probe all functions.")
        return

    if args.json:
        output_json(gpu_name, points, offsets, voltage, curve_info)
        return

    print(f"GPU: {gpu_name}")

    if args.raw:
        def fill_vfp(buf):
            _fill_mask_from_boost(buf, mask)
            struct.pack_into("<I", buf, 0x14, 15)
        vfp_raw, _ = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE,
                             ver=1, pre_fill=fill_vfp)
        ct_raw, _ = read_clock_table_raw(gpu, mask)

        if vfp_raw:
            print()
            print("=== VFP Curve (0x21537AD4) — header + first entries ===")
            print(hexdump(vfp_raw, 0x00, 0x48))
            print("  --- data at 0x48, stride 0x1C ---")
            print(hexdump(vfp_raw, 0x48, VFP_STRIDE * 5))

        if ct_raw:
            print()
            print("=== ClockBoostTable (0x23F1B133) — header + first entries ===")
            print(hexdump(ct_raw, 0x00, 0x44))
            print("  --- data at 0x44, stride 0x24, freqDelta at +0x14 ---")
            print(hexdump(ct_raw, 0x44, CT_STRIDE * 5))

        # Show around the memory domain if known
        if curve_info and curve_info.mem_points:
            mp = curve_info.mem_points[0]
            if ct_raw:
                mp_off = CT_BASE + (mp - 1) * CT_STRIDE
                print(f"\n  --- around memory transition (point {mp}) ---")
                print(hexdump(ct_raw, mp_off, CT_STRIDE * 4))
        print()

    if not offsets:
        print(f"(Clock offsets unavailable: {ct_err})")

    print_curve(points, offsets, voltage, curve_info, full=args.full)


# ═══════════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════════

def parse_range(s: str) -> Tuple[int, int]:
    """Parse 'A-B' into (A, B) tuple."""
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Expected A-B format, got '{s}'")
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Non-integer in range: '{s}'")
    if a > b:
        raise argparse.ArgumentTypeError(f"Start > end in range: {a}-{b}")
    if a < 0 or b >= CT_MAX_ENTRIES:
        raise argparse.ArgumentTypeError(f"Range {a}-{b} outside 0–{CT_MAX_ENTRIES - 1}")
    return (a, b)


def main():
    parser = argparse.ArgumentParser(
        description="Read/Write NVIDIA GPU V/F curve via undocumented NvAPI (Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s read                          Condensed V/F curve (GPU + memory)
  %(prog)s read --full                   All points including empty slots
  %(prog)s read --json                   JSON output with domain classification
  %(prog)s read --diag                   Probe all functions + mask comparison
  %(prog)s inspect                       Show boundary and interesting points
  %(prog)s inspect --point 80            Show all 9 fields for point 80
  %(prog)s inspect --range 125-132       Show GPU/boundary/memory transition
  %(prog)s write --point 80 --delta 15 --dry-run
                                         Preview writing +15 MHz to point 80
  %(prog)s verify --point 80 --delta 15  Write + verify cycle
  %(prog)s write --global --delta 50     +50 MHz to all GPU core points
  %(prog)s write --reset                 Reset all GPU core offsets to 0
  %(prog)s snapshot save                 Save current ClockBoostTable
  %(prog)s snapshot restore              Restore most recent snapshot
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- read ---
    p_read = sub.add_parser("read", help="Read V/F curve (default)")
    p_read.add_argument("--full", action="store_true",
                        help="Show all points including empty slots")
    p_read.add_argument("--json", action="store_true",
                        help="JSON output with domain classification")
    p_read.add_argument("--raw", action="store_true",
                        help="Include hex dumps")
    p_read.add_argument("--diag", action="store_true",
                        help="Probe all functions with mask comparison")

    # --- inspect ---
    p_insp = sub.add_parser("inspect", help="Show detailed entry fields")
    p_insp.add_argument("--point", type=int, help="Single point index")
    p_insp.add_argument("--range", type=parse_range, help="Point range A-B")

    # --- write ---
    p_write = sub.add_parser("write", help="Write frequency offsets")
    tgt = p_write.add_mutually_exclusive_group()
    tgt.add_argument("--point", type=int, help="Single point index")
    tgt.add_argument("--range", type=parse_range, help="Point range A-B")
    tgt.add_argument("--global", dest="glob", action="store_true",
                     help="All GPU core points")
    tgt.add_argument("--reset", action="store_true",
                     help="Reset all GPU core offsets to 0")
    p_write.add_argument("--delta", type=float, default=0.0,
                         help="Frequency offset in MHz (e.g. 15, -30)")
    p_write.add_argument("--dry-run", action="store_true",
                         help="Preview changes without applying")
    p_write.add_argument("--force", action="store_true",
                         help="Allow modifying memory points")
    p_write.add_argument("--max-delta", type=float, default=300.0,
                         help="Override safety limit (MHz, default 300)")

    # --- verify ---
    p_ver = sub.add_parser("verify", help="Write-verify-read cycle")
    p_ver.add_argument("--point", type=int, help="Single point index")
    p_ver.add_argument("--range", type=parse_range, help="Point range A-B")
    p_ver.add_argument("--delta", type=float, required=True,
                       help="Frequency offset in MHz")

    # --- snapshot ---
    p_snap = sub.add_parser("snapshot", help="Save/restore ClockBoostTable")
    p_snap.add_argument("action", choices=["save", "restore"],
                        help="save or restore")
    p_snap.add_argument("--file", help="Snapshot file path (for restore)")

    args = parser.parse_args()

    # Default to 'read' if no subcommand
    if args.command is None:
        args.command = "read"
        args.full = False
        args.json = False
        args.raw = False
        args.diag = False

    # Update safety limit if overridden
    global MAX_DELTA_KHZ
    if args.command == "write" and hasattr(args, "max_delta"):
        MAX_DELTA_KHZ = int(args.max_delta * 1000)

    gpu, gpu_name = init_gpu()

    # Read boost mask first — this is the canonical source per nvapioc
    mask, mask_err = read_boost_mask(gpu)
    if not mask:
        print(f"Error: Could not read boost mask ({mask_err}).")
        print("This mask is required to safely read/write the V/F curve.")
        sys.exit(1)

    # Classify points (GPU core vs memory)
    curve_info = CurveInfo.build(gpu, mask)

    if args.command == "read":
        cmd_read(gpu, gpu_name, args, mask, curve_info)
    elif args.command == "inspect":
        cmd_inspect(gpu, gpu_name, args, mask, curve_info)
    elif args.command == "write":
        cmd_write(gpu, gpu_name, args, mask, curve_info)
    elif args.command == "verify":
        cmd_verify(gpu, gpu_name, args, mask, curve_info)
    elif args.command == "snapshot":
        if args.action == "save":
            snapshot_save(gpu, gpu_name, mask)
        elif args.action == "restore":
            snapshot_restore(gpu, mask, args.file)


if __name__ == "__main__":
    main()
