export type AnalyticsErrorCode =
  /** Input data breaks the API contract (unsorted dates, NaN, negative price, ...). */
  | 'invalid_series'
  /** A series needed for the computation was not supplied. */
  | 'missing_series'
  /** Metadata needed for exposures was not supplied. */
  | 'missing_meta'
  /** No calendar date on which every holding and the benchmark have data. */
  | 'no_common_data'
  /** basket.start is before some holding or the benchmark has data. */
  | 'start_before_data'
  /** basket.start is after the last calendar date. */
  | 'start_after_data'
  /** A price or risk-free rate needed on a date is missing. */
  | 'missing_data'
  /** A successor chain is longer than 5 links or loops. */
  | 'successor_chain_too_long'
  /** scheme = "mcap" and a holding has no shares data. */
  | 'missing_shares'
  /** Rule-based target weights cannot be scaled because every raw weight is 0. */
  | 'zero_gross'
  /** Too few observations for the statistic. */
  | 'insufficient_data'
  /** Regression design matrix is singular. */
  | 'singular'
  // The four codes below are cases the specification does not define; the
  // computation stops rather than choose a behaviour.
  /** Portfolio value would reach zero or below (1 + daily return <= 0). */
  | 'nav_nonpositive'
  /** Inverse-vol weighting with a trailing volatility of exactly 0. */
  | 'zero_volatility'
  /** A rebalance would put non-zero weight on a holding whose price is 0. */
  | 'zero_price_allocation'
  /** Two merging slots have opposite signs under a non-custom scheme. */
  | 'slot_sign_conflict'

export class AnalyticsError extends Error {
  override name = 'AnalyticsError'
  readonly code: AnalyticsErrorCode

  constructor(code: AnalyticsErrorCode, message: string) {
    super(message)
    this.code = code
  }
}
