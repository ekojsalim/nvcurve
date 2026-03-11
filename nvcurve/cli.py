"""nvcurve CLI — replaces nv_vfcurve_rw.py's argparse main().

All subcommands produce identical output to the original script.

Usage:
    sudo nvcurve read [--full|--json|--raw|--diag]
    sudo nvcurve inspect [--point N|--range A-B]
    sudo nvcurve write [--point N|--range A-B|--global|--reset] --delta D [--dry-run]
    sudo nvcurve verify --point N --delta D
    sudo nvcurve snapshot [save|restore|list]
"""

import argparse
import json
import struct
import sys
import time
import os

from .config import Config, default_config
from .hal.gpu import get_gpu
from .hal.monitoring import read_voltage
from .hal.ranges import get_clock_ranges
from .hal.snapshot import save as snapshot_save, restore as snapshot_restore, list_snapshots
from .hal.vfcurve import (
    build_write_buffer,
    read_clock_entry_full,
    read_clock_offsets,
    read_clock_table_raw,
    read_vfp_curve,
    write_offsets,
)
from .nvapi.bootstrap import nvcall, query_interface
from .nvapi.constants import (
    FUNC,
    VFP_SIZE, VFP_BASE, VFP_STRIDE,
    CT_SIZE, CT_BASE, CT_STRIDE,
    CT_POINTS, IDLE_POINT,
    MASK_SIZE, VOLT_SIZE, RANGES_SIZE, PERF_SIZE, VBOOST_SIZE,
)
from .safety import validate_write


# ── Utilities ────────────────────────────────────────────────────────────────

def hexdump(data: bytes, start: int, length: int, cols: int = 16) -> str:
    lines = []
    end = min(start + length, len(data))
    for off in range(start, end, cols):
        chunk = data[off:off + cols]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {off:04x}: {hx:<{cols * 3}}  {asc}")
    return "\n".join(lines)


def parse_range(s: str):
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
    if a < 0 or b >= CT_POINTS:
        raise argparse.ArgumentTypeError(f"Range {a}-{b} outside 0–{CT_POINTS - 1}")
    return (a, b)


# ── Output formatters ────────────────────────────────────────────────────────

def print_curve(points, offsets, voltage, full=False):
    """Print formatted V/F curve table."""
    from .nvapi.constants import VFP_POINTS
    if voltage:
        print(f"Current voltage: {voltage / 1000:.1f} mV")
    print()

    current_idx = None
    if voltage:
        for i, (f, v) in enumerate(points):
            if v > 0 and abs(v - voltage) < 10000:
                current_idx = i
                break

    if full:
        show = list(range(len(points)))
    else:
        show = []
        prev_freq = -1
        for i, (f, v) in enumerate(points):
            if f == 0 and v == 0:
                continue
            if f != prev_freq or i == len(points) - 1:
                show.append(i)
            prev_freq = f

    print(f"{'#':>3s}  {'Freq':>8s}  {'Voltage':>8s}  {'Offset':>8s}")
    print("-" * 42)

    for i in show:
        f, v = points[i]
        if f == 0 and v == 0:
            continue
        freq_s = f"{f / 1000:.0f} MHz"
        volt_s = f"{v / 1000:.0f} mV"
        offset_s = ""
        if offsets and offsets[i] != 0:
            offset_s = f"{offsets[i] / 1000:+.0f} MHz"
        marker = ""
        if current_idx is not None and i == current_idx:
            marker = "  <-- current"
        elif i == len(points) - 1 and f < 1_000_000:
            marker = "  (idle/low-power)"
        print(f"{i:3d}  {freq_s:>8s}  {volt_s:>8s}  {offset_s:>8s}{marker}")

    active = [(f, v) for i, (f, v) in enumerate(points)
              if f > 0 and v > 0 and i < len(points) - 1]
    if active:
        freqs = [f for f, v in active]
        volts = [v for f, v in active]
        print()
        print(f"Frequency range: {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz")
        print(f"Voltage range:   {min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV")
        print(f"Active V/F points: {len(active)} (+ 1 idle)")
        if offsets:
            nonzero = sum(1 for o in offsets if o != 0)
            if nonzero > 0:
                vals = set(o for o in offsets if o != 0)
                if len(vals) == 1:
                    print(f"Global offset: {next(iter(vals))/1000:+.0f} MHz "
                          f"(applied to {nonzero} points)")
                else:
                    print(f"Per-point offsets active on {nonzero} points "
                          f"(range: {min(vals)/1000:+.0f} to {max(vals)/1000:+.0f} MHz)")


