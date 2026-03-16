import type { CurveState, GpuInfo, MonitoringSample, SnapshotInfo, LimitsState, ProfileData } from '../types';

async function get<T>(path: string, gpuIndex?: number): Promise<T> {
  const url = gpuIndex !== undefined ? `/api${path}?gpu_index=${gpuIndex}` : `/api${path}`;
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`GET ${url}: ${res.status} — ${text}`);
  }
  return res.json() as Promise<T>;
}

async function post<T = void>(path: string, body?: unknown, gpuIndex?: number): Promise<T> {
  const url = gpuIndex !== undefined ? `/api${path}?gpu_index=${gpuIndex}` : `/api${path}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: body != null ? { 'Content-Type': 'application/json' } : {},
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`POST ${url}: ${res.status} — ${text}`);
  }
  // Some endpoints return no body (204)
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return res.json() as Promise<T>;
  return undefined as unknown as T;
}

async function del<T = void>(path: string, gpuIndex?: number): Promise<T> {
  const url = gpuIndex !== undefined ? `/api${path}?gpu_index=${gpuIndex}` : `/api${path}`;
  const res = await fetch(url, { method: 'DELETE' });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`DELETE ${url}: ${res.status} — ${text}`);
  }
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return res.json() as Promise<T>;
  return undefined as unknown as T;
}

export const api = {
  gpus: () => get<GpuInfo[]>('/gpus'),
  gpu: (gpuIndex: number) => get<GpuInfo>('/gpu', gpuIndex),
  curve: (gpuIndex: number) => get<CurveState>('/curve', gpuIndex),
  ranges: (gpuIndex: number) => get<Record<string, { min_khz: number; max_khz: number }>>('/ranges', gpuIndex),
  voltage: (gpuIndex: number) => get<{ voltage_uv: number; voltage_mv: number }>('/voltage', gpuIndex),
  monitor: (gpuIndex: number) => get<MonitoringSample>('/monitor', gpuIndex),
  snapshots: (gpuIndex: number) => get<SnapshotInfo[]>('/snapshots', gpuIndex),

  /** Write per-point frequency deltas. deltas: { pointIndex: deltaKhz } */
  writeDeltas: (deltas: Record<number, number>, gpuIndex: number) =>
    post<{ ok: boolean; freq_warnings?: string[] }>('/curve/write', { deltas }, gpuIndex),

  /** Reset all frequency deltas to zero. */
  resetCurve: (gpuIndex: number) => post('/curve/reset', undefined, gpuIndex),

  /** Get performance limits mapping */
  limits: (gpuIndex: number) => get<LimitsState>('/limits', gpuIndex),

  /** Set performance limits */
  updateLimits: (updates: Partial<LimitsState>, gpuIndex: number) =>
    post('/limits', updates, gpuIndex),

  /** Reset power limit and memory offset to hardware defaults */
  resetLimits: (gpuIndex: number) => post('/limits/reset', undefined, gpuIndex),

  /** Profile Management */
  profiles: (gpuIndex: number) => get<{ profiles: ProfileData[], active: string | null, auto_load: string | null }>('/profiles', gpuIndex),
  saveProfile: (name: string, gpuIndex: number) => post<{ ok: boolean; filepath: string }>('/profiles', { name }, gpuIndex),
  applyProfile: (name: string, gpuIndex: number) => post(`/profiles/${encodeURIComponent(name)}/apply`, undefined, gpuIndex),
  deleteProfile: (name: string) => del(`/profiles/${encodeURIComponent(name)}`),
  renameProfile: (oldName: string, newName: string) =>
    post(`/profiles/${encodeURIComponent(oldName)}/rename`, { new_name: newName }),

  /** Server config */
  setAutoLoadProfile: (name: string | null) =>
    post<{ ok: boolean; auto_load_profile: string | null }>('/config', { auto_load_profile: name }),
};
