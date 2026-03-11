import { useState, useRef, useEffect } from 'react';
import { fmt } from '../../utils/units';
import type { VFPoint } from '../../types';
import { useCurveStore } from '../../store/curveStore';

interface Props {
  point: VFPoint;
  isCurrent: boolean;
  isSelected: boolean;
  isClamped: boolean;
  pendingDeltaKhz?: number;
  shouldAutoScroll?: boolean;
  onMouseDown?: (e: React.MouseEvent) => void;
  onMouseEnter?: () => void;
}

export function PointRow({ point, isCurrent, isSelected, isClamped, pendingDeltaKhz, shouldAutoScroll, onMouseDown, onMouseEnter }: Props) {
  const { stageEdit } = useCurveStore();
  const [editing, setEditing] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const trRef = useRef<HTMLTableRowElement>(null);

  useEffect(() => {
    if (shouldAutoScroll && trRef.current) {
      trRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [shouldAutoScroll]);

  const hasPending = pendingDeltaKhz !== undefined;
  const displayDeltaMhz = hasPending ? pendingDeltaKhz / 1000 : point.delta_mhz;
  const displayDeltaKhz = hasPending ? pendingDeltaKhz : point.delta_khz;
  // Effective = current effective + delta change
  const deltaChange = hasPending ? pendingDeltaKhz - point.delta_khz : 0;
  const displayEffMhz = point.freq_mhz + deltaChange / 1000;

  const deltaColor = hasPending
    ? displayDeltaKhz > 0 ? 'text-cyan-400' : displayDeltaKhz < 0 ? 'text-orange-400' : 'text-zinc-400'
    : point.delta_khz > 0 ? 'text-emerald-400' : point.delta_khz < 0 ? 'text-red-400' : 'text-zinc-500';

  function startEdit() {
    if (point.is_idle) return;
    setInputValue((displayDeltaMhz).toFixed(1));
    setEditing(true);
    setTimeout(() => {
      inputRef.current?.select();
    }, 0);
  }

  function commitEdit() {
    const mhz = parseFloat(inputValue);
    if (!isNaN(mhz) && isFinite(mhz)) {
      const clamped = Math.max(-1000, Math.min(1000, mhz));
      stageEdit(point.index, Math.round(clamped * 1000));
    }
    setEditing(false);
  }

  function cancelEdit() {
    setEditing(false);
  }

  return (
    <tr
      ref={trRef}
      className={[
        'border-b border-zinc-800 text-xs font-mono',
        isCurrent ? 'bg-yellow-400/10' : isSelected ? 'bg-cyan-500/10' : 'hover:bg-zinc-800/50',
        point.is_idle ? 'opacity-50' : 'cursor-pointer',
      ].join(' ')}
      onMouseDown={(e) => {
        if (point.is_idle || editing) return;
        onMouseDown?.(e);
      }}
      onMouseEnter={() => {
        if (point.is_idle || editing) return;
        onMouseEnter?.();
      }}
    >
      <td className="px-3 py-1 text-zinc-500">{point.index}</td>
      <td className="px-3 py-1 text-zinc-300">{fmt.mv(point.volt_mv, 0)}</td>

      {/* Offset — click to edit inline */}
      <td className={`px-3 py-1 ${deltaColor}`} onClick={(e) => { e.stopPropagation(); startEdit(); }}>
        {editing ? (
          <input
            ref={inputRef}
            type="number"
            step="1"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onBlur={commitEdit}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); commitEdit(); }
              if (e.key === 'Escape') cancelEdit();
            }}
            className="w-20 bg-zinc-700 text-cyan-300 rounded px-1 py-0 border border-cyan-500 outline-none text-xs"
            style={{ fontFamily: 'monospace' }}
          />
        ) : (
          <span title={point.is_idle ? undefined : 'Click to edit'}>
            {hasPending && <span className="text-cyan-500 mr-0.5">✎</span>}
            {displayDeltaKhz > 0 ? '+' : ''}{displayDeltaMhz.toFixed(1)} MHz
          </span>
        )}
      </td>

      {/* Eff. Freq */}
      <td className={`px-3 py-1 font-semibold ${hasPending ? 'text-cyan-200' : 'text-zinc-100'}`}>
        {fmt.mhz(displayEffMhz, 0)}
        {isClamped && !hasPending && (
          <span
            className="ml-1 text-amber-500 cursor-help"
            title="Clamped by monotonicity — a lower-voltage point with a higher offset is holding this frequency up"
          >⇡</span>
        )}
      </td>

      <td className="px-3 py-1">
        {isCurrent && <span className="text-yellow-400">◀ now</span>}
        {point.is_idle && <span className="text-zinc-600">idle</span>}
        {isSelected && !isCurrent && <span className="text-cyan-500">●</span>}
      </td>
    </tr>
  );
}
