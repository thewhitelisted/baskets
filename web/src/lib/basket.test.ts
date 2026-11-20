import { compressToEncodedURIComponent } from 'lz-string'
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import {
  BasketError,
  STORAGE_PREFIX,
  decodeBasket,
  deleteBasket,
  encodeBasket,
  loadBaskets,
  parseBasket,
  saveBasket,
  type Basket,
} from './basket'
import { isIsoDate } from './dates'

// Input classes. Basket fields: each at min, max, just outside, wrong type,
// missing (with and without a documented default), unknown extra keys.
// Holdings: 0 / 1 / 50 / 51 entries, duplicate ids, empty id, fred: ids,
// non-finite weights, negative and zero weights. Names: empty, 80 vs 81
// characters, multi-code-point graphemes. Dates: valid, impossible (Feb 30),
// wrong format. Encoded links: valid, empty, garbage, valid lz-string of
// non-JSON, of JSON that is not a basket. Storage: empty, one, many,
// corrupt entries next to good ones, foreign keys, empty key.

const minimal = {
  v: 1,
  name: 'Tech',
  holdings: [{ id: 'AAPL', weight: 0.5 }],
  scheme: 'custom',
  rebalance: 'monthly',
  overlays: [],
  start: null,
}

const full: Basket = {
  v: 1,
  name: 'Full',
  holdings: [
    { id: 'AAPL', weight: 0.6 },
    { id: 'pm:123', weight: -0.2 },
  ],
  scheme: 'invvol',
  invvolLookback: 120,
  rebalance: 'quarterly',
  leverage: 2,
  benchmark: 'QQQ',
  overlays: ['fred:DGS10', 'pm:9'],
  start: '2020-02-29',
}

const holdings = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ id: `S${String(i)}`, weight: 0.01 }))

describe('parseBasket', () => {
  it('applies the documented defaults', () => {
    expect(parseBasket(minimal)).toEqual({
      ...minimal,
      invvolLookback: 63,
      leverage: 1,
      benchmark: 'SPY',
    })
  })

  it('accepts a fully specified basket unchanged', () => {
    expect(parseBasket(full)).toEqual(full)
  })

  it.each([
    ['name of 80 characters', { name: 'x'.repeat(80) }],
    ['name of 80 multi-code-point graphemes', { name: '👨‍👩‍👧'.repeat(80) }],
    ['50 holdings', { holdings: holdings(50) }],
    [
      'zero and negative weights',
      {
        holdings: [
          { id: 'A', weight: 0 },
          { id: 'B', weight: -3 },
        ],
      },
    ],
    ['invvolLookback 20', { invvolLookback: 20 }],
    ['invvolLookback 756', { invvolLookback: 756 }],
    ['leverage 10', { leverage: 10 }],
    ['tiny positive leverage', { leverage: 1e-9 }],
    ['5 overlays', { overlays: ['a', 'b', 'c', 'd', 'e'] }],
    ['leap-day start', { start: '2024-02-29' }],
  ])('accepts %s', (_, patch) => {
    expect(() => parseBasket({ ...minimal, ...patch })).not.toThrow()
  })

  it.each([
    ['v 2', { v: 2 }],
    ['empty name', { name: '' }],
    ['name of 81 characters', { name: 'x'.repeat(81) }],
    ['no holdings', { holdings: [] }],
    ['51 holdings', { holdings: holdings(51) }],
    [
      'duplicate ids',
      {
        holdings: [
          { id: 'A', weight: 1 },
          { id: 'A', weight: 1 },
        ],
      },
    ],
    ['empty id', { holdings: [{ id: '', weight: 1 }] }],
    ['fred holding', { holdings: [{ id: 'fred:DGS10', weight: 1 }] }],
    ['NaN weight', { holdings: [{ id: 'A', weight: NaN }] }],
    ['infinite weight', { holdings: [{ id: 'A', weight: Infinity }] }],
    ['missing weight', { holdings: [{ id: 'A' }] }],
    ['extra holding key', { holdings: [{ id: 'A', weight: 1, side: 'long' }] }],
    ['unknown scheme', { scheme: 'risk_parity' }],
    ['invvolLookback 19', { invvolLookback: 19 }],
    ['invvolLookback 757', { invvolLookback: 757 }],
    ['fractional invvolLookback', { invvolLookback: 63.5 }],
    ['unknown rebalance', { rebalance: 'weekly' }],
    ['leverage 0', { leverage: 0 }],
    ['negative leverage', { leverage: -1 }],
    ['leverage above 10', { leverage: 10.0001 }],
    ['empty benchmark', { benchmark: '' }],
    ['fred benchmark', { benchmark: 'fred:DGS10' }],
    ['6 overlays', { overlays: ['a', 'b', 'c', 'd', 'e', 'f'] }],
    ['empty overlay id', { overlays: [''] }],
    ['impossible start', { start: '2023-02-29' }],
    ['non-ISO start', { start: '2023/01/01' }],
    ['missing start', { start: undefined }],
    ['unknown key', { extra: true }],
  ])('rejects %s', (_, patch) => {
    expect(() => parseBasket({ ...minimal, ...patch })).toThrow(BasketError)
  })

  it.each([null, 'basket', 42, []])('rejects non-object %j', (value) => {
    expect(() => parseBasket(value)).toThrow(BasketError)
  })
})

