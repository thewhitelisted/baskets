import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { alignToCalendar, resolveStartIndex, validateSeries } from './align'
import { AnalyticsError } from './errors'
import { alignOverlay } from './overlay'
import { simpleReturn, simpleReturns } from './returns'
import { alignDailyRf, dailyRfFromAnnual } from './rf'

// Input classes: series starting before / on / after the calendar start,
// ending before the calendar end, observations on non-calendar dates
// (weekends, holidays), observations after the last calendar date, a single
// observation, empty calendar; invalid series (length mismatch, empty,
// unsorted, duplicate dates, bad date strings, NaN / Infinity, negative
// prices where forbidden); prices 0 -> x, x -> 0, 0 -> 0; rf at -1, below
// -1, 0, negative, large.

const cal = ['2024-01-05', '2024-01-08', '2024-01-09', '2024-01-10']

describe('alignToCalendar', () => {
  it('forward-fills as of each calendar date and folds weekend moves into Monday', () => {
    const dates = ['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-09']
    expect(alignToCalendar(cal, dates, [1, 2, 3, 4])).toEqual([1, 3, 4, 4])
  })

  it('is null before the first observation and ignores observations after the calendar', () => {
    expect(alignToCalendar(cal, ['2024-01-09', '2024-02-01'], [7, 8])).toEqual([
      null,
      null,
      7,
      7,
    ])
  })

  it('carries the last value of a series that ended', () => {
    expect(alignToCalendar(cal, ['2024-01-01', '2024-01-08'], [5, 6])).toEqual([
      5, 6, 6, 6,
    ])
  })

  it('handles an empty calendar', () => {
    expect(alignToCalendar([], ['2024-01-01'], [1])).toEqual([])
  })

  it('agrees with a brute-force as-of lookup', () => {
    const day = fc
      .integer({ min: 0, max: 60 })
      .map((n) => new Date(Date.UTC(2024, 0, 1 + n)).toISOString().slice(0, 10))
    const sortedUnique = fc
      .uniqueArray(day, { maxLength: 40 })
      .map((xs) => [...xs].sort())
    fc.assert(
      fc.property(
        sortedUnique,
        fc
          .uniqueArray(day, { minLength: 1, maxLength: 40 })
          .map((xs) => [...xs].sort()),
        (calendar, dates) => {
          const values = dates.map((_, i) => i * 1.5)
          const got = alignToCalendar(calendar, dates, values)
          calendar.forEach((c, i) => {
            let want: number | null = null
            dates.forEach((d, j) => {
              if (d <= c) want = values[j] ?? null
            })
            expect(got[i]).toBe(want)
          })
        },
      ),
    )
  })
})

describe('validateSeries', () => {
  it('accepts a valid series', () => {
    expect(() => {
      validateSeries('X', ['2024-01-01', '2024-01-02'], [0, 1], {
        nonNegative: true,
      })
    }).not.toThrow()
  })
  it.each([
    ['length mismatch', ['2024-01-01'], [1, 2], true, /differ in length/],
    ['empty series', [], [], true, /empty/],
    [
      'unsorted dates',
      ['2024-01-02', '2024-01-01'],
      [1, 2],
      true,
      /not strictly ascending/,
    ],
    [
      'duplicate dates',
      ['2024-01-01', '2024-01-01'],
      [1, 2],
      true,
      /not strictly ascending/,
    ],
    ['bad date', ['2024-1-01'], [1], true, /bad date/],
    ['NaN value', ['2024-01-01'], [NaN], true, /non-finite/],
    ['infinite value', ['2024-01-01'], [Infinity], false, /non-finite/],
    ['negative price', ['2024-01-01'], [-1], true, /negative price/],
  ])('rejects %s', (_, dates, values, nonNegative, message) => {
    expect(() => {
      validateSeries('X', dates, values, { nonNegative })
    }).toThrow(message)
  })
  it('allows negative values when not prices', () => {
    expect(() => {
      validateSeries('X', ['2024-01-01'], [-1], { nonNegative: false })
    }).not.toThrow()
  })
})

