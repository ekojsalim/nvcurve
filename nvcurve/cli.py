"""nvcurve CLI

Normal use (no root required):
    nvcurve                                        Launch web UI
    nvcurve read [--full|--json]                   Read V/F curve
    nvcurve write [--point N|--range A-B|--global|--reset] --delta D [--dry-run]
    nvcurve verify --point N --delta D             Write-verify cycle
    nvcurve snapshot [save|restore|list]           Manage snapshots
    nvcurve profile [save|apply|list]              Manage profiles
    nvcurve serve start [--detach]                 Start server (escalates to root)
    nvcurve serve stop                             Stop running server
    nvcurve serve status                           Check server status
    nvcurve service install                        Register systemd service (escalates to root)
    nvcurve service uninstall                      Remove systemd service (escalates to root)
    nvcurve service start                          Start systemd service (escalates to root)
    nvcurve service stop                           Stop systemd service (escalates to root)
    nvcurve service restart                        Restart systemd service (escalates to root)
    nvcurve service status                         Check systemd service status

Diagnostic commands (bypass server, escalate to root):
    nvcurve read --diag                            Probe all NvAPI functions
    nvcurve read --raw                             Raw hex dumps of hardware buffers
    nvcurve inspect [--point N|--range A-B]        Raw ClockBoostTable field detail
"""

import argparse
import json
import struct
import sys
import time
import os

from .config import Config, default_config
from .client import NvCurveClient, ServerNotRunning, ApiError
from .nvapi.constants import (
    VFP_SIZE, VFP_BASE, VFP_STRIDE,
    CT_SIZE, CT_BASE, CT_STRIDE,
    CT_POINTS,
)


# ── Utilities ─────────────────────────────────────────────────────────────────

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


# ── Output formatters ─────────────────────────────────────────────────────────

def print_curve(points, offsets, voltage, full=False):
    """Print formatted V/F curve table."""
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
        elif f < 1_000_000 and v > 0:
            marker = "  (low-power)"
        print(f"{i:3d}  {freq_s:>8s}  {volt_s:>8s}  {offset_s:>8s}{marker}")

    active = [(f, v) for f, v in points if f > 0 and v > 0]
    if active:
        freqs = [f for f, v in active]
        volts = [v for f, v in active]
        print()
        print(f"Frequency range: {min(freqs)/1000:.0f} – {max(freqs)/1000:.0f} MHz")
        print(f"Voltage range:   {min(volts)/1000:.0f} – {max(volts)/1000:.0f} mV")
        print(f"V/F points: {len(active)}")
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


# ── Diagnostics (direct HAL, root required) ───────────────────────────────────

def run_diagnostics(gpu, gpu_name):
    """Probe all known NvAPI functions and report results."""
    from .hal.vfcurve import get_boost_mask
    from .nvapi.bootstrap import nvcall, query_interface
    from .nvapi.constants import FUNC, MASK_SIZE, VOLT_SIZE, RANGES_SIZE, PERF_SIZE, VBOOST_SIZE

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

    mask_bytes = None
    mask_err = ""
    print()
    print("=== Read function tests ===")
    
    mask_bytes, mask_err = get_boost_mask(gpu)
    if not mask_bytes:
        print(f"  WARNING: Failed to get boost mask: {mask_err}")

    for name, fid, size, ver, needs_mask in probes:
        if name.startswith("Set"):
            continue

        def fill(buf, _nm=needs_mask, _fid=fid, _mask=mask_bytes):
            if _nm and _mask:
                for i in range(32):
                    buf[4 + i] = _mask[i]
            if _fid == FUNC["GetVFPCurve"]:
                struct.pack_into("<I", buf, 0x14, 15)

        d, err = nvcall(fid, gpu, size, ver=ver, pre_fill=fill)
        status = f"OK ({len(d)} bytes)" if d else f"FAILED: {err}"
        print(f"  {name:30s}  {status}")
        if d:
            vw = struct.unpack_from("<I", d, 0)[0]
            print(f"    version_word = 0x{vw:08X}")


