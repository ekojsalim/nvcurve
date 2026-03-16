import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { createWsConnection } from '../api/websocket';
import { useCurveStore } from '../store/curveStore';
import type { CurveState } from '../types';

export function useCurve() {
  const { curve, setCurve, selectedGpuIndex } = useCurveStore();
  const [wsStatus, setWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const wsRef = useRef<ReturnType<typeof createWsConnection> | null>(null);

  useEffect(() => {
    // Initial fetch
    api.curve(selectedGpuIndex).then(setCurve).catch(console.error);

    // Subscribe to /ws/curve for push updates after writes
    wsRef.current = createWsConnection<CurveState>(
      '/ws/curve',
      (data) => setCurve(data),
      setWsStatus,
      selectedGpuIndex,
    );

    return () => wsRef.current?.close();
  }, [setCurve, selectedGpuIndex]);

  return { curve, wsStatus };
}
