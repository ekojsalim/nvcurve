"""HTTP client for communicating with a running nvcurve server."""

import httpx
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8042"
_TIMEOUT = 5.0


class ServerNotRunning(Exception):
    """Raised when the nvcurve server cannot be reached."""


class ApiError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class NvCurveClient:
    def __init__(self, base: str = DEFAULT_BASE):
        self._base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _raise(self, r: httpx.Response) -> None:
        if r.is_error:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise ApiError(r.status_code, detail)

    def _get(self, path: str) -> Any:
        try:
            r = httpx.get(self._url(path), timeout=_TIMEOUT)
        except httpx.ConnectError:
            raise ServerNotRunning()
        self._raise(r)
        return r.json()

    def _post(self, path: str, body: Any = None) -> Any:
        try:
            r = httpx.post(self._url(path), json=body, timeout=_TIMEOUT)
        except httpx.ConnectError:
            raise ServerNotRunning()
        self._raise(r)
        return r.json()

    def _delete(self, path: str) -> Any:
        try:
            r = httpx.delete(self._url(path), timeout=_TIMEOUT)
        except httpx.ConnectError:
            raise ServerNotRunning()
        self._raise(r)
        return r.json()

    def ping(self) -> bool:
        try:
            httpx.get(self._url("/api/gpu"), timeout=1.0)
            return True
        except Exception:
            return False

    # ── GPU ──────────────────────────────────────────────────────────────────

    def gpu(self) -> dict:
        return self._get("/api/gpu")

    # ── Curve ────────────────────────────────────────────────────────────────

    def curve(self) -> dict:
        """Returns {gpu_name, timestamp, points: [{index, freq_khz, volt_uv, delta_khz, ...}]}"""
        return self._get("/api/curve")

    def voltage(self) -> int | None:
        """Returns current GPU voltage in µV, or None if unavailable."""
        try:
            return self._get("/api/voltage")["voltage_uv"]
        except Exception:
            return None

    def write_curve(
        self,
        deltas: dict[int, int],
        force_idle: bool = False,
        max_delta_khz: int | None = None,
    ) -> dict:
        body: dict = {"deltas": deltas, "force_idle": force_idle}
        if max_delta_khz is not None:
            body["max_delta_khz"] = max_delta_khz
        return self._post("/api/curve/write", body)

    def write_global(self, delta_khz: int, max_delta_khz: int | None = None) -> dict:
        body: dict = {"delta_khz": delta_khz}
        if max_delta_khz is not None:
            body["max_delta_khz"] = max_delta_khz
        return self._post("/api/curve/write/global", body)

    def reset_curve(self) -> dict:
        return self._post("/api/curve/reset")

    def verify_write(self, deltas: dict[int, int]) -> dict:
        return self._post("/api/curve/verify", {"deltas": deltas})

    # ── Snapshots ────────────────────────────────────────────────────────────

    def snapshot_save(self) -> dict:
        return self._post("/api/snapshot/save")

    def snapshot_restore(self, filepath: str | None = None) -> dict:
        return self._post("/api/snapshot/restore", {"filepath": filepath})

    def snapshots(self) -> list:
        return self._get("/api/snapshots")

    # ── Profiles ─────────────────────────────────────────────────────────────

    def profiles(self) -> dict:
        return self._get("/api/profiles")

    def profile_save(self, name: str) -> dict:
        return self._post("/api/profiles", {"name": name})

    def profile_apply(self, name: str) -> dict:
        return self._post(f"/api/profiles/{name}/apply")

    def profile_delete(self, name: str) -> dict:
        return self._delete(f"/api/profiles/{name}")

    # ── Server control ───────────────────────────────────────────────────────

    def shutdown(self) -> dict:
        return self._post("/api/shutdown")
