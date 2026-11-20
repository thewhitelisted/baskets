/** Trading days per year used for every annualization. */
export const PERIODS_PER_YEAR = 252

export function sum(xs: readonly number[]): number {
  let s = 0
  for (const x of xs) s += x
  return s
}

/** Arithmetic mean; throws on an empty array. */
export function mean(xs: readonly number[]): number {
  if (xs.length === 0) throw new RangeError('mean of empty array')
  return sum(xs) / xs.length
}

/**
 * Sample covariance (denominator n - 1) of xs[from..to) and ys[from..to) by
 * the shifted two-pass method: values are measured from the first element of
 * the range before centring, so a constant input gives exactly 0 rather than
 * rounding noise. Requires equal lengths and at least 2 values in the range.
 */
export function sampleCovariance(
  xs: readonly number[],
  ys: readonly number[],
  from = 0,
  to = xs.length,
): number {
  if (xs.length !== ys.length) throw new RangeError('length mismatch')
  if (from < 0 || to > xs.length || to - from < 2)
    throw new RangeError('sample covariance needs at least 2 values')
  const x0 = num(xs, from)
  const y0 = num(ys, from)
  let mx = 0
  let my = 0
  for (let i = from; i < to; i++) {
    mx += num(xs, i) - x0
    my += num(ys, i) - y0
  }
  const n = to - from
  mx /= n
  my /= n
  let s = 0
  for (let i = from; i < to; i++)
    s += (num(xs, i) - x0 - mx) * (num(ys, i) - y0 - my)
  return s / (n - 1)
}

/** Sample variance (denominator n - 1) of xs[from..to); exactly 0 for a constant input. */
export function sampleVariance(
  xs: readonly number[],
  from = 0,
  to = xs.length,
): number {
  return sampleCovariance(xs, xs, from, to)
}

export function sampleStd(
  xs: readonly number[],
  from = 0,
  to = xs.length,
): number {
  return Math.sqrt(sampleVariance(xs, from, to))
}

/** Median; the mean of the two middle values for an even count. */
export function median(xs: readonly number[]): number {
  if (xs.length === 0) throw new RangeError('median of empty array')
  const s = [...xs].sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 === 1 ? num(s, mid) : (num(s, mid - 1) + num(s, mid)) / 2
}

/** Indexed read that asserts the index is in bounds. */
export function at<T>(xs: readonly T[], i: number): T {
  const v = xs[i]
  if (v === undefined)
    throw new RangeError(
      `index ${String(i)} out of bounds (length ${String(xs.length)})`,
    )
  return v
}

/**
 * The same assertion specialised to number arrays. A single generic helper
 * becomes megamorphic in V8 and dominates hot loops; these monomorphic ones
 * are inlined.
 */
export function num(xs: readonly number[], i: number): number {
  const v = xs[i]
  if (v === undefined)
    throw new RangeError(
      `index ${String(i)} out of bounds (length ${String(xs.length)})`,
    )
  return v
}

/** String-array read that asserts the index is in bounds. */
export function str(xs: readonly string[], i: number): string {
  const v = xs[i]
  if (v === undefined)
    throw new RangeError(
      `index ${String(i)} out of bounds (length ${String(xs.length)})`,
    )
  return v
}

/** Column read (number or null) that asserts the index is in bounds. */
export function cell(xs: readonly (number | null)[], i: number): number | null {
  const v = xs[i]
  if (v === undefined)
    throw new RangeError(
      `index ${String(i)} out of bounds (length ${String(xs.length)})`,
    )
  return v
}

/** Float64Array read that asserts the index is in bounds. */
export function f64(xs: Float64Array, i: number): number {
  const v = xs[i]
  if (v === undefined)
    throw new RangeError(
      `index ${String(i)} out of bounds (length ${String(xs.length)})`,
    )
  return v
}
