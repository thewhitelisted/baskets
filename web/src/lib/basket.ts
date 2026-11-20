import {
  compressToEncodedURIComponent,
  decompressFromEncodedURIComponent,
} from 'lz-string'
import { z } from 'zod'
import { isIsoDate } from './dates'

const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
const charLength = (s: string): number => [...graphemes.segment(s)].length

const holdingSchema = z.strictObject({
  id: z
    .string()
    .min(1)
    .refine((id) => !id.startsWith('fred:'), {
      message: 'fred: series are not holdable',
    }),
  weight: z.number(),
})

/** Basket per docs/CONTRACTS.md. Parsing applies the documented defaults. */
export const basketSchema = z.strictObject({
  v: z.literal(1),
  name: z.string().refine((s) => charLength(s) >= 1 && charLength(s) <= 80, {
    message: 'name must be 1..80 characters',
  }),
  holdings: z
    .array(holdingSchema)
    .min(1)
    .max(50)
    .refine((hs) => new Set(hs.map((h) => h.id)).size === hs.length, {
      message: 'holding ids must be unique',
    }),
  scheme: z.enum(['custom', 'equal', 'mcap', 'invvol']),
  invvolLookback: z.number().int().min(20).max(756).default(63),
  rebalance: z.enum(['none', 'monthly', 'quarterly', 'annual']),
  leverage: z.number().gt(0).max(10).default(1),
  benchmark: z
    .string()
    .min(1)
    .refine((id) => !id.startsWith('fred:'), {
      message: 'fred: series cannot be a benchmark',
    })
    .default('SPY'),
  overlays: z.array(z.string().min(1)).max(5),
  start: z
    .string()
    .refine(isIsoDate, { message: 'start must be an ISO date (YYYY-MM-DD)' })
    .nullable(),
})

export type Basket = z.output<typeof basketSchema>
export type Holding = Basket['holdings'][number]

export class BasketError extends Error {
  override name = 'BasketError'
}

/** Parses untrusted data into a Basket, throwing BasketError if it is invalid. */
export function parseBasket(data: unknown): Basket {
  const result = basketSchema.safeParse(data)
  if (!result.success) {
    throw new BasketError(`invalid basket: ${z.prettifyError(result.error)}`)
  }
  return result.data
}

/** Encodes a basket for the share URL fragment `#/b/<encoded>`. */
export function encodeBasket(basket: Basket): string {
  return compressToEncodedURIComponent(JSON.stringify(basket))
}

/** Inverse of encodeBasket. Throws BasketError on anything that is not a valid encoded basket. */
export function decodeBasket(encoded: string): Basket {
  // Typed as string, but lz-string returns null or "" for some undecodable
  // input and throws for other input.
  let json: unknown
  try {
    json = decompressFromEncodedURIComponent(encoded)
  } catch (cause) {
    throw new BasketError('share link is not a compressed basket', { cause })
  }
  if (typeof json !== 'string' || json === '') {
    throw new BasketError('share link is not a compressed basket')
  }
  let data: unknown
  try {
    data = JSON.parse(json)
  } catch (cause) {
    throw new BasketError('share link does not contain JSON', { cause })
  }
  return parseBasket(data)
}

export const STORAGE_PREFIX = 'baskets:v1:basket:'

export type StoredBasket =
  | { key: string; ok: true; basket: Basket }
  | { key: string; ok: false; error: string; raw: string }

function storageKey(key: string): string {
  if (key === '') throw new BasketError('storage key must be non-empty')
  return STORAGE_PREFIX + key
}

/** Validates and stores one basket under its own storage entry. */
export function saveBasket(
  storage: Storage,
  key: string,
  basket: Basket,
): void {
  const valid = parseBasket(basket)
  storage.setItem(storageKey(key), JSON.stringify(valid))
}

export function deleteBasket(storage: Storage, key: string): void {
  storage.removeItem(storageKey(key))
}

/**
 * Loads every saved basket, sorted by key. A corrupt entry is reported as
 * `ok: false` with its raw text and is left in storage untouched; it does not
 * prevent the other entries from loading.
 */
export function loadBaskets(storage: Storage): StoredBasket[] {
  const keys: string[] = []
  for (let i = 0; i < storage.length; i++) {
    const k = storage.key(i)
    if (k?.startsWith(STORAGE_PREFIX)) keys.push(k)
  }
  keys.sort()
  return keys.map((fullKey): StoredBasket => {
    const key = fullKey.slice(STORAGE_PREFIX.length)
    const raw = storage.getItem(fullKey)
    if (raw === null) {
      return { key, ok: false, error: 'entry vanished while loading', raw: '' }
    }
    try {
      return { key, ok: true, basket: parseBasket(JSON.parse(raw)) }
    } catch (e) {
      const error = e instanceof Error ? e.message : String(e)
      return { key, ok: false, error, raw }
    }
  })
}
