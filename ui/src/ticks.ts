/** Round axis ticks from zero: 0 and three or four clean steps covering `max`. */
export function niceTicks(max: number, target = 4): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, 1];
  const raw = max / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? 10 * power;
  const ticks: number[] = [];
  for (let value = 0; value < max + step * 1e-9; value += step) ticks.push(roundTo(value, step));
  if (ticks[ticks.length - 1]! < max) ticks.push(roundTo(ticks[ticks.length - 1]! + step, step));
  return ticks;
}

function roundTo(value: number, step: number) {
  const digits = Math.max(0, -Math.floor(Math.log10(step)) + 1);
  return Number(value.toFixed(digits));
}
