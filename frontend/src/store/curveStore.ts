import { create } from 'zustand';
import type { CurveState, GpuInfo, MonitoringSample, VFPoint } from '../types';
import { api } from '../api/client';
import { toast } from 'sonner';

const HISTORY_SIZE = 120; // ~60s at 2Hz

interface CurveStore {
  // Hardware state (from API)
  curve: CurveState | null;
  gpuInfo: GpuInfo | null;
  monitor: MonitoringSample | null;
  monitorHistory: MonitoringSample[];

  activeProfile: string | null;

  // Phase 4 — edit state
  /** point index → pending delta in kHz (overrides curve.points[i].delta_khz for display) */
  pendingDeltas: Map<number, number>;
  selectedPoints: Set<number>;

  // Hardware state setters
  setCurve: (c: CurveState) => void;
  setGpuInfo: (g: GpuInfo) => void;
  pushMonitor: (s: MonitoringSample) => void;
  setActiveProfile: (name: string | null) => void;

  // Edit actions
  stageEdit: (pointIndex: number, deltaKhz: number) => void;
  stageMultiEdit: (edits: Map<number, number>) => void;
  stageRangeEdit: (points: VFPoint[], deltaKhz: number) => void;
  discardEdits: () => void;
  /**
   * POST pending deltas to the backend, then call onSuccess (which re-fetches
   * the curve) and clear the pending state.
   */
  applyEdits: (onSuccess: () => void) => Promise<void>;
  /** POST /api/curve/reset, call onSuccess, clear pending state. */
  resetAllDeltas: (onSuccess: () => void) => Promise<void>;

  // Selection actions
  selectPoint: (index: number, multi?: boolean) => void;
  selectRange: (indices: number[]) => void;
  clearSelection: () => void;

  // Derived helper — effective MHz for a point including any pending delta
  effectiveMhz: (point: VFPoint) => number;
  // True if any pending delta would produce a negative effective frequency
  hasNegativeFreqWarning: () => boolean;
}

export const useCurveStore = create<CurveStore>()((set, get) => ({
  curve: null,
  gpuInfo: null,
  monitor: null,
  monitorHistory: [],
  activeProfile: null,
  pendingDeltas: new Map(),
  selectedPoints: new Set(),

  setCurve: (curve) => set({ curve }),
  setGpuInfo: (gpuInfo) => set({ gpuInfo }),
  setActiveProfile: (activeProfile) => set({ activeProfile }),
  pushMonitor: (sample) =>
    set((s) => {
      const history = [...s.monitorHistory, sample];
      if (history.length > HISTORY_SIZE) history.shift();
      return { monitor: sample, monitorHistory: history };
    }),

  stageEdit: (pointIndex, deltaKhz) =>
    set((s) => {
      const next = new Map(s.pendingDeltas);
      const point = s.curve?.points.find(p => p.index === pointIndex);
      if (point && point.delta_khz === deltaKhz) {
        next.delete(pointIndex);
      } else {
        next.set(pointIndex, deltaKhz);
      }
      return { pendingDeltas: next };
    }),

  stageMultiEdit: (edits) =>
    set((s) => {
      const next = new Map(s.pendingDeltas);
      edits.forEach((deltaKhz, index) => {
        const point = s.curve?.points.find(p => p.index === index);
        if (point && point.delta_khz === deltaKhz) {
          next.delete(index);
        } else {
          next.set(index, deltaKhz);
        }
      });
      return { pendingDeltas: next };
    }),

  stageRangeEdit: (points, deltaKhz) =>
    set((s) => {
      const next = new Map(s.pendingDeltas);
      for (const p of points) {
        next.set(p.index, deltaKhz);
      }
      return { pendingDeltas: next };
    }),

  discardEdits: () =>
    set({ pendingDeltas: new Map(), selectedPoints: new Set() }),

  applyEdits: async (onSuccess) => {
    const { pendingDeltas } = get();
    if (pendingDeltas.size === 0) return;

    // Convert Map to plain record for the API
    const deltas: Record<number, number> = {};
    pendingDeltas.forEach((v, k) => { deltas[k] = v; });

    try {
      const result = await api.writeDeltas(deltas);
      set({ pendingDeltas: new Map(), selectedPoints: new Set(), activeProfile: null });
      if (result?.freq_warnings?.length) {
        toast.warning('Curve applied — driver clamped some points to 0 MHz (negative freq delta)');
      } else {
        toast.success('Curve applied successfully');
      }
      onSuccess();
    } catch (e: any) {
      toast.error('Failed to apply curve: ' + (e.message || String(e)));
    }
  },

  resetAllDeltas: async (onSuccess) => {
    try {
      await api.resetCurve();
      set({ pendingDeltas: new Map(), selectedPoints: new Set(), activeProfile: null });
      toast.success('Curve reset to hardware defaults');
      onSuccess();
    } catch (e: any) {
      toast.error('Failed to reset curve: ' + (e.message || String(e)));
    }
  },

  selectPoint: (index, multi = false) =>
    set((s) => {
      const next = new Set(s.selectedPoints);
      if (multi) {
        if (next.has(index)) next.delete(index);
        else next.add(index);
      } else {
        if (next.size === 1 && next.has(index)) {
          next.clear(); // clicking the only selected point deselects
        } else {
          next.clear();
          next.add(index);
        }
      }
      return { selectedPoints: next };
    }),

  selectRange: (indices) =>
    set({ selectedPoints: new Set(indices) }),

  clearSelection: () =>
    set({ selectedPoints: new Set() }),

  effectiveMhz: (point) => {
    const { pendingDeltas } = get();
    if (pendingDeltas.has(point.index)) {
      // freq_mhz is the current effective (VFP freq, already includes applied delta).
      // Adjust by the change in delta: (pending - current_delta).
      const pendingKhz = pendingDeltas.get(point.index)!;
      const deltaChange = pendingKhz - point.delta_khz;
      return point.freq_mhz + deltaChange / 1000;
    }
    return point.freq_mhz;
  },

  hasNegativeFreqWarning: () => {
    const { pendingDeltas, curve } = get();
    if (!curve || pendingDeltas.size === 0) return false;
    for (const [index, pendingKhz] of pendingDeltas) {
      const p = curve.points.find((pt) => pt.index === index);
      if (!p || p.freq_khz === 0) continue;
      // freq_khz is current effective; true base = freq_khz - delta_khz
      // new effective = true_base + pendingKhz = freq_khz + (pendingKhz - delta_khz)
      if (p.freq_khz + pendingKhz - p.delta_khz < 0) return true;
    }
    return false;
  },
}));
