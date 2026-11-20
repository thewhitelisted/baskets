import type { Column } from './align'
import { AnalyticsError } from './errors'
import { cell } from './numeric'

/** Simple return from prev to cur. A previous price of 0 gives 0: a worthless position stays worthless. */
export function simpleReturn(prev: number, cur: number): number {
  return prev === 0 ? 0 : cur / prev - 1
}

/**
 * Daily simple returns of an aligned price column; null on the first date and
 * wherever either price is missing. Throws if a price ratio overflows.
 */
export function simpleReturns(prices: Column): Column {
  const out: Column = new Array<number | null>(prices.length)
  for (let t = 0; t < prices.length; t++) {
    const prev = t === 0 ? null : cell(prices, t - 1)
    const cur = cell(prices, t)
    if (prev === null || cur === null) {
      out[t] = null
      continue
    }
    const r = simpleReturn(prev, cur)
    if (!Number.isFinite(r)) {
      throw new AnalyticsError(
        'invalid_series',
        `return from ${String(prev)} to ${String(cur)} overflows`,
      )
    }
    out[t] = r
  }
  return out
}
