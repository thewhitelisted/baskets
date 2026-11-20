import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { near } from './testing'
import {
  drawdown,
  performanceStats,
  ROLLING_WINDOW,
  type StatsInput,
} from './stats'

// Input classes: 0, 1, 2 and many returns; constant returns (zero vol),
// constant excess returns, no negative excess returns (zero downside),
// benchmark with zero variance; NAV monotone up (no drawdown), drawdown with
// recovery, without recovery, repeated equal peaks; history of exactly 252
// and 253 NAV points (rolling window boundary); single year, year boundary
// on the first and last date; inconsistent array lengths.

function input(
  dates: string[],
  returns: number[],
  rf?: number[],
  bench?: number[],
): StatsInput {
  const nav = [1]
  for (const r of returns) nav.push((nav.at(-1) ?? NaN) * (1 + r))
  return {
    dates,
    nav,
    returns,
    rf: rf ?? returns.map(() => 0),
    benchmarkReturns: bench ?? returns.map(() => 0),
  }
}

const days = (n: number, from = Date.UTC(2023, 0, 2)) =>
  Array.from({ length: n }, (_, i) =>
    new Date(from + i * 86400000).toISOString().slice(0, 10),
  )

describe('drawdown', () => {
  it('is all zeros with no dates for a NAV that never falls', () => {
    expect(drawdown(days(3), [1, 1, 1.2])).toEqual({
      series: [0, 0, 0],
      max: 0,
      peakDate: null,
      troughDate: null,
      recoveryDate: null,
    })
  })

  it('finds peak, trough and recovery; the peak is the first date at the high', () => {
    const d = days(7)
    const got = drawdown(d, [1, 2, 2, 1.5, 1, 1.8, 2.1])
    expect(got.max).toBe(-0.5)
    expect([got.peakDate, got.troughDate, got.recoveryDate]).toEqual([
      d[1],
      d[4],
      d[6],
    ])
    expect(got.series).toEqual([0, 0, 0, -0.25, -0.5, near(-0.1, 15), 0])
  })

  it('counts regaining the peak exactly as recovery and reports null when not recovered', () => {
    const d = days(4)
    expect(drawdown(d, [1, 0.5, 1, 0.9]).recoveryDate).toBe(d[2])
    expect(drawdown(d, [1, 0.5, 0.6, 0.9]).recoveryDate).toBeNull()
  })

  it('keeps the first trough when a later drawdown is equally deep', () => {
    const d = days(5)
    const got = drawdown(d, [2, 1, 2, 1, 1.5])
    expect([got.peakDate, got.troughDate, got.recoveryDate]).toEqual([
      d[0],
      d[1],
      d[2],
    ])
  })

  it('is between -1 and 0 everywhere', () => {
    fc.assert(
      fc.property(
        fc.array(fc.double({ min: 1e-6, max: 1e6, noNaN: true }), {
          minLength: 1,
          maxLength: 50,
        }),
        (nav) => {
          const got = drawdown(days(nav.length), nav)
          for (const x of got.series) expect(x <= 0 && x > -1).toBe(true)
          expect(got.max).toBe(Math.min(...got.series))
        },
      ),
    )
  })
})

