import { alignToCalendar, validateSeries } from './align'
import { AnalyticsError } from './errors'
import { at, num, PERIODS_PER_YEAR } from './numeric'
import { simpleReturns } from './returns'

/** A matrix as an array of rows. */
export type Matrix = number[][]

export type Estimator = 'sample' | 'ledoit_wolf' | 'ewma'

export const EWMA_LAMBDA = 0.94
export const DEFAULT_RISK_WINDOW = 252

function columnMeans(
  rows: readonly (readonly number[])[],
  p: number,
): number[] {
  const m = new Array<number>(p).fill(0)
  for (const row of rows)
    for (let j = 0; j < p; j++) m[j] = num(m, j) + num(row, j)
  return m.map((x) => x / rows.length)
}

/** sum over rows of weight(t) * x_t x_t^T, where x_t = row - center. */
function weightedCrossProduct(
  rows: readonly (readonly number[])[],
  p: number,
  center: readonly number[],
  weight: (t: number) => number,
): Matrix {
  const out: Matrix = Array.from({ length: p }, () =>
    new Array<number>(p).fill(0),
  )
  const x = new Array<number>(p)
  rows.forEach((row, t) => {
    const wt = weight(t)
    for (let j = 0; j < p; j++) x[j] = num(row, j) - num(center, j)
    for (let i = 0; i < p; i++) {
      const xi = num(x, i) * wt
      const oi = at(out, i)
      for (let j = 0; j <= i; j++) oi[j] = num(oi, j) + xi * num(x, j)
    }
  })
  for (let i = 0; i < p; i++)
    for (let j = 0; j < i; j++) at(out, j)[i] = at(at(out, i), j)
  return out
}

function checkRows(
  rows: readonly (readonly number[])[],
  minRows: number,
): number {
  if (rows.length < minRows) {
    throw new AnalyticsError(
      'insufficient_data',
      `need at least ${String(minRows)} observations, got ${String(rows.length)}`,
    )
  }
  const p = at(rows, 0).length
  if (p === 0 || rows.some((r) => r.length !== p))
    throw new RangeError('rows must be non-empty and equal length')
  return p
}

/** Sample covariance with denominator T - 1 (numpy.cov default). */
export function sampleCovarianceMatrix(
  rows: readonly (readonly number[])[],
): Matrix {
  const p = checkRows(rows, 2)
  const c = weightedCrossProduct(rows, p, columnMeans(rows, p), () => 1)
  return c.map((r) => r.map((v) => v / (rows.length - 1)))
}

/**
 * Ledoit-Wolf (2004) shrinkage toward mu * I, mu = trace(S) / p, computed as
 * sklearn.covariance.LedoitWolf with default arguments (data centred, S with
 * denominator T).
 */
export function ledoitWolf(rows: readonly (readonly number[])[]): {
  covariance: Matrix
  shrinkage: number
} {
  const p = checkRows(rows, 2)
  const n = rows.length
  const means = columnMeans(rows, p)
  const s = weightedCrossProduct(rows, p, means, () => 1 / n)
  if (p === 1) return { covariance: s, shrinkage: 0 }
  const trace = s.reduce((acc, r, i) => acc + num(r, i), 0)
  const mu = trace / p
  // beta_ = sum_ij (X2^T X2)_ij = sum_t (sum_i x_ti^2)^2
  let betaRaw = 0
  for (const row of rows) {
    let rowSq = 0
    for (let j = 0; j < p; j++) {
      const x = num(row, j) - num(means, j)
      rowSq += x * x
    }
    betaRaw += rowSq * rowSq
  }
  // delta_ = sum_ij (X^T X)_ij^2 / n^2 = sum_ij S_ij^2
  let deltaRaw = 0
  for (const r of s) for (const v of r) deltaRaw += v * v
  let beta = (betaRaw / n - deltaRaw) / (p * n)
  const delta = (deltaRaw - 2 * mu * trace + p * mu * mu) / p
  beta = Math.min(beta, delta)
  // sklearn divides unguarded; delta is 0 only when S = mu I, where beta <= 0 and shrinkage is moot.
  const shrinkage = beta === 0 || delta === 0 ? 0 : beta / delta
  const covariance = s.map((r, i) =>
    r.map((v, j) => (1 - shrinkage) * v + (i === j ? shrinkage * mu : 0)),
  )
  return { covariance, shrinkage }
}

/**
 * RiskMetrics EWMA covariance, zero mean: sum_t w_t r_t r_t^T with
 * w_t proportional to lambda^(T-1-t) (newest row weighted most), normalized to sum to 1.
 */
export function ewmaCovariance(
  rows: readonly (readonly number[])[],
  lambda = EWMA_LAMBDA,
): Matrix {
  const p = checkRows(rows, 2)
  const n = rows.length
  const norm = (1 - lambda) / (1 - Math.pow(lambda, n))
  return weightedCrossProduct(
    rows,
    p,
    new Array<number>(p).fill(0),
    (t) => norm * Math.pow(lambda, n - 1 - t),
  )
}

/** Daily covariance by estimator; shrinkage is reported for Ledoit-Wolf only. */
export function covarianceMatrix(
  rows: readonly (readonly number[])[],
  estimator: Estimator,
): { covariance: Matrix; shrinkage: number | null } {
  switch (estimator) {
    case 'sample':
      return { covariance: sampleCovarianceMatrix(rows), shrinkage: null }
    case 'ledoit_wolf':
      return ledoitWolf(rows)
    case 'ewma':
      return { covariance: ewmaCovariance(rows), shrinkage: null }
  }
}

