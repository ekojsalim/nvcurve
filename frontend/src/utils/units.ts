export const fmt = {
  mhz: (v: number | null | undefined, decimals = 0) =>
    v == null ? '—' : `${v.toFixed(decimals)} MHz`,
  mv: (v: number | null | undefined, decimals = 0) =>
    v == null ? '—' : `${v.toFixed(decimals)} mV`,
  celsius: (v: number | null | undefined, decimals = 0) =>
    v == null ? '—' : `${v.toFixed(decimals)} °C`,
  watts: (v: number | null | undefined, decimals = 0) =>
    v == null ? '—' : `${v.toFixed(decimals)} W`,
  pct: (v: number | null | undefined, decimals = 0) =>
    v == null ? '—' : `${v.toFixed(decimals)}%`,
  mib: (v: number | null | undefined) =>
    v == null ? '—' : `${v.toFixed(0)} MiB`,
};

export const conv = {
  khzToMhz: (khz: number) => khz / 1000,
  uvToMv: (uv: number) => uv / 1000,
  mvToUv: (mv: number) => mv * 1000,
  mhzToKhz: (mhz: number) => mhz * 1000,
};