# ── Privilege / browser helpers ───────────────────────────────────────────────

def _open_browser_as_user(url: str) -> None:
    """Open URL in browser, switching back to the original user if running under sudo."""
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


def _server_not_running(base_url: str = "http://127.0.0.1:8042") -> None:
    """Print a helpful error and exit when the server is not reachable."""
    unit_path = "/etc/systemd/system/nvcurve.service"
    print(f"nvcurve: server not reachable at {base_url}", file=sys.stderr)
    print(file=sys.stderr)
    if os.path.exists(unit_path):
        print("Start it with:", file=sys.stderr)
        print("  sudo systemctl start nvcurve", file=sys.stderr)
    else:
        print("Start it with:", file=sys.stderr)
        print("  nvcurve serve start --detach", file=sys.stderr)
        print(file=sys.stderr)
        print("Or register as a persistent systemd service:", file=sys.stderr)
        print("  nvcurve service install", file=sys.stderr)
    sys.exit(1)


def require_root():
    """Ensure the process is running as root, re-invoking via sudo if necessary."""
    if os.geteuid() != 0:
        # Forward display/session vars so the server can open the browser as the
        # original user (Wayland sockets are user-owned; Firefox refuses to run as root).
        passthrough = [
            f"{k}={v}"
            for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                      "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY")
            if (v := os.environ.get(k))
        ]
        try:
            # PYTHONDONTWRITEBYTECODE prevents root-owned __pycache__ in site-packages.
            os.execvp("sudo", [
                "sudo", "env", "PYTHONDONTWRITEBYTECODE=1", *passthrough,
                sys.executable, "-m", "nvcurve", *sys.argv[1:]
            ])
        except Exception as e:
            print(f"nvcurve: sudo failed: {e}", file=sys.stderr)
            sys.exit(1)


_SERVER_INFO_FILE = "/run/nvcurve.json"       # runtime: written by server, deleted on exit
_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"  # persistent: written by service install

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _safe_host(host: str, cfg: Config) -> str:
    """Return host if it is a loopback address, otherwise fall back to cfg.host."""
    if host not in _ALLOWED_HOSTS:
        print(f"nvcurve: ignoring untrusted host '{host}' in server info; "
              f"using {cfg.host}", file=sys.stderr)
        return cfg.host
    return host


def _log_file() -> str:
    return "/var/log/nvcurve.log" if os.geteuid() == 0 else "/tmp/nvcurve.log"


def _read_server_info() -> dict | None:
    """Read the server's runtime info (host, port, pid) from its info file.

    The file is written by the server process on startup and deleted on exit.
    Returns None if the file is absent, stale, or unreadable.
    """
    try:
        with open(_SERVER_INFO_FILE) as f:
            info = json.load(f)
        # Verify the process is still alive
        os.kill(info["pid"], 0)
        return info
    except (FileNotFoundError, KeyError, ProcessLookupError, OSError, json.JSONDecodeError):
        return None


def _discover_server_url(cfg: Config) -> str:
    """Return the server's base URL using a three-level priority chain:

    1. /run/nvcurve.json      — runtime info written by the running server process
    2. /etc/nvcurve/config.json — persistent config written by `service install`
    3. Config defaults          — 127.0.0.1:8042
    """
    # 1. Runtime info (most accurate — reflects the actual running port)
    info = _read_server_info()
    if info:
        host = _safe_host(info["host"], cfg)
        return f"http://{host}:{info['port']}"

    # 2. Persistent config (survives reboots; written by `service install`)
    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            data = json.load(f)
        host = _safe_host(data.get("host", cfg.host), cfg)
        port = data.get("port", cfg.port)
        return f"http://{host}:{port}"
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # 3. Hardcoded defaults
    return f"http://{cfg.host}:{cfg.port}"


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _show_curve(gpu_name, points, offsets, voltage, args) -> None:
    """Format and print curve data — shared by HTTP and direct-HAL paths."""
    if args.json:
        output_json(gpu_name, points, offsets, voltage)
        return
    print(f"GPU: {gpu_name}")
    print_curve(points, offsets, voltage, full=args.full)


