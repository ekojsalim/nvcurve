import { useState, useMemo, useEffect } from 'react';
import { PointRow } from './PointRow';
import type { VFPoint } from '../../types';
import { findCurrentPoint, detectClampedPoints } from '../../utils/curveHelpers';
import { useCurveStore } from '../../store/curveStore';

interface Props {
  points: VFPoint[];
  currentVoltageMv: number | null;
  readOnly?: boolean;
}

export function PointTable({ points, currentVoltageMv, readOnly }: Props) {
  const { pendingDeltas, selectedPoints, selectPoint, selectRange } = useCurveStore();
  const currentPoint = findCurrentPoint(points, currentVoltageMv);
  const clampedPoints = useMemo(() => detectClampedPoints(points), [points]);

  const [dragStartIdx, setDragStartIdx] = useState<number | null>(null);

  useEffect(() => {
    function onUp() { setDragStartIdx(null); }
    window.addEventListener('mouseup', onUp);
    return () => window.removeEventListener('mouseup', onUp);
  }, []);

  return (
    <div className="bg-zinc-900 rounded-lg overflow-hidden flex flex-col">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800 shrink-0">
        <span className="text-xs text-zinc-500 uppercase tracking-wider mr-2">Points</span>
        {!readOnly && pendingDeltas.size > 0 && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-cyan-500/15 border border-cyan-500/30 text-cyan-400 text-xs">
            {pendingDeltas.size} staged
          </span>
        )}
        {readOnly && (
          <span className="text-xs text-zinc-600 italic">read-only</span>
        )}
        <span className="ml-auto text-xs text-zinc-600">{points.length} points</span>
        {!readOnly && selectedPoints.size === 1 && (
          <div className="flex gap-1 ml-4 border-l border-zinc-800 pl-4">
            <button
              onClick={() => {
                const idx = Array.from(selectedPoints)[0];
                const beforeIdxs = points.filter(p => p.index <= idx).map(p => p.index);
                selectRange(beforeIdxs);
              }}
              className="px-2 py-0.5 rounded text-xs bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition-colors whitespace-nowrap"
            >
              Select Before
            </button>
            <button
              onClick={() => {
                const idx = Array.from(selectedPoints)[0];
                const afterIdxs = points.filter(p => p.index >= idx).map(p => p.index);
                selectRange(afterIdxs);
              }}
              className="px-2 py-0.5 rounded text-xs bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition-colors whitespace-nowrap"
            >
              Select After
            </button>
          </div>
        )}
      </div>
      <div className="overflow-auto max-h-[32rem] select-none">
        <table className="w-full">
          <thead className="sticky top-0 bg-zinc-900 z-10">
            <tr className="text-xs text-zinc-500 uppercase tracking-wider border-b border-zinc-800">
              <th className="px-3 py-2 text-left font-normal bg-zinc-900">#</th>
              <th className="px-3 py-2 text-left font-normal bg-zinc-900">Voltage</th>
              <th className="px-3 py-2 text-left font-normal bg-zinc-900">Offset</th>
              <th className="px-3 py-2 text-left font-normal bg-zinc-900">Eff. Freq</th>
              <th className="px-3 py-2 text-left font-normal bg-zinc-900" />
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <PointRow
                key={p.index}
                point={p}
                isCurrent={currentPoint?.index === p.index}
                isSelected={selectedPoints.has(p.index)}
                isClamped={clampedPoints.has(p.index)}
                pendingDeltaKhz={pendingDeltas.get(p.index)}
                shouldAutoScroll={selectedPoints.size === 1 && selectedPoints.has(p.index)}
                onMouseDown={readOnly ? undefined : (e) => {
                  if (e.shiftKey) {
                    const currentSelected = Array.from(selectedPoints);
                    if (currentSelected.length > 0) {
                      const last = Math.max(...currentSelected);
                      const min = Math.min(last, p.index);
                      const max = Math.max(last, p.index);
                      const toSelect = points.filter(a => a.index >= min && a.index <= max).map(a => a.index);
                      selectRange(toSelect);
                    } else {
                      selectPoint(p.index, false);
                    }
                  } else if (e.ctrlKey || e.metaKey) {
                    selectPoint(p.index, true);
                  } else {
                    setDragStartIdx(p.index);
                    selectPoint(p.index, false);
                  }
                }}
                onMouseEnter={readOnly ? undefined : () => {
                  if (dragStartIdx !== null) {
                    const min = Math.min(dragStartIdx, p.index);
                    const max = Math.max(dragStartIdx, p.index);
                    const toSelect = points.filter(a => a.index >= min && a.index <= max).map(a => a.index);
                    selectRange(toSelect);
                  }
                }}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
