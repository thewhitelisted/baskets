import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { driftWeights, portfolioGrowth } from './drift'
import { AnalyticsError } from './errors'
import {
  equalTargets,
  invvolTargets,
  mcapTargets,
  MIN_INVVOL_RETURNS,
  redistributeCustom,
  scaleToGross,
  sharesAsOf,
  signOf,
  trailingSigma,
  type Sign,
} from './targets'

// Input classes: one / many holdings; all long, all short, mixed; leverage at
// its bounds (tiny, 1, 10); raw weights all zero; invvol with every sigma
// known, some unknown (odd and even count of known -> median), none known,
// a zero sigma; trailing windows with 19 / 20 / lookback returns, nulls
// before a series starts, lookback longer than history; shares before the
// first known date, on a known date, between, after the last, "current"
// source, malformed entries; custom redistribution with removed long,
// removed short, removed zero, no same-sign survivor, several removed at once.

const signArb = fc.constantFrom<Sign>(1, -1)
const levArb = fc.double({ min: 1e-6, max: 10, noNaN: true })
// Weights are 0 or at least 1e-8 in magnitude: subnormal pools lose all precision.
const weightArb = fc
  .double({ min: -1, max: 1, noNaN: true })
  .map((x) => (Math.abs(x) < 1e-8 ? 0 : x))
const grossOf = (w: number[]) => w.reduce((a, b) => a + Math.abs(b), 0)

describe('signOf', () => {
  it('maps negative to -1 and zero or positive to 1', () => {
    expect([signOf(-0.1), signOf(0), signOf(-0), signOf(2)]).toEqual([
      -1, 1, 1, 1,
    ])
  })
})

describe('scaleToGross / equal / mcap', () => {
  it('scales any non-zero raw weights to sum |w| = leverage, keeping signs and proportions', () => {
    fc.assert(
      // Subnormal raw weights are excluded: scaling them can underflow to 0.
      fc.property(
        fc.array(
          fc
            .double({ min: -1e6, max: 1e6, noNaN: true })
            .filter((r) => r === 0 || Math.abs(r) > 1e-200),
          { minLength: 1, maxLength: 50 },
        ),
        levArb,
        (raw, lev) => {
          fc.pre(raw.some((r) => r !== 0))
          const w = scaleToGross(raw, lev)
          expect(grossOf(w)).toBeCloseTo(lev, 9)
          w.forEach((x, i) => {
            expect(Math.sign(x) === Math.sign(raw[i] ?? NaN)).toBe(true)
          })
        },
      ),
    )
  })

  it('throws zero_gross when every raw weight is 0', () => {
    expect(() => scaleToGross([0, 0], 1)).toThrow(AnalyticsError)
  })

  it('gives sign * leverage / n for equal weights', () => {
    expect(equalTargets([1, -1, 1, 1], 2)).toEqual([0.5, -0.5, 0.5, 0.5])
    fc.assert(
      fc.property(
        fc.array(signArb, { minLength: 1, maxLength: 50 }),
        levArb,
        (signs, lev) => {
          const w = equalTargets(signs, lev)
          expect(grossOf(w)).toBeCloseTo(lev, 9)
          w.forEach((x, i) => {
            expect(x).toBeCloseTo(((signs[i] ?? 0) * lev) / signs.length, 12)
          })
        },
      ),
    )
  })

  it('weights by sign * cap', () => {
    expect(mcapTargets([1, -1, 1], [100, 300, 0], 2)).toEqual([0.5, -1.5, 0])
    expect(() => mcapTargets([1], [1, 2], 1)).toThrow(RangeError)
  })
})

describe('invvolTargets', () => {
  it('weights by sign / sigma when every sigma is known', () => {
    const r = invvolTargets([1, -1], [0.1, 0.3], 1)
    expect(r.fallback).toEqual({ kind: 'none' })
    expect(r.weights[0]).toBeCloseTo(0.75, 15)
    expect(r.weights[1]).toBeCloseTo(-0.25, 15)
  })

  it('fills unknown sigmas with the median of the known ones (odd count)', () => {
    const r = invvolTargets([1, 1, 1, 1], [0.1, null, 0.2, 0.4], 1)
    expect(r.fallback).toEqual({ kind: 'median', indices: [1], sigma: 0.2 })
    expect(r.weights[1]).toBeCloseTo(r.weights[2] ?? NaN, 15)
  })

  it('uses the mean of the middle two for an even count of known sigmas', () => {
    const r = invvolTargets([1, 1, 1], [0.1, 0.3, null], 1)
    expect(r.fallback).toEqual({ kind: 'median', indices: [2], sigma: 0.2 })
  })

  it('falls back to equal weights when no sigma is known', () => {
    const r = invvolTargets([1, -1], [null, null], 3)
    expect(r).toEqual({ weights: [1.5, -1.5], fallback: { kind: 'equal' } })
  })

  it('keeps sum |w| = leverage in every branch', () => {
    const sigma = fc.option(fc.double({ min: 1e-4, max: 2, noNaN: true }), {
      nil: null,
    })
    fc.assert(
      fc.property(
        fc.array(fc.tuple(signArb, sigma), { minLength: 1, maxLength: 30 }),
        levArb,
        (pairs, lev) => {
          const w = invvolTargets(
            pairs.map((p) => p[0]),
            pairs.map((p) => p[1]),
            lev,
          ).weights
          expect(grossOf(w)).toBeCloseTo(lev, 9)
        },
      ),
    )
  })

  it('asserts sigmas are positive and lengths match', () => {
    expect(() => invvolTargets([1], [0], 1)).toThrow(RangeError)
    expect(() => invvolTargets([1], [-0.1], 1)).toThrow(RangeError)
    expect(() => invvolTargets([1, 1], [0.1], 1)).toThrow(RangeError)
  })
})

