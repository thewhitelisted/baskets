import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { near } from './testing'
import type { SharesEntry, Successor } from '../../api/types'
import { parseBasket, type Basket } from '../basket'
import {
  isRebalanceDate,
  MAX_SUCCESSOR_DEPTH,
  runBacktest,
  type BacktestResult,
  type SeriesInput,
} from './backtest'
import { AnalyticsError, type AnalyticsErrorCode } from './errors'

// Input classes (backtest): custom / equal / invvol / mcap; rebalance none /
// monthly / quarterly / annual; start null, on a calendar date, on a
// non-calendar date, before data, after the calendar; cash positive, zero,
// negative (borrowing); long, short, zero weights; prices hitting 0; holdings
// that end with no successor (before start, on a rebalance day, between
// rebalances, under "none"), with a successor (rename, cash + shares,
// successor already held -> merge, opposite-side merge, chains of 1..5 and 6
// links, a successor that has itself already converted, effective date on a
// non-calendar day, after the calendar end, successor without a price);
// leverage that drives NAV to zero; missing rf; missing series / shares.

/** Weekday dates starting at `from`. */
function weekdays(from: string, n: number): string[] {
  const out: string[] = []
  const d = new Date(`${from}T00:00:00Z`)
  while (out.length < n) {
    const wd = d.getUTCDay()
    if (wd !== 0 && wd !== 6) out.push(d.toISOString().slice(0, 10))
    d.setUTCDate(d.getUTCDate() + 1)
  }
  return out
}

function active(dates: string[], close: number[]): SeriesInput {
  return {
    dates,
    close,
    status: 'active',
    lastDate: dates[dates.length - 1] ?? '',
    successor: null,
  }
}

function ended(
  dates: string[],
  close: number[],
  successor: Successor | null = null,
): SeriesInput {
  return { ...active(dates, close), status: 'ended', successor }
}

function succ(
  id: string,
  ratio: number,
  cashPerShare: number,
  effectiveDate: string,
): Successor {
  return { id, ratio, cashPerShare, effectiveDate, source: 'corporate_actions' }
}

function makeBasket(
  holdings: [string, number][],
  patch: Partial<Basket> = {},
): Basket {
  return parseBasket({
    v: 1,
    name: 'test',
    holdings: holdings.map(([id, weight]) => ({ id, weight })),
    scheme: 'custom',
    rebalance: 'none',
    overlays: [],
    start: null,
    ...patch,
  })
}

const ZERO_RF = { dates: ['2000-01-03'], annualRate: [0] }

function run(
  basket: Basket,
  calendar: string[],
  series: Record<string, SeriesInput>,
  extra: {
    rf?: { dates: string[]; annualRate: number[] }
    shares?: Record<string, SharesEntry>
  } = {},
): BacktestResult {
  return runBacktest({
    basket,
    calendar,
    series: {
      SPY: active(
        calendar,
        calendar.map(() => 100),
      ),
      ...series,
    },
    rf: extra.rf ?? ZERO_RF,
    ...(extra.shares === undefined ? {} : { shares: extra.shares }),
  })
}

function expectCode(fn: () => unknown, code: AnalyticsErrorCode): void {
  try {
    fn()
  } catch (e) {
    expect(e).toBeInstanceOf(AnalyticsError)
    expect((e as AnalyticsError).code).toBe(code)
    return
  }
  expect.fail(`expected AnalyticsError ${code}`)
}

const navAt = (r: BacktestResult, i: number): number => r.nav[i] ?? NaN

const sumW = (w: Record<string, number>) =>
  Object.values(w).reduce((a, b) => a + b, 0)

describe('isRebalanceDate', () => {
  it.each([
    ['2024-01-31', '2024-02-01', 'monthly', true],
    ['2024-02-01', '2024-02-02', 'monthly', false],
    ['2023-12-29', '2024-01-02', 'monthly', true],
    ['2024-03-28', '2024-04-01', 'quarterly', true],
    ['2024-02-29', '2024-03-01', 'quarterly', false],
    ['2023-12-29', '2024-01-02', 'quarterly', true],
    ['2024-06-28', '2024-07-01', 'annual', false],
    ['2023-12-29', '2024-01-02', 'annual', true],
    ['2023-12-29', '2024-01-02', 'none', false],
  ] as const)('%s -> %s %s: %s', (prev, date, freq, want) => {
    expect(isRebalanceDate(prev, date, freq)).toBe(want)
  })
})

