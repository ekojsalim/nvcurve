interface Props {
  label: string;
  value: string;
  history: number[];
  unit?: string;
  color?: string; // tailwind color class for the sparkline stroke
  max?: number;
}

const SPARKLINE_H = 32;
const SPARKLINE_W = 120;

function Sparkline({ data, color = '#a78bfa', max }: { data: number[]; color?: string; max?: number }) {
  const points = data.length >= 2 ? (() => {
    const dataMax = max ?? Math.max(...data, 1);
    const dataMin = Math.min(...data, 0);
    const range = dataMax - dataMin || 1;
    return data.map((v, i) => {
      const x = (i / (data.length - 1)) * SPARKLINE_W;
      const y = SPARKLINE_H - ((v - dataMin) / range) * SPARKLINE_H;
      return `${x},${y}`;
    }).join(' ');
  })() : null;

  return (
    <svg width="100%" height={SPARKLINE_H} viewBox={`0 0 ${SPARKLINE_W} ${SPARKLINE_H}`} preserveAspectRatio="none" className="overflow-visible">
      {points && (
        <polyline
          points={points}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.8"
        />
      )}
    </svg>
  );
}

export function GaugeCard({ label, value, history, color = '#a78bfa', max }: Props) {
  return (
    <div className="bg-zinc-900 rounded-lg p-3 flex flex-col gap-1 min-w-0">
      <div className="text-xs text-zinc-500 uppercase tracking-wider">{label}</div>
      <div className="text-xl font-mono font-semibold text-zinc-100">{value}</div>
      <div className="mt-1 w-full">
        <Sparkline data={history} color={color} max={max} />
      </div>
    </div>
  );
}
