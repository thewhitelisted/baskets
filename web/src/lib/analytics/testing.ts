// Test-only helpers shared by the analytics test files.
import { expect } from 'vitest'

/** Typed asymmetric matcher: a number within 10^-digits / 2 of `value`. */
export function near(value: number, digits: number): number {
  return expect.closeTo(value, digits) as number
}

/** Default fixture tolerance: |a - b| <= REL * max(|a|, |b|) + ABS. */
export const REL = 1e-9
export const ABS = 1e-12

/**
 * Deep comparison where numbers match within tolerance and everything else
 * (strings, booleans, null, keys, lengths) must match exactly. Reports the
 * path of the first mismatch.
 */
export function expectClose(
  actual: unknown,
  expected: unknown,
  path = '$',
  tol: { rel: number; abs: number } = { rel: REL, abs: ABS },
): void {
  if (typeof expected === 'number') {
    expect(typeof actual, path).toBe('number')
    const a = actual as number
    const ok =
      Math.abs(a - expected) <=
      tol.rel * Math.max(Math.abs(a), Math.abs(expected)) + tol.abs
    if (!ok)
      expect.fail(`${path}: ${String(a)} vs expected ${String(expected)}`)
    return
  }
  if (Array.isArray(expected)) {
    expect(Array.isArray(actual), path).toBe(true)
    const a = actual as unknown[]
    expect(a.length, `${path}.length`).toBe(expected.length)
    expected.forEach((e, i) => {
      expectClose(a[i], e, `${path}[${String(i)}]`, tol)
    })
    return
  }
  if (expected !== null && typeof expected === 'object') {
    expect(actual !== null && typeof actual === 'object', path).toBe(true)
    const a = actual as Record<string, unknown>
    const e = expected as Record<string, unknown>
    expect(Object.keys(a).sort(), `${path} keys`).toEqual(Object.keys(e).sort())
    for (const k of Object.keys(e)) expectClose(a[k], e[k], `${path}.${k}`, tol)
    return
  }
  expect(actual, path).toEqual(expected)
}

const fixtures = import.meta.glob<unknown>('./__fixtures__/*.json', {
  eager: true,
  import: 'default',
})

/** Returns the parsed fixture; callers cast it to the shape they expect. */
export function readFixture(name: string): unknown {
  const data = fixtures[`./__fixtures__/${name}.json`]
  if (data === undefined) throw new Error(`no fixture ${name}`)
  return data
}
