"""Minimal daemon: apply auto-load profiles on boot, manage web server via Unix socket.

Run via:  nvcurve daemon
Or via systemd:  nvcurve service install

Protocol: newline-delimited JSON, one request → one response, connection closed.

Commands:
  {"cmd": "ping"}
  {"cmd": "serve_start", "host": "127.0.0.1", "port": 8042}
  {"cmd": "serve_stop"}
  {"cmd": "serve_status"}

Requires root.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys

log = logging.getLogger("nvcurve.daemon")

SOCKET_PATH = "/run/nvcurve-daemon.sock"
_PERSISTENT_CONFIG_FILE = "/etc/nvcurve/config.json"

# Global server subprocess — only touched from the asyncio event loop.
_server_proc: subprocess.Popen | None = None
_cfg = None  # Config instance, set in run()


# ── Socket command handlers ────────────────────────────────────────────────────

async def _handle_serve_start(host: str, port: int) -> dict:
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        return {"ok": False, "error": "web server already running", "pid": _server_proc.pid}

    cmd = [sys.executable, "-m", "nvcurve", "serve", "start",
           "--host", host, "--port", str(port), "--direct"]
    log_path = "/var/log/nvcurve-server.log"
    log.info("Starting web server on %s:%d (log: %s)", host, port, log_path)
    with open(log_path, "a") as lf:
        _server_proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    log.info("Web server started (PID %d)", _server_proc.pid)
    return {"ok": True, "pid": _server_proc.pid}


async def _handle_serve_stop() -> dict:
    global _server_proc
    if _server_proc is None or _server_proc.poll() is not None:
        _server_proc = None
        return {"ok": False, "error": "web server is not running"}
    log.info("Stopping web server (PID %d)…", _server_proc.pid)
    _server_proc.terminate()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _server_proc.wait, 5
        )
    except Exception:
        _server_proc.kill()
    _server_proc = None
    return {"ok": True}


async def _handle_serve_status() -> dict:
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        return {"ok": True, "running": True, "pid": _server_proc.pid}
    _server_proc = None
    return {"ok": True, "running": False}


async def _dispatch(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    elif cmd == "serve_start":
        host = req.get("host", _cfg.host)
        port = req.get("port", _cfg.port)
        return await _handle_serve_start(host, port)
    elif cmd == "serve_stop":
        return await _handle_serve_stop()
    elif cmd == "serve_status":
        return await _handle_serve_status()
    else:
        return {"ok": False, "error": f"unknown command: {cmd!r}"}


async def _handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    resp: dict = {"ok": False, "error": "internal error"}
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        req = json.loads(data)
        resp = await _dispatch(req)
    except asyncio.TimeoutError:
        resp = {"ok": False, "error": "timeout reading request"}
    except json.JSONDecodeError as exc:
        resp = {"ok": False, "error": f"invalid JSON: {exc}"}
    except Exception as exc:
        resp = {"ok": False, "error": str(exc)}
    finally:
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def _load_persistent_config() -> dict:
    try:
        with open(_PERSISTENT_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def run() -> None:
    """Run the nvcurve daemon: apply auto-load profiles, then serve the Unix socket."""
    global _cfg

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if os.geteuid() != 0:
        print("nvcurve daemon: must run as root", file=sys.stderr)
        sys.exit(1)

    cfg_data = _load_persistent_config()

    from .config import Config
    _cfg = Config()
    for key in ("max_delta_khz", "auto_snapshot", "max_snapshots",
                "snapshot_dir", "profile_dir", "host", "port"):
        if key in cfg_data:
            setattr(_cfg, key, cfg_data[key])

    # Apply auto-load profiles in a subprocess so the daemon process itself
    # never loads NvAPI/NVML/HAL modules — keeps steady-state RSS low.
    auto_load_profiles: dict = cfg_data.get("auto_load_profiles", {})
    if auto_load_profiles:
        log.info("Applying auto-load profiles…")
        subprocess.run(
            [sys.executable, "-m", "nvcurve", "autoload"],
            check=False,
        )

    auto_serve: bool = cfg_data.get("auto_serve", False)
    asyncio.run(_serve_socket(auto_serve=auto_serve))

    log.info("Daemon stopped.")


async def _serve_socket(auto_serve: bool = False) -> None:
    global _server_proc

    # Clean up stale socket from a previous (unclean) run.
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = await asyncio.start_unix_server(_handle_client, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)  # allow non-root CLI to connect
    log.info("Daemon listening on %s", SOCKET_PATH)

    if auto_serve:
        log.info("auto_serve enabled — starting web server on boot")
        await _handle_serve_start(_cfg.host, _cfg.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    async with server:
        await stop_event.wait()

    # Gracefully stop the web server if it's running.
    if _server_proc is not None and _server_proc.poll() is None:
        log.info("Stopping web server (PID %d)…", _server_proc.pid)
        _server_proc.terminate()
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _server_proc.wait),
                timeout=5,
            )
        except asyncio.TimeoutError:
            _server_proc.kill()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
