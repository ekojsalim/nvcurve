import { useState, useEffect } from 'react';
import { Check, X, RotateCcw } from 'lucide-react';
import { api } from '../../api/client';
import { useCurveStore } from '../../store/curveStore';
import type { LimitsState } from '../../types';
import { toast } from 'sonner';
import { ConfirmDialog } from '../common/ConfirmDialog';

type Pending = Pick<LimitsState, 'power_limit_w' | 'mem_offset_mhz'>;

export function PerformancePanel() {
  const [limits, setLimits] = useState<LimitsState | null>(null);
  const [pending, setPending] = useState<Partial<Pending>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmApply, setConfirmApply] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  async function fetchLimits() {
    try {
      setLoading(true);
      setLimits(await api.limits());
    } catch {
      toast.error('Failed to load performance limits');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchLimits(); }, []);

  async function handleApply() {
    setBusy(true);
    setError(null);
    try {
      await api.updateLimits(pending);
      useCurveStore.getState().setActiveProfile(null);
      setPending({});
      setConfirmApply(false);
      await fetchLimits();
      toast.success('Performance limits applied');
    } catch (e: any) {
      setError(e.message ?? String(e));
      setConfirmApply(false);
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    setBusy(true);
    setError(null);
    try {
      await api.resetLimits();
      useCurveStore.getState().setActiveProfile(null);
      setPending({});
      setConfirmReset(false);
      await fetchLimits();
      toast.success('Performance limits reset to defaults');
    } catch (e: any) {
      setError(e.message ?? String(e));
      setConfirmReset(false);
    } finally {
      setBusy(false);
    }
  }

  if (loading && !limits) {
    return (
      <div className="bg-zinc-900 rounded-lg overflow-hidden flex flex-col animate-pulse">
        <div className="px-3 py-2 border-b border-zinc-800 h-9" />
        <div className="p-4 flex flex-col gap-6">
          <div className="h-12 bg-zinc-800 rounded" />
          <div className="h-12 bg-zinc-800 rounded" />
        </div>
      </div>
    );
  }

  if (!limits) return null;

  const hasPending = Object.keys(pending).length > 0;

  // Power
  const pwrVal = pending.power_limit_w ?? limits.power_limit_w ?? 0;
  const pwrMin = limits.min_power_limit_w ?? 100;
  const pwrMax = limits.max_power_limit_w ?? 600;

  // Memory offset
  const memVal = pending.mem_offset_mhz ?? limits.mem_offset_mhz ?? 0;
  const memMin = limits.min_mem_offset_mhz ?? -1000;
  const memMax = limits.max_mem_offset_mhz ?? 3000;

  return (
    <>
      <div className="bg-zinc-900 rounded-lg overflow-hidden flex flex-col">

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800 shrink-0">
          <span className="text-xs text-zinc-500 uppercase tracking-wider font-semibold">Performance</span>

          {hasPending && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-cyan-500/15 border border-cyan-500/30 text-cyan-400 text-xs">
              {Object.keys(pending).length} pending
            </span>
          )}

          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setConfirmApply(true)}
              disabled={!hasPending || busy}
              className="flex items-center gap-1.5 px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-xs font-semibold transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Check size={12} />
              Apply
            </button>
            <button
              onClick={() => { setPending({}); setError(null); }}
              disabled={!hasPending || busy}
              className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <X size={12} />
              Discard
            </button>
            <button
              onClick={() => setConfirmReset(true)}
              disabled={busy}
              className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-red-900 text-zinc-500 hover:text-red-300 text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <RotateCcw size={11} />
              Reset
            </button>
          </div>
        </div>

        {/* ── Error banner ────────────────────────────────────────────────── */}
        {error && (
          <div className="px-3 py-1.5 bg-red-900/40 border-b border-red-700 text-red-300 text-xs flex items-center justify-between">
            <span>⚠ {error}</span>
            <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-200">✕</button>
          </div>
        )}

        <div className="flex flex-col divide-y divide-zinc-800">

          {/* ── Board Power Limit ─────────────────────────────────────────── */}
          <div className="px-4 py-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-zinc-500 uppercase tracking-wider">Board Power Limit</span>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min={pwrMin}
                  max={pwrMax}
                  value={pwrVal}
                  onChange={e => {
                    const v = parseInt(e.target.value);
                    if (!isNaN(v)) setPending(p => ({ ...p, power_limit_w: v }));
                  }}
                  className="w-14 bg-zinc-950 border border-zinc-800 rounded text-xs px-2 py-1 text-right font-mono focus:outline-none focus:border-cyan-500"
                />
                <span className="text-xs text-zinc-600 w-3">W</span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-600 font-mono w-8 text-right">{pwrMin}</span>
              <input
                type="range"
                min={pwrMin}
                max={pwrMax}
                value={pwrVal}
                onChange={e => setPending(p => ({ ...p, power_limit_w: parseInt(e.target.value) }))}
                className="flex-1 accent-cyan-400 h-1 cursor-pointer"
              />
              <span className="text-xs text-zinc-600 font-mono w-8">{pwrMax}</span>
            </div>
          </div>

          {/* ── Memory Clock Offset ───────────────────────────────────────── */}
          <div className="px-4 py-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-zinc-500 uppercase tracking-wider">Memory Clock Offset</span>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min={memMin}
                  max={memMax}
                  value={memVal}
                  onChange={e => {
                    const v = parseInt(e.target.value);
                    if (!isNaN(v)) setPending(p => ({ ...p, mem_offset_mhz: v }));
                  }}
                  className="w-16 bg-zinc-950 border border-zinc-800 rounded text-xs px-2 py-1 text-right font-mono focus:outline-none focus:border-cyan-500"
                />
                <span className="text-xs text-zinc-600 w-6">MHz</span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-600 font-mono w-10 text-right">{memMin}</span>
              <input
                type="range"
                min={memMin}
                max={memMax}
                step={1}
                value={memVal}
                onChange={e => setPending(p => ({ ...p, mem_offset_mhz: parseInt(e.target.value) }))}
                className="flex-1 accent-cyan-400 h-1 cursor-pointer"
              />
              <span className="text-xs text-zinc-600 font-mono w-10">+{memMax}</span>
            </div>
          </div>

        </div>
      </div>

      {confirmApply && (
        <ConfirmDialog
          message="Apply performance limits to hardware?"
          detail="Power limit and memory clock offset take effect immediately."
          confirmLabel="Apply"
          onConfirm={handleApply}
          onCancel={() => setConfirmApply(false)}
        />
      )}
      {confirmReset && (
        <ConfirmDialog
          message="Reset performance limits to defaults?"
          detail="Power limit will be restored to the hardware default and memory clock offset set to 0."
          confirmLabel="Reset"
          isDestructive
          onConfirm={handleReset}
          onCancel={() => setConfirmReset(false)}
        />
      )}
    </>
  );
}
