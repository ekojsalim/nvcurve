import { Cpu, Wifi, WifiOff, Loader, Terminal } from 'lucide-react';
import type { GpuInfo, MonitoringSample } from '../../types';
import { fmt } from '../../utils/units';

interface Props {
  gpuInfo: GpuInfo | null;
  wsStatus: 'connecting' | 'connected' | 'disconnected';
  monitor: MonitoringSample | null;
}

const statusIcon = {
  connected: <Wifi size={14} className="text-emerald-400" />,
  connecting: <Loader size={14} className="text-yellow-400 animate-spin" />,
  disconnected: <WifiOff size={14} className="text-red-400" />,
};

const statusText = {
  connected: 'Connected',
  connecting: 'Connecting…',
  disconnected: 'Disconnected',
};

export function StatusBar({ gpuInfo, wsStatus, monitor }: Props) {
  return (
    <header className="bg-zinc-900 border-b border-zinc-800 text-sm">
      <div className="flex items-center gap-4 px-4 py-2 w-full max-w-[1600px] mx-auto min-w-0">
        {/* Left: GPU name */}
        <div className="flex items-center gap-2 min-w-0 shrink-0">
          <img src="/logo.svg" alt="NVCurve" className="w-5 h-5 shrink-0" />
          <span className="font-bold text-transparent bg-clip-text bg-gradient-to-r from-violet-400 to-fuchsia-400 tracking-tight mr-1">
            NVCurve
          </span>
          <div className="w-px h-4 bg-zinc-700 mx-1 hidden sm:block"></div>
          <Cpu size={16} className="shrink-0 text-zinc-400 ml-1 hidden sm:block" />
          <span className="font-medium text-zinc-200 truncate">
            {gpuInfo?.name ?? 'No Device'}
          </span>
          {gpuInfo?.driver_version && (
            <div className="flex items-center gap-1.5 text-zinc-400 shrink-0 hidden sm:flex bg-zinc-800/80 border border-zinc-700/50 px-2 py-0.5 rounded text-xs ml-2">
              <Terminal size={12} className="text-zinc-500" />
              <span>{gpuInfo.driver_version}</span>
            </div>
          )}
        </div>

        {/* Centre: live stats */}
        <div className="flex-1 flex items-center justify-center gap-5 min-w-0 hidden lg:flex">
          <StatPill label="Temp" value={fmt.celsius(monitor?.temp_c)} color="text-orange-400" />
          <StatPill label="Power" value={fmt.watts(monitor?.power_w)} color="text-pink-400" />
          <StatPill label="Fan" value={fmt.pct(monitor?.fan_pct)} color="text-blue-400" />
          <StatPill label="GPU Util" value={fmt.pct(monitor?.gpu_util_pct)} color="text-yellow-400" />
          <StatPill label="Mem Util" value={fmt.pct(monitor?.mem_util_pct)} color="text-sky-400" />
        </div>

        {/* Right: connection status */}
        <div className="flex items-center gap-1.5 text-zinc-400 shrink-0">
          {statusIcon[wsStatus]}
          <span className="hidden sm:inline">{statusText[wsStatus]}</span>
        </div>
      </div>
    </header>
  );
}

function StatPill({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="flex items-baseline gap-1">
      <span className="text-zinc-500 text-xs">{label}</span>
      <span className={`font-mono font-semibold text-sm ${color}`}>{value}</span>
    </div>
  );
}