describe('performanceStats', () => {
  it('returns nulls where a statistic needs more data', () => {
    const one = performanceStats(input(days(1), []))
    expect(one).toMatchObject({
      cumulativeReturn: 0,
      cagr: null,
      annualVol: null,
      sharpe: null,
      sortino: null,
      calmar: null,
    })
    expect(one.benchmark).toEqual({
      beta: null,
      alpha: null,
      trackingError: null,
      informationRatio: null,
      correlation: null,
    })
    const two = performanceStats(input(days(2), [-0.01]))
    expect(two.annualVol).toBeNull()
    expect(two.sharpe).toBeNull()
    expect(two.sortino).toBeCloseTo(-Math.sqrt(252), 12)
    expect(two.cagr).toBeCloseTo(Math.pow(0.99, 252) - 1, 12)
  })

  it('computes the textbook formulas', () => {
    const r = [0.01, -0.02, 0.03, 0.005]
    const rf = [0.0001, 0.0001, 0.0002, 0.0001]
    const b = [0.008, -0.01, 0.02, 0.0]
    const s = performanceStats(input(days(5), r, rf, b))
    const mean = (x: number[]) => x.reduce((a, c) => a + c, 0) / x.length
    const sd = (x: number[]) =>
      Math.sqrt(x.reduce((a, c) => a + (c - mean(x)) ** 2, 0) / (x.length - 1))
    const e = r.map((x, i) => x - (rf[i] ?? 0))
    const nav = 1.01 * 0.98 * 1.03 * 1.005
    expect(s.cumulativeReturn).toBeCloseTo(nav - 1, 14)
    expect(s.cagr).toBeCloseTo(Math.pow(nav, 252 / 4) - 1, 12)
    expect(s.annualVol).toBeCloseTo(sd(r) * Math.sqrt(252), 14)
    expect(s.sharpe).toBeCloseTo((mean(e) / sd(e)) * Math.sqrt(252), 12)
    const dd = Math.sqrt(
      e.reduce((a, c) => a + Math.min(c, 0) ** 2, 0) / e.length,
    )
    expect(s.sortino).toBeCloseTo((mean(e) / dd) * Math.sqrt(252), 12)
    expect(s.calmar).toBeCloseTo((s.cagr ?? NaN) / 0.02, 12)
    const x = b.map((v, i) => v - (rf[i] ?? 0))
    const cov =
      e.reduce((a, c, i) => a + (c - mean(e)) * ((x[i] ?? 0) - mean(x)), 0) / 3
    const beta = cov / sd(x) ** 2
    expect(s.benchmark.beta).toBeCloseTo(beta, 12)
    expect(s.benchmark.alpha).toBeCloseTo((mean(e) - beta * mean(x)) * 252, 12)
    const active = r.map((v, i) => v - (b[i] ?? 0))
    expect(s.benchmark.trackingError).toBeCloseTo(
      sd(active) * Math.sqrt(252),
      14,
    )
    expect(s.benchmark.informationRatio).toBeCloseTo(
      (mean(active) * 252) / (sd(active) * Math.sqrt(252)),
      12,
    )
    const corr =
      r.reduce((a, c, i) => a + (c - mean(r)) * ((b[i] ?? 0) - mean(b)), 0) /
      3 /
      (sd(r) * sd(b))
    expect(s.benchmark.correlation).toBeCloseTo(corr, 12)
  })

  it('reports null for ratios whose denominator is exactly zero', () => {
    const flat = performanceStats(
      input(
        days(6),
        [0.001, 0.001, 0.001, 0.001, 0.001],
        undefined,
        [0.001, 0.001, 0.001, 0.001, 0.001],
      ),
    )
    expect(flat.annualVol).toBe(0)
    expect(flat.sharpe).toBeNull()
    expect(flat.sortino).toBeNull()
    expect(flat.calmar).toBeNull()
    expect(flat.benchmark).toEqual({
      beta: null,
      alpha: null,
      trackingError: 0,
      informationRatio: null,
      correlation: null,
    })
  })

  it('splits calendar years at year ends, the first year measured from the start', () => {
    const d = ['2022-12-30', '2023-01-03', '2023-12-29', '2024-01-02']
    const s = performanceStats(input(d, [0.1, 0.2, -0.5]))
    expect(s.calendarYears).toEqual([
      { year: 2022, startDate: '2022-12-30', endDate: '2022-12-30', return: 0 },
      {
        year: 2023,
        startDate: '2023-01-03',
        endDate: '2023-12-29',
        return: near(1.1 * 1.2 - 1, 14),
      },
      {
        year: 2024,
        startDate: '2024-01-02',
        endDate: '2024-01-02',
        return: near(-0.5, 14),
      },
    ])
  })

  it('starts rolling 252-day series once 252 returns exist', () => {
    const r = Array.from(
      { length: ROLLING_WINDOW + 1 },
      (_, i) => (i % 3) * 0.001,
    )
    expect(
      performanceStats(
        input(days(ROLLING_WINDOW), r.slice(0, ROLLING_WINDOW - 1)),
      ).rolling.dates,
    ).toEqual([])
    const s = performanceStats(input(days(ROLLING_WINDOW + 2), r))
    expect(s.rolling.dates).toEqual(
      days(ROLLING_WINDOW + 2).slice(ROLLING_WINDOW),
    )
    const nav = input(days(ROLLING_WINDOW + 2), r).nav
    expect(s.rolling.return[1]).toBeCloseTo(
      (nav[ROLLING_WINDOW + 1] ?? NaN) / (nav[1] ?? NaN) - 1,
      14,
    )
  })

  it('rejects inconsistent lengths', () => {
    const ok = input(days(3), [0.1, 0.2])
    expect(() => performanceStats({ ...ok, rf: [0] })).toThrow(RangeError)
    expect(() => performanceStats({ ...ok, benchmarkReturns: [0] })).toThrow(
      RangeError,
    )
    expect(() => performanceStats({ ...ok, dates: days(2) })).toThrow(
      RangeError,
    )
    expect(() => performanceStats({ ...ok, nav: [1, 2] })).toThrow(RangeError)
  })
})