describe('resolveStartIndex', () => {
  const a = { id: 'A', values: [null, 1, 1, 1] }
  const b = { id: 'B', values: [null, null, 2, 2] }
  it('defaults to the first date on which every column has data', () => {
    expect(resolveStartIndex(cal, [a, b], null)).toBe(2)
  })
  it('uses the first calendar date on or after an explicit start', () => {
    expect(resolveStartIndex(cal, [a, b], '2024-01-09')).toBe(2)
    expect(resolveStartIndex(cal, [a, b], '2024-01-08T')).toBe(2)
    expect(resolveStartIndex(cal, [a], '2024-01-06')).toBe(1)
  })
  it('rejects an explicit start before some column has data', () => {
    expect(() => resolveStartIndex(cal, [a, b], '2024-01-06')).toThrow(
      /no data on 2024-01-08 for: B/,
    )
  })
  it('rejects a start after the calendar', () => {
    expect(() => resolveStartIndex(cal, [a], '2024-01-11')).toThrow(
      AnalyticsError,
    )
  })
  it('rejects columns with no common date', () => {
    const c = { id: 'C', values: [1, null, null, null] }
    expect(() => resolveStartIndex(cal, [b, c], null)).toThrow(
      /no calendar date/,
    )
  })
})

describe('simpleReturn(s)', () => {
  it('computes cur / prev - 1', () => {
    expect(simpleReturn(100, 110)).toBeCloseTo(0.1, 15)
    expect(simpleReturn(100, 0)).toBe(-1)
  })
  it('returns 0 when the previous price is 0, whatever the current price', () => {
    expect(simpleReturn(0, 0)).toBe(0)
    expect(simpleReturn(0, 5)).toBe(0)
  })
  it('is null at the start and where either price is missing', () => {
    expect(simpleReturns([null, 1, 2, 0, 3])).toEqual([null, null, 1, -1, 0])
    expect(simpleReturns([])).toEqual([])
  })
  it('is finite for non-negative finite prices, or throws invalid_series on overflow', () => {
    const price = fc.oneof(
      fc.constant(0),
      fc.double({ min: 0, max: 1e12, noNaN: true }),
    )
    fc.assert(
      fc.property(fc.array(price, { maxLength: 30 }), (prices) => {
        let rs
        try {
          rs = simpleReturns(prices)
        } catch (e) {
          expect(e).toBeInstanceOf(AnalyticsError)
          expect((e as AnalyticsError).code).toBe('invalid_series')
          return
        }
        for (const r of rs)
          if (r !== null) expect(Number.isFinite(r)).toBe(true)
      }),
    )
  })

  it('throws on a price ratio that overflows', () => {
    expect(() => simpleReturns([5e-324, 1])).toThrow(/overflows/)
  })
})

describe('risk-free rate', () => {
  it('converts an annual yield to (1 + r)^(1/252) - 1', () => {
    expect(dailyRfFromAnnual(0)).toBe(0)
    expect(dailyRfFromAnnual(0.05)).toBeCloseTo(Math.pow(1.05, 1 / 252) - 1, 17)
    expect(Math.pow(1 + dailyRfFromAnnual(0.05), 252)).toBeCloseTo(1.05, 12)
    expect(dailyRfFromAnnual(-0.005)).toBeLessThan(0)
  })
  it.each([-1, -2, NaN])('rejects annual rate %d', (r) => {
    expect(() => dailyRfFromAnnual(r)).toThrow(AnalyticsError)
  })
  it('aligns onto the calendar with forward fill', () => {
    const got = alignDailyRf(cal, {
      dates: ['2024-01-08', '2024-01-10'],
      annualRate: [0.01, 0.02],
    })
    expect(got).toEqual([
      null,
      dailyRfFromAnnual(0.01),
      dailyRfFromAnnual(0.01),
      dailyRfFromAnnual(0.02),
    ])
  })
  it('validates the rf series', () => {
    expect(() =>
      alignDailyRf(cal, { dates: ['2024-01-08'], annualRate: [] }),
    ).toThrow(/differ in length/)
  })
})

describe('alignOverlay', () => {
  it('aligns levels, including negative ones, with forward fill', () => {
    expect(
      alignOverlay('fred:X', cal, {
        dates: ['2024-01-08', '2024-01-10'],
        close: [-0.5, 0.25],
      }),
    ).toEqual([null, -0.5, -0.5, 0.25])
  })
  it('rejects malformed overlays', () => {
    expect(() =>
      alignOverlay('pm:1', cal, { dates: ['2024-01-08'], close: [NaN] }),
    ).toThrow(/pm:1/)
  })
})