def cmd_read(args, client: NvCurveClient):
    # ── Direct HAL paths (need root) ──────────────────────────────────────────
    if args.diag:
        require_root()
        from .hal.gpu import get_gpu
        gpu, gpu_name = get_gpu(index=0)
        run_diagnostics(gpu, gpu_name)
        return

    if args.raw:
        require_root()
        from .hal.gpu import get_gpu
        from .hal.vfcurve import read_clock_table_raw, get_boost_mask
        from .hal.monitoring import read_voltage
        from .nvapi.bootstrap import nvcall
        from .nvapi.constants import FUNC
        gpu, gpu_name = get_gpu(index=0)

        print(f"GPU: {gpu_name}")

        mask_bytes, _ = get_boost_mask(gpu)
        def fill_vfp(buf):
            if mask_bytes:
                for i in range(32):
                    buf[4 + i] = mask_bytes[i]
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

        from .hal.vfcurve import read_curve
        curve_state, _ = read_curve(gpu, gpu_name)
        voltage, _ = read_voltage(gpu)
        if curve_state:
            points = [(p.freq_khz, p.volt_uv) for p in curve_state.points]
            offsets = [p.delta_khz for p in curve_state.points]
            _show_curve(gpu_name, points, offsets, voltage, args)
        return

    # ── Normal path — via server, with direct-HAL fallback ────────────────────
    try:
        curve_data = client.curve()
        voltage = client.voltage()
    except ServerNotRunning:
        # Server not running: escalate and read hardware directly.
        # require_root() either re-execs (if not root, doesn't return here) or
        # is a no-op (if already root); direct HAL runs in the root process.
        print("(server not running — reading hardware directly)", file=sys.stderr)
        require_root()
        from .hal.gpu import get_gpu
        from .hal.vfcurve import read_curve
        from .hal.monitoring import read_voltage as _read_voltage
        gpu, gpu_name = get_gpu(index=0)
        curve_state, curve_err = read_curve(gpu, gpu_name)
        if not curve_state:
            print(f"Failed to read V/F curve: {curve_err}")
            return
        voltage, _ = _read_voltage(gpu)
        points = [(p.freq_khz, p.volt_uv) for p in curve_state.points]
        offsets = [p.delta_khz for p in curve_state.points]
        _show_curve(gpu_name, points, offsets, voltage, args)
        return

    gpu_name = curve_data["gpu_name"]
    pts = curve_data["points"]
    points = [(p["freq_khz"], p["volt_uv"]) for p in pts]
    offsets = [p["delta_khz"] for p in pts]
    _show_curve(gpu_name, points, offsets, voltage, args)


def cmd_inspect(args):
    """Show detailed raw ClockBoostTable fields. Requires root (direct HAL)."""
    require_root()
    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_clock_table_raw, read_clock_entry_full, read_vfp_curve

    gpu, gpu_name = get_gpu(index=0)
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