def output_json(gpu_name, points, offsets, voltage):
    """Output JSON format."""
    data = {
        "gpu": gpu_name,
        "current_voltage_uV": voltage,
        "layout": {
            "vfp_curve": {"size": VFP_SIZE, "base": VFP_BASE,
                          "stride": VFP_STRIDE, "points": len(points)},
            "clock_table": {"size": CT_SIZE, "base": CT_BASE,
                            "stride": CT_STRIDE, "delta_offset": 0x14,
                            "points": CT_POINTS},
        },
        "vf_curve": [],
    }
    if points:
        for i, (f, v) in enumerate(points):
            if f > 0 or v > 0:
                entry = {"index": i, "freq_kHz": f, "volt_uV": v}
                if offsets:
                    entry["freq_offset_kHz"] = offsets[i]
                if i == len(points) - 1 and f < 1_000_000:
                    entry["type"] = "idle"
                data["vf_curve"].append(entry)
    print(json.dumps(data, indent=2))


# ── Diagnostics ──────────────────────────────────────────────────────────────

def run_diagnostics(gpu, gpu_name):
    """Probe all known functions and report results."""
    from .hal.vfcurve import fill_mask_128

    print(f"GPU: {gpu_name}")
    print()
    print("=== Function probe ===")
    print()

    probes = [
        ("GetVFPCurve",         FUNC["GetVFPCurve"],         VFP_SIZE,    1, True),
        ("GetClockBoostMask",   FUNC["GetClockBoostMask"],   MASK_SIZE,   1, True),
        ("GetClockBoostTable",  FUNC["GetClockBoostTable"],  CT_SIZE,     1, True),
        ("GetCurrentVoltage",   FUNC["GetCurrentVoltage"],   VOLT_SIZE,   1, False),
        ("GetClockBoostRanges", FUNC["GetClockBoostRanges"], RANGES_SIZE, 1, False),
        ("GetPerfLimits",       FUNC["GetPerfLimits"],       PERF_SIZE,   2, False),
        ("GetVoltBoostPercent", FUNC["GetVoltBoostPercent"], VBOOST_SIZE, 1, False),
        ("SetClockBoostTable",  FUNC["SetClockBoostTable"],  CT_SIZE,     1, True),
    ]

    for name, fid, size, ver, needs_mask in probes:
        ptr = query_interface(fid)
        resolved = "resolved" if ptr else "NOT FOUND"
        print(f"  {name:30s}  0x{fid:08X}  size=0x{size:04X}  ver={ver}  {resolved}")

    print()
    print("=== Read function tests ===")
    for name, fid, size, ver, needs_mask in probes:
        if name.startswith("Set"):
            continue

        def fill(buf, _nm=needs_mask, _fid=fid):
            if _nm:
                fill_mask_128(buf)
            if _fid == FUNC["GetVFPCurve"]:
                struct.pack_into("<I", buf, 0x14, 15)

        d, err = nvcall(fid, gpu, size, ver=ver, pre_fill=fill)
        status = f"OK ({len(d)} bytes)" if d else f"FAILED: {err}"
        print(f"  {name:30s}  {status}")
        if d:
            vw = struct.unpack_from("<I", d, 0)[0]
            print(f"    version_word = 0x{vw:08X}")


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_read(gpu, gpu_name, args):
    if args.diag:
        run_diagnostics(gpu, gpu_name)
        return

    points, vfp_err = read_vfp_curve(gpu)
    offsets, ct_err = read_clock_offsets(gpu)
    voltage, _ = read_voltage(gpu)

    if not points:
        print(f"GPU: {gpu_name}")
        print(f"Failed to read V/F curve: {vfp_err}")
        print("Run with 'read --diag' to probe all functions.")
        return

    if args.json:
        output_json(gpu_name, points, offsets, voltage)
        return

    print(f"GPU: {gpu_name}")

    if args.raw:
        def fill_vfp(buf):
            from .hal.vfcurve import fill_mask_128
            fill_mask_128(buf)
            struct.pack_into("<I", buf, 0x14, 15)

        vfp_raw, _ = nvcall(FUNC["GetVFPCurve"], gpu, VFP_SIZE, ver=1, pre_fill=fill_vfp)
        ct_raw, _ = read_clock_table_raw(gpu)

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
        print()

    if not offsets:
        print(f"(Clock offsets unavailable: {ct_err})")

    print_curve(points, offsets, voltage, full=args.full)


