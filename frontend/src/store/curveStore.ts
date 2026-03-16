import { create } from 'zustand';
import type { CurveState, GpuInfo, MonitoringSample, VFPoint } from '../types';
import { api } from '../api/client';
import { toast } from 'sonner';

const HISTORY_SIZE = 120; // ~60s at 2Hz

interface CurveStore {
  // Hardware state (from API)
  availableGpus: GpuInfo[];
  selectedGpuIndex: number;
  curve: CurveState | null;
  gpuInfo: GpuInfo | null;
  monitor: MonitoringSample | null;
  monitorHistory: MonitoringSample[];

  activeProfile: string | null;

  // Phase 4 — edit state
  /** point index → pending delta in kHz (overrides curve.points[i].delta_khz for display) */
  pendingDeltas: Map<number, number>;
  selectedPoints: Set<number>;
  /**
   * The anchor point for multi-point operations (e.g. flatten).
   * Set to the last explicitly clicked point. Bulk selects (box, range, Ctrl+A)
   * leave it unchanged; clearSelection resets it to null.
   * If null or not in selectedPoints, operations fall back to the lowest selected index.
   */
  anchorPoint: number | null;

  // Hardware state setters
  setAvailableGpus: (gpus: GpuInfo[]) => void;
  setSelectedGpuIndex: (index: number) => void;
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
  /**
   * Stage all selected points to the anchor point's current effective delta.
   * Falls back to the lowest selected index if anchorPoint is null or deselected.
   * No-op if fewer than 2 points are selected.
   */
  flattenToAnchor: () => void;

  // Derived helper — effective MHz for a point including any pending delta
  effectiveMhz: (point: VFPoint) => number;
  // True if any pending delta would produce a negative effective frequency
  hasNegativeFreqWarning: () => boolean;
}

export const useCurveStore = create<CurveStore>()((set, get) => ({
  availableGpus: [],
  selectedGpuIndex: 0,
  curve: null,
  gpuInfo: null,
  monitor: null,
  monitorHistory: [],
  activeProfile: null,
  pendingDeltas: new Map(),
  selectedPoints: new Set(),
  anchorPoint: null,

  setAvailableGpus: (availableGpus) => set({ availableGpus }),
  setSelectedGpuIndex: (selectedGpuIndex) => {
    set({ selectedGpuIndex, curve: null, gpuInfo: null, monitor: null, monitorHistory: [], pendingDeltas: new Map(), selectedPoints: new Set(), anchorPoint: null });
  },
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
    set({ pendingDeltas: new Map(), selectedPoints: new Set(), anchorPoint: null }),

  applyEdits: async (onSuccess) => {
    const { pendingDeltas, selectedGpuIndex } = get();
    if (pendingDeltas.size === 0) return;

    // Convert Map to plain record for the API
    const deltas: Record<number, number> = {};
    pendingDeltas.forEach((v, k) => { deltas[k] = v; });

    try {
      const result = await api.writeDeltas(deltas, selectedGpuIndex);
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
    const { selectedGpuIndex } = get();
    try {
      await api.resetCurve(selectedGpuIndex);
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
      let anchor = s.anchorPoint;
      if (multi) {
        if (next.has(index)) {
          next.delete(index);
          if (anchor === index) anchor = next.size > 0 ? [...next].at(-1)! : null;
        } else {
          next.add(index);
          anchor = index; // last explicitly added point is the new anchor
        }
      } else {
        if (next.size === 1 && next.has(index)) {
          next.clear();
          anchor = null;
        } else {
          next.clear();
          next.add(index);
          anchor = index;
        }
      }
      return { selectedPoints: next, anchorPoint: anchor };
    }),

  selectRange: (indices) =>
    // Bulk selects don't change the anchor — preserve it if still in the new selection.
    set((s) => {
      const next = new Set(indices);
      const anchor = s.anchorPoint !== null && next.has(s.anchorPoint) ? s.anchorPoint : null;
      return { selectedPoints: next, anchorPoint: anchor };
    }),

  clearSelection: () =>
    set({ selectedPoints: new Set(), anchorPoint: null }),

  flattenToAnchor: () => {
    const { selectedPoints, anchorPoint, pendingDeltas, curve, stageMultiEdit } = get();
    if (selectedPoints.size < 2 || !curve) return;

    const anchor = anchorPoint !== null && selectedPoints.has(anchorPoint)
      ? anchorPoint
      : Math.min(...selectedPoints);

    const anchorDelta =
      pendingDeltas.get(anchor) ??
      curve.points.find(p => p.index === anchor)?.delta_khz ??
      0;

    const edits = new Map<number, number>();
    for (const idx of selectedPoints) {
      edits.set(idx, anchorDelta);
    }
    stageMultiEdit(edits);
  },

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