describe('trailingSigma', () => {
  const series = (n: number, lead = 0) => [
    ...Array<null>(lead).fill(null),
    ...Array.from({ length: n }, (_, i) => (i % 2 === 0 ? 0.01 : -0.01)),
  ]

  it(`is null below ${String(MIN_INVVOL_RETURNS)} returns and defined at exactly ${String(MIN_INVVOL_RETURNS)}`, () => {
    const r = series(MIN_INVVOL_RETURNS, 5)
    expect(trailingSigma(r, r.length - 2, 63)).toEqual({
      sigma: null,
      count: MIN_INVVOL_RETURNS - 1,
    })
    const at20 = trailingSigma(r, r.length - 1, 63)
    expect(at20.count).toBe(MIN_INVVOL_RETURNS)
    expect(at20.sigma).toBeCloseTo(Math.sqrt((20 * 0.0001) / 19), 15)
  })

  it('uses only the lookback window ending at t', () => {
    const r = [...Array<number>(30).fill(0.05), ...series(40)]
    expect(trailingSigma(r, r.length - 1, 40).sigma).toBeCloseTo(
      Math.sqrt((40 * 0.0001) / 39),
      15,
    )
    expect(trailingSigma(r, r.length - 1, 41).sigma).not.toBeCloseTo(
      Math.sqrt((40 * 0.0001) / 39),
      6,
    )
  })

  it('ignores data after t', () => {
    const r = [...series(25), 10, 10]
    expect(trailingSigma(r, 24, 63)).toEqual(trailingSigma(series(25), 24, 63))
  })

  it('reports 0 for a constant series', () => {
    expect(trailingSigma(Array<number>(25).fill(0), 24, 63).sigma).toBe(0)
  })
})

describe('sharesAsOf', () => {
  const edgar = {
    source: 'edgar' as const,
    knownDates: ['2020-01-10', '2020-06-10'],
    shares: [100, 200],
  }
  it.each([
    ['2020-01-09', 100, true],
    ['2020-01-10', 100, false],
    ['2020-03-01', 100, false],
    ['2020-06-10', 200, false],
    ['2030-01-01', 200, false],
  ])('on %s uses %d (lookAhead %s)', (date, shares, lookAhead) => {
    expect(sharesAsOf(edgar, date)).toEqual({ shares, lookAhead })
  })
  it('always flags source "current" as look-ahead', () => {
    expect(
      sharesAsOf(
        { source: 'current', knownDates: ['2026-11-20'], shares: [5] },
        '2030-01-01',
      ),
    ).toEqual({ shares: 5, lookAhead: true })
  })
  it('rejects malformed entries', () => {
    expect(() =>
      sharesAsOf({ source: 'edgar', knownDates: [], shares: [] }, '2020-01-01'),
    ).toThrow(AnalyticsError)
    expect(() =>
      sharesAsOf(
        { source: 'edgar', knownDates: ['2020-01-01'], shares: [] },
        '2020-01-01',
      ),
    ).toThrow(AnalyticsError)
  })
})