describe('runBacktest: basics', () => {
  const cal = weekdays('2024-01-02', 3)

  it('drifts custom weights with no trading under rebalance "none"', () => {
    const r = run(
      makeBasket([
        ['A', 0.5],
        ['B', 0.3],
      ]),
      cal,
      {
        A: active(cal, [100, 110, 121]),
        B: active(cal, [50, 50, 55]),
      },
    )
    expect(r.dates).toEqual(cal)
    // Values: A 0.5 -> 0.55 -> 0.605, B 0.3 -> 0.3 -> 0.33, cash 0.2.
    expect(r.nav).toEqual([1, near(1.05, 14), near(1.135, 14)])
    expect(r.returns).toEqual([near(0.05, 14), near(1.135 / 1.05 - 1, 14)])
    expect(r.latest.weights.A).toBeCloseTo(0.605 / 1.135, 14)
    expect(r.latest.weights.B).toBeCloseTo(0.33 / 1.135, 14)
    expect(r.cash).toEqual([
      near(0.2, 14),
      near(0.2 / 1.05, 14),
      near(0.2 / 1.135, 14),
    ])
    expect(r.rebalances).toEqual([
      {
        date: cal[0],
        weights: { A: 0.5, B: 0.3 },
        cash: near(0.2, 15),
        lookAhead: [],
      },
    ])
    expect(r.benchmarkReturns).toEqual([0, 0])
  })

  it('earns rf on positive cash and pays it on borrowed cash', () => {
    const rf = { dates: ['2023-12-01'], annualRate: [0.0252] }
    const daily = Math.pow(1.0252, 1 / 252) - 1
    const lend = run(
      makeBasket([['A', 0.4]]),
      cal,
      { A: active(cal, [1, 1, 1]) },
      { rf },
    )
    expect(lend.returns[0]).toBeCloseTo(0.6 * daily, 15)
    expect(lend.rf).toEqual([daily, daily])
    const borrow = run(
      makeBasket([['A', 1.5]]),
      cal,
      { A: active(cal, [1, 1.01, 1.01]) },
      { rf },
    )
    expect(borrow.returns[0]).toBeCloseTo(1.5 * 0.01 - 0.5 * daily, 15)
  })

  it('folds a crypto weekend move into the next calendar day', () => {
    const c = ['2024-01-05', '2024-01-08']
    const r = run(makeBasket([['BTC', 1]]), c, {
      BTC: active(
        ['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'],
        [100, 120, 90, 110],
      ),
    })
    expect(r.returns[0]).toBeCloseTo(0.1, 15)
  })

  it('keeps a position that hits price 0 worthless when the price recovers', () => {
    const c = weekdays('2024-01-02', 4)
    const r = run(
      makeBasket([
        ['Z', 0.5],
        ['A', 0.5],
      ]),
      c,
      { Z: active(c, [10, 0, 0, 7]), A: active(c, [1, 1, 1, 1]) },
    )
    expect(r.nav).toEqual([1, 0.5, 0.5, 0.5])
    expect(r.latest.weights.Z).toBe(0)
  })

  it('starts on the first date every holding and the benchmark have data', () => {
    const c = weekdays('2024-01-02', 5)
    const r = run(
      makeBasket([
        ['A', 1],
        ['B', 0],
      ]),
      c,
      { A: active(c, [1, 2, 3, 4, 5]), B: active(c.slice(2), [1, 1, 1]) },
    )
    expect(r.dates).toEqual(c.slice(2))
  })

  it('starts on the first calendar date on or after an explicit start', () => {
    const c = weekdays('2024-01-04', 4) // Thu Fri Mon Tue
    const r = run(makeBasket([['A', 1]], { start: '2024-01-06' }), c, {
      A: active(c, [1, 2, 3, 4]),
    })
    expect(r.dates[0]).toBe('2024-01-08')
  })
})

