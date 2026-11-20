import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { near } from './testing'
import {
  exposures,
  riskEffectiveN,
  UNCLASSIFIED,
  weightConcentration,
  type ExposureMeta,
} from './concentration'
import { AnalyticsError } from './errors'
import {
  basketRisk,
  correlationFromCovariance,
  decomposeRisk,
  ewmaCovariance,
  ledoitWolf,
  sampleCovarianceMatrix,
} from './risk'

// Input classes (risk): 1 and many holdings; long-only, long/short, zero
// weights; zero-variance holdings (correlation undefined), all-zero returns
// (sigma_p = 0); 1, 2 and many observations; history shorter than the window;
// missing series. Magnitudes are 0 or >= 1e-8: products of smaller values
// underflow to subnormals, where no floating-point identity holds. Concentration: all-zero weights, one holding, equal
// weights, more and fewer than 5 / 10 holdings, negative weights, negative
// %CTR (hedges). Exposures: fund look-through (sums below and above 1),
// missing sector / country, invalid fund weights, missing metadata.

const normal = (max: number) =>
  fc
    .double({ min: -max, max, noNaN: true })
    .map((x) => (Math.abs(x) < 1e-8 ? 0 : x))
const rowsArb = (p: number) =>
  fc.array(fc.array(normal(0.1), { minLength: p, maxLength: p }), {
    minLength: 3,
    maxLength: 40,
  })

const casesArb = fc.integer({ min: 1, max: 6 }).chain((p) =>
  fc.record({
    rows: rowsArb(p),
    weights: fc.array(normal(2), { minLength: p, maxLength: p }),
    estimator: fc.constantFrom('sample', 'ledoit_wolf', 'ewma'),
  }),
)

const estimate = (rows: number[][], e: string) =>
  e === 'sample'
    ? sampleCovarianceMatrix(rows)
    : e === 'ewma'
      ? ewmaCovariance(rows)
      : ledoitWolf(rows).covariance

describe('covariance estimators', () => {
  it('sample covariance uses denominator T - 1', () => {
    expect(
      sampleCovarianceMatrix([
        [1, 2],
        [3, 6],
        [5, 10],
      ]),
    ).toEqual([
      [4, 8],
      [8, 16],
    ])
  })

  it('EWMA weights the newest row most and normalizes the weights', () => {
    const lam = 0.94
    const got = ewmaCovariance([[1], [2]])
    const w0 = (lam * (1 - lam)) / (1 - lam * lam)
    const w1 = (1 - lam) / (1 - lam * lam)
    expect(got[0]?.[0]).toBeCloseTo(w0 * 1 + w1 * 4, 15)
    expect(
      ewmaCovariance([
        [0.5, -1],
        [0.5, -1],
        [0.5, -1],
      ]),
    ).toEqual([
      [near(0.25, 15), near(-0.5, 15)],
      [near(-0.5, 15), near(1, 15)],
    ])
  })

  it('Ledoit-Wolf: one feature has zero shrinkage and denominator T', () => {
    expect(ledoitWolf([[1], [3]])).toEqual({ covariance: [[1]], shrinkage: 0 })
  })

  it('Ledoit-Wolf: shrinkage in [0, 1], trace preserved, all-equal data gives 0', () => {
    fc.assert(
      fc.property(fc.integer({ min: 2, max: 5 }).chain(rowsArb), (rows) => {
        const { covariance, shrinkage } = ledoitWolf(rows)
        expect(shrinkage).toBeGreaterThanOrEqual(0)
        expect(shrinkage).toBeLessThanOrEqual(1)
        const s = sampleCovarianceMatrix(rows).map((r) =>
          r.map((v) => (v * (rows.length - 1)) / rows.length),
        )
        const tr = (m: number[][]) => m.reduce((a, r, i) => a + (r[i] ?? 0), 0)
        expect(tr(covariance)).toBeCloseTo(tr(s), 12)
      }),
    )
    expect(
      ledoitWolf([
        [1, 1],
        [1, 1],
      ]).shrinkage,
    ).toBe(0)
  })

  it('needs at least 2 rows of equal non-zero length', () => {
    expect(() => sampleCovarianceMatrix([[1]])).toThrow(AnalyticsError)
    expect(() => ledoitWolf([[1, 2]])).toThrow(AnalyticsError)
    expect(() => ewmaCovariance([[1, 2], [1]])).toThrow(RangeError)
    expect(() => sampleCovarianceMatrix([[], []])).toThrow(RangeError)
  })
})

