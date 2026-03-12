import { useState, useMemo, useEffect } from 'react';
import { RefreshCw, Search } from 'lucide-react';
import { useCurveStore } from '../../store/curveStore';
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
  const { pendingDeltas, stageRangeEdit } = useCurveStore();

  const [offsetMhz, setOffsetMhz] = useState(0);

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

  return (
    <div className="flex flex-wrap items-center gap-2 px-1 pb-2">
      {/* View controls */}
      <button
        onClick={onRefresh}
        className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs transition-colors"
      >
        <RefreshCw size={12} />
        Refresh
      </button>
      {isZoomed && (
        <button
          onClick={onResetZoom}
          className="flex items-center gap-1.5 px-2 py-1 rounded bg-cyan-900/50 hover:bg-cyan-800/60 border border-cyan-700/50 text-cyan-300 text-xs transition-colors"
          title="Reset x-axis zoom (Ctrl+scroll to zoom)"
        >
          <Search size={12} />
          Reset Zoom
        </button>
      )}

      {/* Divider */}
      <span className="w-px h-6 bg-zinc-800 mx-1" />

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

      {/* Legend — right-aligned */}
      <div className="flex items-center gap-3 text-xs text-zinc-500 ml-auto">
        <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5 bg-emerald-400 rounded" /> effective</span>
        <span className="flex items-center gap-1"><span className="inline-block w-3 h-px border-t-2 border-dashed border-cyan-400" /> pending</span>
        <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-yellow-400" /> current</span>
      </div>
    </div>
  );
}
