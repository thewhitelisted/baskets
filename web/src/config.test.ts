import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { DEFAULT_API_BASE, resolveApiBase } from './config'

// Input classes: unset; valid http/https origins (with port, with path); empty string;
// relative path; non-http scheme; trailing slash; arbitrary strings.
describe('resolveApiBase', () => {
  it('defaults to the local dev server when unset', () => {
    expect(resolveApiBase(undefined)).toBe('http://localhost:8800')
    expect(DEFAULT_API_BASE).toBe('http://localhost:8800')
  })

  it.each([
    'https://baskets-api.christopherjlee.ca',
    'http://localhost:8800',
    'http://127.0.0.1:9000/api',
  ])('returns a valid http(s) base unchanged: %s', (value) => {
    expect(resolveApiBase(value)).toBe(value)
  })

  it.each([
    ['', /absolute URL/],
    ['/api', /absolute URL/],
    ['localhost:8800', /http or https/],
    ['ftp://example.com', /http or https/],
    ['https://baskets-api.christopherjlee.ca/', /must not end with/],
  ])('rejects %j', (value, message) => {
    expect(() => resolveApiBase(value)).toThrow(message)
  })

  it('never rewrites its input: returns it unchanged or throws', () => {
    fc.assert(
      fc.property(fc.oneof(fc.webUrl(), fc.string()), (value) => {
        let result: string
        try {
          result = resolveApiBase(value)
        } catch {
          return
        }
        expect(result).toBe(value)
      }),
    )
  })
})