def cmd_write(args, client: NvCurveClient):
    delta_khz = int(args.delta * 1000)
    max_delta_khz = int(args.max_delta * 1000) if args.max_delta is not None else None
    point_deltas = {}

    if args.reset:
        if args.dry_run:
            print("DRY RUN — would reset all offsets to 0.")
            return
        try:
            result = client.reset_curve()
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        except ApiError as e:
            print(f"Reset failed: {e.detail}")
            return
        print("Reset: all offsets set to 0.")
        _print_write_warnings(result)
        return

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
        print(f"Target: all active points (global), "
              f"delta {args.delta:+.0f} MHz")

    else:
        print("Error: specify --point N, --range A-B, --global, or --reset")
        return

    if args.dry_run:
        if args.glob or args.reset:
            print()
            print("DRY RUN — would send:")
            print(f"  Target: {'Reset' if args.reset else 'Global active points'}")
            print(f"  Delta:  {delta_khz:+d} kHz ({args.delta:+.0f} MHz)")
        else:
            keys = sorted(point_deltas.keys())
            preview = keys[:5]
            tail = f"...and {len(keys) - 5} more" if len(keys) > 5 else ""
            print()
            print("DRY RUN — would send:")
            print(f"  Points: {preview}{(' ' + tail) if tail else ''}")
            print(f"  Delta:  {delta_khz:+d} kHz ({args.delta:+.0f} MHz)")
        if max_delta_khz is not None:
            print(f"  Max delta override: {args.max_delta:+.0f} MHz")
        return

    try:
        if args.glob:
            result = client.write_global(delta_khz, max_delta_khz=max_delta_khz)
        else:
            result = client.write_curve(
                point_deltas,

                max_delta_khz=max_delta_khz,
            )
    except ServerNotRunning:
        _server_not_running(client._base)
        return
    except ApiError as e:
        print(f"Write failed: {e.detail}")
        return

    print(f"Write OK — {len(point_deltas)} point(s) updated.")
    _print_write_warnings(result)


def _print_write_warnings(result: dict) -> None:
    if result.get("warning"):
        w = result["warning"]
        msg = w.get("message", w) if isinstance(w, dict) else w
        print(f"\nWARNING: {msg}")
    for w in result.get("freq_warnings", []):
        print(f"WARNING: {w}")


def cmd_verify(args, client: NvCurveClient):
    """Write-verify-read cycle — runs directly against hardware (requires root)."""
    require_root()

    from .hal.gpu import get_gpu
    from .hal.vfcurve import read_clock_offsets, write_offsets
    from .hal.snapshot import save as snapshot_save

    delta_khz = int(args.delta * 1000)

    if args.point is not None:
        points = [args.point]
    elif args.range:
        points = list(range(args.range[0], args.range[1] + 1))
    else:
        print("Error: --point or --range required for verify mode")
        return

    point_deltas = {p: delta_khz for p in points}

    gpu, gpu_name = get_gpu(index=0)

    print("=== Write-Verify Cycle ===")
    print(f"GPU:    {gpu_name}")
    print(f"Points: {points[0]}{'–' + str(points[-1]) if len(points) > 1 else ''}")
    print(f"Delta:  {args.delta:+.0f} MHz ({delta_khz:+d} kHz)")
    print()

    # Step 1: read before state
    before_offsets, err = read_clock_offsets(gpu)
    if before_offsets is None:
        print(f"Failed to read current state: {err}")
        return

    # Step 2: snapshot before write
    filepath = snapshot_save(gpu, gpu_name, default_config.snapshot_dir)
    if filepath:
        print(f"Snapshot saved: {filepath}")

    # Step 3: write
    print("Writing and verifying...")
    ret, desc = write_offsets(gpu, point_deltas)
    if ret != 0:
        print(f"Write failed ({ret}): {desc}")
        return

    time.sleep(0.2)

    # Step 4: read after state
    after_offsets, err = read_clock_offsets(gpu)
    if after_offsets is None:
        print(f"Verification read failed: {err}")
        return

    print()
    print("Verification results:")
    all_matched = True
    for p in sorted(point_deltas):
        expected = point_deltas[p]
        actual = after_offsets[p] if p < len(after_offsets) else 0
        match = actual == expected
        if not match:
            all_matched = False
        match_s = "OK" if match else "MISMATCH"
        print(f"  Point {p:3d}: expected {expected/1000:+8.0f} MHz, "
              f"got {actual/1000:+8.0f} MHz  [{match_s}]")

    collateral = [
        {"point": i, "before_khz": before_offsets[i], "after_khz": after_offsets[i]}
        for i in range(min(len(before_offsets), len(after_offsets)))
        if i not in point_deltas and before_offsets[i] != after_offsets[i]
    ]
    print()
    if collateral:
        print("Unintended side effects detected:")
        for c in collateral:
            print(f"  WARNING: Point {c['point']} changed: "
                  f"{c['before_khz']/1000:+.0f} → {c['after_khz']/1000:+.0f} MHz")
    else:
        print("No unintended side effects detected.")

    print()
    print("=" * 50)
    if all_matched and not collateral:
        print("RESULT: Write verified successfully.")
    elif not all_matched:
        print("RESULT: Write verification FAILED — offsets don't match.")
    else:
        print("RESULT: Write applied but with unexpected side effects.")

    print()
    print("To undo this change, run:")
    print("  nvcurve snapshot restore")


