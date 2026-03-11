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

export function LiveMonitor({ monitor, history }: Props) {
  return (
    <div className="flex flex-col gap-2 w-full h-full">
      <div className="bg-zinc-900 rounded-lg p-3 flex flex-col gap-2 h-full">
        <div className="flex items-center justify-between pb-2 border-b border-zinc-800">
          <span className="text-xs text-zinc-500 uppercase tracking-wider font-semibold">Live Monitor</span>
        </div>
        <div className="flex flex-col gap-2 mt-1">
        <GaugeCard
          label="Core Clock"
          value={fmt.mhz(monitor?.clock_mhz)}
          history={pluck(history, 'clock_mhz')}
          color="#34d399"
          max={3000}
        />
        <GaugeCard
          label="Voltage"
          value={fmt.mv(monitor?.voltage_mv)}
          history={pluck(history, 'voltage_mv')}
          color="#a78bfa"
          max={1100}
        />
        <GaugeCard
          label="Power Draw"
          value={fmt.watts(monitor?.power_w)}
          history={pluck(history, 'power_w')}
          color="#f472b6"
          max={600}
        />
        <GaugeCard
          label="GPU Util"
          value={fmt.pct(monitor?.gpu_util_pct)}
          history={pluck(history, 'gpu_util_pct')}
          color="#facc15"
          max={100}
        />
        <div className="mt-auto">
          <GaugeCard
            label="P-State"
            value={monitor?.pstate_label ?? 'Unknown'}
            history={pluck(history, 'pstate')}
            color="#a8a29e"
            max={15}
          />
        </div>
      </div>
      </div>
    </div>
  );
}