describe('runBacktest: rebalancing and schemes', () => {
  const cal = weekdays('2024-01-29', 50) // 2024-01-29 .. 2024-04-05

  it.each([
    ['monthly', ['2024-01-29', '2024-02-01', '2024-03-01', '2024-04-01']],
    ['quarterly', ['2024-01-29', '2024-04-01']],
    ['annual', ['2024-01-29']],
    ['none', ['2024-01-29']],
  ] as const)(
    '%s rebalances at the start and the first date of each new period',
    (freq, dates) => {
      const r = run(
        makeBasket(
          [
            ['A', 0.6],
            ['B', 0.4],
          ],
          { rebalance: freq },
        ),
        cal,
        {
          A: active(
            cal,
            cal.map((_, i) => 100 + i),
          ),
          B: active(
            cal,
            cal.map((_, i) => 100 - i),
          ),
        },
      )
      expect(r.rebalances.map((x) => x.date)).toEqual(dates)
      for (const reb of r.rebalances)
        expect(reb.weights).toEqual({ A: 0.6, B: 0.4 })
    },
  )

  it('equal: sign / n scaled to leverage, zero weight counted as long', () => {
    const r = run(
      makeBasket(
        [
          ['A', 5],
          ['B', -0.1],
          ['C', 0],
        ],
        { scheme: 'equal', leverage: 3 },
      ),
      cal,
      {
        A: active(
          cal,
          cal.map(() => 1),
        ),
        B: active(
          cal,
          cal.map(() => 1),
        ),
        C: active(
          cal,
          cal.map(() => 1),
        ),
      },
    )
    expect(r.rebalances[0]?.weights).toEqual({ A: 1, B: -1, C: 1 })
    expect(r.rebalances[0]?.cash).toBe(0)
  })

  it('mcap: sign * price * shares known at the date, flagging look-ahead', () => {
    const shares: Record<string, SharesEntry> = {
      A: { source: 'edgar', knownDates: ['2024-02-01'], shares: [10] },
      B: { source: 'current', knownDates: ['2026-11-20'], shares: [30] },
    }
    const r = run(
      makeBasket(
        [
          ['A', 1],
          ['B', -1],
        ],
        { scheme: 'mcap', rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(
          cal,
          cal.map(() => 2),
        ),
        B: active(
          cal,
          cal.map(() => 1),
        ),
      },
      { shares },
    )
    expect(r.rebalances[0]).toEqual({
      date: '2024-01-29',
      weights: { A: 0.4, B: -0.6 },
      cash: near(1.2, 15),
      lookAhead: ['A', 'B'],
    })
    expect(r.rebalances[1]?.lookAhead).toEqual(['B'])
  })

  it('mcap: requires shares for every holding and reachable successor', () => {
    const b = makeBasket([['A', 1]], { scheme: 'mcap' })
    const series = {
      A: ended(cal.slice(0, 5), [1, 1, 1, 1, 1], succ('B', 1, 0, cal[6] ?? '')),
      B: active(
        cal,
        cal.map(() => 1),
      ),
    }
    expectCode(
      () =>
        run(b, cal, series, {
          shares: {
            A: { source: 'edgar', knownDates: ['2020-01-01'], shares: [1] },
          },
        }),
      'missing_shares',
    )
    expectCode(() => run(b, cal, series), 'missing_shares')
  })

  it('invvol: uses only returns up to the rebalance date and falls back with warnings', () => {
    const c = weekdays('2024-01-02', 70)
    const noisy = (amp: number) =>
      c.map((_, i) => 100 * (1 + (i % 2 === 0 ? amp : -amp)))
    const b = makeBasket(
      [
        ['A', 1],
        ['B', 1],
        ['C', -1],
      ],
      {
        scheme: 'invvol',
        rebalance: 'monthly',
        invvolLookback: 20,
        start: c[25] ?? null,
      },
    )
    const r = run(b, c, {
      A: active(c, noisy(0.01)),
      B: active(c, noisy(0.02)),
      C: active(c.slice(20), noisy(0.01).slice(20)),
    })
    // At start (2024-02-06) C has 5 returns: median of A and B sigma is used.
    expect(r.warnings[0]).toMatchObject({
      date: '2024-02-06',
      code: 'invvol_median_sigma',
      ids: ['C'],
    })
    const w0 = r.rebalances[0]?.weights ?? {}
    expect(
      Math.abs(w0.A ?? 0) + Math.abs(w0.B ?? 0) + Math.abs(w0.C ?? 0),
    ).toBeCloseTo(1, 14)
    expect((w0.A ?? 0) / (w0.B ?? 1)).toBeCloseTo(2, 2)
    // By March C has its own sigma (equal to A's): no warning.
    expect(r.warnings.map((w) => w.date)).toEqual(['2024-02-06'])
    const w1 = r.rebalances[1]?.weights ?? {}
    expect(w1.C).toBeCloseTo(-(w1.A ?? 0), 2)
  })

  it('invvol: equal weights with a warning when nobody has 20 returns', () => {
    const c = weekdays('2024-01-02', 5)
    const r = run(
      makeBasket(
        [
          ['A', 1],
          ['B', -1],
        ],
        { scheme: 'invvol', leverage: 2 },
      ),
      c,
      {
        A: active(c, [1, 2, 1, 2, 1]),
        B: active(c, [1, 2, 1, 2, 1]),
      },
    )
    expect(r.rebalances[0]?.weights).toEqual({ A: 1, B: -1 })
    expect(r.warnings).toEqual([
      {
        date: c[0],
        code: 'invvol_equal_weight',
        ids: ['A', 'B'],
        message: expect.any(String) as string,
      },
    ])
  })

  it('invvol: refuses a zero trailing volatility', () => {
    const c = weekdays('2024-01-02', 30)
    expectCode(
      () =>
        run(
          makeBasket(
            [
              ['A', 1],
              ['B', 1],
            ],
            { scheme: 'invvol', start: c[25] ?? null },
          ),
          c,
          {
            A: active(
              c,
              c.map(() => 5),
            ),
            B: active(
              c,
              c.map((_, i) => 5 + (i % 2)),
            ),
          },
        ),
      'zero_volatility',
    )
  })

  it('refuses to allocate to a holding whose price is 0', () => {
    const c = weekdays('2024-01-30', 3)
    expectCode(
      () =>
        run(
          makeBasket(
            [
              ['A', 0.5],
              ['Z', 0.5],
            ],
            { rebalance: 'monthly' },
          ),
          c,
          { A: active(c, [1, 1, 1]), Z: active(c, [1, 0, 0]) },
        ),
      'zero_price_allocation',
    )
    // A zero target on a zero price is fine.
    expect(() =>
      run(
        makeBasket(
          [
            ['A', 0.5],
            ['Z', 0],
          ],
          { rebalance: 'monthly' },
        ),
        c,
        { A: active(c, [1, 1, 1]), Z: active(c, [1, 0, 0]) },
      ),
    ).not.toThrow()
  })

  it('refuses a day on which portfolio value reaches zero or below', () => {
    const c = weekdays('2024-01-02', 2)
    expectCode(
      () => run(makeBasket([['A', 2]]), c, { A: active(c, [1, 0.5]) }),
      'nav_nonpositive',
    )
    expectCode(
      () => run(makeBasket([['A', -1]]), c, { A: active(c, [1, 3]) }),
      'nav_nonpositive',
    )
  })
})

describe('runBacktest: input errors', () => {
  const cal = weekdays('2024-01-02', 5)
  const ok = active(cal, [1, 1, 1, 1, 1])
  it.each<[string, () => unknown, AnalyticsErrorCode]>([
    [
      'missing holding series',
      () => run(makeBasket([['A', 1]]), cal, {}),
      'missing_series',
    ],
    [
      'missing benchmark series',
      () => run(makeBasket([['A', 1]], { benchmark: 'QQQ' }), cal, { A: ok }),
      'missing_series',
    ],
    [
      'missing successor series',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: ended(cal.slice(0, 2), [1, 1], succ('B', 1, 0, cal[2] ?? '')),
        }),
      'missing_series',
    ],
    [
      'no common date',
      () =>
        run(
          makeBasket([
            ['A', 1],
            ['B', 1],
          ]),
          cal,
          { A: ended(cal.slice(0, 1), [1]), B: active(['2030-01-01'], [1]) },
        ),
      'no_common_data',
    ],
    [
      'start before data',
      () =>
        run(makeBasket([['A', 1]], { start: cal[0] ?? '' }), cal, {
          A: active(cal.slice(1), [1, 1, 1, 1]),
        }),
      'start_before_data',
    ],
    [
      'start after data',
      () =>
        run(makeBasket([['A', 1]], { start: '2030-01-01' }), cal, { A: ok }),
      'start_after_data',
    ],
    [
      'missing rf',
      () =>
        run(
          makeBasket([['A', 1]]),
          cal,
          { A: ok },
          { rf: { dates: [cal[3] ?? ''], annualRate: [0.01] } },
        ),
      'missing_data',
    ],
    [
      'negative price',
      () =>
        run(makeBasket([['A', 1]]), cal, { A: active(cal, [1, -1, 1, 1, 1]) }),
      'invalid_series',
    ],
    [
      'unsorted calendar',
      () => run(makeBasket([['A', 1]]), [...cal].reverse(), { A: ok }),
      'invalid_series',
    ],
    [
      'lastDate not last',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: { ...ok, lastDate: cal[0] ?? '' },
        }),
      'invalid_series',
    ],
    [
      'successor on active',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: { ...ok, successor: succ('B', 1, 0, cal[4] ?? '') },
          B: ok,
        }),
      'invalid_series',
    ],
    [
      'negative ratio',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: ended(cal.slice(0, 2), [1, 1], succ('B', -1, 0, cal[2] ?? '')),
          B: ok,
        }),
      'invalid_series',
    ],
    [
      'NaN cash per share',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: ended(cal.slice(0, 2), [1, 1], succ('B', 1, NaN, cal[2] ?? '')),
          B: ok,
        }),
      'invalid_series',
    ],
    [
      'effective before last date',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: ended(cal.slice(0, 2), [1, 1], succ('B', 1, 0, cal[0] ?? '')),
          B: ok,
        }),
      'invalid_series',
    ],
    [
      'bad effective date',
      () =>
        run(makeBasket([['A', 1]]), cal, {
          A: ended(cal.slice(0, 2), [1, 1], succ('B', 1, 0, 'soon')),
          B: ok,
        }),
      'invalid_series',
    ],
  ])('%s', (_, fn, code) => {
    expectCode(fn, code)
  })
})

