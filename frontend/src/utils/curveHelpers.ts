import type { VFPoint } from '../types';

/** Non-idle points only */
export function activePoints(points: VFPoint[]): VFPoint[] {
  return points.filter((p) => !p.is_idle);
}

/**
 * Approximate reference frequency (MHz) for a point: effective − delta.
 *
 * `p.freq_mhz` is the current effective frequency as reported by GetVFPCurve
 * (already including any applied boost delta). Subtracting the delta gives an
 * approximation of the hardware base.
 *
 * Note: NVIDIA enforces monotonicity across the V/F curve — a large delta on
 * a lower-voltage point pushes up neighbouring points' effective frequencies
 * even when those points have zero delta. So for monotonicity-affected points,
 * `effective - delta` doesn't recover the true unmodified base. It is still
 * useful as a faint reference line ("where this point would sit with no boost").
 */
export function refBaseMhz(p: VFPoint): number {
  return p.freq_mhz - p.delta_mhz;
}

/** Find which VF point the GPU is currently near based on voltage reading */
export function findCurrentPoint(
  points: VFPoint[],
  voltage_mv: number | null,
): VFPoint | null {
  if (voltage_mv == null) return null;
  const active = activePoints(points);
  if (active.length === 0) return null;
  // Find the closest voltage match
  return active.reduce((best, p) =>
    Math.abs(p.volt_mv - voltage_mv) < Math.abs(best.volt_mv - voltage_mv) ? p : best,
  );
}

/** Voltage domain extent for active points, with padding */
export function voltExtent(points: VFPoint[], padMv = 20): [number, number] {
  const active = activePoints(points);
  if (active.length === 0) return [600, 1100];
  const min = Math.min(...active.map((p) => p.volt_mv));
  const max = Math.max(...active.map((p) => p.volt_mv));
  return [min - padMv, max + padMv];
}

/** Frequency domain extent for the effective (boosted) curve, with padding */
export function freqExtent(points: VFPoint[], padMhz = 50): [number, number] {
  const active = activePoints(points);
  if (active.length === 0) return [1000, 3000];
  // freq_mhz is the current effective value; also consider ref base for lower bound
  const allFreqs = active.flatMap((p) => [p.freq_mhz, refBaseMhz(p)]);
  const min = Math.min(...allFreqs);
  const max = Math.max(...active.map((p) => p.freq_mhz));
  return [min - padMhz, max + padMhz];
}

/**
 * Detect points whose effective frequency is being held up by NVIDIA's
 * monotonicity enforcement rather than their own offset.
 *
 * Walk active points in voltage order, tracking the "ceiling" — the highest
 * effective frequency seen so far and the offset that produced it. A point is
 * clamped when:
 *   1. Its effective freq is at or below the ceiling (hasn't moved past it)
 *   2. Its own offset is lower than the offset that set the ceiling
 *
 * This catches cases like: point 103 has +950 MHz → effective 3907 MHz,
 * point 104 has +315 MHz → effective also 3907 MHz (clamped).
 */
export function detectClampedPoints(points: VFPoint[]): Set<number> {
  const clamped = new Set<number>();
  const active = activePoints(points);

  let ceiling = -Infinity;
  let ceilingOffset = -Infinity;

  for (const p of active) {
    if (p.freq_mhz <= ceiling && p.delta_khz < ceilingOffset) {
      clamped.add(p.index);
    }
    if (p.freq_mhz > ceiling) {
      ceiling = p.freq_mhz;
      ceilingOffset = p.delta_khz;
    }
  }

  return clamped;
}
