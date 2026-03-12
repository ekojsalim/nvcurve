import { useState, useCallback, useRef, useEffect, useMemo } from 'react';
import { Check, X, RotateCcw } from 'lucide-react';
import { scaleLinear } from 'd3';
import type { VFPoint, CurveState } from '../../types';
import { CurveTooltip } from './CurveTooltip';
import { CurveToolbar } from './CurveToolbar';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { voltExtent, refBaseMhz, detectClampedPoints } from '../../utils/curveHelpers';
import { useCurveStore } from '../../store/curveStore';

interface Props {
  curve: CurveState;
  currentVoltageMv: number | null;
  currentClockMhz: number | null;
  onRefresh: () => void;
}

const MARGIN = { top: 16, right: 24, bottom: 48, left: 64 };
const SVG_W = 680;
const SVG_H = 460;
const INNER_W = SVG_W - MARGIN.left - MARGIN.right;
const INNER_H = SVG_H - MARGIN.top - MARGIN.bottom;

/** Drag-tooltip info (shown while actively dragging a point) */
interface DragInfo {
  pointIndex: number;
  /** Current pending delta kHz while dragging */
  currentDeltaKhz: number;
  /** SVG inner coords of the point, for tooltip positioning */
  cx: number;
  cy: number;
}

/** Box-select rubber-band rect (SVG inner coords) */
interface BoxRect { x0: number; y0: number; x1: number; y1: number }