export function correlationFromCovariance(cov: Matrix): (number | null)[][] {
  return cov.map((r, i) =>
    r.map((v, j) => {
      const d = num(r, i) * at(at(cov, j), j)
      if (d <= 0) return null
      return i === j ? 1 : v / Math.sqrt(d)
    }),
  )
}

export type RiskContributions = {
  /** Marginal contribution (Sigma w)_i / sigma_p. */
  mctr: number[]
  /** w_i * mctr_i; sums to sigma_p. */
  ctr: number[]
  /** ctr_i / sigma_p; sums to 1. */
  pctCtr: number[]
}

export type RiskDecomposition = {
  /** Annualized portfolio volatility sqrt(w^T Sigma w). */
  sigmaP: number
  /** Annualized volatility of each holding. */
  vols: number[]
  /** null when sigmaP = 0: contributions are undefined. */
  contributions: RiskContributions | null
  /** sum |w_i| sigma_i / sigma_p; null when sigmaP = 0. */
  diversificationRatio: number | null
}

/** Risk decomposition for weights under an (annualized) covariance matrix. */
export function decomposeRisk(
  cov: Matrix,
  weights: readonly number[],
): RiskDecomposition {
  const p = weights.length
  if (cov.length !== p || cov.some((r) => r.length !== p))
    throw new RangeError('covariance and weights differ in size')
  const sw = cov.map((r) =>
    r.reduce((acc, v, j) => acc + v * num(weights, j), 0),
  )
  let variance = 0
  for (let i = 0; i < p; i++) variance += num(weights, i) * num(sw, i)
  const vols = cov.map((r, i) => Math.sqrt(num(r, i)))
  if (variance <= 0) {
    // A PSD matrix gives w^T S w >= 0; anything below is rounding of an exact 0.
    const scale =
      cov.reduce((acc, r, i) => Math.max(acc, Math.abs(num(r, i))), 0) * p * p
    if (variance < -1e-12 * scale)
      throw new Error(
        `invariant: covariance is not PSD (w'Sw = ${String(variance)})`,
      )
    return { sigmaP: 0, vols, contributions: null, diversificationRatio: null }
  }
  const sigmaP = Math.sqrt(variance)
  const mctr = sw.map((v) => v / sigmaP)
  const ctr = mctr.map((m, i) => num(weights, i) * m)
  const pctCtr = ctr.map((c) => c / sigmaP)
  const weightedVol = vols.reduce(
    (acc, v, i) => acc + Math.abs(num(weights, i)) * v,
    0,
  )
  return {
    sigmaP,
    vols,
    contributions: { mctr, ctr, pctCtr },
    diversificationRatio: weightedVol / sigmaP,
  }
}

export type RiskReport = RiskDecomposition & {
  ids: string[]
  weights: number[]
  estimator: Estimator
  /** Ledoit-Wolf shrinkage intensity; null for other estimators. */
  shrinkage: number | null
  /** Number of daily returns used (<= window when history is shorter). */
  observations: number
  startDate: string
  endDate: string
  /** Annualized covariance. */
  covariance: Matrix
  correlation: (number | null)[][]
}

/**
 * Risk of the given weights (cash excluded) from the trailing `window` daily
 * returns ending at the last calendar date. If some holding's history is
 * shorter, the window is the longest trailing run on which all have returns;
 * `observations` reports its length.
 */
export function basketRisk(input: {
  calendar: readonly string[]
  series: Readonly<
    Record<string, { dates: readonly string[]; close: readonly number[] }>
  >
  weights: Readonly<Record<string, number>>
  estimator: Estimator
  window?: number
}): RiskReport {
  const window = input.window ?? DEFAULT_RISK_WINDOW
  if (!Number.isInteger(window) || window < 2)
    throw new RangeError('window must be an integer >= 2')
  const ids = Object.keys(input.weights)
  if (ids.length === 0)
    throw new AnalyticsError('insufficient_data', 'no holdings to analyse')
  const cols = ids.map((id) => {
    const s = input.series[id]
    if (s === undefined)
      throw new AnalyticsError('missing_series', `no series supplied for ${id}`)
    validateSeries(id, s.dates, s.close, { nonNegative: true })
    return simpleReturns(alignToCalendar(input.calendar, s.dates, s.close))
  })
  const end = input.calendar.length - 1
  let first = end + 1
  while (
    first - 1 >= 0 &&
    end - (first - 1) < window &&
    cols.every((c) => at(c, first - 1) !== null)
  )
    first--
  const rows: number[][] = []
  for (let t = first; t <= end; t++)
    rows.push(cols.map((c) => at(c, t) as number))
  const daily = covarianceMatrix(rows, input.estimator)
  const covariance = daily.covariance.map((r) =>
    r.map((v) => v * PERIODS_PER_YEAR),
  )
  const weights = ids.map((id) => input.weights[id] as number)
  return {
    ids,
    weights,
    estimator: input.estimator,
    shrinkage: daily.shrinkage,
    observations: rows.length,
    startDate: at(input.calendar, first),
    endDate: at(input.calendar, end),
    covariance,
    correlation: correlationFromCovariance(covariance),
    ...decomposeRisk(covariance, weights),
  }
}
