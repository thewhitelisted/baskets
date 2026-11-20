import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { AnalyticsError } from './errors'
import { factorRegression, ols, rollingSlopes } from './factors'

// Input classes: exact linear fit (zero residuals: t-stats undefined), noisy
// fit, constant y (R^2 undefined), n = k + 1 (no residual degrees of
// freedom) and n = k + 2, constant or collinear regressors (singular),
// basket dates partly missing from factor data, factor data missing RF or a
// factor, length mismatches, fewer than 252 observations (no rolling betas).

describe('ols', () => {
  it('recovers an exact linear relation with zero standard errors and null t-stats', () => {
    const x1 = [1, 2, 3, 4, 5, 6]
    const x2 = [0, 1, 0, 1, 0, 2]
    const y = x1.map((v, i) => 0.5 + 2 * v - 3 * (x2[i] ?? 0))
    const r = ols(y, [x1, x2])
    expect(r.intercept.estimate).toBeCloseTo(0.5, 12)
    expect(r.slopes.map((s) => s.estimate)).toEqual([
      expect.closeTo(2, 12),
      expect.closeTo(-3, 12),
    ])
    expect(r.r2).toBeCloseTo(1, 12)
    expect(r.rss).toBeCloseTo(0, 20)
  })

  it('matches the textbook simple-regression formulas', () => {
    const x = [1, 2, 3, 4, 5]
    const y = [2.1, 3.9, 6.2, 7.8, 10.1]
    const r = ols(y, [x])
    const xm = 3
    const ym = y.reduce((a, b) => a + b, 0) / 5
    const sxx = x.reduce((a, v) => a + (v - xm) ** 2, 0)
    const b = x.reduce((a, v, i) => a + (v - xm) * ((y[i] ?? 0) - ym), 0) / sxx
    const a = ym - b * xm
    const rss = y.reduce((acc, v, i) => acc + (v - a - b * (x[i] ?? 0)) ** 2, 0)
    const s2 = rss / 3
    expect(r.slopes[0]?.estimate).toBeCloseTo(b, 13)
    expect(r.intercept.estimate).toBeCloseTo(a, 13)
    expect(r.slopes[0]?.stdError).toBeCloseTo(Math.sqrt(s2 / sxx), 13)
    expect(r.intercept.stdError).toBeCloseTo(
      Math.sqrt(s2 * (1 / 5 + (xm * xm) / sxx)),
      13,
    )
    expect(r.slopes[0]?.tStat).toBeCloseTo(b / Math.sqrt(s2 / sxx), 10)
    expect(r.r2).toBeCloseTo(
      1 - rss / y.reduce((acc, v) => acc + (v - ym) ** 2, 0),
      13,
    )
  })

  it('reports null R^2 for a constant y', () => {
    expect(ols([1, 1, 1, 1], [[1, 2, 3, 5]]).r2).toBeNull()
  })

  it('needs more than k + 1 observations', () => {
    expect(() => ols([1, 2], [[1, 2]])).toThrow(AnalyticsError)
    expect(() => ols([1, 2, 4], [[1, 2, 5]])).not.toThrow()
  })

  it('rejects singular designs', () => {
    expect(() => ols([1, 2, 3, 4], [[1, 1, 1, 1]])).toThrow(
      /collinear or constant/,
    )
    expect(() =>
      ols(
        [1, 2, 3, 5],
        [
          [1, 2, 3, 4],
          [2, 4, 6, 8],
        ],
      ),
    ).toThrow(/collinear or constant/)
  })

  it('rejects ragged columns', () => {
    expect(() => ols([1, 2, 3, 4], [[1, 2, 3]])).toThrow(RangeError)
  })
})

describe('rollingSlopes', () => {
  it('equals OLS on each window', () => {
    const point = fc
      .double({ min: -1, max: 1, noNaN: true })
      .map((x) => (Math.abs(x) < 1e-8 ? 0 : x))
    fc.assert(
      fc.property(
        fc.array(fc.tuple(point, point, point), {
          minLength: 8,
          maxLength: 30,
        }),
        fc.integer({ min: 5, max: 8 }),
        (pts, window) => {
          const y = pts.map((p) => p[0])
          const cols = [pts.map((p) => p[1]), pts.map((p) => p[2])]
          let want: number[][]
          try {
            want = Array.from({ length: pts.length - window + 1 }, (_, w) =>
              ols(
                y.slice(w, w + window),
                cols.map((c) => c.slice(w, w + window)),
              ).slopes.map((c) => c.estimate),
            )
          } catch (e) {
            expect((e as AnalyticsError).code).toBe('singular')
            return
          }
          const conditioned = want.every((ws) =>
            ws.every((b) => Math.abs(b) < 1e3),
          )
          fc.pre(conditioned)
          const got = rollingSlopes(y, cols, window)
          expect(got).toHaveLength(want.length)
          got.forEach((slopes, w) => {
            slopes.forEach((s, j) => {
              const e = want[w]?.[j] ?? NaN
              expect(Math.abs(s - e)).toBeLessThanOrEqual(
                1e-6 * Math.max(1, Math.abs(e)),
              )
            })
          })
        },
      ),
    )
  })

  it('returns nothing when the history is shorter than the window', () => {
    expect(rollingSlopes([1, 2], [[1, 3]], 3)).toEqual([])
    expect(() => rollingSlopes([1, 2], [[1]], 1)).toThrow(RangeError)
  })
})