def cmd_snapshot(args, client: NvCurveClient):
    if args.action == "save":
        try:
            result = client.snapshot_save()
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        except ApiError as e:
            print(f"Snapshot save failed: {e.detail}")
            return
        print(f"Snapshot saved: {result.get('filepath', '?')}")

    elif args.action == "restore":
        try:
            client.snapshot_restore(filepath=args.file)
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        except ApiError as e:
            print(f"Restore failed: {e.detail}")
            return
        print("Snapshot restored.")

    elif args.action == "list":
        try:
            snapshots = client.snapshots()
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        if not snapshots:
            print("No snapshots found.")
            return
        print("Snapshots:")
        for s in snapshots:
            print(f"  {s['timestamp']}  {s['gpu']}  non-zero: {s['nonzero_offsets']}")
            print(f"    {s['filepath']}")


def cmd_profile(args, client: NvCurveClient):
    if args.action == "list":
        try:
            data = client.profiles()
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        profiles = data.get("profiles", [])
        active = data.get("active")
        if not profiles:
            print("No profiles found.")
            return
        print("Profiles:")
        for p in profiles:
            marker = "  *" if p["name"] == active else ""
            pts = len(p["curve_deltas"])
            print(f"  - {p['name']} ({pts} pts){marker}")

    elif args.action == "save":
        if not args.name:
            print("Error: --name required for profile save")
            return
        try:
            result = client.profile_save(args.name)
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        except ApiError as e:
            print(f"Save failed: {e.detail}")
            return
        print(f"Saved profile '{args.name}' to {result.get('filepath', '?')}")

    elif args.action == "apply":
        if not args.name:
            print("Error: --name required for profile apply")
            return
        try:
            client.profile_apply(args.name)
        except ServerNotRunning:
            _server_not_running(client._base)
            return
        except ApiError as e:
            print(f"Apply failed: {e.detail}")
            return
        print(f"Applied profile '{args.name}'.")


