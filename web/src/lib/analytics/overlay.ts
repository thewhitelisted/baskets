import { alignToCalendar, validateSeries, type Column } from './align'

/**
 * Overlay values (Polymarket probability, FRED level, Yahoo close) on the
 * chart dates, forward-filled; null before the overlay's first observation.
 */
export function alignOverlay(
  id: string,
  dates: readonly string[],
  overlay: { dates: readonly string[]; close: readonly number[] },
): Column {
  validateSeries(id, overlay.dates, overlay.close, { nonNegative: false })
  return alignToCalendar(dates, overlay.dates, overlay.close)
}
