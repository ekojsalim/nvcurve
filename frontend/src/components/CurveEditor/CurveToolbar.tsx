import { useState, useMemo, useEffect } from 'react';
import { ZoomIn, RotateCcw } from 'lucide-react';
import { useCurveStore } from '../../store/curveStore';
import type { VFPoint } from '../../types';

interface Props {
  /** All curve points — used by global offset slider */
  activePts: VFPoint[];
  /** Called to reset the x-axis zoom to default */
  onResetZoom: () => void;
  /** True if the chart is currently zoomed in */
  isZoomed: boolean;
  /** True when viewing a read-only domain (memory) */
  readOnly?: boolean;
  /** Current zoom factor (1 = no zoom) */
  zoomFactor: number;
  /** Called when user changes zoom via slider */
  onZoomChange: (factor: number) => void;
}

export function CurveToolbar({ activePts, onResetZoom, isZoomed, readOnly, zoomFactor, onZoomChange }: Props) {
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
      {/* Zoom control */}
      <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-800/60 border border-zinc-700/40" title="Zoom x-axis (Alt+scroll also works)">
        <ZoomIn size={11} className="text-zinc-500 shrink-0" />
        <input
          type="range"
          min={1}
          max={10}
          step={0.1}
          value={zoomFactor}
          onChange={(e) => onZoomChange(Number(e.target.value))}
          className="w-20 h-1 cursor-pointer accent-cyan-400"
        />
        <span className={`text-xs font-mono w-8 tabular-nums ${isZoomed ? 'text-cyan-400' : 'text-zinc-600'}`}>
          {zoomFactor.toFixed(1)}×
        </span>
        {isZoomed && (
          <button
            onClick={onResetZoom}
            title="Reset zoom"
            className="text-zinc-500 hover:text-zinc-300 transition-colors ml-0.5"
          >
            <RotateCcw size={11} />
          </button>
        )}
      </div>

      {/* Divider */}
      <span className="w-px h-6 bg-zinc-800 mx-1" />

      {/* Global offset slider — GPU only */}
      {!readOnly && uniformDeltaMhz !== null && (
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
