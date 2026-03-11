"""Live GPU monitoring.

Voltage is read via NvAPI GetCurrentVoltage.
Clock, temperature, power draw, and fan speed are read via NVML (nvidia-ml-py).
"""

import struct
import time
from typing import Optional

from ..nvapi.bootstrap import nvcall
from ..nvapi.constants import FUNC, VOLT_SIZE
from ..nvapi.types import MonitoringSample

try:
    import pynvml as _pynvml
    _NVML_AVAILABLE = True
except ImportError:
    _pynvml = None
    _NVML_AVAILABLE = False

_nvml_initialized = False


def init_nvml() -> bool:
    """Initialize NVML. Call once at startup. Returns True on success."""
    global _nvml_initialized
    if not _NVML_AVAILABLE:
        return False
    try:
        _pynvml.nvmlInit()
        _nvml_initialized = True
        return True
    except _pynvml.NVMLError:
        return False


def shutdown_nvml() -> None:
    """Shut down NVML. Call at process exit."""
    global _nvml_initialized
    if _NVML_AVAILABLE and _nvml_initialized:
        try:
            _pynvml.nvmlShutdown()
        except _pynvml.NVMLError:
            pass
        _nvml_initialized = False


def get_driver_version() -> Optional[str]:
    """Return the NVIDIA driver version string, or None if unavailable."""
    if not (_NVML_AVAILABLE and _nvml_initialized):
        return None
    try:
        return _pynvml.nvmlSystemGetDriverVersion()
    except _pynvml.NVMLError:
        return None


def get_vram_total(gpu_index: int = 0) -> Optional[int]:
    """Return total VRAM in bytes, or None if unavailable."""
    if not (_NVML_AVAILABLE and _nvml_initialized):
        return None
    try:
        handle = _pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        return _pynvml.nvmlDeviceGetMemoryInfo(handle).total
    except _pynvml.NVMLError:
        return None


def read_voltage(gpu) -> tuple[Optional[int], str]:
    """Read current GPU core voltage in µV via NvAPI GetCurrentVoltage.

    Returns (voltage_uV, "OK") or (None, error).
    """
    d, err = nvcall(FUNC["GetCurrentVoltage"], gpu, VOLT_SIZE, ver=1)
    if not d:
        return None, err
    return struct.unpack_from("<I", d, 0x28)[0], "OK"


def _nvml_read(gpu_index: int) -> dict:
    """Read all NVML fields. Returns a dict with keys matching MonitoringSample fields."""
    out = {
        "clock_mhz": None, "temp_c": None, "power_w": None, "fan_pct": None,
        "pstate": None, "mem_used_bytes": None, "mem_total_bytes": None,
        "gpu_util_pct": None, "mem_util_pct": None, "mem_clock_mhz": None,
    }
    if not (_NVML_AVAILABLE and _nvml_initialized):
        return out
    try:
        handle = _pynvml.nvmlDeviceGetHandleByIndex(gpu_index)

        out["clock_mhz"] = float(_pynvml.nvmlDeviceGetClockInfo(handle, _pynvml.NVML_CLOCK_GRAPHICS))
        out["mem_clock_mhz"] = float(_pynvml.nvmlDeviceGetClockInfo(handle, _pynvml.NVML_CLOCK_MEM))
        out["temp_c"] = float(_pynvml.nvmlDeviceGetTemperature(handle, _pynvml.NVML_TEMPERATURE_GPU))
        out["power_w"] = _pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW → W
        out["pstate"] = int(_pynvml.nvmlDeviceGetPerformanceState(handle))

        mem = _pynvml.nvmlDeviceGetMemoryInfo(handle)
        out["mem_used_bytes"] = mem.used
        out["mem_total_bytes"] = mem.total

        util = _pynvml.nvmlDeviceGetUtilizationRates(handle)
        out["gpu_util_pct"] = float(util.gpu)
        out["mem_util_pct"] = float(util.memory)

        try:
            out["fan_pct"] = float(_pynvml.nvmlDeviceGetFanSpeed(handle))
        except _pynvml.NVMLError:
            pass
    except _pynvml.NVMLError:
        pass
    return out


def poll(gpu, gpu_index: int = 0) -> MonitoringSample:
    """Read all available monitoring data and return a MonitoringSample."""
    voltage_uv, _ = read_voltage(gpu)
    nvml = _nvml_read(gpu_index)
    return MonitoringSample(
        timestamp=time.time(),
        voltage_uv=voltage_uv,
        **nvml,
    )