const basketArb: fc.Arbitrary<Basket> = fc.record({
  v: fc.constant(1 as const),
  name: fc.string({ minLength: 1, maxLength: 80, unit: 'grapheme' }),
  holdings: fc.uniqueArray(
    fc.record({
      id: fc
        .string({ minLength: 1, maxLength: 12 })
        .filter((s) => !s.startsWith('fred:')),
      // JSON has no -0; it reads back as 0, which means the same (0 counts as long).
      weight: fc
        .double({ noNaN: true, min: -5, max: 5 })
        .map((w) => (w === 0 ? 0 : w)),
    }),
    { minLength: 1, maxLength: 50, selector: (h) => h.id },
  ),
  scheme: fc.constantFrom('custom', 'equal', 'mcap', 'invvol'),
  invvolLookback: fc.integer({ min: 20, max: 756 }),
  rebalance: fc.constantFrom('none', 'monthly', 'quarterly', 'annual'),
  leverage: fc.double({ min: 1e-6, max: 10, noNaN: true }),
  benchmark: fc.string({ minLength: 1 }).filter((s) => !s.startsWith('fred:')),
  overlays: fc.array(fc.string({ minLength: 1 }), { maxLength: 5 }),
  start: fc.option(
    fc
      .date({
        min: new Date('1900-01-01'),
        max: new Date('2100-12-31'),
        noInvalidDate: true,
      })
      .map((d) => d.toISOString().slice(0, 10)),
  ),
})

describe('encodeBasket / decodeBasket', () => {
  it('round-trips every valid basket exactly', () => {
    fc.assert(
      fc.property(basketArb, (b) => {
        expect(decodeBasket(encodeBasket(b))).toEqual(b)
      }),
    )
  })

  it('produces a URL-fragment-safe string', () => {
    fc.assert(
      fc.property(basketArb, (b) => {
        expect(encodeBasket(b)).toMatch(/^[A-Za-z0-9+\-$]*$/)
      }),
    )
  })

  it.each([
    ['empty string', '', /not a compressed basket/],
    [
      'non-JSON payload',
      compressToEncodedURIComponent('{not json'),
      /does not contain JSON/,
    ],
    [
      'JSON that is not a basket',
      compressToEncodedURIComponent('{"v":1}'),
      /invalid basket/,
    ],
  ])('rejects %s', (_, encoded, message) => {
    expect(() => decodeBasket(encoded)).toThrow(message)
  })

  it('never throws anything but BasketError on arbitrary input', () => {
    fc.assert(
      fc.property(fc.string(), (s) => {
        try {
          decodeBasket(s)
        } catch (e) {
          expect(e).toBeInstanceOf(BasketError)
        }
      }),
    )
  })
})