describe('runBacktest: holding lifecycle', () => {
  // 2024-01-25 .. 2024-03-08 (Thu); month boundaries at 2024-02-01 and 2024-03-01.
  const cal = weekdays('2024-01-25', 32)
  const idx = (d: string) => cal.indexOf(d)
  const flat = (v: number) => cal.map(() => v)
  const upTo = (d: string, f: (i: number) => number) => {
    const n = idx(d) + 1
    return {
      dates: cal.slice(0, n),
      close: cal.slice(0, n).map((_, i) => f(i)),
    }
  }
  const endedAt = (
    d: string,
    f: (i: number) => number,
    s: Successor | null = null,
  ): SeriesInput => {
    const { dates, close } = upTo(d, f)
    return ended(dates, close, s)
  }

  it('active -> cashed out at its last close; slot removed and custom weight redistributed to the same side at the next rebalance', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.3],
          ['B', 0.1],
          ['D', 0.2],
          ['S', -0.2],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        B: active(cal, flat(1)),
        D: endedAt('2024-01-29', (i) => 10 + i),
        S: active(cal, flat(1)),
      },
    )
    const out = r.events.find((e) => e.kind === 'cash_out')
    expect(out).toEqual({
      id: 'D',
      date: '2024-01-29',
      kind: 'cash_out',
      details: {
        price: 12,
        weight: near((0.2 * (12 / 10)) / navAt(r, 2), 14),
        cashPerShare: null,
        cashValue: near((0.2 * (12 / 10)) / navAt(r, 2), 14),
      },
    })
    // Held as cash until the next rebalance.
    expect(r.cash[2]).toBeCloseTo(1 - 0.4 / navAt(r, 2) + 0.2 / navAt(r, 2), 12)
    expect(r.events.find((e) => e.kind === 'slot_removed')).toEqual({
      id: 'D',
      date: '2024-02-01',
      kind: 'slot_removed',
      details: {
        holdings: ['D'],
        customWeight: 0.2,
        redistributedTo: ['A', 'B'],
      },
    })
    expect(r.rebalances[1]?.weights).toEqual({
      A: near(0.45, 15),
      B: near(0.15, 15),
      S: -0.2,
    })
  })

  it('a cashed-out short with no surviving short leaves its weight as cash', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.5],
          ['S', -0.2],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        S: endedAt('2024-01-26', () => 1),
      },
    )
    expect(r.rebalances[1]).toEqual({
      date: '2024-02-01',
      weights: { A: 0.5 },
      cash: 0.5,
      lookAhead: [],
    })
    expect(r.events.at(-1)).toMatchObject({
      kind: 'slot_removed',
      details: { redistributedTo: [] },
    })
  })

  it('rule-based schemes renormalize over survivors', () => {
    const r = run(
      makeBasket(
        [
          ['A', 1],
          ['B', -1],
          ['D', 1],
        ],
        { scheme: 'equal', leverage: 1.5, rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        B: active(cal, flat(1)),
        D: endedAt('2024-01-26', () => 1),
      },
    )
    expect(r.rebalances[0]?.weights).toEqual({ A: 0.5, B: -0.5, D: 0.5 })
    expect(r.rebalances[1]?.weights).toEqual({ A: 0.75, B: -0.75 })
    expect(r.events.at(-1)).toMatchObject({
      kind: 'slot_removed',
      details: { customWeight: null, redistributedTo: [] },
    })
  })

  it('with rebalance "none" cash from a cash-out stays cash forever', () => {
    const r = run(
      makeBasket([
        ['A', 0.5],
        ['D', 0.5],
      ]),
      cal,
      { A: active(cal, flat(1)), D: endedAt('2024-01-26', () => 1) },
    )
    expect(r.cash.at(-1)).toBeCloseTo(0.5, 15)
    expect(r.latest.weights).toEqual({ A: 0.5 })
    expect(r.events.map((e) => e.kind)).toEqual(['cash_out'])
  })

  it('ended with a successor: held flat until the effective date, then converted into ratio shares plus cash', () => {
    // P ends 2024-01-31 at 20; effective 2024-02-03 (Saturday) -> converts at the close of Monday 2024-02-05.
    const r = run(makeBasket([['P', 1]]), cal, {
      P: endedAt(
        '2024-01-31',
        (i) => [10, 12, 15, 18, 20][i] ?? NaN,
        succ('Q', 0.5, 4, '2024-02-03'),
      ),
      Q: active(
        cal,
        cal.map((_, i) => 30 + i),
      ),
    })
    const t = idx('2024-02-05')
    // returns[i] ends at dates[i + 1]: 2024-02-01 and 2024-02-02 are flat.
    expect(r.returns.slice(idx('2024-01-31'), t - 1)).toEqual([0, 0])
    const nPerNav = 1 / 20 // shares per unit of NAV before the event (NAV_pre = 2)
    const q = 30 + t
    expect(r.nav[t - 1]).toBeCloseTo(2, 14)
    expect(r.nav[t]).toBeCloseTo(2 * nPerNav * (0.5 * q + 4), 12)
    const ev = r.events[0]
    expect(ev).toMatchObject({
      id: 'P',
      date: '2024-02-05',
      kind: 'conversion',
    })
    if (ev?.kind !== 'conversion') throw new Error('unreachable')
    expect(ev.details).toMatchObject({
      id: 'Q',
      price: 20,
      successorPrice: q,
      weight: 1,
      merged: false,
    })
    expect(ev.details.successorValue).toBeCloseTo((0.5 * q) / 20, 14)
    expect(ev.details.cashValue).toBeCloseTo(4 / 20, 14)
    // Afterwards Q is held under P's slot and drifts with Q's own prices.
    expect(r.latest.weights.Q).toBeGreaterThan(0)
    const n = 2 * nPerNav
    expect(r.returns[t]).toBeCloseTo(
      (n * 0.5 * (q + 1) + n * 4) / (n * 0.5 * q + n * 4) - 1,
      12,
    )
  })

  it('a rename (ratio 1, cash 0) keeps the slot and its custom weight for later rebalances', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.3],
          ['OLD', 0.6],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        OLD: endedAt('2024-01-29', () => 5, {
          ...succ('NEW', 1, 0, '2024-01-29'),
          source: 'edgar_rename',
        }),
        NEW: active(cal, flat(5)),
      },
    )
    expect(r.events).toHaveLength(1)
    expect(r.rebalances[1]?.weights).toEqual({ A: 0.3, NEW: 0.6 })
    expect(r.nav.every((v) => Math.abs(v - 1) < 1e-15)).toBe(true)
  })

  it('a successor that is already held merges slots: custom weights add', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.3],
          ['T', 0.2],
          ['B', 0.1],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(10)),
        T: endedAt('2024-01-29', () => 4, succ('A', 0.2, 2, '2024-01-29')),
        B: active(cal, flat(1)),
      },
    )
    const conv = r.events[0]
    expect(conv).toMatchObject({
      kind: 'conversion',
      details: { merged: true },
    })
    // 0.2 of NAV in T (4 per share) -> 0.05 shares/NAV * (0.2 * 10 + 2) = 0.2: value preserved here.
    expect(r.latest.weights.T).toBeUndefined()
    expect(r.rebalances[1]?.weights).toEqual({
      A: near(0.5, 15),
      B: 0.1,
    })
  })

  it('a merge under a rule-based scheme counts as one slot', () => {
    const r = run(
      makeBasket(
        [
          ['A', 1],
          ['T', 1],
          ['B', 1],
        ],
        { scheme: 'equal', rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(10)),
        T: endedAt('2024-01-29', () => 4, succ('A', 0.2, 2, '2024-01-29')),
        B: active(cal, flat(1)),
      },
    )
    expect(r.rebalances[1]?.weights).toEqual({ A: 0.5, B: 0.5 })
  })

  it('a merge of opposite sides under a rule-based scheme is refused', () => {
    expectCode(
      () =>
        run(
          makeBasket(
            [
              ['A', 1],
              ['T', -1],
            ],
            { scheme: 'equal' },
          ),
          cal,
          {
            A: active(cal, flat(10)),
            T: endedAt('2024-01-29', () => 4, succ('A', 1, 0, '2024-01-29')),
          },
        ),
      'slot_sign_conflict',
    )
  })

  it('a merge of opposite sides under custom nets the weights', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.5],
          ['T', -0.2],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(10)),
        T: endedAt('2024-01-29', () => 10, succ('A', 1, 0, '2024-01-29')),
      },
    )
    expect(r.rebalances[1]?.weights).toEqual({ A: near(0.3, 15) })
  })

  it('follows a chain whose next link has already converted, at the same close', () => {
    const r = run(makeBasket([['X1', 1]]), cal, {
      X1: endedAt('2024-01-30', () => 10, succ('X2', 1, 0, '2024-02-02')),
      X2: endedAt('2024-01-29', () => 10, succ('X3', 2, 1, '2024-01-31')),
      X3: active(cal, flat(10)),
    })
    expect(r.events.map((e) => [e.id, e.date, e.kind])).toEqual([
      ['X1', '2024-02-02', 'conversion'],
      ['X2', '2024-02-02', 'conversion'],
    ])
    expect(r.latest.weights).toEqual({ X3: near(20 / 21, 14) })
    expect(r.nav.at(-1)).toBeCloseTo(2.1, 14)
  })

  it(`accepts a chain of ${String(MAX_SUCCESSOR_DEPTH)} links and refuses ${String(MAX_SUCCESSOR_DEPTH + 1)} or a loop`, () => {
    const chain = (links: number): Record<string, SeriesInput> => {
      const s: Record<string, SeriesInput> = {}
      for (let i = 0; i < links; i++)
        s[`C${String(i)}`] = endedAt(
          cal[i] ?? '',
          () => 1,
          succ(`C${String(i + 1)}`, 1, 0, cal[i] ?? ''),
        )
      s[`C${String(links)}`] = active(cal, flat(1))
      return s
    }
    const ok = run(makeBasket([['C0', 1]]), cal, chain(MAX_SUCCESSOR_DEPTH))
    expect(ok.latest.weights).toEqual({
      [`C${String(MAX_SUCCESSOR_DEPTH)}`]: 1,
    })
    expectCode(
      () => run(makeBasket([['C0', 1]]), cal, chain(MAX_SUCCESSOR_DEPTH + 1)),
      'successor_chain_too_long',
    )
    expectCode(
      () =>
        run(makeBasket([['L1', 1]]), cal, {
          L1: endedAt(cal[0] ?? '', () => 1, succ('L2', 1, 0, cal[1] ?? '')),
          L2: endedAt(cal[1] ?? '', () => 1, succ('L1', 1, 0, cal[2] ?? '')),
        }),
      'successor_chain_too_long',
    )
  })

  it('stays pending (held flat) when the effective date is after the calendar', () => {
    const r = run(makeBasket([['P', 1]]), cal, {
      P: endedAt('2024-03-01', () => 3, succ('Q', 1, 0, '2030-01-01')),
      Q: active(cal, flat(1)),
    })
    expect(r.events).toEqual([])
    expect(r.latest.weights).toEqual({ P: 1 })
  })

  it('refuses a conversion when the successor has no price on the effective date', () => {
    expectCode(
      () =>
        run(makeBasket([['P', 1]]), cal, {
          P: endedAt('2024-01-29', () => 3, succ('Q', 1, 0, '2024-01-29')),
          Q: active(cal.slice(10), flat(1).slice(10)),
        }),
      'missing_data',
    )
  })

  it('applies events at a close before that close’s rebalance', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.5],
          ['D', 0.5],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        D: endedAt('2024-02-01', () => 1),
      },
    )
    expect(r.events.map((e) => [e.kind, e.date])).toEqual([
      ['cash_out', '2024-02-01'],
      ['slot_removed', '2024-02-01'],
    ])
    expect(r.rebalances[1]?.weights).toEqual({ A: 1 })
  })

  it('resolves events dated before the start before the initial allocation', () => {
    const late = cal.slice(5)
    const r = run(
      makeBasket(
        [
          ['A', 0.4],
          ['G', 0.2],
          ['O', 0.4],
        ],
        { start: cal[5] ?? null, rebalance: 'none' },
      ),
      cal,
      {
        A: active(cal, flat(1)),
        G: endedAt('2024-01-26', () => 1),
        O: endedAt('2024-01-26', () => 2, succ('N', 1, 0, '2024-01-29')),
        N: active(
          late,
          late.map(() => 3),
        ),
      },
    )
    expect(r.events.map((e) => [e.id, e.date, e.kind])).toEqual([
      ['G', cal[5], 'cash_out'],
      ['O', cal[5], 'conversion'],
      ['G', cal[5], 'slot_removed'],
    ])
    // G's 0.2 goes to the surviving longs A and N (0.4 each) pro rata.
    expect(r.rebalances[0]?.weights).toEqual({ A: 0.5, N: 0.5 })
  })

  it('a Polymarket market resolving YES is a cash-out at 1', () => {
    const r = run(makeBasket([['pm:1', 0.5]]), cal, {
      'pm:1': endedAt('2024-02-05', (i) => (idx('2024-02-05') === i ? 1 : 0.5)),
    })
    expect(r.events).toEqual([
      {
        id: 'pm:1',
        date: '2024-02-05',
        kind: 'cash_out',
        details: {
          price: 1,
          weight: near(1 / 1.5, 14),
          cashPerShare: null,
          cashValue: near(1 / 1.5, 14),
        },
      },
    ])
    expect(r.nav.at(-1)).toBeCloseTo(1.5, 14)
  })
})

