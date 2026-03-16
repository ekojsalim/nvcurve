import { useEffect } from 'react';
import { api } from '../api/client';
import { useCurveStore } from '../store/curveStore';

export function useGpu() {
  const { gpuInfo, setGpuInfo, setAvailableGpus, selectedGpuIndex } = useCurveStore();

  useEffect(() => {
    api.gpus().then(setAvailableGpus).catch(console.error);
  }, [setAvailableGpus]);

  useEffect(() => {
    api.gpu(selectedGpuIndex).then(setGpuInfo).catch(console.error);
  }, [setGpuInfo, selectedGpuIndex]);

  return gpuInfo;
}