def cmd_inspect(gpu, gpu_name, args):
    raw, err = read_clock_table_raw(gpu)
    if not raw:
        print(f"Failed to read ClockBoostTable: {err}")
        return

    points_data, _ = read_vfp_curve(gpu)

    if args.point is not None:
        indices = [args.point]
    elif args.range:
        indices = list(range(args.range[0], args.range[1] + 1))
    else:
        indices = [0, 1, 50, 51, 80, 126, 127]

    print(f"GPU: {gpu_name}")
    print(f"ClockBoostTable entry detail (stride=0x{CT_STRIDE:02X}, "
          f"9 fields × 4 bytes)")
    print()

    for p in indices:
        if p < 0 or p >= CT_POINTS:
            continue
        entry = read_clock_entry_full(raw, p)
        off = CT_BASE + p * CT_STRIDE

        freq_str = ""
        if points_data:
            f, v = points_data[p]
            freq_str = f"  (VFP: {f/1000:.0f} MHz @ {v/1000:.0f} mV)"

        print(f"Point {p:3d} — buffer offset 0x{off:04X}{freq_str}")
        for key, val in entry.items():
            if key == "freqDelta_kHz":
                continue
            if "0x14" in key:
                print(f"  {key}: {val:12d}  (0x{val & 0xFFFFFFFF:08X})"
                      f"  = {val/1000:+.0f} MHz  ← freqDelta")
            else:
                print(f"  {key}: {val:12d}  (0x{val:08X})")
        print()


def cmd_write(gpu, gpu_name, args, cfg: Config):
    delta_khz = int(args.delta * 1000)
    point_deltas = {}

    if args.reset:
        delta_khz = 0
        for i in range(CT_POINTS):
            if i != IDLE_POINT or args.force_idle:
                point_deltas[i] = 0
        print(f"Resetting offsets to 0 on {len(point_deltas)} points")

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
        for i in range(CT_POINTS):
            if i != IDLE_POINT or args.force_idle:
                point_deltas[i] = delta_khz
        print(f"Target: all {len(point_deltas)} points (global), "
              f"delta {args.delta:+.0f} MHz")

    else:
        print("Error: specify --point N, --range A-B, --global, or --reset")
        return

    errors = validate_write(point_deltas, cfg.max_delta_khz, allow_idle=args.force_idle)
    if errors:
        print(f"\nSafety check FAILED: {errors[0]}")
        return

    current_offsets, _ = read_clock_offsets(gpu)

    print()
    print("Changes to apply:")
    changed = 0
    for point in sorted(point_deltas.keys()):
        new = point_deltas[point]
        old = current_offsets[point] if current_offsets else 0
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
        buf, err = build_write_buffer(gpu, point_deltas)
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

    if cfg.auto_snapshot:
        print()
        print("Saving pre-write snapshot...")
        snapshot_save(gpu, gpu_name, cfg.snapshot_dir)

    print()
    print("Writing to GPU...")
    ret, desc = write_offsets(gpu, point_deltas)
    print(f"SetClockBoostTable returned: {ret} ({desc})")

    if ret != 0:
        print("\nWrite FAILED. GPU state unchanged.")
        return

    print()
    print("Verifying write...")
    time.sleep(0.1)
    new_offsets, err = read_clock_offsets(gpu)
    if not new_offsets:
        print(f"WARNING: Verification read failed: {err}")
        return

    mismatches = 0
    for point, expected in point_deltas.items():
        actual = new_offsets[point]
        if actual != expected:
            mismatches += 1
            print(f"  MISMATCH point {point}: expected {expected/1000:+.0f} MHz, "
                  f"got {actual/1000:+.0f} MHz")

    if mismatches == 0:
        print(f"Verified: all {len(point_deltas)} points match expected values.")
    else:
        print(f"\nWARNING: {mismatches} point(s) did not match!")


