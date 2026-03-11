interface Props {
  label: string;
  value: string;
  history: number[];
  unit?: string;
  color?: string; // tailwind color class for the sparkline stroke
  max?: number;
}

function Sparkline({ data, color = '#a78bfa', max }: { data: number[]; color?: string; max?: number }) {
  if (data.length < 2) return null;
  const h = 32;
  const w = 120;
  const dataMax = max ?? Math.max(...data, 1);
  const dataMin = Math.min(...data, 0);
  const range = dataMax - dataMin || 1;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - dataMin) / range) * h;
    return `${x},${y}`;
  });
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="overflow-visible">
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity="0.8"
      />
    </svg>
  );
}

export function GaugeCard({ label, value, history, color = '#a78bfa', max }: Props) {
  return (
    <div className="bg-zinc-900 rounded-lg p-3 flex flex-col gap-1 min-w-0">
      <div className="text-xs text-zinc-500 uppercase tracking-wider">{label}</div>
      <div className="text-xl font-mono font-semibold text-zinc-100">{value}</div>
      {history.length > 0 && (
        <div className="mt-1 w-full flex-1">
          <Sparkline data={history} color={color} max={max} />
        </div>
      )}
    </div>
  );
}
