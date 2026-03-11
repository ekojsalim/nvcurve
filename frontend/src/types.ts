export interface VFPoint {
  index: number;
  freq_khz: number;
  freq_mhz: number;
  volt_uv: number;
  volt_mv: number;
  delta_khz: number;
  delta_mhz: number;
  effective_freq_khz: number;
  effective_freq_mhz: number;
  is_idle: boolean;
}

export interface CurveState {
  gpu_name: string;
  timestamp: number;
  points: VFPoint[];
}

export interface MonitoringSample {
  timestamp: number;
  voltage_uv: number | null;
  voltage_mv: number | null;
  clock_mhz: number | null;
  temp_c: number | null;
  power_w: number | null;
  fan_pct: number | null;
  pstate: number | null;
  pstate_label: string | null;
  mem_used_bytes: number | null;
  mem_total_bytes: number | null;
  mem_used_mib: number | null;
  mem_total_mib: number | null;
  gpu_util_pct: number | null;
  mem_util_pct: number | null;
  mem_clock_mhz: number | null;
}

export interface GpuInfo {
  name: string;
  index: number;
  driver_version: string | null;
  vram_bytes: number | null;
  vram_gib: number | null;
}

export interface SnapshotInfo {
  filepath: string;
  timestamp: string;
  gpu: string;
  nonzero_offsets: number;
  size: number;
}

export interface LimitsState {
  // Power
  power_limit_w: number | null;
  default_power_limit_w: number | null;
  min_power_limit_w: number | null;
  max_power_limit_w: number | null;
  // Clock offsets — current values
  gpc_offset_mhz: number | null;
  mem_offset_mhz: number | null;
  // Memory offset — hardware-reported range
  min_mem_offset_mhz: number | null;
  max_mem_offset_mhz: number | null;
}

export interface ProfileData {
  name: string;
  gpu_name: string;
  curve_deltas: Record<string, number>;
  mem_offset_mhz: number | null;
  power_limit_w: number | null;
}
