import { useState, useMemo, useEffect } from 'react';
import { RefreshCw, Check, X, RotateCcw, Search } from 'lucide-react';
import { useCurveStore } from '../../store/curveStore';
import { ConfirmDialog } from '../common/ConfirmDialog';
import type { VFPoint } from '../../types';

interface Props {
  onRefresh: () => void;
  /** All non-idle active points — used by global offset slider */
  activePts: VFPoint[];
  /** Called to reset the x-axis zoom to default */
  onResetZoom: () => void;
  /** True if the chart is currently zoomed in */
  isZoomed: boolean;
}

export function CurveToolbar({ onRefresh, activePts, onResetZoom, isZoomed }: Props) {
  const { pendingDeltas, applyEdits, discardEdits, resetAllDeltas, stageRangeEdit } =
    useCurveStore();

  const [offsetMhz, setOffsetMhz] = useState(0);
  const [dialog, setDialog] = useState<'apply' | 'reset' | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasPending = pendingDeltas.size > 0;

  const uniformDeltaMhz = useMemo(() => {
    if (activePts.length === 0) return 0;
    const firstD = pendingDeltas.get(activePts[0].index) ?? activePts[0].delta_khz;
    const uniform = activePts.every((p) => (pendingDeltas.get(p.index) ?? p.delta_khz) === firstD);
    return uniform ? firstD / 1000 : null;
  }, [activePts, pendingDeltas]);

  useEffect(() => {
    if (uniformDeltaMhz !== null) {
      setOffsetMhz(uniformDeltaMhz);
    }
  }, [uniformDeltaMhz]);

  function handleOffsetChange(mhz: number) {
    setOffsetMhz(mhz);
    stageRangeEdit(activePts, Math.round(mhz * 1000));
  }

  async function handleApplyConfirm() {
    setDialog(null);
    setBusy(true);
    setError(null);
    try {
      await applyEdits(() => {
        onRefresh();
        setOffsetMhz(0);
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleResetConfirm() {
    setDialog(null);
    setBusy(true);
    setError(null);
    try {
      await resetAllDeltas(() => {
        onRefresh();
        setOffsetMhz(0);
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function handleDiscard() {
    discardEdits();
    setOffsetMhz(0);
    setError(null);
  }

  return (
    <>
      {/* ─── Toolbar row ─────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2 px-1 pb-2">
        {/* Left side: view controls */}
        <button
          onClick={onRefresh}
          disabled={busy}
          className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs transition-colors disabled:opacity-50"
        >
          <RefreshCw size={12} />
          Refresh
        </button>
        {isZoomed && (
          <button
            onClick={onResetZoom}
            className="flex items-center gap-1.5 px-2 py-1 rounded bg-cyan-900/50 hover:bg-cyan-800/60 border border-cyan-700/50 text-cyan-300 text-xs transition-colors"
            title="Reset x-axis zoom to full range"
          >
            <Search size={12} />
            Reset Zoom
          </button>
        )}

        {/* Divider */}
        <span className="w-px h-6 bg-zinc-800 mx-3" />

        {/* Global offset slider */}
        {uniformDeltaMhz !== null && (
          <div className="flex items-center gap-1.5 min-w-[260px]">
            <span className="text-zinc-500 text-xs whitespace-nowrap">Global Offset</span>
            <input
              type="range"
              min={-1000}
              max={1000}
              step={5}
              value={offsetMhz}
              onChange={(e) => handleOffsetChange(Number(e.target.value))}
              className="w-32 accent-cyan-400"
              title={`${offsetMhz > 0 ? '+' : ''}${offsetMhz} MHz`}
            />
            <span
              className={[
                'text-xs font-mono w-16',
                offsetMhz > 0 ? 'text-cyan-400' : offsetMhz < 0 ? 'text-orange-400' : 'text-zinc-500',
              ].join(' ')}
            >
              {offsetMhz > 0 ? '+' : ''}{offsetMhz} MHz
            </span>
          </div>
        )}

        {/* Divider */}
        <span className="w-px h-6 bg-zinc-800 mx-3 ml-auto" />

        {/* Pending badge + Apply / Discard / Reset */}
        <div className="flex items-center gap-2">
          {hasPending && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-cyan-500/15 border border-cyan-500/30 text-cyan-400 text-xs">
              {pendingDeltas.size} pending
            </span>
          )}

          <button
            onClick={() => setDialog('apply')}
            disabled={!hasPending || busy}
            className="flex items-center gap-1.5 px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-xs font-semibold transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Check size={12} />
            Apply Changes
          </button>

          <button
            onClick={handleDiscard}
            disabled={!hasPending || busy}
            className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <X size={12} />
            Discard
          </button>

          <button
            onClick={() => setDialog('reset')}
            disabled={busy}
            className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-red-900 text-zinc-400 hover:text-red-300 text-xs transition-colors"
          >
            <RotateCcw size={12} />
            Reset All
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mb-2 px-3 py-1.5 rounded bg-red-900/40 border border-red-700 text-red-300 text-xs flex items-center justify-between">
          <span>⚠ {error}</span>
          <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-200">✕</button>
        </div>
      )}

      {/* Confirm dialogs */}
      {dialog === 'apply' && (
        <ConfirmDialog
          message={`Apply ${pendingDeltas.size} pending change${pendingDeltas.size !== 1 ? 's' : ''} to hardware?`}
          detail="This will write the staged frequency deltas to the GPU via NvAPI. The changes take effect immediately."
          confirmLabel="Apply"
          onConfirm={handleApplyConfirm}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog === 'reset' && (
        <ConfirmDialog
          message="Reset all frequency offsets to zero?"
          detail="This will write zero delta to every V/F point on the GPU. Any pending staged changes will also be discarded."
          confirmLabel="Reset All"
          isDestructive
          onConfirm={handleResetConfirm}
          onCancel={() => setDialog(null)}
        />
      )}
    </>
  );
}
