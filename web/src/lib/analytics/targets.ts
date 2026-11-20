import type { SharesEntry } from '../../api/types'
import type { Column } from './align'
import { AnalyticsError } from './errors'
import { at, cell, median, sampleStd } from './numeric'

export type Sign = 1 | -1

/** Basket weights carry the side for non-custom schemes; 0 counts as long. */
export function signOf(weight: number): Sign {
  return weight < 0 ? -1 : 1
}

/** Scales raw signed weights so that sum |w| = leverage. */
export function scaleToGross(
  raw: readonly number[],
  leverage: number,
): number[] {
  let gross = 0
  for (const r of raw) gross += Math.abs(r)
  if (gross === 0) {
    throw new AnalyticsError(
      'zero_gross',
      'every raw target weight is 0; cannot scale to leverage',
    )
  }
  return raw.map((r) => (r / gross) * leverage)
}

export function equalTargets(
  signs: readonly Sign[],
  leverage: number,
): number[] {
  return scaleToGross(signs, leverage)
}

/** Minimum number of trailing returns for a holding's own inverse-vol sigma. */
export const MIN_INVVOL_RETURNS = 20

export type InvvolFallback =
  | { kind: 'none' }
  /** Holdings at these indices had too few returns and used the median sigma of the others. */
  | { kind: 'median'; indices: number[]; sigma: number }
  /** No holding had enough returns; equal weights were used. */
  | { kind: 'equal' }

/**
 * Weights proportional to sign / sigma, scaled to sum |w| = leverage. A null
 * sigma marks a holding with fewer than MIN_INVVOL_RETURNS returns.
 * Every non-null sigma must be positive.
 */
export function invvolTargets(
  signs: readonly Sign[],
  sigmas: readonly (number | null)[],
  leverage: number,
): { weights: number[]; fallback: InvvolFallback } {
  if (signs.length !== sigmas.length)
    throw new RangeError('signs and sigmas differ in length')
  const known: number[] = []
  for (const s of sigmas) {
    if (s === null) continue
    if (!(s > 0))
      throw new RangeError(`invvol sigma must be positive, got ${String(s)}`)
    known.push(s)
  }
  if (known.length === 0)
    return {
      weights: equalTargets(signs, leverage),
      fallback: { kind: 'equal' },
    }
  const fill = median(known)
  const raw = signs.map((sign, i) => sign / (at(sigmas, i) ?? fill))
  const weights = scaleToGross(raw, leverage)
  const indices = sigmas.flatMap((s, i) => (s === null ? [i] : []))
  if (indices.length === 0) return { weights, fallback: { kind: 'none' } }
  return { weights, fallback: { kind: 'median', indices, sigma: fill } }
}

/** Weights proportional to sign * market cap, scaled to sum |w| = leverage. */
export function mcapTargets(
  signs: readonly Sign[],
  caps: readonly number[],
  leverage: number,
): number[] {
  if (signs.length !== caps.length)
    throw new RangeError('signs and caps differ in length')
  return scaleToGross(
    signs.map((sign, i) => sign * at(caps, i)),
    leverage,
  )
}

/**
 * Sample standard deviation (n - 1) of the non-null returns among the
 * `lookback` calendar returns ending at index t, or null when fewer than
 * MIN_INVVOL_RETURNS are available.
 */
export function trailingSigma(
  returns: Column,
  t: number,
  lookback: number,
): { sigma: number | null; count: number } {
  const window: number[] = []
  for (let i = Math.max(0, t - lookback + 1); i <= t; i++) {
    const r = cell(returns, i)
    if (r !== null) window.push(r)
  }
  const sigma = window.length >= MIN_INVVOL_RETURNS ? sampleStd(window) : null
  return { sigma, count: window.length }
}

/**
 * Shares outstanding known at `date`: the latest value with knownDate <= date.
 * If none is known yet, the earliest value is used with lookAhead = true;
 * source "current" is always lookAhead.
 */
export function sharesAsOf(
  entry: SharesEntry,
  date: string,
): { shares: number; lookAhead: boolean } {
  if (
    entry.knownDates.length === 0 ||
    entry.knownDates.length !== entry.shares.length
  ) {
    throw new AnalyticsError(
      'invalid_series',
      'shares entry must have equal, non-empty knownDates and shares',
    )
  }
  let idx = -1
  for (let i = 0; i < entry.knownDates.length; i++)
    if (at(entry.knownDates, i) <= date) idx = i
  const lookAhead = entry.source === 'current' || idx < 0
  return { shares: at(entry.shares, Math.max(idx, 0)), lookAhead }
}

/**
 * Removes the slots marked in `removed` from custom target weights. Each
 * removed weight is added to the surviving weights of the same sign in
 * proportion to their |w|; if no survivor has that sign it is left as cash.
 * Removed entries are returned as 0.
 */
export function redistributeCustom(
  weights: readonly number[],
  removed: readonly boolean[],
): number[] {
  if (weights.length !== removed.length)
    throw new RangeError('weights and removed differ in length')
  let freedPos = 0
  let freedNeg = 0
  let poolPos = 0
  let poolNeg = 0
  weights.forEach((w, i) => {
    if (at(removed, i)) {
      if (w > 0) freedPos += w
      else freedNeg += w
    } else if (w > 0) poolPos += w
    else poolNeg -= w
  })
  return weights.map((w, i) => {
    if (at(removed, i)) return 0
    if (w > 0) return w + (freedPos * w) / poolPos
    if (w < 0) return w + (freedNeg * -w) / poolNeg
    return w
  })
}