describe('runBacktest: cash-out at a deal price (successor id null)', () => {
  const cal = weekdays('2024-01-25', 32)
  const deal = (cashPerShare: number, effectiveDate: string): Successor => ({
    id: null,
    ratio: 0,
    cashPerShare,
    effectiveDate,
    source: 'corporate_actions',
  })

  it('holds the position flat until the effective date, then turns each share into the deal cash', () => {
    // T ends 2024-01-29 at 40 (w = 0.5 of NAV 1, so n = 0.5 / 40 shares); deal 50 per share on 2024-01-31.
    const r = run(
      makeBasket([
        ['A', 0.5],
        ['T', 0.5],
      ]),
      cal,
      {
        A: active(
          cal,
          cal.map(() => 1),
        ),
        T: ended(cal.slice(0, 3), [40, 40, 40], deal(50, '2024-01-31')),
      },
    )
    const t = cal.indexOf('2024-01-31')
    expect(r.returns.slice(0, t - 1)).toEqual([0, 0, 0])
    expect(r.nav[t]).toBeCloseTo(0.5 + (0.5 / 40) * 50, 14)
    expect(r.events).toEqual([
      {
        id: 'T',
        date: '2024-01-31',
        kind: 'cash_out',
        details: {
          price: 40,
          weight: 0.5,
          cashPerShare: 50,
          cashValue: near(0.625, 14),
        },
      },
    ])
    expect(r.latest.weights).toEqual({ A: near(0.5 / 1.125, 14) })
  })

  it('removes the slot at the next rebalance like any cash-out', () => {
    const r = run(
      makeBasket(
        [
          ['A', 0.5],
          ['T', 0.3],
        ],
        { rebalance: 'monthly' },
      ),
      cal,
      {
        A: active(
          cal,
          cal.map(() => 1),
        ),
        T: ended(cal.slice(0, 3), [40, 40, 40], deal(50, '2024-01-31')),
      },
    )
    expect(r.events.map((e) => [e.kind, e.date])).toEqual([
      ['cash_out', '2024-01-31'],
      ['slot_removed', '2024-02-01'],
    ])
    expect(r.rebalances[1]?.weights).toEqual({ A: near(0.8, 15) })
  })

  it('is not followed as a chain and needs no successor series', () => {
    const r = run(
      makeBasket([['T', 1]], { scheme: 'mcap' }),
      cal,
      { T: ended(cal.slice(0, 3), [40, 40, 40], deal(1, '2024-01-29')) },
      {
        shares: {
          T: { source: 'edgar', knownDates: ['2020-01-01'], shares: [1] },
        },
      },
    )
    expect(r.latest).toMatchObject({ weights: {}, cash: 1 })
  })

  it('rejects a deal cash-out with a non-zero ratio', () => {
    expectCode(
      () =>
        run(makeBasket([['T', 1]]), cal, {
          T: ended(cal.slice(0, 3), [40, 40, 40], {
            ...deal(1, '2024-01-29'),
            ratio: 0.5,
          }),
        }),
      'invalid_series',
    )
  })

  it('conserves value: V_after = V_before - n P + n X', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0.01, max: 100, noNaN: true }),
        fc.double({ min: 0, max: 200, noNaN: true }),
        fc.double({ min: -2, max: 2, noNaN: true }),
        (p, x, w) => {
          const series = {
            T: ended(cal.slice(0, 3), [p, p, p], deal(x, cal[4] ?? '')),
          }
          const n = w / p
          const after = 1 - n * p + n * x
          if (!(after > 0)) {
            expectCode(
              () => run(makeBasket([['T', w]]), cal, series),
              'nav_nonpositive',
            )
            return
          }
          const r = run(makeBasket([['T', w]]), cal, series)
          expect(r.nav[4]).toBeCloseTo(after, 9)
          expect(r.cash[4] ?? NaN).toBeCloseTo(1, 12)
          const ev = r.events[0]
          if (ev?.kind !== 'cash_out') throw new Error('expected a cash-out')
          expect(ev.details.cashValue).toBeCloseTo(n * x, 9)
        },
      ),
    )
  })
})