describe('redistributeCustom', () => {
  it('gives a removed long weight to surviving longs pro rata by |w|', () => {
    const got = redistributeCustom(
      [0.2, 0.3, 0.1, -0.4],
      [true, false, false, false],
    )
    expect(got[0]).toBe(0)
    expect(got[1]).toBeCloseTo(0.3 + 0.2 * 0.75, 15)
    expect(got[2]).toBeCloseTo(0.1 + 0.2 * 0.25, 15)
    expect(got[3]).toBe(-0.4)
  })

  it('gives a removed short weight to surviving shorts', () => {
    const got = redistributeCustom(
      [0.5, -0.1, -0.3, -0.1],
      [false, true, false, false],
    )
    expect(got).toEqual([
      0.5,
      0,
      expect.closeTo(-0.375, 15),
      expect.closeTo(-0.125, 15),
    ])
  })

  it('leaves the weight as cash when no survivor has that sign, and ignores zero-weight survivors', () => {
    expect(redistributeCustom([-0.2, 0.5, 0], [true, false, false])).toEqual([
      0, 0.5, 0,
    ])
    expect(redistributeCustom([0.2, 0, -0.1], [true, false, false])).toEqual([
      0, 0, -0.1,
    ])
  })

  it('preserves the net weight of each side that has survivors, whatever is removed', () => {
    const w = weightArb
    fc.assert(
      fc.property(
        fc.array(fc.tuple(w, fc.boolean()), { minLength: 1, maxLength: 20 }),
        (pairs) => {
          const weights = pairs.map((p) => p[0])
          const removed = pairs.map((p) => p[1])
          const got = redistributeCustom(weights, removed)
          for (const side of [1, -1]) {
            const before = weights
              .filter((x) => x * side > 0)
              .reduce((a, b) => a + b, 0)
            const survivors = weights.filter(
              (x, i) => x * side > 0 && !removed[i],
            )
            const after = got
              .filter((x) => x * side > 0)
              .reduce((a, b) => a + b, 0)
            if (survivors.length > 0) expect(after).toBeCloseTo(before, 12)
          }
          got.forEach((x, i) => {
            if (removed[i]) expect(x).toBe(0)
            else expect(Math.sign(x)).toBe(Math.sign(weights[i] ?? NaN))
          })
        },
      ),
    )
  })

  it('gives the same result as removing slots one at a time', () => {
    fc.assert(
      fc.property(
        fc.array(weightArb, {
          minLength: 2,
          maxLength: 12,
        }),
        fc.array(fc.boolean(), { minLength: 12, maxLength: 12 }),
        (weights, mask) => {
          const removed = weights.map((_, i) => mask[i] ?? false)
          let seq = [...weights]
          removed.forEach((r, i) => {
            if (r)
              seq = redistributeCustom(
                seq,
                seq.map((_, j) => j === i),
              )
          })
          const all = redistributeCustom(weights, removed)
          all.forEach((x, i) => {
            expect(x).toBeCloseTo(seq[i] ?? NaN, 12)
          })
        },
      ),
    )
  })
})

describe('drift', () => {
  const finite = fc.double({ min: -2, max: 2, noNaN: true })
  const ret = fc.double({ min: -1, max: 1, noNaN: true })

  it('computes 1 + sum w r + cash rf', () => {
    expect(portfolioGrowth([0.5, -0.2], 0.7, [0.1, 0.05], 0.001)).toBeCloseTo(
      1 + 0.05 - 0.01 + 0.0007,
      15,
    )
    expect(() => portfolioGrowth([1], 0, [], 0)).toThrow(RangeError)
  })

  it('does not cancel to noise near a total loss', () => {
    const w = 0.9999999999999988
    expect(portfolioGrowth([w], 1 - w, [-1], 0)).toBe(1 - w)
    expect(driftWeights([w], 1 - w, [-1], 0)).toEqual({ weights: [0], cash: 1 })
  })

  it('keeps sum w + cash = 1 and matches a value-based reference', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(finite, ret), { maxLength: 20 }),
        fc.double({ min: -0.001, max: 0.001, noNaN: true }),
        (pairs, rf) => {
          const w = pairs.map((p) => p[0])
          const r = pairs.map((p) => p[1])
          const cash = 1 - w.reduce((a, b) => a + b, 0)
          const growth = portfolioGrowth(w, cash, r, rf)
          const rp = w.reduce((acc, x, i) => acc + x * (r[i] ?? NaN), cash * rf)
          expect(growth).toBeCloseTo(1 + rp, 12)
          fc.pre(growth > 1e-6)
          const d = driftWeights(w, cash, r, rf)
          expect(d.weights.reduce((a, b) => a + b, 0) + d.cash).toBeCloseTo(
            1,
            9,
          )
          // Value-based: hold w_i of 1 unit of NAV, grow each by its return.
          const values = w.map((x, i) => x * (1 + (r[i] ?? NaN)))
          const total = values.reduce((a, b) => a + b, 0) + cash * (1 + rf)
          d.weights.forEach((x, i) => {
            expect(x).toBeCloseTo((values[i] ?? NaN) / total, 9)
          })
        },
      ),
    )
  })

  it('refuses to drift through a non-positive portfolio value', () => {
    expect(() => driftWeights([2], -1, [-0.5], 0)).toThrow(RangeError)
    expect(() => driftWeights([3], -2, [-0.5], 0)).toThrow(RangeError)
  })
})
