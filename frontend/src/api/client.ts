import type { CurveState, GpuInfo, MonitoringSample, SnapshotInfo, LimitsState, ProfileData } from '../types';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`GET /api${path}: ${res.status} — ${text}`);
  }
  return res.json() as Promise<T>;
}

async function post<T = void>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: body != null ? { 'Content-Type': 'application/json' } : {},
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`POST /api${path}: ${res.status} — ${text}`);
  }
  // Some endpoints return no body (204)
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return res.json() as Promise<T>;
  return undefined as unknown as T;
}

async function del<T = void>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: 'DELETE' });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`DELETE /api${path}: ${res.status} — ${text}`);
  }
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return res.json() as Promise<T>;
  return undefined as unknown as T;
}

export const api = {
  gpu: () => get<GpuInfo>('/gpu'),
  curve: () => get<CurveState>('/curve'),
  ranges: () => get<Record<string, { min_khz: number; max_khz: number }>>('/ranges'),
  voltage: () => get<{ voltage_uv: number; voltage_mv: number }>('/voltage'),
  monitor: () => get<MonitoringSample>('/monitor'),
  snapshots: () => get<SnapshotInfo[]>('/snapshots'),

  /** Write per-point frequency deltas. deltas: { pointIndex: deltaKhz } */
  writeDeltas: (deltas: Record<number, number>) =>
    post<{ ok: boolean; freq_warnings?: string[] }>('/curve/write', { deltas }),

  /** Reset all frequency deltas to zero. */
  resetCurve: () => post('/curve/reset'),

  /** Get performance limits mapping */
  limits: () => get<LimitsState>('/limits'),

  /** Set performance limits */
  updateLimits: (updates: Partial<LimitsState>) =>
    post('/limits', updates),

  /** Reset power limit and memory offset to hardware defaults */
  resetLimits: () => post('/limits/reset'),

  /** Profile Management */
  profiles: () => get<{ profiles: ProfileData[], active: string | null }>('/profiles'),
  saveProfile: (name: string) => post<{ ok: boolean; filepath: string }>('/profiles', { name }),
  applyProfile: (name: string) => post(`/profiles/${encodeURIComponent(name)}/apply`),
  deleteProfile: (name: string) => del(`/profiles/${encodeURIComponent(name)}`),
  renameProfile: (oldName: string, newName: string) =>
    post(`/profiles/${encodeURIComponent(oldName)}/rename`, { new_name: newName }),
};
