import { fmt } from '../../utils/units';
import type { VFPoint } from '../../types';

interface Props {
  point: VFPoint;
  /** Pending delta in kHz (from store), if any */
  pendingDeltaKhz?: number;
  /** True if this point is being held up by monotonicity enforcement */
  isClamped?: boolean;
}

/**
 * HTML tooltip rendered as an absolutely-positioned overlay on top of the
 * SVG so it's never clipped by the SVG viewport.
 */
export function CurveTooltip({ point, pendingDeltaKhz, isClamped }: Props) {
  const hasPending = pendingDeltaKhz !== undefined;
  const pendingMhz = hasPending ? pendingDeltaKhz! / 1000 : 0;
  const deltaChange = hasPending ? pendingDeltaKhz! - point.delta_khz : 0;
  const pendingEffMhz = hasPending ? point.freq_mhz + deltaChange / 1000 : null;

  return (
    <div
      style={{
        position: 'absolute',
        right: 16,
        bottom: 16,
        pointerEvents: 'none',
        zIndex: 50,
        width: 172,
      }}
      className="bg-zinc-800 border border-zinc-700 rounded-md p-2 text-xs shadow-xl"
    >
      <div className="text-zinc-400 mb-1">Point {point.index}</div>
      <div className="text-zinc-200">
        <span className="text-zinc-400">Volt:   </span>{fmt.mv(point.volt_mv, 1)}
      </div>
      <div className="text-zinc-200">
        <span className="text-zinc-400">Offset: </span>
        <span className={point.delta_khz > 0 ? 'text-emerald-400' : point.delta_khz < 0 ? 'text-red-400' : 'text-zinc-400'}>
          {point.delta_khz > 0 ? '+' : ''}{fmt.mhz(point.delta_mhz, 1)}
        </span>
      </div>
      <div className="text-emerald-300 font-semibold">
        <span className="text-zinc-400">Eff.:   </span>{fmt.mhz(point.freq_mhz, 0)}
        {isClamped && <span className="text-amber-500 ml-1">⇡</span>}
      </div>
      {isClamped && (
        <div className="text-amber-500/80 text-[10px] mt-0.5">
          Clamped by lower-voltage point
        </div>
      )}
      {hasPending && (
        <>
          <div className="border-t border-zinc-700 mt-1.5 pt-1.5">
            <div className="text-zinc-200">
              <span className="text-zinc-400">Pending: </span>
              <span className={pendingMhz > 0 ? 'text-cyan-400' : pendingMhz < 0 ? 'text-orange-400' : 'text-zinc-400'}>
                {pendingMhz > 0 ? '+' : ''}{pendingMhz.toFixed(1)} MHz
              </span>
            </div>
            <div className="text-cyan-300 font-semibold">
              <span className="text-zinc-400">→ Eff.: </span>{fmt.mhz(pendingEffMhz, 0)}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
