import { num } from './numeric'

/**
 * Portfolio growth over one day, 1 + r_p with r_p = sum w_i r_i + cash * rf
 * (cash = 1 - sum w_i; negative cash is borrowing at rf). Computed as the sum
 * of the grown components, sum w_i (1 + r_i) + cash (1 + rf), which equals
 * 1 + r_p when the weights and cash sum to 1 and does not cancel to rounding
 * noise when the portfolio nearly loses everything.
 */
export function portfolioGrowth(
  weights: readonly number[],
  cash: number,
  returns: readonly number[],
  rf: number,
): number {
  if (weights.length !== returns.length)
    throw new RangeError('weights and returns differ in length')
  let g = cash * (1 + rf)
  for (let i = 0; i < weights.length; i++)
    g += num(weights, i) * (1 + num(returns, i))
  return g
}

/**
 * Weights after one day of returns with no trading: w_i' = w_i (1 + r_i) / (1 + r_p)
 * and cash' = cash (1 + rf) / (1 + r_p). Requires 1 + r_p > 0.
 */
export function driftWeights(
  weights: readonly number[],
  cash: number,
  returns: readonly number[],
  rf: number,
): { weights: number[]; cash: number } {
  const growth = portfolioGrowth(weights, cash, returns, rf)
  if (!(growth > 0))
    throw new RangeError(`drift needs 1 + r_p > 0, got ${String(growth)}`)
  return {
    weights: weights.map((w, i) => (w * (1 + num(returns, i))) / growth),
    cash: (cash * (1 + rf)) / growth,
  }
}