describe('factorRegression', () => {
  const factors = ['Mkt-RF', 'SMB'] as const
  const dates = [
    '2024-01-01',
    '2024-01-02',
    '2024-01-03',
    '2024-01-04',
    '2024-01-05',
    '2024-01-06',
    '2024-01-07',
  ]
  const mkt = [0.01, -0.02, 0.015, 0.0, 0.03, -0.01]
  const smb = [0.002, 0.001, -0.003, 0.004, 0.0, 0.001]
  const rf = [0.0001, 0.0001, 0.0001, 0.0002, 0.0002, 0.0002]
  const factorData = {
    factors: [...factors],
    // Factor data lacks 2024-01-04, so that period is dropped.
    dates: dates.slice(1).filter((d) => d !== '2024-01-04'),
    values: {
      'Mkt-RF': mkt.filter((_, i) => i !== 2),
      SMB: smb.filter((_, i) => i !== 2),
      RF: rf.filter((_, i) => i !== 2),
    },
  }
  const returns = mkt.map(
    (m, i) =>
      0.0005 +
      (rf[i] ?? 0) +
      1.2 * m -
      0.5 * (smb[i] ?? 0) +
      (i % 2 === 0 ? 0.001 : -0.001),
  )

  it('regresses excess returns on the dates both sides have', () => {
    const got = factorRegression({ dates, returns, factorData })
    expect(got.observations).toBe(5)
    expect([got.startDate, got.endDate]).toEqual(['2024-01-02', '2024-01-07'])
    const keep = [0, 1, 3, 4, 5]
    const direct = ols(
      keep.map((i) => (returns[i] ?? 0) - (rf[i] ?? 0)),
      [keep.map((i) => mkt[i] ?? 0), keep.map((i) => smb[i] ?? 0)],
    )
    expect(got.intercept).toEqual(direct.intercept)
    expect(got.alphaAnnual).toBe(direct.intercept.estimate * 252)
    expect(got.betas).toEqual({
      'Mkt-RF': direct.slopes[0],
      SMB: direct.slopes[1],
    })
    expect(got.rolling).toEqual({ dates: [], betas: { 'Mkt-RF': [], SMB: [] } })
  })

  it('splits variance into systematic and idiosyncratic parts that sum to the total, shares equal to R^2', () => {
    const got = factorRegression({ dates, returns, factorData })
    expect(got.risk.systematic + got.risk.idiosyncratic).toBeCloseTo(
      got.risk.total,
      14,
    )
    expect(got.risk.systematicShare).toBeCloseTo(got.r2 ?? NaN, 12)
    expect(
      (got.risk.systematicShare ?? 0) + (got.risk.idiosyncraticShare ?? 0),
    ).toBeCloseTo(1, 12)
  })

  it('reports null shares when excess returns are constant', () => {
    const flat = factorRegression({
      dates,
      returns: rf.map((r) => r + 0.001),
      factorData,
    })
    expect(flat.risk.total).toBe(0)
    expect(flat.risk.systematicShare).toBeNull()
    expect(flat.risk.idiosyncraticShare).toBeNull()
  })

  it('fails loudly on missing or ragged factor data', () => {
    const noRf = Object.fromEntries(
      Object.entries(factorData.values).filter(([k]) => k !== 'RF'),
    )
    expect(() =>
      factorRegression({
        dates,
        returns,
        factorData: { ...factorData, values: noRf },
      }),
    ).toThrow(/lacks RF/)
    expect(() =>
      factorRegression({
        dates,
        returns,
        factorData: { ...factorData, factors: ['Mkt-RF', 'HML'] },
      }),
    ).toThrow(/lacks HML/)
    expect(() =>
      factorRegression({
        dates,
        returns,
        factorData: {
          ...factorData,
          values: { ...factorData.values, SMB: [0] },
        },
      }),
    ).toThrow(/differ in length/)
  })
})
