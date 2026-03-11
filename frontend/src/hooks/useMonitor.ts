import { useEffect, useRef, useState } from 'react';
import { createWsConnection } from '../api/websocket';
import { useCurveStore } from '../store/curveStore';
import type { MonitoringSample } from '../types';

export function useMonitor() {
  const { monitor, monitorHistory, pushMonitor } = useCurveStore();
  const [wsStatus, setWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const wsRef = useRef<ReturnType<typeof createWsConnection> | null>(null);

  useEffect(() => {
    wsRef.current = createWsConnection<MonitoringSample>(
      '/ws/monitor',
      pushMonitor,
      setWsStatus,
    );
    return () => wsRef.current?.close();
  }, [pushMonitor]);

  return { monitor, monitorHistory, wsStatus };
}