def cmd_verify(gpu, gpu_name, args, cfg: Config):
    delta_khz = int(args.delta * 1000)

    if args.point is not None:
        points = [args.point]
    elif args.range:
        points = list(range(args.range[0], args.range[1] + 1))
    else:
        print("Error: --point or --range required for verify mode")
        return

    point_deltas = {p: delta_khz for p in points}

    errors = validate_write(point_deltas, cfg.max_delta_khz)
    if errors:
        print(f"Safety check FAILED: {errors[0]}")
        return

    print(f"=== Write-Verify Cycle ===")
    print(f"GPU: {gpu_name}")
    print(f"Points: {points[0]}{'–' + str(points[-1]) if len(points) > 1 else ''}")
    print(f"Delta:  {args.delta:+.0f} MHz ({delta_khz:+d} kHz)")
    print()

    print("Step 1: Reading current state...")
    before_offsets, err = read_clock_offsets(gpu)
    if not before_offsets:
        print(f"  FAILED: {err}")
        return
    before_raw, _ = read_clock_table_raw(gpu)

    for p in points[:5]:
        entry = read_clock_entry_full(before_raw, p) if before_raw else {}
        print(f"  Point {p:3d}: freqDelta = {before_offsets[p]/1000:+8.0f} MHz")
        if entry:
            print(f"    All fields: {entry}")

    print()
    print("Step 2: Saving pre-write snapshot...")
    snapshot_save(gpu, gpu_name, cfg.snapshot_dir)

    print()
    print("Step 3: Writing offsets...")
    ret, desc = write_offsets(gpu, point_deltas)
    print(f"  SetClockBoostTable returned: {ret} ({desc})")
    if ret != 0:
        print("  FAILED — aborting verify cycle.")
        return

    print()
    print("Step 4: Reading back (verification)...")
    time.sleep(0.2)
    after_offsets, err = read_clock_offsets(gpu)
    if not after_offsets:
        print(f"  FAILED: {err}")
        return
    after_raw, _ = read_clock_table_raw(gpu)

    all_ok = True
    for p in points:
        expected = delta_khz
        actual = after_offsets[p]
        match = "OK" if actual == expected else "MISMATCH"
        if actual != expected:
            all_ok = False
        print(f"  Point {p:3d}: expected {expected/1000:+8.0f} MHz, "
              f"got {actual/1000:+8.0f} MHz  [{match}]")

    print()
    print("Step 5: Checking for unintended side effects...")
    collateral = 0
    for i in range(CT_POINTS):
        if i in point_deltas:
            continue
        if before_offsets[i] != after_offsets[i]:
            collateral += 1
            print(f"  WARNING: Point {i} changed unexpectedly: "
                  f"{before_offsets[i]/1000:+.0f} → {after_offsets[i]/1000:+.0f} MHz")
    if collateral == 0:
        print("  No unintended changes detected.")

    if before_raw and after_raw:
        print()
        print("Step 6: Checking if driver modified any unknown fields...")
        for p in points[:5]:
            before_entry = read_clock_entry_full(before_raw, p)
            after_entry = read_clock_entry_full(after_raw, p)
            for key in before_entry:
                if key == "freqDelta_kHz":
                    continue
                if before_entry[key] != after_entry[key]:
                    print(f"  Point {p}, {key}: {before_entry[key]} → {after_entry[key]}")

    voltage, _ = read_voltage(gpu)
    if voltage:
        print(f"\nCurrent voltage after write: {voltage/1000:.1f} mV")

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
    print(f"  sudo nvcurve snapshot restore")