export function CurveEditor({ curve, currentVoltageMv, currentClockMhz, onRefresh }: Props) {
  const [hoveredPoint, setHoveredPoint] = useState<VFPoint | null>(null);
  const [dragInfo, setDragInfo] = useState<DragInfo | null>(null);
  const [boxRect, setBoxRect] = useState<BoxRect | null>(null);
  const [inlineInput, setInlineInput] = useState<{ pointIndex: number; value: string } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Store
  const {
    pendingDeltas,
    selectedPoints,
    stageEdit,
    stageMultiEdit,
    selectPoint,
    selectRange,
    clearSelection,
    effectiveMhz,
    applyEdits,
    discardEdits,
    resetAllDeltas,
    hasNegativeFreqWarning,
  } = useCurveStore();

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<'apply' | 'reset' | null>(null);

  async function handleApplyConfirm() {
    setDialog(null);
    setBusy(true);
    setActionError(null);
    try {
      await applyEdits(onRefresh);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleResetConfirm() {
    setDialog(null);
    setBusy(true);
    setActionError(null);
    try {
      await resetAllDeltas(onRefresh);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const pts = curve.points;
  const clampedPoints = useMemo(() => detectClampedPoints(curve.points), [curve.points]);

  // ─── X-axis zoom / viewport ──────────────────────────────────────────────
  // Use a slightly larger padding to space things out better
  const [xViewport, setXViewport] = useState<[number, number]>(() => voltExtent(pts, 40));

  // Reset viewport when the set of points changes (curve refresh)
  const ptsKey = pts.map((p) => p.index).join(',');
  useEffect(() => {
    setXViewport(voltExtent(pts, 40));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ptsKey]);

  // Compute Y domain accounting for pending changes
  const allEffective = pts.map((p) => effectiveMhz(p));
  const allBase = pts.map((p) => refBaseMhz(p));
  const freqMin = Math.min(...allBase, ...allEffective) - 50;
  const freqMax = Math.max(...allBase, ...allEffective) + 50;

  const xScale = scaleLinear().domain(xViewport).range([0, INNER_W]);
  const yScale = scaleLinear().domain([freqMin, freqMax]).range([INNER_H, 0]);

  // Keep scales in a ref so event handler closures always see the latest
  const scalesRef = useRef({ xScale, yScale, xViewport });
  scalesRef.current = { xScale, yScale, xViewport };

  const xTicks = xScale.ticks(8);
  const yTicks = yScale.ticks(6);

  // Polylines
  const visiblePts = pts.filter((p) => {
    const v = p.volt_mv;
    return v >= xViewport[0] && v <= xViewport[1];
  });

  const baseLine = visiblePts
    .map((p) => `${xScale(p.volt_mv).toFixed(1)},${yScale(refBaseMhz(p)).toFixed(1)}`)
    .join(' ');

  const effectiveLine = visiblePts
    .map((p) => `${xScale(p.volt_mv).toFixed(1)},${yScale(p.freq_mhz).toFixed(1)}`)
    .join(' ');

  const hasPending = pendingDeltas.size > 0;
  const pendingLine = hasPending
    ? visiblePts
      .map((p) => `${xScale(p.volt_mv).toFixed(1)},${yScale(effectiveMhz(p)).toFixed(1)}`)
      .join(' ')
    : null;

  // ─── Coordinate helpers ──────────────────────────────────────────────────
  function svgToContainer(svgX: number, svgY: number): { x: number; y: number } | null {
    const svg = svgRef.current;
    const container = containerRef.current;
    if (!svg || !container) return null;
    const svgRect = svg.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const scaleX = svgRect.width / SVG_W;
    const scaleY = svgRect.height / SVG_H;
    return {
      x: svgRect.left - containerRect.left + (svgX + MARGIN.left) * scaleX,
      y: svgRect.top - containerRect.top + (svgY + MARGIN.top) * scaleY,
    };
  }

  function clientToInner(clientX: number, clientY: number): { x: number; y: number } | null {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    return {
      x: (clientX - rect.left) * (SVG_W / rect.width) - MARGIN.left,
      y: (clientY - rect.top) * (SVG_H / rect.height) - MARGIN.top,
    };
  }

  // ─── Point hover ─────────────────────────────────────────────────────────
  const handleMouseEnter = useCallback((p: VFPoint) => {
    setHoveredPoint(p);
  }, []);

  const handleMouseLeave = useCallback(() => {
    setHoveredPoint(null);
  }, []);

  // ─── Drag state (refs — survive renders without triggering effects) ───────
  const dragState = useRef<{
    active: boolean;
    pointIndex: number;
    startY: number;
    startDeltaKhz: number;
    pointInitialDeltas: Map<number, number>;
  } | null>(null);
  /** True if the mouse moved meaningfully during the current mousedown */
  const dragMoved = useRef(false);

  // ─── Box select refs ─────────────────────────────────────────────────────
  const boxStartRef = useRef<{ x: number; y: number } | null>(null);
  const isBoxSelecting = useRef(false);

  // ─── Pan refs ─────────────────────────────────────────────────────────────
  const panState = useRef<{ startX: number; startY: number; startViewport: [number, number] } | null>(null);
  const isPanning = useRef(false);

  // ─── Point mousedown → start drag ────────────────────────────────────────
  function handlePointMouseDown(e: React.MouseEvent, p: VFPoint) {
    containerRef.current?.focus();
    e.stopPropagation();
    e.preventDefault();

    const inner = clientToInner(e.clientX, e.clientY);
    if (!inner) return;

    const currentDelta = pendingDeltas.has(p.index)
      ? pendingDeltas.get(p.index)!
      : p.delta_khz;

    const initialDeltas = new Map<number, number>();

    if (selectedPoints.has(p.index) && selectedPoints.size > 1) {
      // Feature: Dragging a point in a multi-selection moves the whole selection
      for (const index of selectedPoints) {
        const pt = pts.find(x => x.index === index);
        if (pt) {
          initialDeltas.set(index, pendingDeltas.has(index) ? pendingDeltas.get(index)! : pt.delta_khz);
        }
      }
    } else {
      // Default: Just drag this point
      initialDeltas.set(p.index, currentDelta);
    }

    dragState.current = {
      active: true,
      pointIndex: p.index,
      startY: inner.y,
      startDeltaKhz: currentDelta,
      pointInitialDeltas: initialDeltas
    };
    dragMoved.current = false;
  }

  // ─── SVG background mousedown → pan OR shift+drag = box select ───────────
  function handleSvgMouseDown(e: React.MouseEvent) {
    containerRef.current?.focus();
    if (e.button !== 0) return;
    const inner = clientToInner(e.clientX, e.clientY);
    if (!inner) return;

    if (e.shiftKey) {
      // Shift+drag = box select
      isBoxSelecting.current = true;
      boxStartRef.current = inner;
      setBoxRect({ x0: inner.x, y0: inner.y, x1: inner.x, y1: inner.y });
    } else {
      // Plain drag = pan
      isPanning.current = true;
      panState.current = { startX: inner.x, startY: inner.y, startViewport: [...scalesRef.current.xViewport] as [number, number] };
    }
  }

  // ─── Scroll wheel → zoom x-axis around cursor (Ctrl/Meta + scroll only) ──
  function handleWheel(e: React.WheelEvent) {
    if (!e.ctrlKey && !e.metaKey) return; // plain scroll → let page scroll normally
    e.preventDefault();
    const inner = clientToInner(e.clientX, e.clientY);
    if (!inner) return;

    const { xScale: sx, xViewport: vp } = scalesRef.current;
    const zoomFactor = e.deltaY < 0 ? 1.15 : 1 / 1.15; // scroll up = zoom in
    const mouseVolt = sx.invert(inner.x);
    const [vpMin, vpMax] = vp;
    const currentWidth = vpMax - vpMin;
    const newWidth = Math.max(20, Math.min(voltExtent(pts)[1] - voltExtent(pts)[0] + 40, currentWidth / zoomFactor));
    const anchorFrac = (mouseVolt - vpMin) / currentWidth;
    let newMin = mouseVolt - anchorFrac * newWidth;
    let newMax = mouseVolt + (1 - anchorFrac) * newWidth;
    // Clamp to full extent
    const [fullMin, fullMax] = voltExtent(pts);
    const pad = 20;
    if (newMin < fullMin - pad) { newMax += (fullMin - pad - newMin); newMin = fullMin - pad; }
    if (newMax > fullMax + pad) { newMin -= (newMax - fullMax - pad); newMax = fullMax + pad; }
    setXViewport([newMin, newMax]);
  }

  // ─── Global mousemove + mouseup ──────────────────────────────────────────
  useEffect(() => {
    function onMove(e: MouseEvent) {
      // --- Point drag ---
      if (dragState.current?.active) {
        const inner = clientToInner(e.clientX, e.clientY);
        if (!inner) return;
        const ds = dragState.current;
        const dyPx = inner.y - ds.startY;

        // Consider moved if more than 3px
        if (Math.abs(dyPx) > 3) dragMoved.current = true;

        const { yScale: ys } = scalesRef.current;
        const [yRangeBottom, yRangeTop] = ys.range() as [number, number];
        const [yDomainBottom, yDomainTop] = ys.domain() as [number, number];
        const pxPerMhz = (yRangeBottom - yRangeTop) / (yDomainTop - yDomainBottom);
        const deltaMhz = -dyPx / pxPerMhz;

        // Calculate the absolute delta for the primary dragged point
        const primaryNewDeltaKhz = Math.round(ds.startDeltaKhz + deltaMhz * 1000);
        const primaryClamped = Math.max(-1000_000, Math.min(1000_000, primaryNewDeltaKhz));

        // Difference to apply to all other points in the selection
        const validDeltaDiff = primaryClamped - ds.startDeltaKhz;

        if (ds.pointInitialDeltas.size > 1) {
          const edits = new Map<number, number>();
          ds.pointInitialDeltas.forEach((initialDelta, index) => {
            const newDelta = initialDelta + validDeltaDiff;
            const clampedNewDelta = Math.max(-1000_000, Math.min(1000_000, newDelta));
            edits.set(index, clampedNewDelta);
          });
          stageMultiEdit(edits);
        } else {
          stageEdit(ds.pointIndex, primaryClamped);
        }

        // Update drag tooltip info
        const point = pts.find((p) => p.index === ds.pointIndex);
        if (point) {
          const cx = scalesRef.current.xScale(point.volt_mv);
          const deltaChange = primaryClamped - point.delta_khz;
          const cy = scalesRef.current.yScale(point.freq_mhz + deltaChange / 1000);
          setDragInfo({ pointIndex: ds.pointIndex, currentDeltaKhz: primaryClamped, cx, cy });
        }
        return;
      }

      // --- Pan ---
      if (isPanning.current && panState.current) {
        const inner = clientToInner(e.clientX, e.clientY);
        if (!inner) return;
        const ps = panState.current;
        // How many volts does 1 SVG-inner-px correspond to?
        const voltPerPx = (ps.startViewport[1] - ps.startViewport[0]) / INNER_W;
        const dxVolt = (inner.x - ps.startX) * voltPerPx;
        const [fullMin, fullMax] = voltExtent(pts);
        const pad = 20;
        const vpWidth = ps.startViewport[1] - ps.startViewport[0];
        let newMin = ps.startViewport[0] - dxVolt;
        let newMax = ps.startViewport[1] - dxVolt;
        // Clamp so we don't pan completely outside
        if (newMin < fullMin - pad) { newMin = fullMin - pad; newMax = newMin + vpWidth; }
        if (newMax > fullMax + pad) { newMax = fullMax + pad; newMin = newMax - vpWidth; }
        setXViewport([newMin, newMax]);
        return;
      }

      // --- Box select ---
      if (isBoxSelecting.current && boxStartRef.current) {
        const inner = clientToInner(e.clientX, e.clientY);
        if (!inner) return;
        setBoxRect({
          x0: boxStartRef.current.x,
          y0: boxStartRef.current.y,
          x1: inner.x,
          y1: inner.y,
        });
      }
    }

    function onUp(e: MouseEvent) {
      // --- End drag ---
      if (dragState.current?.active) {
        dragState.current = null;
        setDragInfo(null);
        return;
      }

      // --- End pan ---
      if (isPanning.current) {
        isPanning.current = false;
        if (panState.current) {
          const inner = clientToInner(e.clientX, e.clientY);
          if (inner) {
            const dx = Math.abs(inner.x - panState.current.startX);
            const dy = Math.abs(inner.y - panState.current.startY);
            if (dx < 3 && dy < 3) {
              // It was just a click on the background, not a pan, so clear selection
              clearSelection();
            }
          }
        }
        panState.current = null;
        return;
      }

      // --- End box select ---
      if (isBoxSelecting.current && boxStartRef.current) {
        isBoxSelecting.current = false;
        const inner = clientToInner(e.clientX, e.clientY);
        if (inner) {
          const finalRect = {
            x0: boxStartRef.current.x,
            y0: boxStartRef.current.y,
            x1: inner.x,
            y1: inner.y,
          };
          const minX = Math.min(finalRect.x0, finalRect.x1);
          const maxX = Math.max(finalRect.x0, finalRect.x1);
          const minY = Math.min(finalRect.y0, finalRect.y1);
          const maxY = Math.max(finalRect.y0, finalRect.y1);
          const sizeTrivial = Math.abs(finalRect.x1 - finalRect.x0) < 4
            && Math.abs(finalRect.y1 - finalRect.y0) < 4;

          if (!sizeTrivial) {
            const { xScale: sx, yScale: ys } = scalesRef.current;
            const selected = pts
              .filter((p) => {
                const cx = sx(p.volt_mv);
                const cy = ys(effectiveMhz(p));
                return cx >= minX && cx <= maxX && cy >= minY && cy <= maxY;
              })
              .map((p) => p.index);
            selectRange(selected);
          } else {
            clearSelection();
          }
        }
        boxStartRef.current = null;
        setBoxRect(null);
      }
    }

    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    // Only re-register when structural deps change; scales are accessed via scalesRef
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pts, pendingDeltas, stageEdit, selectRange, clearSelection, effectiveMhz]);

  return (
    <div className="bg-zinc-900 rounded-lg overflow-hidden flex flex-col h-full">
      {/* Header: title + action buttons */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800 shrink-0">
        <span className="text-xs text-zinc-500 uppercase tracking-wider font-semibold">V/F Curve</span>
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
            Apply
          </button>
          <button
            onClick={() => discardEdits()}
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
            Reset
          </button>
        </div>
      </div>

      {/* Banners */}
      {hasNegativeFreqWarning() && (
        <div className="px-3 py-1.5 bg-amber-900/40 border-b border-amber-700 text-amber-300 text-xs">
          ⚠ One or more pending deltas would produce a negative effective frequency. The driver will clamp to 0 MHz.
        </div>
      )}
      {actionError && (
        <div className="px-3 py-1.5 bg-red-900/40 border-b border-red-700 text-red-300 text-xs flex items-center justify-between">
          <span>⚠ {actionError}</span>
          <button onClick={() => setActionError(null)} className="ml-2 text-red-400 hover:text-red-200">✕</button>
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
          confirmLabel="Reset"
          isDestructive
          onConfirm={handleResetConfirm}
          onCancel={() => setDialog(null)}
        />
      )}

      <div className="px-3 pt-3">
        <CurveToolbar
          onRefresh={onRefresh}
          activePts={pts}
          onResetZoom={() => setXViewport(voltExtent(pts))}
          isZoomed={Math.abs((xViewport[1] - xViewport[0]) - (voltExtent(pts)[1] - voltExtent(pts)[0])) > 5}
        />
      </div>

      {/* Need tabIndex=0 to capture keyboard events */}
      <div className="flex-1 min-h-0 relative focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50 px-3 pb-3"
        ref={containerRef}
        style={{ maxHeight: '600px' }}
        tabIndex={0}
        onKeyDown={(e) => {
          if (inlineInput) {
            if (e.key === 'Escape') setInlineInput(null);
            return; // Let the input handle it
          }

          if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            selectRange(pts.map(p => p.index));
            return;
          }

          if (e.key === 'Escape') {
            e.preventDefault();
            clearSelection();
            return;
          }

          if (e.key === 'Tab') {
            e.preventDefault();
            if (pts.length === 0) return;
            const currentSelected = Array.from(selectedPoints);
            if (currentSelected.length === 0) {
              selectPoint(pts[0].index);
            } else {
              const lastSelected = e.shiftKey ? Math.min(...currentSelected) : Math.max(...currentSelected);
              const idx = pts.findIndex(p => p.index === lastSelected);
              if (idx >= 0) {
                let nextIdx = e.shiftKey ? idx - 1 : idx + 1;
                if (nextIdx < 0) nextIdx = pts.length - 1;
                if (nextIdx >= pts.length) nextIdx = 0;
                selectPoint(pts[nextIdx].index);
              }
            }
            return;
          }

          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            if (selectedPoints.size === 0) return;
            const changeMhz = e.key === 'ArrowUp' ? 1 : -1;
            const multiplier = (e.ctrlKey || e.metaKey) ? 10 : 1;
            const changeKhz = changeMhz * multiplier * 1000;

            const edits = new Map<number, number>();
            for (const idx of selectedPoints) {
              const p = pts.find(pt => pt.index === idx);
              if (p) {
                const current = pendingDeltas.has(idx) ? pendingDeltas.get(idx)! : p.delta_khz;
                const clamped = Math.max(-500_000, Math.min(500_000, current + changeKhz));
                edits.set(idx, clamped);
              }
            }
            stageMultiEdit(edits);
            return;
          }

          if (e.key === 'Enter') {
            e.preventDefault();
            if (selectedPoints.size === 1) {
              const idx = Array.from(selectedPoints)[0];
              const p = pts.find(pt => pt.index === idx);
              if (p) {
                const initVal = (pendingDeltas.has(idx) ? pendingDeltas.get(idx)! : p.delta_khz) / 1000;
                setInlineInput({ pointIndex: idx, value: initVal.toFixed(1) });
                // We'll focus the input in an effect
              }
            }
            return;
          }
        }}
      >
        <svg
          ref={svgRef}
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          width="100%"
          height="100%"
          className="block"
          style={{ fontFamily: 'monospace', maxHeight: '600px', cursor: isPanning.current ? 'grabbing' : 'grab', userSelect: 'none' }}
          onMouseDown={handleSvgMouseDown}
          onWheel={handleWheel}
        >
          <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
            <defs>
              <clipPath id="chart-clip">
                <rect x={0} y={0} width={INNER_W + 1} height={INNER_H + 1} />
              </clipPath>
            </defs>

            {/* Grid */}
            {xTicks.map((t) => (
              <line key={t} x1={xScale(t)} x2={xScale(t)} y1={0} y2={INNER_H} stroke="#27272a" strokeWidth={1} />
            ))}
            {yTicks.map((t) => (
              <line key={t} x1={0} x2={INNER_W} y1={yScale(t)} y2={yScale(t)} stroke="#27272a" strokeWidth={1} />
            ))}

            {/* X axis */}
            {xTicks.map((t) => (
              <g key={t} transform={`translate(${xScale(t)},${INNER_H})`}>
                <line y2={5} stroke="#52525b" />
                <text y={18} textAnchor="middle" fontSize={10} fill="#71717a">{t.toFixed(0)}</text>
              </g>
            ))}

            {/* Y axis */}
            {yTicks.map((t) => (
              <g key={t} transform={`translate(0,${yScale(t)})`}>
                <line x2={-5} stroke="#52525b" />
                <text x={-10} textAnchor="end" dominantBaseline="middle" fontSize={10} fill="#71717a">{t.toFixed(0)}</text>
              </g>
            ))}

            {/* Axis labels */}
            <text x={INNER_W / 2} y={INNER_H + 40} textAnchor="middle" fontSize={11} fill="#52525b">Voltage (mV)</text>
            <text x={-INNER_H / 2} y={-50} textAnchor="middle" fontSize={11} fill="#52525b" transform="rotate(-90)">Frequency (MHz)</text>

            {/* Border */}
            <rect x={0} y={0} width={INNER_W} height={INNER_H} fill="none" stroke="#3f3f46" strokeWidth={1} />

            <g clipPath="url(#chart-clip)">
              {/* Base curve */}
              <polyline points={baseLine} fill="none" stroke="#7c3aed" strokeWidth={1.5} strokeOpacity={0.4} strokeLinejoin="round" />

              {/* Confirmed effective curve */}
              <polyline points={effectiveLine} fill="none" stroke="#34d399" strokeWidth={2} strokeLinejoin="round" />

              {/* Pending effective curve */}
              {pendingLine && (
                <polyline
                  points={pendingLine}
                  fill="none"
                  stroke="#22d3ee"
                  strokeWidth={2}
                  strokeDasharray="6 3"
                  strokeLinejoin="round"
                  strokeOpacity={0.85}
                />
              )}

              {/* Current voltage crosshair */}
              {currentVoltageMv != null && xScale(currentVoltageMv) >= 0 && xScale(currentVoltageMv) <= INNER_W && (
                <line
                  x1={xScale(currentVoltageMv)} x2={xScale(currentVoltageMv)}
                  y1={0} y2={INNER_H}
                  stroke="#facc15" strokeWidth={0.5} strokeDasharray="4 4" strokeOpacity={0.3}
                />
              )}

              {/* Current clock crosshair */}
              {currentClockMhz != null && yScale(currentClockMhz) >= 0 && yScale(currentClockMhz) <= INNER_H && (
                <line
                  x1={0} x2={INNER_W}
                  y1={yScale(currentClockMhz)} y2={yScale(currentClockMhz)}
                  stroke="#facc15" strokeWidth={0.5} strokeDasharray="4 4" strokeOpacity={0.3}
                />
              )}

              {/* Current operating point marker */}
              {currentVoltageMv != null && currentClockMhz != null &&
                xScale(currentVoltageMv) >= 0 && xScale(currentVoltageMv) <= INNER_W &&
                yScale(currentClockMhz) >= 0 && yScale(currentClockMhz) <= INNER_H && (
                  <circle
                    cx={xScale(currentVoltageMv)}
                    cy={yScale(currentClockMhz)}
                    r={3.5}
                    fill="#fde047"
                    stroke="#18181b"
                    strokeWidth={1}
                  />
                )}

              {pts.map((p) => {
                if (p.volt_mv < xViewport[0] - 5 || p.volt_mv > xViewport[1] + 5) return null;

                const cx = xScale(p.volt_mv);
                /** Confirmed position (hardware state — VFP effective frequency) */
                const confirmedCy = yScale(p.freq_mhz);
                /** Staged/pending position (what will be applied) */
                const pendingCy = yScale(effectiveMhz(p));
                const hasPendingEdit = pendingDeltas.has(p.index);
                /**
                 * The "main" interactive circle sits at the pending position when
                 * there's a staged edit — that's the point the user is working with.
                 * Otherwise it sits at the confirmed position.
                 */
                const mainCy = hasPendingEdit ? pendingCy : confirmedCy;
                const ghostCy = hasPendingEdit ? confirmedCy : null; // dim ring showing where it was

                const isHovered = hoveredPoint?.index === p.index;
                const isSelected = selectedPoints.has(p.index);
                const isDragging = dragInfo?.pointIndex === p.index;

                let fill = '#34d399';
                if (hasPendingEdit && !isSelected) fill = '#22d3ee';
                if (isSelected) fill = '#22d3ee';

                // Tweaked radius for less bloated flat regions
                const r = isDragging ? 5.5 : isHovered || isSelected || hasPendingEdit ? 4.5 : 2.5;

                return (
                  <g key={p.index}>
                    {/* Dim ring at confirmed position (only visible when there's a pending edit) */}
                    {ghostCy !== null && Math.abs(ghostCy - mainCy) > 0.5 && (
                      <circle cx={cx} cy={ghostCy} r={3}
                        fill="none" stroke="#34d399" strokeWidth={1} strokeOpacity={0.4} />
                    )}

                    {/* Drop-line from confirmed → pending while dragging */}
                    {isDragging && ghostCy !== null && Math.abs(ghostCy - mainCy) > 1 && (
                      <line x1={cx} y1={ghostCy} x2={cx} y2={mainCy}
                        stroke="#22d3ee" strokeWidth={1} strokeDasharray="3 2" strokeOpacity={0.5} />
                    )}

                    {/* Main interactive circle — at pending position if staged, otherwise confirmed */}
                    <circle
                      cx={cx}
                      cy={mainCy}
                      r={r}
                      fill={fill}
                      stroke={isDragging ? '#fff' : isSelected ? '#fff' : isHovered ? '#fff' : 'none'}
                      strokeWidth={isDragging ? 2 : isSelected ? 2 : 1}
                      style={{ cursor: 'ns-resize', transition: 'r 0.08s' }}
                      onMouseEnter={() => handleMouseEnter(p)}
                      onMouseLeave={handleMouseLeave}
                      onMouseDown={(e) => handlePointMouseDown(e, p)}
                      onClick={(e) => {
                        if (dragMoved.current) return;
                        selectPoint(p.index, e.shiftKey);
                      }}
                    />
                  </g>
                );
              })}
            </g>

            {/* Box-select rubber band */}
            {boxRect && (
              <rect
                x={Math.min(boxRect.x0, boxRect.x1)}
                y={Math.min(boxRect.y0, boxRect.y1)}
                width={Math.abs(boxRect.x1 - boxRect.x0)}
                height={Math.abs(boxRect.y1 - boxRect.y0)}
                fill="#22d3ee" fillOpacity={0.08}
                stroke="#22d3ee" strokeWidth={1} strokeDasharray="4 2" strokeOpacity={0.6}
                pointerEvents="none"
              />
            )}
          </g>
        </svg>

        {/* Inline Input Overlay */}
        {inlineInput && (() => {
          const pt = pts.find(p => p.index === inlineInput.pointIndex);
          if (!pt) return null;
          const cx = scalesRef.current.xScale(pt.volt_mv);
          const currentDelta = pendingDeltas.has(pt.index) ? pendingDeltas.get(pt.index)! : pt.delta_khz;
          const deltaChange = currentDelta - pt.delta_khz;
          const cy = scalesRef.current.yScale(pt.freq_mhz + deltaChange / 1000);
          const pos = svgToContainer(cx, cy);
          if (!pos) return null;
          return (
            <div
              style={{
                position: 'absolute',
                left: pos.x - 40,
                top: pos.y - 12,
                zIndex: 100,
              }}
            >
              <input
                autoFocus
                type="number"
                step="1"
                value={inlineInput.value}
                onChange={(e) => setInlineInput({ ...inlineInput, value: e.target.value })}
                onBlur={() => setInlineInput(null)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    const mhz = parseFloat(inlineInput.value);
                    if (!isNaN(mhz) && isFinite(mhz)) {
                      const clamped = Math.max(-500_000, Math.min(500_000, Math.round(mhz * 1000)));
                      stageEdit(pt.index, clamped);
                    }
                    setInlineInput(null);
                    // Refocus the container
                    containerRef.current?.focus();
                  }
                  if (e.key === 'Escape') {
                    setInlineInput(null);
                    containerRef.current?.focus();
                  }
                }}
                className="w-20 bg-zinc-800 text-cyan-300 rounded px-1.5 py-0.5 border-2 border-cyan-500 shadow-lg outline-none text-xs font-mono text-center"
              />
            </div>
          );
        })()}

        {/* Hover / Active tooltip */}
        {(() => {
          if (dragInfo) return null;
          let tooltipPt = hoveredPoint;
          if (!tooltipPt && selectedPoints.size === 1) {
            tooltipPt = pts.find(p => p.index === Array.from(selectedPoints)[0]) || null;
          }
          if (!tooltipPt) return null;

          return (
            <CurveTooltip
              point={tooltipPt}
              pendingDeltaKhz={pendingDeltas.get(tooltipPt.index)}
              isClamped={clampedPoints.has(tooltipPt.index)}
            />
          );
        })()}

        {/* Drag tooltip — shown while actively dragging */}
        {dragInfo && (() => {
          const point = pts.find((p) => p.index === dragInfo.pointIndex);
          if (!point) return null;
          const deltaMhz = dragInfo.currentDeltaKhz / 1000;
          const deltaChange = dragInfo.currentDeltaKhz - point.delta_khz;
          const effMhz = point.freq_mhz + deltaChange / 1000;
          return (
            <div
              style={{
                position: 'absolute',
                right: 16,
                bottom: 16,
                pointerEvents: 'none',
                zIndex: 50,
                width: 164,
              }}
              className="bg-zinc-800 border border-cyan-500/50 rounded-md p-2 text-xs shadow-xl"
            >
              <div className="text-zinc-400 mb-1 flex items-center gap-1">
                <span className="text-cyan-400">↕</span> Point {point.index}
                <span className="ml-auto text-zinc-500">{point.volt_mv.toFixed(0)} mV</span>
              </div>
              <div className={`font-semibold ${deltaMhz > 0 ? 'text-cyan-400' : deltaMhz < 0 ? 'text-orange-400' : 'text-zinc-400'}`}>
                Δ {deltaMhz > 0 ? '+' : ''}{deltaMhz.toFixed(1)} MHz
              </div>
              <div className="text-cyan-200 font-semibold">
                → {effMhz.toFixed(0)} MHz
              </div>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
