import { isIsoDate } from '../dates'
import { AnalyticsError } from './errors'
import { cell, num, str } from './numeric'

/** A value per calendar date; null where the series has not started yet. */
export type Column = (number | null)[]

/** Throws unless dates are ISO, strictly ascending and the same length as values, and values are finite. */
export function validateSeries(
  id: string,
  dates: readonly string[],
  values: readonly number[],
  opts: { nonNegative: boolean },
): void {
  const fail = (why: string): never => {
    throw new AnalyticsError('invalid_series', `series ${id}: ${why}`)
  }
  if (dates.length !== values.length) fail('dates and values differ in length')
  if (dates.length === 0) fail('series is empty')
  for (let i = 0; i < dates.length; i++) {
    const d = str(dates, i)
    if (!isIsoDate(d)) fail(`bad date ${JSON.stringify(d)}`)
    if (i > 0 && d <= str(dates, i - 1))
      fail(`dates not strictly ascending at ${d}`)
    const v = num(values, i)
    if (!Number.isFinite(v)) fail(`non-finite value on ${d}`)
    if (opts.nonNegative && v < 0) fail(`negative price on ${d}`)
  }
}

/**
 * As-of alignment: the value on calendar date c is the last observation dated
 * on or before c (so weekend moves land on the next calendar date), or null
 * before the first observation. Inputs must already be validated.
 */
export function alignToCalendar(
  calendar: readonly string[],
  dates: readonly string[],
  values: readonly number[],
): Column {
  const out: Column = new Array<number | null>(calendar.length)
  let j = -1
  for (let i = 0; i < calendar.length; i++) {
    const c = str(calendar, i)
    while (j + 1 < dates.length && str(dates, j + 1) <= c) j++
    out[i] = j < 0 ? null : num(values, j)
  }
  return out
}

/**
 * Index of the first backtest date: the first calendar index on or after
 * `start` (or, when start is null, the first index where every column has a
 * value). Throws if that date is missing data or does not exist.
 */
export function resolveStartIndex(
  calendar: readonly string[],
  columns: readonly { id: string; values: Column }[],
  start: string | null,
): number {
  const hasAll = (i: number): boolean =>
    columns.every((c) => cell(c.values, i) !== null)
  if (start === null) {
    for (let i = 0; i < calendar.length; i++) if (hasAll(i)) return i
    throw new AnalyticsError(
      'no_common_data',
      'no calendar date on which every holding and the benchmark have data',
    )
  }
  const i = calendar.findIndex((d) => d >= start)
  if (i < 0) {
    throw new AnalyticsError(
      'start_after_data',
      `start ${start} is after the last calendar date`,
    )
  }
  const missing = columns
    .filter((c) => cell(c.values, i) === null)
    .map((c) => c.id)
  if (missing.length > 0) {
    throw new AnalyticsError(
      'start_before_data',
      `no data on ${str(calendar, i)} for: ${missing.join(', ')}`,
    )
  }
  return i
}