class MemoryStorage implements Storage {
  private readonly data = new Map<string, string>()
  get length(): number {
    return this.data.size
  }
  clear(): void {
    this.data.clear()
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null
  }
  removeItem(key: string): void {
    this.data.delete(key)
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value)
  }
}

describe('basket storage', () => {
  it('returns nothing from empty storage', () => {
    expect(loadBaskets(new MemoryStorage())).toEqual([])
  })

  it('saves, loads (sorted by key) and deletes baskets', () => {
    const s = new MemoryStorage()
    saveBasket(s, 'b', full)
    saveBasket(s, 'a', parseBasket(minimal))
    expect(loadBaskets(s)).toEqual([
      { key: 'a', ok: true, basket: parseBasket(minimal) },
      { key: 'b', ok: true, basket: full },
    ])
    deleteBasket(s, 'a')
    expect(loadBaskets(s).map((e) => e.key)).toEqual(['b'])
  })

  it('reports each corrupt entry, keeps it in storage and still loads the good ones', () => {
    const s = new MemoryStorage()
    saveBasket(s, 'good', full)
    s.setItem(`${STORAGE_PREFIX}notjson`, '{oops')
    s.setItem(
      `${STORAGE_PREFIX}invalid`,
      JSON.stringify({ ...full, leverage: 99 }),
    )
    s.setItem('unrelated', 'x')
    const loaded = loadBaskets(s)
    expect(loaded.map((e) => [e.key, e.ok])).toEqual([
      ['good', true],
      ['invalid', false],
      ['notjson', false],
    ])
    expect(loaded.flatMap((e) => (e.ok ? [] : [e.raw]))).toEqual([
      JSON.stringify({ ...full, leverage: 99 }),
      '{oops',
    ])
    expect(s.getItem(`${STORAGE_PREFIX}notjson`)).toBe('{oops')
    expect(s.getItem('unrelated')).toBe('x')
  })

  it('refuses to save an invalid basket or use an empty key', () => {
    const s = new MemoryStorage()
    expect(() => {
      saveBasket(s, 'k', { ...full, leverage: 0 })
    }).toThrow(BasketError)
    expect(() => {
      saveBasket(s, '', full)
    }).toThrow(BasketError)
    expect(() => {
      deleteBasket(s, '')
    }).toThrow(BasketError)
    expect(s.length).toBe(0)
  })

  it('propagates storage write failures', () => {
    const s = new MemoryStorage()
    s.setItem = () => {
      throw new DOMException('full', 'QuotaExceededError')
    }
    expect(() => {
      saveBasket(s, 'k', full)
    }).toThrow('full')
  })
})

describe('isIsoDate leap years', () => {
  it.each([
    ['2000-02-29', true],
    ['1900-02-29', false],
    ['2024-02-29', true],
    ['2023-02-28', true],
    ['2100-02-29', false],
  ])('%s -> %s', (d, want) => {
    expect(isIsoDate(d)).toBe(want)
  })

  it('agrees with Date for days 28..32 of every month in a 400-year cycle', () => {
    for (let y = 2000; y < 2400; y += 1) {
      for (let m = 1; m <= 12; m++) {
        for (let d = 28; d <= 32; d++) {
          const s = `${String(y)}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`
          const real = new Date(Date.UTC(y, m - 1, d)).getUTCDate() === d
          expect(isIsoDate(s)).toBe(real)
        }
      }
    }
  })
})

describe('isIsoDate', () => {
  it.each(['2024-02-29', '2023-12-31', '0001-01-01'])('accepts %s', (d) => {
    expect(isIsoDate(d)).toBe(true)
  })
  it.each([
    '2023-02-29',
    '2023-13-01',
    '2023-00-10',
    '2023-01-00',
    '2023-04-31',
    '2023-1-01',
    '20230101',
    ' 2023-01-01',
    '',
  ])('rejects %j', (d) => {
    expect(isIsoDate(d)).toBe(false)
  })
})
