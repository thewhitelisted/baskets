import type { Meta } from '../../api/types'
import { AnalyticsError } from './errors'

export type WeightConcentration = {
  /** G = sum |w|. */
  gross: number
  /** sum (|w_i| / G)^2, in [1/n, 1]. */
  hhi: number
  effectiveN: number
  /** Share of gross in the 5 / 10 largest |w|. */
  top5: number
  top10: number
}

/** Weight concentration over gross exposure; null when every weight is 0. */
export function weightConcentration(
  weights: readonly number[],
): WeightConcentration | null {
  const abs = weights.map(Math.abs)
  const gross = abs.reduce((a, b) => a + b, 0)
  if (gross === 0) return null
  const shares = abs.map((a) => a / gross).sort((a, b) => b - a)
  const hhi = shares.reduce((acc, s) => acc + s * s, 0)
  const top = (k: number): number =>
    shares.slice(0, k).reduce((a, b) => a + b, 0)
  return { gross, hhi, effectiveN: 1 / hhi, top5: top(5), top10: top(10) }
}

/**
 * Risk-based effective number of holdings, 1 / sum(pctCtr_i^2). Squaring
 * ignores sign, so a hedge (negative %CTR) counts as concentration: with
 * offsetting contributions the sum of squares exceeds 1 and the result drops
 * below 1. Because sum pctCtr = 1, the result is always in (0, n].
 */
export function riskEffectiveN(pctCtr: readonly number[]): number {
  if (pctCtr.length === 0) throw new RangeError('no contributions')
  return 1 / pctCtr.reduce((acc, c) => acc + c * c, 0)
}

export type ExposureMeta = Pick<
  Meta,
  'assetClass' | 'sector' | 'country' | 'currency' | 'fundSectorWeights'
>

export type Exposure = { key: string; net: number; gross: number }

export type Exposures = {
  assetClass: Exposure[]
  sector: Exposure[]
  country: Exposure[]
  currency: Exposure[]
}

export const UNCLASSIFIED = 'Unclassified'

class Buckets {
  private readonly map = new Map<string, Exposure>()
  add(key: string, weight: number): void {
    const e = this.map.get(key) ?? { key, net: 0, gross: 0 }
    e.net += weight
    e.gross += Math.abs(weight)
    this.map.set(key, e)
  }
  list(): Exposure[] {
    return [...this.map.values()].sort(
      (a, b) => b.gross - a.gross || (a.key < b.key ? -1 : 1),
    )
  }
}

/**
 * Net and gross exposure by asset class, sector, country and currency.
 * Sector looks through funds via fundSectorWeights (normalized to sum to 1),
 * else uses the holding's sector, else "Unclassified"; country likewise.
 */
export function exposures(
  weights: Readonly<Record<string, number>>,
  meta: Readonly<Record<string, ExposureMeta>>,
): Exposures {
  const assetClass = new Buckets()
  const sector = new Buckets()
  const country = new Buckets()
  const currency = new Buckets()
  for (const [id, w] of Object.entries(weights)) {
    const m = meta[id]
    if (m === undefined)
      throw new AnalyticsError('missing_meta', `no metadata supplied for ${id}`)
    assetClass.add(m.assetClass, w)
    country.add(m.country ?? UNCLASSIFIED, w)
    currency.add(m.currency, w)
    const fund = m.fundSectorWeights
    if (fund === null) {
      sector.add(m.sector ?? UNCLASSIFIED, w)
      continue
    }
    const parts = Object.entries(fund)
    const total = parts.reduce((acc, [, f]) => acc + f, 0)
    if (
      parts.some(([, f]) => !(Number.isFinite(f) && f >= 0)) ||
      !(total > 0)
    ) {
      throw new AnalyticsError(
        'invalid_series',
        `fundSectorWeights for ${id} must be non-negative with a positive sum`,
      )
    }
    for (const [name, f] of parts) sector.add(name, (w * f) / total)
  }
  return {
    assetClass: assetClass.list(),
    sector: sector.list(),
    country: country.list(),
    currency: currency.list(),
  }
}