describe('decomposeRisk', () => {
  it('CTRs sum to sigma_p and %CTRs to 1 for every estimator', () => {
    fc.assert(
      fc.property(casesArb, ({ rows, weights, estimator }) => {
        const cov = estimate(rows, estimator)
        const d = decomposeRisk(cov, weights)
        if (d.contributions === null) {
          expect(d.sigmaP).toBe(0)
          expect(d.diversificationRatio).toBeNull()
          return
        }
        const sum = (x: number[]) => x.reduce((a, b) => a + b, 0)
        expect(sum(d.contributions.ctr)).toBeCloseTo(d.sigmaP, 12)
        expect(sum(d.contributions.pctCtr)).toBeCloseTo(1, 9)
        expect(d.sigmaP).toBeCloseTo(
          Math.sqrt(
            weights.reduce(
              (a, wi, i) =>
                a +
                wi *
                  weights.reduce((b, wj, j) => b + (cov[i]?.[j] ?? 0) * wj, 0),
              0,
            ),
          ),
          12,
        )
        // Diversification ratio >= 1 for a PSD covariance (triangle inequality on volatilities).
        expect(d.diversificationRatio ?? NaN).toBeGreaterThanOrEqual(1 - 1e-9)
      }),
    )
  })

  it('handles sigma_p = 0 explicitly', () => {
    const d = decomposeRisk(
      [
        [0, 0],
        [0, 0.04],
      ],
      [1, 0],
    )
    expect(d).toEqual({
      sigmaP: 0,
      vols: [0, 0.2],
      contributions: null,
      diversificationRatio: null,
    })
  })

  it('treats a perfectly hedged pair as sigma_p = 0', () => {
    const cov = [
      [0.04, 0.04],
      [0.04, 0.04],
    ]
    expect(decomposeRisk(cov, [1, -1]).contributions).toBeNull()
  })

  it('rejects a matrix that is not PSD', () => {
    expect(() =>
      decomposeRisk(
        [
          [1, 2],
          [2, 1],
        ],
        [1, -1],
      ),
    ).toThrow(/not PSD/)
  })

  it('rejects mismatched sizes', () => {
    expect(() => decomposeRisk([[1]], [1, 2])).toThrow(RangeError)
  })
})

describe('correlationFromCovariance', () => {
  it('is 1 on the diagonal and null where a variance is 0', () => {
    expect(
      correlationFromCovariance([
        [4, 2, 0],
        [2, 9, 0],
        [0, 0, 0],
      ]),
    ).toEqual([
      [1, 2 / 6, null],
      [2 / 6, 1, null],
      [null, null, null],
    ])
  })
})

describe('basketRisk', () => {
  const cal = [
    '2024-01-01',
    '2024-01-02',
    '2024-01-03',
    '2024-01-04',
    '2024-01-05',
  ]
  const series = {
    A: { dates: cal, close: [1, 1.1, 1.0, 1.2, 1.1] },
    B: { dates: cal.slice(2), close: [1, 0.9, 1.0] },
  }

  it('uses the trailing window and annualizes by 252', () => {
    const r = basketRisk({
      calendar: cal,
      series,
      weights: { A: 1 },
      estimator: 'sample',
      window: 2,
    })
    expect(r.observations).toBe(2)
    expect(r.startDate).toBe(cal[3])
    const rets = [1.2 / 1.0 - 1, 1.1 / 1.2 - 1]
    const [r1 = NaN, r2 = NaN] = rets
    const mean = (r1 + r2) / 2
    const v = ((r1 - mean) ** 2 + (r2 - mean) ** 2) * 252
    expect(r.covariance).toEqual([[near(v, 12)]])
    expect(r.sigmaP).toBeCloseTo(Math.sqrt(v), 12)
    expect(r.shrinkage).toBeNull()
  })

  it('shortens the window to the history every holding has and reports it', () => {
    const r = basketRisk({
      calendar: cal,
      series,
      weights: { A: 0.5, B: -0.5 },
      estimator: 'ledoit_wolf',
    })
    expect(r.observations).toBe(2)
    expect(r.startDate).toBe(cal[3])
    expect(r.ids).toEqual(['A', 'B'])
    expect(typeof r.shrinkage).toBe('number')
  })

  it('fails loudly without enough data, holdings or series', () => {
    expect(() =>
      basketRisk({
        calendar: cal,
        series,
        weights: { A: 1 },
        estimator: 'ewma',
        window: 1,
      }),
    ).toThrow(RangeError)
    expect(() =>
      basketRisk({
        calendar: cal.slice(0, 2),
        series,
        weights: { A: 1 },
        estimator: 'ewma',
      }),
    ).toThrow(AnalyticsError)
    expect(() =>
      basketRisk({ calendar: cal, series, weights: {}, estimator: 'sample' }),
    ).toThrow(/no holdings/)
    expect(() =>
      basketRisk({
        calendar: cal,
        series,
        weights: { C: 1 },
        estimator: 'sample',
      }),
    ).toThrow(/no series/)
  })
})