def cmd_service(args):
    """Manage the nvcurve systemd service."""
    action = getattr(args, "action", None)
    if not action:
        print("Usage: nvcurve service [install|uninstall|start|stop|restart|status]")
        return

    unit_path = "/etc/systemd/system/nvcurve.service"

    if action == "install":
        require_root()
        import subprocess

        exec_cmd = f"{sys.executable} -m nvcurve"

        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8042)

        unit = (
            "[Unit]\n"
            "Description=NVCurve NVIDIA GPU V/F Curve Server\n"
            "After=nvidia-persistenced.service\n"
            "Wants=nvidia-persistenced.service\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_cmd} serve start --host {host} --port {port}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

        with open(unit_path, "w") as f:
            f.write(unit)
        print(f"Unit file written to {unit_path}")

        # Write persistent config so clients can discover host:port without the
        # runtime info file (e.g. before the service has started, after reboot).
        os.makedirs("/etc/nvcurve", exist_ok=True)
        with open(_PERSISTENT_CONFIG_FILE, "w") as f:
            json.dump({"host": host, "port": port}, f)
        print(f"Persistent config written to {_PERSISTENT_CONFIG_FILE}")

        try:
            subprocess.run(["systemctl", "daemon-reload"], check=True)

            was_active = subprocess.run(
                ["systemctl", "is-active", "--quiet", "nvcurve"],
            ).returncode == 0

            subprocess.run(["systemctl", "enable", "--now", "nvcurve"], check=True)
            print("Service enabled and started.")

            if was_active:
                print()
                print("Note: the service was already running and is still on the old version.")
                print("  Restart it to pick up the update:  nvcurve service restart")

            print()
            print("Useful commands:")
            print("  systemctl status nvcurve")
            print("  journalctl -u nvcurve -f")
            print("  nvcurve service uninstall")
        except subprocess.CalledProcessError as e:
            print(f"systemctl failed: {e}", file=sys.stderr)

    elif action == "uninstall":
        require_root()
        import subprocess

        if not os.path.exists(unit_path):
            print("Service is not installed.")
            return
        try:
            subprocess.run(["systemctl", "stop", "nvcurve"], check=False)
            subprocess.run(["systemctl", "disable", "nvcurve"], check=False)
            os.remove(unit_path)
            if os.path.exists(_PERSISTENT_CONFIG_FILE):
                os.remove(_PERSISTENT_CONFIG_FILE)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            print("Service stopped, disabled, and removed.")
        except Exception as e:
            print(f"Error during uninstall: {e}", file=sys.stderr)

    elif action == "start":
        require_root()
        import subprocess
        if not os.path.exists(unit_path):
            print("Service is not installed. Run: nvcurve service install")
            return
        try:
            subprocess.run(["systemctl", "start", "nvcurve"], check=True)
            print("Service started.")
        except subprocess.CalledProcessError as e:
            print(f"systemctl start failed: {e}", file=sys.stderr)

    elif action == "stop":
        require_root()
        import subprocess
        if not os.path.exists(unit_path):
            print("Service is not installed.")
            return
        try:
            subprocess.run(["systemctl", "stop", "nvcurve"], check=True)
            print("Service stopped.")
        except subprocess.CalledProcessError as e:
            print(f"systemctl stop failed: {e}", file=sys.stderr)

    elif action == "restart":
        require_root()
        import subprocess
        if not os.path.exists(unit_path):
            print("Service is not installed. Run: nvcurve service install")
            return
        try:
            subprocess.run(["systemctl", "restart", "nvcurve"], check=True)
            print("Service restarted.")
        except subprocess.CalledProcessError as e:
            print(f"systemctl restart failed: {e}", file=sys.stderr)

    elif action == "status":
        import subprocess

        if os.path.exists(unit_path):
            result = subprocess.run(
                ["systemctl", "is-active", "nvcurve"],
                capture_output=True, text=True,
            )
            active = result.stdout.strip()
            pid_info = ""
            if active == "active":
                r2 = subprocess.run(
                    ["systemctl", "show", "nvcurve", "--property=MainPID"],
                    capture_output=True, text=True,
                )
                pid = r2.stdout.strip().replace("MainPID=", "")
                if pid and pid != "0":
                    pid_info = f" (PID {pid})"
            print(f"systemd service: {active}{pid_info}")
        else:
            print("systemd service: not installed")
            print(f"  (no unit file at {unit_path})")
            print()
            print("Register with:  nvcurve service install")


# ── Server management ─────────────────────────────────────────────────────────

def _cmd_serve_start(args, cfg: Config, open_browser: bool = False) -> None:
    """Start the server. Requires root — calls require_root() internally."""
    require_root()

    host = getattr(args, "host", cfg.host)
    port = getattr(args, "port", cfg.port)

    # Warn if a systemd-managed server is already active — running a second
    # instance alongside it will cause port conflicts or split-brain state.
    unit_path = "/etc/systemd/system/nvcurve.service"
    if os.path.exists(unit_path):
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", "nvcurve"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            print("Warning: the nvcurve systemd service is already active.",
                  file=sys.stderr)
            print("  Starting a second instance may cause port conflicts.",
                  file=sys.stderr)
            print("  Manage it with:  systemctl stop nvcurve  /  nvcurve service status",
                  file=sys.stderr)
            print(file=sys.stderr)

    info = _read_server_info()
    if info:
        url = f"http://{info['host']}:{info['port']}"
        print(f"Server is already running (PID {info['pid']}) at {url}.")
        if open_browser:
            _open_browser_as_user(url)
        return

    if getattr(args, "detach", False):
        import subprocess
        # Already root (require_root() ran above) — spawn child without sudo.
        # Child inherits root UID, skips require_root() itself, runs foreground.
        cmd = [sys.executable, "-m", "nvcurve", "serve", "start",
               "--host", host, "--port", str(port)]
        if getattr(args, "gpu_index", 0):
            cmd += ["--gpu", str(args.gpu_index)]

        log_path = _log_file()
        print("Starting nvcurve server in background...")
        with open(log_path, "a") as lf:
            p = subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
        print(f"Server starting (PID {p.pid}). Logs: {log_path}")

        if open_browser:
            # Give the server a moment to write its info file and start accepting.
            time.sleep(1.5)
            url = _discover_server_url(cfg)
            _open_browser_as_user(url)
        return

    # Foreground mode — write info file so clients can discover host:port.
    with open(_SERVER_INFO_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "host": host, "port": port}, f)
    try:
        from .server import run as server_run
        server_run(
            host=host,
            port=port,
            gpu_index=getattr(args, "gpu_index", 0),
            config=cfg,
            open_browser=open_browser,
        )
    finally:
        if os.path.exists(_SERVER_INFO_FILE):
            os.remove(_SERVER_INFO_FILE)


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    from importlib.metadata import version as pkg_version
    try:
        __version__ = pkg_version("nvcurve")
    except Exception:
        __version__ = "unknown"

    parser = argparse.ArgumentParser(
        prog="nvcurve",
        description="Read/Write NVIDIA GPU V/F curve via undocumented NvAPI (Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                               Launch web UI (default)
  %(prog)s read                          Condensed V/F curve
  %(prog)s read --full                   All points
  %(prog)s read --json                   JSON output
  %(prog)s write --global --delta 50     +50 MHz to all points
  %(prog)s write --point 80 --delta 100  +100 MHz to point 80
  %(prog)s write --reset                 Reset all offsets to 0
  %(prog)s verify --point 80 --delta 15  Write + verify cycle
  %(prog)s snapshot save/restore/list    Manage snapshots
  %(prog)s profile save --name balanced  Save current state as profile
  %(prog)s serve start --detach          Start server in background
  %(prog)s serve stop                    Stop running server
  %(prog)s service install               Register as systemd service (recommended)
  %(prog)s read --diag                   Probe all NvAPI functions (needs root)
  %(prog)s inspect --point 80            Raw buffer fields for a point (needs root)
""",
    )
    parser.add_argument("-v", "--version", action="version", version=f"nvcurve {__version__}")
    parser.add_argument(
        "--server", default=None, metavar="URL",
        help="Server base URL (default: http://127.0.0.1:8042)",
    )
    sub = parser.add_subparsers(dest="command")

    # read
    p_read = sub.add_parser("read", help="Read V/F curve")
    p_read.add_argument("--full", action="store_true", help="Show all points")
    p_read.add_argument("--json", action="store_true", help="JSON output")
    p_read.add_argument("--raw", action="store_true",
                        help="Raw hex dumps of hardware buffers (needs root)")
    p_read.add_argument("--diag", action="store_true",
                        help="Probe all NvAPI functions (needs root)")

    # inspect
    p_insp = sub.add_parser("inspect",
                             help="Show raw ClockBoostTable buffer fields (needs root)")
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
    p_write.add_argument("--max-delta", type=float, default=None,
                         help="Override safety limit for this write (MHz)")

    # verify
    p_ver = sub.add_parser("verify", help="Write-verify-read cycle")
    p_ver.add_argument("--point", type=int, help="Single point index")
    p_ver.add_argument("--range", type=parse_range, help="Point range A-B")
    p_ver.add_argument("--delta", type=float, required=True,
                       help="Frequency offset in MHz")

    # snapshot
    p_snap = sub.add_parser("snapshot",
                             help="Save/restore/list ClockBoostTable snapshots")
    p_snap.add_argument("action", choices=["save", "restore", "list"])
    p_snap.add_argument("--file", help="Snapshot file path (for restore)")

    # profile
    p_prof = sub.add_parser("profile", help="Save/apply/list native profiles")
    p_prof.add_argument("action", choices=["save", "apply", "list"])
    p_prof.add_argument("--name", help="Profile name (for save/apply)")

    # serve
    p_srv = sub.add_parser("serve", help="Start or manage the server directly")
    s_srv = p_srv.add_subparsers(dest="action")

    p_start = s_srv.add_parser("start", help="Start the server (escalates to root)")
    p_start.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default 127.0.0.1)")
    p_start.add_argument("--port", type=int, default=8042,
                         help="Port (default 8042)")
    p_start.add_argument("--gpu", type=int, default=0, dest="gpu_index",
                         help="GPU index (default 0)")
    p_start.add_argument("--detach", "-d", action="store_true",
                         help="Run in background")

    s_srv.add_parser("stop", help="Stop the running server")
    s_srv.add_parser("status", help="Check server status")

    # service
    p_svc = sub.add_parser("service", help="Manage the nvcurve systemd service")
    s_svc = p_svc.add_subparsers(dest="action")

    p_install = s_svc.add_parser("install",
                                  help="Register as systemd service (escalates to root)")
    p_install.add_argument("--host", default="127.0.0.1",
                           help="Server bind address")
    p_install.add_argument("--port", type=int, default=8042,
                           help="Server port")

    s_svc.add_parser("uninstall",
                     help="Remove systemd service (escalates to root)")
    s_svc.add_parser("start",
                     help="Start systemd service (escalates to root)")
    s_svc.add_parser("stop",
                     help="Stop systemd service (escalates to root)")
    s_svc.add_parser("restart",
                     help="Restart systemd service (escalates to root)")
    s_svc.add_parser("status", help="Check systemd service status")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    cfg = Config()
    base_url = args.server or _discover_server_url(cfg)
    client = NvCurveClient(base=base_url)

    # Default — no subcommand: open the web UI.
    # If the server is already running, just open a browser tab (no root needed).
    # Otherwise start the server (require_root is called inside _cmd_serve_start).
    if args.command is None:
        if client.ping():
            print(f"Server is running at {base_url}")
            _open_browser_as_user(base_url)
        else:
            _cmd_serve_start(args, cfg, open_browser=True)
        return

    if args.command == "serve":
        action = getattr(args, "action", None) or "start"

        if action == "start":
            _cmd_serve_start(args, cfg, open_browser=False)

        elif action == "stop":
            try:
                client.shutdown()
                print("Server stopped.")
            except ServerNotRunning:
                print("Server is not running.")
                # Clean up stale info file if the process is gone
                if os.path.exists(_SERVER_INFO_FILE):
                    try:
                        os.remove(_SERVER_INFO_FILE)
                    except OSError:
                        pass
            except ApiError as e:
                print(f"Shutdown failed: {e.detail}", file=sys.stderr)

        elif action == "status":
            if client.ping():
                try:
                    info = client.gpu()
                    print(f"Server is running at {base_url}")
                    print(f"  GPU: {info.get('name', '?')}")
                    driver = info.get("driver_version")
                    if driver:
                        print(f"  Driver: {driver}")
                except Exception:
                    print(f"Server is running at {base_url}")
            else:
                print(f"Server is NOT running at {base_url}")
        return

    if args.command == "read":
        cmd_read(args, client)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "write":
        cmd_write(args, client)
    elif args.command == "verify":
        cmd_verify(args, client)
    elif args.command == "snapshot":
        cmd_snapshot(args, client)
    elif args.command == "profile":
        cmd_profile(args, client)
    elif args.command == "service":
        cmd_service(args)
