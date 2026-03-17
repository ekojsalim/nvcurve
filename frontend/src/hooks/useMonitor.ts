import { useEffect, useRef, useState } from 'react';
import { createWsConnection } from '../api/websocket';
import { useCurveStore } from '../store/curveStore';
import type { MonitoringSample } from '../types';

export function useMonitor() {
  const { monitor, monitorHistory, pushMonitor, selectedGpuIndex } = useCurveStore();
  const [wsStatus, setWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const wsRef = useRef<ReturnType<typeof createWsConnection> | null>(null);

  useEffect(() => {
    wsRef.current = createWsConnection<MonitoringSample>(
      '/ws/monitor',
      pushMonitor,
      setWsStatus,
      selectedGpuIndex,
    );
    return () => wsRef.current?.close();
  }, [pushMonitor, selectedGpuIndex]);

  return { monitor, monitorHistory, wsStatus };
}
