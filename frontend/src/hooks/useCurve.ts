import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { createWsConnection } from '../api/websocket';
import { useCurveStore } from '../store/curveStore';
import type { CurveState } from '../types';

export function useCurve() {
  const { curve, setCurve } = useCurveStore();
  const [wsStatus, setWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const wsRef = useRef<ReturnType<typeof createWsConnection> | null>(null);

  useEffect(() => {
    // Initial fetch
    api.curve().then(setCurve).catch(console.error);

    // Subscribe to /ws/curve for push updates after writes
    wsRef.current = createWsConnection<CurveState>(
      '/ws/curve',
      (data) => setCurve(data),
      setWsStatus,
    );

    return () => wsRef.current?.close();
  }, [setCurve]);

  return { curve, wsStatus };
}
