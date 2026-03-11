import { useEffect } from 'react';
import { api } from '../api/client';
import { useCurveStore } from '../store/curveStore';

export function useGpu() {
  const { gpuInfo, setGpuInfo } = useCurveStore();

  useEffect(() => {
    api.gpu().then(setGpuInfo).catch(console.error);
  }, [setGpuInfo]);

  return gpuInfo;
}