describe('weightConcentration', () => {
  it('is null when every weight is 0', () => {
    expect(weightConcentration([])).toBeNull()
    expect(weightConcentration([0, 0])).toBeNull()
  })

  it('uses shares of gross exposure, so shorts count by magnitude', () => {
    expect(weightConcentration([0.5, -0.5])).toEqual({
      gross: 1,
      hhi: 0.5,
      effectiveN: 2,
      top5: 1,
      top10: 1,
    })
    const twelve = Array.from({ length: 12 }, (_, i) => i + 1)
    const c = weightConcentration(twelve)
    expect(c?.top5).toBeCloseTo((12 + 11 + 10 + 9 + 8) / 78, 15)
    expect(c?.top10).toBeCloseTo((78 - 1 - 2) / 78, 15)
  })

  it('satisfies 1/n <= HHI <= 1 and effective N = 1/HHI', () => {
    fc.assert(
      fc.property(
        fc.array(fc.double({ min: -5, max: 5, noNaN: true }), {
          minLength: 1,
          maxLength: 50,
        }),
        (w) => {
          const c = weightConcentration(w)
          if (c === null) return
          const n = w.filter((x) => x !== 0).length
          expect(c.hhi).toBeGreaterThanOrEqual(1 / n - 1e-12)
          expect(c.hhi).toBeLessThanOrEqual(1 + 1e-12)
          expect(c.effectiveN * c.hhi).toBeCloseTo(1, 12)
          expect(c.top5).toBeLessThanOrEqual(c.top10 + 1e-15)
          expect(c.top10).toBeLessThanOrEqual(1 + 1e-12)
        },
      ),
    )
  })
})

describe('riskEffectiveN', () => {
  it('is n for equal contributions and drops below 1 with a hedge', () => {
    expect(riskEffectiveN([0.25, 0.25, 0.25, 0.25])).toBe(4)
    expect(riskEffectiveN([1.5, -0.5])).toBeCloseTo(0.4, 15)
  })

  it('lies in (0, n] whenever the %CTRs sum to 1', () => {
    fc.assert(
      fc.property(
        fc.array(fc.double({ min: -3, max: 3, noNaN: true }), {
          minLength: 1,
          maxLength: 20,
        }),
        (xs) => {
          const pct = [...xs, 1 - xs.reduce((a, b) => a + b, 0)]
          const n = riskEffectiveN(pct)
          expect(n).toBeGreaterThan(0)
          expect(n).toBeLessThanOrEqual(pct.length * (1 + 1e-9))
        },
      ),
    )
    expect(() => riskEffectiveN([])).toThrow(RangeError)
  })
})

describe('exposures', () => {
  const meta = (m: Partial<ExposureMeta>): ExposureMeta => ({
    assetClass: 'equity',
    sector: null,
    country: null,
    currency: 'USD',
    fundSectorWeights: null,
    ...m,
  })

  it('splits net and gross by asset class, sector (with fund look-through), country and currency', () => {
    const got = exposures(
      { A: 0.5, ETF: 0.4, S: -0.3 },
      {
        A: meta({ sector: 'Tech', country: 'US' }),
        ETF: meta({
          assetClass: 'multi_asset',
          fundSectorWeights: { Tech: 0.5, Energy: 0.3 },
        }),
        S: meta({ sector: 'Energy', country: 'JP', currency: 'JPY' }),
      },
    )
    expect(got.assetClass).toEqual([
      { key: 'equity', net: 0.2, gross: 0.8 },
      { key: 'multi_asset', net: 0.4, gross: 0.4 },
    ])
    expect(got.sector).toEqual([
      { key: 'Tech', net: 0.75, gross: 0.75 },
      {
        key: 'Energy',
        net: near(-0.15, 15),
        gross: near(0.45, 15),
      },
    ])
    expect(got.country).toEqual([
      { key: 'US', net: 0.5, gross: 0.5 },
      { key: UNCLASSIFIED, net: 0.4, gross: 0.4 },
      { key: 'JP', net: -0.3, gross: 0.3 },
    ])
    expect(got.currency).toEqual([
      { key: 'USD', net: 0.9, gross: 0.9 },
      { key: 'JPY', net: -0.3, gross: 0.3 },
    ])
  })

  it('puts a holding with no sector and no fund weights in Unclassified', () => {
    expect(exposures({ X: 1 }, { X: meta({}) }).sector).toEqual([
      { key: UNCLASSIFIED, net: 1, gross: 1 },
    ])
  })

  it('preserves total net and gross exposure in every dimension', () => {
    const w = { A: 0.3, B: -0.7, C: 1.1 }
    const got = exposures(w, {
      A: meta({ fundSectorWeights: { X: 0.2, Y: 0.9 } }),
      B: meta({ sector: 'X' }),
      C: meta({}),
    })
    for (const dim of Object.values(got)) {
      expect(dim.reduce((a, e) => a + e.net, 0)).toBeCloseTo(0.7, 14)
      expect(dim.reduce((a, e) => a + e.gross, 0)).toBeCloseTo(2.1, 14)
    }
  })

  it('fails loudly on missing metadata or invalid fund weights', () => {
    expect(() => exposures({ A: 1 }, {})).toThrow(AnalyticsError)
    expect(() =>
      exposures({ A: 1 }, { A: meta({ fundSectorWeights: {} }) }),
    ).toThrow(/fundSectorWeights/)
    expect(() =>
      exposures(
        { A: 1 },
        { A: meta({ fundSectorWeights: { X: -0.1, Y: 1 } }) },
      ),
    ).toThrow(/fundSectorWeights/)
    expect(() =>
      exposures({ A: 1 }, { A: meta({ fundSectorWeights: { X: NaN } }) }),
    ).toThrow(/fundSectorWeights/)
  })
})
