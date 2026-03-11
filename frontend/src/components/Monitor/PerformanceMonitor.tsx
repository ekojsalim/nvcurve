import { GaugeCard } from './GaugeCard';
import { fmt } from '../../utils/units';
import type { MonitoringSample } from '../../types';

interface Props {
  monitor: MonitoringSample | null;
  history: MonitoringSample[];
}

function pluck<K extends keyof MonitoringSample>(
  history: MonitoringSample[],
  key: K,
): number[] {
  return history.map((s) => (s[key] as number | null) ?? 0);
}

export function PerformanceMonitor({ monitor, history }: Props) {
  const memUsed = monitor?.mem_used_mib ?? null;
  const memTotal = monitor?.mem_total_mib ?? null;
  const memLabel = memUsed != null && memTotal != null
    ? `${memUsed.toFixed(0)} / ${memTotal.toFixed(0)} MiB`
    : '—';

  return (
    <div className="flex flex-col gap-2 w-full h-full">
      <div className="bg-zinc-900 rounded-lg p-3 flex flex-col gap-2 h-full">
        <div className="flex items-center justify-between pb-2 border-b border-zinc-800">
          <span className="text-xs text-zinc-500 uppercase tracking-wider font-semibold">Live Monitor</span>
        </div>
        <div className="flex flex-col gap-2 mt-1">
          <GaugeCard
            label="Mem Clock"
            value={fmt.mhz(monitor?.mem_clock_mhz)}
            history={pluck(history, 'mem_clock_mhz')}
            color="#67e8f9"
            max={20000}
          />
          <GaugeCard
            label="Power Draw"
            value={fmt.watts(monitor?.power_w)}
            history={pluck(history, 'power_w')}
            color="#f472b6"
            max={600}
          />
          <GaugeCard
            label="VRAM Used"
            value={memLabel}
            history={pluck(history, 'mem_used_mib')}
            color="#a78bfa"
            max={memTotal ?? 32768}
          />
        </div>
      </div>
    </div>
  );
}
