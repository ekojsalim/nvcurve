"""Clean Python dataclasses for V/F curve data.

Raw struct manipulation stays in the call layer (bootstrap.py + hal/).
Everything above HAL works with these types.
"""

from dataclasses import dataclass, field


@dataclass
class VFPoint:
    index: int
    freq_khz: int       # Base frequency from VFP curve
    volt_uv: int        # Voltage from VFP curve
    delta_khz: int      # Offset from ClockBoostTable (signed)

    @property
    def effective_freq_khz(self) -> int:
        return self.freq_khz + self.delta_khz

    @property
    def freq_mhz(self) -> float:
        return self.freq_khz / 1000.0

    @property
    def effective_freq_mhz(self) -> float:
        return self.effective_freq_khz / 1000.0

    @property
    def volt_mv(self) -> float:
        return self.volt_uv / 1000.0

    @property
    def delta_mhz(self) -> float:
        return self.delta_khz / 1000.0


@dataclass
class CurveState:
    points: list[VFPoint]
    timestamp: float
    gpu_name: str


@dataclass
class MonitoringSample:
    timestamp: float
    voltage_uv: int | None
    clock_mhz: float | None
    temp_c: float | None
    power_w: float | None
    fan_pct: float | None
    pstate: int | None          # Performance state: 0 (P0, max) – 15 (P15, min)
    mem_used_bytes: int | None  # VRAM used (bytes)
    mem_total_bytes: int | None # VRAM total (bytes)
    gpu_util_pct: float | None  # GPU core utilization (0–100)
    mem_util_pct: float | None  # Memory bus utilization (0–100)
    mem_clock_mhz: float | None = None  # Current memory clock (NVML_CLOCK_MEM)


@dataclass
class GpuInfo:
    name: str
    index: int


@dataclass
class SnapshotInfo:
    filepath: str
    timestamp: str
    gpu: str
    nonzero_offsets: int
    size: int
