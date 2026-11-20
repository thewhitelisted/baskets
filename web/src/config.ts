export const DEFAULT_API_BASE = 'http://localhost:8800'

export function resolveApiBase(value: string | undefined): string {
  if (value === undefined) return DEFAULT_API_BASE
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(
      `VITE_API_BASE must be an absolute URL, got ${JSON.stringify(value)}`,
    )
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(
      `VITE_API_BASE must use http or https, got ${JSON.stringify(value)}`,
    )
  }
  const canonical = (url.origin + url.pathname).replace(/\/$/, '')
  if (value !== canonical) {
    throw new Error(
      `VITE_API_BASE must be written as ${JSON.stringify(canonical)} (lower-case host, no trailing "/", query, fragment or whitespace), got ${JSON.stringify(value)}`,
    )
  }
  return value
}

export const API_BASE = resolveApiBase(import.meta.env.VITE_API_BASE)