def cmd_snapshot(gpu, gpu_name, args, cfg: Config):
    if args.action == "save":
        snapshot_save(gpu, gpu_name, cfg.snapshot_dir)
    elif args.action == "restore":
        snapshot_restore(gpu, cfg.snapshot_dir, filepath=args.file)
    elif args.action == "list":
        snapshots = list_snapshots(cfg.snapshot_dir)
        if not snapshots:
            print(f"No snapshots in {cfg.snapshot_dir}")
            return
        print(f"Snapshots in {cfg.snapshot_dir}:")
        for s in snapshots:
            print(f"  {s.timestamp}  {s.gpu}  non-zero: {s.nonzero_offsets}")
            print(f"    {s.filepath}")


def cmd_profile(gpu, gpu_name, args, cfg: Config):
    from .profiles.native import save_profile, load_profile, list_profiles, ProfileData
    from .hal.limits import get_power_limit, set_power_limit, get_clock_offsets, set_clock_offsets
    from .hal.vfcurve import reset_offsets, write_offsets

    if args.action == "list":
        profiles = list_profiles(cfg.profile_dir)
        if not profiles:
            print(f"No profiles found in {cfg.profile_dir}")
            return
        print(f"Profiles in {cfg.profile_dir}:")
        for p in profiles:
            pts = len(p.curve_deltas)
            print(f"  - {p.name} ({pts} pts)")
            
    elif args.action == "save":
        if not args.name:
            print("Error: --name required for profile save")
            return
            
        points, _ = read_vfp_curve(gpu)
        offsets, _ = read_clock_offsets(gpu)
        deltas = {}
        if offsets:
            for i, off in enumerate(offsets):
                if off != 0: deltas[str(i)] = off
                
        power = get_power_limit(0)
        clk_offsets = get_clock_offsets(0)

        pr = ProfileData(
            name=args.name,
            gpu_name=gpu_name,
            curve_deltas=deltas,
            mem_offset_mhz=clk_offsets.get("mem_offset_mhz"),
            power_limit_w=power.get("power_limit_w"),
        )
        
        path = save_profile(cfg.profile_dir, pr)
        print(f"Saved profile '{pr.name}' to {path}")
        
    elif args.action == "apply":
        if not args.name:
            print("Error: --name required for profile apply")
            return
            
        import os
        safe_name = "".join(c for c in args.name if c.isalnum() or c in " _-()").strip()
        filepath = os.path.join(cfg.profile_dir, f"{safe_name}.json")
        if not os.path.exists(filepath):
            print(f"Error: Profile '{args.name}' not found.")
            return
            
        pr = load_profile(filepath)
        print(f"Applying profile '{pr.name}'...")
        
        if pr.curve_deltas:
            reset_offsets(gpu)
            int_deltas = {int(k): int(v) for k, v in pr.curve_deltas.items()}
            errs = validate_write(int_deltas, cfg.max_delta_khz)
            if errs:
                print(f"Curve validation failed: {errs[0]}")
                return
            ret, desc = write_offsets(gpu, int_deltas)
            print(f"  Curve Write: {desc}")
        else:
            reset_offsets(gpu)
            print("  Curve Write: OK (reset)")
            
        if pr.power_limit_w is not None:
            ok, msg = set_power_limit(pr.power_limit_w, 0)
            print(f"  Power Limit: {msg}")

        if pr.mem_offset_mhz is not None:
            ok, msg = set_clock_offsets(mem_offset_mhz=pr.mem_offset_mhz, gpu_index=0)
            print(f"  Mem Offset: {msg}")


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read/Write NVIDIA GPU V/F curve via undocumented NvAPI (Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s read                          Condensed V/F curve
  %(prog)s read --full                   All 128 points
  %(prog)s read --json                   JSON output
  %(prog)s inspect --point 80            Show all 9 fields for point 80
  %(prog)s write --point 80 --delta 15 --dry-run
                                         Preview writing +15 MHz to point 80
  %(prog)s verify --point 80 --delta 15  Write + verify cycle
  %(prog)s write --global --delta 50     +50 MHz to all points
  %(prog)s write --reset                 Reset all offsets to 0
  %(prog)s snapshot save                 Save current ClockBoostTable
  %(prog)s snapshot restore              Restore most recent snapshot
  %(prog)s snapshot list                 List all saved snapshots
""",
    )
    sub = parser.add_subparsers(dest="command")

    # read
    p_read = sub.add_parser("read", help="Read V/F curve (default)")
    p_read.add_argument("--full", action="store_true", help="Show all 128 points")
    p_read.add_argument("--json", action="store_true", help="JSON output")
    p_read.add_argument("--raw", action="store_true", help="Include hex dumps")
    p_read.add_argument("--diag", action="store_true", help="Probe all functions")

    # inspect
    p_insp = sub.add_parser("inspect", help="Show detailed entry fields")
    p_insp.add_argument("--point", type=int, help="Single point index")
    p_insp.add_argument("--range", type=parse_range, help="Point range A-B")

    # write
    p_write = sub.add_parser("write", help="Write frequency offsets")
    tgt = p_write.add_mutually_exclusive_group()
    tgt.add_argument("--point", type=int, help="Single point index")
    tgt.add_argument("--range", type=parse_range, help="Point range A-B")
    tgt.add_argument("--global", dest="glob", action="store_true",
                     help="All points (like global NVML offset)")
    tgt.add_argument("--reset", action="store_true", help="Reset all offsets to 0")
    p_write.add_argument("--delta", type=float, default=0.0,
                         help="Frequency offset in MHz (e.g. 15, -30)")
    p_write.add_argument("--dry-run", action="store_true",
                         help="Preview changes without applying")
    p_write.add_argument("--force-idle", action="store_true",
                         help="Allow modifying point 127 (idle)")
    p_write.add_argument("--max-delta", type=float, default=300.0,
                         help="Override safety limit (MHz, default 300)")

    # verify
    p_ver = sub.add_parser("verify", help="Write-verify-read cycle")
    p_ver.add_argument("--point", type=int, help="Single point index")
    p_ver.add_argument("--range", type=parse_range, help="Point range A-B")
    p_ver.add_argument("--delta", type=float, required=True,
                       help="Frequency offset in MHz")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Save/restore/list ClockBoostTable snapshots")
    p_snap.add_argument("action", choices=["save", "restore", "list"], help="save, restore, or list")
    p_snap.add_argument("--file", help="Snapshot file path (for restore)")

    # profile
    p_prof = sub.add_parser("profile", help="Save/apply/list native profiles")
    p_prof.add_argument("action", choices=["save", "apply", "list"], help="save, apply, or list")
    p_prof.add_argument("--name", help="Profile name (for save/apply)")

    # serve
    p_srv = sub.add_parser("serve", help="Start or manage the FastAPI server")
    s_srv = p_srv.add_subparsers(dest="action")
    
    # serve start (default)
    p_start = s_srv.add_parser("start", help="Start the server")
    p_start.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    p_start.add_argument("--port", type=int, default=8042, help="Port (default 8042)")
    p_start.add_argument("--gpu", type=int, default=0, dest="gpu_index",
                         help="GPU index (default 0)")
    p_start.add_argument("--detach", "-d", action="store_true", help="Run in background")

    # serve stop
    p_stop = s_srv.add_parser("stop", help="Stop the background server")

    # serve status
    p_status = s_srv.add_parser("status", help="Check server status")

    return parser


def require_root():
    """Ensure the process is running as root, by re-invoking via sudo if necessary."""
    if os.geteuid() != 0:
        print("NVCurve requires root privileges to interface with the NVIDIA driver.")
        print("Requesting elevated permissions...")
        try:
            # Prevent Python from creating root-owned __pycache__ inside the user's site-packages
            os.execvp("sudo", ["sudo", "env", "PYTHONDONTWRITEBYTECODE=1", sys.executable, "-m", "nvcurve"] + sys.argv[1:])
        except Exception as e:
            print(f"Failed to elevate privileges: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    require_root()

    parser = build_parser()
    args = parser.parse_args()

    # Default to 'read' with no args
    if args.command is None:
        args.command = "read"
        args.full = False
        args.json = False
        args.raw = False
        args.diag = False

    cfg = Config()

    # serve: the server initializes the GPU itself via FastAPI lifespan
    if args.command == "serve":
        PID_FILE = "/run/nvcurve.pid"
        if os.geteuid() != 0:
            PID_FILE = "/tmp/nvcurve.pid"

        action = getattr(args, "action", "start") or "start"

        if action == "start":
            if os.path.exists(PID_FILE):
                try:
                    with open(PID_FILE, "r") as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 0)
                    print(f"Error: Server is already running (PID {pid}).")
                    return
                except (ProcessLookupError, ValueError, OSError):
                    os.remove(PID_FILE)

            if getattr(args, "detach", False):
                import subprocess
                # Re-run ourselves without --detach, redirecting output
                cmd = ["sudo", sys.executable, "-m", "nvcurve", "serve", "start"]
                if args.host: cmd += ["--host", args.host]
                if args.port: cmd += ["--port", str(args.port)]
                if args.gpu_index: cmd += ["--gpu", str(args.gpu_index)]
                
                # Use a log file for background mode
                log_file = "/var/log/nvcurve.log"
                if os.geteuid() != 0:
                    log_file = "/tmp/nvcurve.log"
                
                print(f"Starting nvcurve server in background...")
                with open(log_file, "a") as log:
                    p = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
                    with open(PID_FILE, "w") as f:
                        f.write(str(p.pid))
                print(f"Server started (PID {p.pid}). Logs: {log_file}")
                return
            else:
                # Foreground mode
                with open(PID_FILE, "w") as f:
                    f.write(str(os.getpid()))
                try:
                    from .server import run as server_run
                    # We need to ensure cfg is populated if we use it inside server_run
                    # but server_run takes direct arguments normally.
                    server_run(host=args.host, port=args.port, gpu_index=args.gpu_index, config=cfg)
                finally:
                    if os.path.exists(PID_FILE):
                        os.remove(PID_FILE)
                return

        elif action == "stop":
            if not os.path.exists(PID_FILE):
                print("Server is not running.")
                return
            try:
                with open(PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                print(f"Stopping server (PID {pid})...")
                os.kill(pid, 15) # SIGTERM
                time.sleep(1)
                if os.path.exists(PID_FILE):
                    os.remove(PID_FILE)
                print("Stopped.")
            except Exception as e:
                print(f"Error stopping server: {e}")
            return

        elif action == "status":
            if not os.path.exists(PID_FILE):
                print("Server is NOT running.")
            else:
                try:
                    with open(PID_FILE, "r") as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 0)
                    print(f"Server is running (PID {pid}).")
                except (ProcessLookupError, OSError):
                    print("Server is NOT running (stale PID file found).")
                    os.remove(PID_FILE)
            return

    # Allow --max-delta to override the safety limit
    if args.command == "write" and hasattr(args, "max_delta"):
        cfg.max_delta_khz = int(args.max_delta * 1000)

    gpu, gpu_name = get_gpu(index=0)

    if args.command == "read":
        cmd_read(gpu, gpu_name, args)
    elif args.command == "inspect":
        cmd_inspect(gpu, gpu_name, args)
    elif args.command == "write":
        cmd_write(gpu, gpu_name, args, cfg)
    elif args.command == "verify":
        cmd_verify(gpu, gpu_name, args, cfg)
    elif args.command == "snapshot":
        cmd_snapshot(gpu, gpu_name, args, cfg)
    elif args.command == "profile":
        cmd_profile(gpu, gpu_name, args, cfg)
