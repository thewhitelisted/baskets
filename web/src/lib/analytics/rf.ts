import { alignToCalendar, validateSeries, type Column } from './align'
import { AnalyticsError } from './errors'
import { PERIODS_PER_YEAR } from './numeric'

/** Daily risk-free return from a decimal annual yield: (1 + r)^(1/252) - 1. */
export function dailyRfFromAnnual(annualRate: number): number {
  if (!(annualRate > -1)) {
    throw new AnalyticsError(
      'invalid_series',
      `annual rate ${String(annualRate)} must exceed -1`,
    )
  }
  return Math.pow(1 + annualRate, 1 / PERIODS_PER_YEAR) - 1
}

/** Daily rf on each calendar date, forward-filled; null before the first rf observation. */
export function alignDailyRf(
  calendar: readonly string[],
  rf: { dates: readonly string[]; annualRate: readonly number[] },
): Column {
  validateSeries('rf', rf.dates, rf.annualRate, { nonNegative: false })
  const daily = rf.annualRate.map(dailyRfFromAnnual)
  return alignToCalendar(calendar, rf.dates, daily)
}
