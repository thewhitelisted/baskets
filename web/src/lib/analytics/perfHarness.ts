// Synthetic 50-holding, 25-year workload for timing backtest + stats + risk +
// factors as shipped (bundled; Vitest's module transform slows these loops
// several-fold, so it is not timed under Vitest). From web/:
//   printf "import { measurePerf } from '$PWD/src/lib/analytics/perfHarness'\nconsole.log(measurePerf(9))\n" > /tmp/perf-entry.ts
//   npx rolldown /tmp/perf-entry.ts -o /tmp/perf.mjs -p node -f esm && node /tmp/perf.mjs
import type { FactorName } from '../../api/types'
import { parseBasket } from '../basket'
import { runBacktest, type SeriesInput } from './backtest'
import { factorRegression } from './factors'
import { basketRisk } from './risk'
import { performanceStats } from './stats'

export const DAYS = 25 * 252
export const HOLDINGS = 50

/** Deterministic pseudo-random numbers (mulberry32). */
function rng(seed: number): () => number {
  let a = seed
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const rand = rng(7)
const calendar: string[] = []
for (
  let d = new Date(Date.UTC(2000, 0, 3));
  calendar.length < DAYS;
  d.setUTCDate(d.getUTCDate() + 1)
) {
  if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6)
    calendar.push(d.toISOString().slice(0, 10))
}
const walk = (): number[] => {
  let p = 100
  return calendar.map(() => (p *= 1 + (rand() - 0.5) * 0.04))
}
const series: Record<string, SeriesInput> = {}
const ids = Array.from({ length: HOLDINGS }, (_, i) => `H${String(i)}`)
for (const id of ['SPY', ...ids]) {
  series[id] = {
    dates: calendar,
    close: walk(),
    status: 'active',
    lastDate: calendar.at(-1) ?? '',
    successor: null,
  }
}
const factorNames: FactorName[] = ['Mkt-RF', 'SMB', 'HML', 'RMW', 'CMA', 'Mom']
const factorData = {
  factors: factorNames,
  dates: calendar,
  values: Object.fromEntries(
    [...factorNames, 'RF'].map((f) => [
      f,
      calendar.map(() => (rand() - 0.5) * 0.02),
    ]),
  ),
}
const basket = parseBasket({
  v: 1,
  name: 'perf',
  holdings: ids.map((id, i) => ({ id, weight: i % 5 === 0 ? -1 : 1 })),
  scheme: 'invvol',
  rebalance: 'monthly',
  overlays: [],
  start: null,
})
const rf = { dates: [calendar[0] ?? ''], annualRate: [0.02] }

function time(fn: () => void): number {
  const t0 = performance.now()
  fn()
  return performance.now() - t0
}

const median = (xs: number[]) =>
  [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] ?? NaN

/** Runs the workload `runs` times after one warm-up and returns median milliseconds per stage. */
export function measurePerf(runs: number): Record<string, number> {
  const parts: Record<string, number[]> = {
    backtest: [],
    stats: [],
    risk: [],
    factors: [],
    total: [],
  }
  for (let run = 0; run <= runs; run++) {
    let result = runBacktest({ basket, calendar, series, rf })
    const b = time(
      () => (result = runBacktest({ basket, calendar, series, rf })),
    )
    const s = time(() => performanceStats(result))
    const r = time(() =>
      basketRisk({
        calendar,
        series,
        weights: result.latest.weights,
        estimator: 'ledoit_wolf',
      }),
    )
    const f = time(() =>
      factorRegression({
        dates: result.dates,
        returns: result.returns,
        factorData,
      }),
    )
    if (run === 0) continue
    parts.backtest?.push(b)
    parts.stats?.push(s)
    parts.risk?.push(r)
    parts.factors?.push(f)
    parts.total?.push(b + s + r + f)
  }
  return Object.fromEntries(
    Object.entries(parts).map(([k, v]) => [k, median(v)]),
  )
}
