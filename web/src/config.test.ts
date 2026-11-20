import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { DEFAULT_API_BASE, resolveApiBase } from './config'

// Input classes: unset; valid http/https origins (with port, with path); empty string;
// relative path; non-http scheme; non-canonical forms (trailing slash, surrounding
// whitespace, query, fragment, upper-case host); arbitrary strings and URLs.
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
    ['https://baskets-api.christopherjlee.ca/', /must be written as/],
    ['http://127.0.0.1:9000/api/', /must be written as/],
    [' http://localhost:8800', /must be written as/],
    ['http://localhost:8800\n', /must be written as/],
    ['http://localhost:8800?x=1', /must be written as/],
    ['http://localhost:8800#x', /must be written as/],
    ['HTTP://LOCALHOST:8800', /must be written as/],
  ])('rejects %j', (value, message) => {
    expect(() => resolveApiBase(value)).toThrow(message)
  })

  it('returns its input unchanged or throws, and accepted values are safe to append paths to', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.webUrl({ withQueryParameters: true, withFragments: true }),
          fc.string(),
        ),
        (value) => {
          let result: string
          try {
            result = resolveApiBase(value)
          } catch {
            return
          }
          expect(result).toBe(value)
          expect(result).toMatch(/^https?:\/\/[^\s?#]*[^\s?#/]$/)
        },
      ),
    )
  })
})