describe('runBacktest: converted (non-USD) series', () => {
  it('starts on the first date the converted series has, later than the others', () => {
    const cal = weekdays('2024-01-02', 6)
    // A converted listing omits dates before its first FX close.
    const r = run(
      makeBasket([
        ['7203.T', 0.5],
        ['A', 0.5],
      ]),
      cal,
      {
        '7203.T': active(cal.slice(3), [10, 11, 12]),
        A: active(
          cal,
          cal.map(() => 1),
        ),
      },
    )
    expect(r.dates).toEqual(cal.slice(3))
  })
})

describe('runBacktest: properties', () => {
  /** A random market of 1..4 holdings over 5..45 weekdays crossing month ends, prices >= 0 (zeros allowed). */
  const marketArb = fc
    .record({
      start: fc.integer({ min: 0, max: 40 }),
      days: fc.integer({ min: 5, max: 45 }),
      paths: fc.array(
        fc.array(
          fc.oneof(
            { weight: 1, arbitrary: fc.constant(0) },
            {
              weight: 9,
              arbitrary: fc.double({ min: -0.2, max: 0.2, noNaN: true }),
            },
          ),
          { minLength: 45, maxLength: 45 },
        ),
        { minLength: 1, maxLength: 4 },
      ),
      weights: fc.array(fc.double({ min: -1, max: 1, noNaN: true }), {
        minLength: 4,
        maxLength: 4,
      }),
      rate: fc.double({ min: -0.01, max: 0.08, noNaN: true }),
      rebalance: fc.constantFrom('none', 'monthly', 'quarterly', 'annual'),
      scheme: fc.constantFrom('custom', 'equal'),
      leverage: fc.double({ min: 0.1, max: 3, noNaN: true }),
    })
    .map((m) => {
      const cal = weekdays(
        new Date(Date.UTC(2023, 10, 1 + m.start)).toISOString().slice(0, 10),
        m.days,
      )
      const series: Record<string, SeriesInput> = {}
      m.paths.forEach((path, k) => {
        let p = 100
        series[`H${String(k)}`] = active(
          cal,
          cal.map((_, i) => {
            const step = path[i] ?? 0
            p = step === 0 && i % 7 === 3 ? 0 : p * (1 + step)
            return p
          }),
        )
      })
      const basket = makeBasket(
        m.paths.map((_, k) => [`H${String(k)}`, m.weights[k] ?? 0]),
        { rebalance: m.rebalance, scheme: m.scheme, leverage: m.leverage },
      )
      return {
        cal,
        series,
        basket,
        rf: { dates: [cal[0] ?? ''], annualRate: [m.rate] },
      }
    })

  const tryRun = (
    cal: string[],
    m: {
      series: Record<string, SeriesInput>
      basket: Basket
      rf: { dates: string[]; annualRate: number[] }
    },
  ) => {
    try {
      return run(m.basket, cal, m.series, { rf: m.rf })
    } catch (e) {
      // Refusals are only the specified ones.
      expect(e).toBeInstanceOf(AnalyticsError)
      expect([
        'zero_price_allocation',
        'nav_nonpositive',
        'zero_gross',
      ]).toContain((e as AnalyticsError).code)
      return null
    }
  }

  it('NAV is the cumulative product of returns, starting at 1', () => {
    fc.assert(
      fc.property(marketArb, (m) => {
        const r = tryRun(m.cal, m)
        if (r === null) return
        expect(r.nav[0]).toBe(1)
        let v = 1
        r.returns.forEach((x, i) => {
          v *= 1 + x
          expect(r.nav[i + 1]).toBe(v)
        })
        expect(r.nav.every((x) => x > 0 && Number.isFinite(x))).toBe(true)
      }),
    )
  })

  it('sum of weights + cash = 1 on every day, and sum |w| = leverage at every rule-based rebalance', () => {
    fc.assert(
      fc.property(marketArb, (m) => {
        for (let k = 1; k <= m.cal.length; k++) {
          const r = tryRun(m.cal.slice(0, k), m)
          if (r === null) return
          // Relative to gross exposure: near ruin weights grow large and the sum loses absolute precision.
          const gross =
            Object.values(r.latest.weights).reduce(
              (a, b) => a + Math.abs(b),
              0,
            ) + Math.abs(r.latest.cash)
          expect(
            Math.abs(sumW(r.latest.weights) + r.latest.cash - 1),
          ).toBeLessThanOrEqual(1e-13 * gross)
          expect(r.latest.cash).toBe(r.cash.at(-1))
          for (const reb of r.rebalances) {
            expect(sumW(reb.weights) + reb.cash).toBeCloseTo(1, 12)
            if (m.basket.scheme !== 'custom') {
              expect(
                Object.values(reb.weights).reduce((a, b) => a + Math.abs(b), 0),
              ).toBeCloseTo(m.basket.leverage, 12)
            }
          }
        }
      }),
      { numRuns: 40 },
    )
  })

  it('matches a share-count simulation', () => {
    fc.assert(
      fc.property(marketArb, (m) => {
        const r = tryRun(m.cal, m)
        if (r === null) return
        const ids = Object.keys(m.series)
        const px = (id: string, t: number) => m.series[id]?.close[t] ?? NaN
        const rf = Math.pow(1 + (m.rf.annualRate[0] ?? 0), 1 / 252) - 1
        let shares = new Map<string, number>()
        let cash = 0
        const allocate = (t: number, value: number) => {
          const w = r.rebalances.find((x) => x.date === m.cal[t])?.weights ?? {}
          shares = new Map(
            ids.map((id) => [
              id,
              px(id, t) === 0 ? 0 : ((w[id] ?? 0) * value) / px(id, t),
            ]),
          )
          cash = value * (1 - sumW(w))
        }
        allocate(0, 1)
        let prev = 1
        for (let t = 1; t < m.cal.length; t++) {
          cash *= 1 + rf
          let v = cash
          for (const id of ids) {
            if (px(id, t) === 0) shares.set(id, 0)
            v += (shares.get(id) ?? 0) * px(id, t)
          }
          expect(v / prev - 1).toBeCloseTo(r.returns[t - 1] ?? NaN, 9)
          if (r.rebalances.some((x) => x.date === m.cal[t])) allocate(t, v)
          prev = v
        }
      }),
    )
  })

  it('conserves value at a conversion: NAV after = NAV before - n P + n (ratio Ps + cash)', () => {
    const cal = weekdays('2024-01-08', 6)
    fc.assert(
      fc.property(
        fc.double({ min: 0.01, max: 100, noNaN: true }),
        fc.double({ min: 0.01, max: 100, noNaN: true }),
        fc.double({ min: 0, max: 5, noNaN: true }),
        fc.double({ min: 0, max: 50, noNaN: true }),
        fc.double({ min: -2, max: 2, noNaN: true }),
        (p, ps, ratio, cps, w) => {
          // w of NAV in P (cash 1 - w earns 0); P ends on day 2 and converts on day 4.
          const series = {
            P: ended(
              cal.slice(0, 3),
              [p, p, p],
              succ('S', ratio, cps, cal[4] ?? ''),
            ),
            S: active(
              cal,
              cal.map(() => ps),
            ),
          }
          const navPre = 1
          const n = (w * navPre) / p
          const navPost = navPre - n * p + n * (ratio * ps + cps)
          if (!(navPost > 0)) {
            expectCode(
              () => run(makeBasket([['P', w]]), cal, series),
              'nav_nonpositive',
            )
            return
          }
          const r = run(makeBasket([['P', w]]), cal, series)
          expect(r.nav[4]).toBeCloseTo(navPost, 9)
          // Successor position = n ratio Ps; cash = (1 - w) + n cps, both over NAV after.
          expect((r.latest.weights.S ?? NaN) * navPost).toBeCloseTo(
            n * ratio * ps,
            9,
          )
          expect((r.cash[4] ?? NaN) * navPost).toBeCloseTo(1 - w + n * cps, 9)
        },
      ),
    )
  })
})
