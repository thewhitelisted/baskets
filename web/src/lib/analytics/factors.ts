import type { FactorName } from '../../api/types'
import { AnalyticsError } from './errors'
import {
  at,
  f64,
  PERIODS_PER_YEAR,
  sampleCovariance,
  sampleVariance,
} from './numeric'
import { ROLLING_WINDOW } from './stats'

/** Lower-triangular Cholesky factor of a symmetric positive-definite matrix. */
function cholesky(a: readonly (readonly number[])[]): number[][] {
  const k = a.length
  const l = Array.from({ length: k }, () => new Array<number>(k).fill(0))
  for (let i = 0; i < k; i++) {
    const li = at(l, i)
    for (let j = 0; j <= i; j++) {
      const lj = at(l, j)
      let s = at(at(a, i), j)
      for (let m = 0; m < j; m++) s -= at(li, m) * at(lj, m)
      if (i === j) {
        if (!(s > 0))
          throw new AnalyticsError(
            'singular',
            'regressors are collinear or constant',
          )
        li[i] = Math.sqrt(s)
      } else {
        li[j] = s / at(lj, j)
      }
    }
  }
  return l
}

/** Solves (L L^T) x = b. */
function choleskySolve(
  l: readonly (readonly number[])[],
  b: readonly number[],
): number[] {
  const k = l.length
  const y = new Array<number>(k).fill(0)
  for (let i = 0; i < k; i++) {
    let s = at(b, i)
    for (let m = 0; m < i; m++) s -= at(at(l, i), m) * at(y, m)
    y[i] = s / at(at(l, i), i)
  }
  const x = new Array<number>(k).fill(0)
  for (let i = k - 1; i >= 0; i--) {
    let s = at(y, i)
    for (let m = i + 1; m < k; m++) s -= at(at(l, m), i) * at(x, m)
    x[i] = s / at(at(l, i), i)
  }
  return x
}

type CenteredFit = {
  /** Slopes, one per regressor column. */
  slopes: number[]
  intercept: number
  /** Cholesky factor of Xc^T Xc. */
  chol: number[][]
  xMeans: number[]
}

/** Regression data with the regressors stored row-major: x[t * k + i]. */
type Design = { y: Float64Array; x: Float64Array; k: number }

function design(
  y: readonly number[],
  cols: readonly (readonly number[])[],
): Design {
  const n = y.length
  const k = cols.length
  if (cols.some((c) => c.length !== n))
    throw new RangeError('columns and y differ in length')
  const x = new Float64Array(n * k)
  cols.forEach((c, i) => {
    c.forEach((v, t) => (x[t * k + i] = v))
  })
  return { y: Float64Array.from(y), x, k }
}

/** OLS with intercept on rows [from, to) via the centred normal equations Xc^T Xc b = Xc^T yc. */
function centeredFit(d: Design, from: number, to: number): CenteredFit {
  const { y, x, k } = d
  const n = to - from
  let yMean = 0
  const xMeans = new Float64Array(k)
  for (let t = from; t < to; t++) {
    yMean += f64(y, t)
    for (let i = 0; i < k; i++) xMeans[i] = f64(xMeans, i) + f64(x, t * k + i)
  }
  yMean /= n
  for (let i = 0; i < k; i++) xMeans[i] = f64(xMeans, i) / n
  const a = new Float64Array(k * k)
  const b = new Float64Array(k)
  const xc = new Float64Array(k)
  for (let t = from; t < to; t++) {
    for (let i = 0; i < k; i++) xc[i] = f64(x, t * k + i) - f64(xMeans, i)
    const yc = f64(y, t) - yMean
    for (let i = 0; i < k; i++) {
      const xi = f64(xc, i)
      b[i] = f64(b, i) + xi * yc
      for (let j = 0; j <= i; j++)
        a[i * k + j] = f64(a, i * k + j) + xi * f64(xc, j)
    }
  }
  const sym = Array.from({ length: k }, (_, i) =>
    Array.from({ length: k }, (__, j) =>
      f64(a, i >= j ? i * k + j : j * k + i),
    ),
  )
  const chol = cholesky(sym)
  const slopes = choleskySolve(chol, Array.from(b))
  const means = Array.from(xMeans)
  const intercept =
    yMean - slopes.reduce((acc, s, i) => acc + s * at(means, i), 0)
  return { slopes, intercept, chol, xMeans: means }
}

export type Coefficient = {
  estimate: number
  stdError: number
  tStat: number | null
}

export type OlsResult = {
  intercept: Coefficient
  slopes: Coefficient[]
  /** Centred R^2; null when y is constant. */
  r2: number | null
  rss: number
  observations: number
}

/** OLS of y on an intercept plus the given columns, with classical (non-robust) standard errors. */
export function ols(
  y: readonly number[],
  cols: readonly (readonly number[])[],
): OlsResult {
  const n = y.length
  const k = cols.length
  const d = design(y, cols)
  if (n <= k + 1) {
    throw new AnalyticsError(
      'insufficient_data',
      `regression needs more than ${String(k + 1)} observations, got ${String(n)}`,
    )
  }
  const fit = centeredFit(d, 0, n)
  let rss = 0
  for (let t = 0; t < n; t++) {
    let e = at(y, t) - fit.intercept
    for (let i = 0; i < k; i++) e -= at(fit.slopes, i) * at(at(cols, i), t)
    rss += e * e
  }
  const tss = sampleVariance(y) * (n - 1)
  const s2 = rss / (n - k - 1)
  // diag of (Xc^T Xc)^-1 and x̄^T (Xc^T Xc)^-1 x̄ via solves against the Cholesky factor.
  const inv = cols.map((_, j) =>
    choleskySolve(
      fit.chol,
      cols.map((__, i) => (i === j ? 1 : 0)),
    ),
  )
  const invXbar = choleskySolve(fit.chol, fit.xMeans)
  const quad = fit.xMeans.reduce((acc, m, i) => acc + m * at(invXbar, i), 0)
  const coef = (estimate: number, variance: number): Coefficient => {
    const stdError = Math.sqrt(s2 * variance)
    return {
      estimate,
      stdError,
      tStat: stdError === 0 ? null : estimate / stdError,
    }
  }
  return {
    intercept: coef(fit.intercept, 1 / n + quad),
    slopes: fit.slopes.map((b, i) => coef(b, at(at(inv, i), i))),
    r2: tss === 0 ? null : 1 - rss / tss,
    rss,
    observations: n,
  }
}

/** OLS slopes on each trailing window of `window` observations; out[w] ends at observation w + window - 1. */
export function rollingSlopes(
  y: readonly number[],
  cols: readonly (readonly number[])[],
  window: number,
): number[][] {
  const d = design(y, cols)
  const out: number[][] = []
  for (let end = window; end <= y.length; end++)
    out.push(centeredFit(d, end - window, end).slopes)
  return out
}

export type FactorInput = {
  factors: readonly FactorName[]
  dates: readonly string[]
  values: Readonly<Record<string, readonly number[]>>
}

export type FactorRegression = {
  factors: FactorName[]
  observations: number
  startDate: string
  endDate: string
  /** Daily intercept with its standard error and t-statistic. */
  intercept: Coefficient
  /** Intercept times 252. */
  alphaAnnual: number
  betas: Record<string, Coefficient>
  r2: number | null
  /** Betas on each trailing 252-observation window. */
  rolling: { dates: string[]; betas: Record<string, number[]> }
  /** Annualized variances: systematic b^T Sigma_f b, idiosyncratic RSS / (n - 1); they sum to var(y). */
  risk: {
    total: number
    systematic: number
    idiosyncratic: number
    systematicShare: number | null
    idiosyncraticShare: number | null
  }
}

/**
 * Regresses basket excess returns (over the French RF) on the factors, using
 * the dates present in both the basket returns and the factor data.
 */
export function factorRegression(input: {
  /** NAV dates; returns[i] is the period ending at dates[i + 1]. */
  dates: readonly string[]
  returns: readonly number[]
  factorData: FactorInput
}): FactorRegression {
  const { dates, returns, factorData } = input
  const names = [...factorData.factors]
  const column = (name: string): readonly number[] => {
    const c = factorData.values[name]
    if (c === undefined)
      throw new AnalyticsError('missing_series', `factor data lacks ${name}`)
    return c
  }
  const rf = column('RF')
  const factorCols = names.map(column)
  if ([rf, ...factorCols].some((c) => c.length !== factorData.dates.length)) {
    throw new AnalyticsError(
      'invalid_series',
      'factor arrays differ in length from dates',
    )
  }
  const index = new Map(factorData.dates.map((d, i) => [d, i]))
  const used: string[] = []
  const y: number[] = []
  const cols: number[][] = names.map(() => [])
  returns.forEach((r, i) => {
    const d = at(dates, i + 1)
    const fi = index.get(d)
    if (fi === undefined) return
    used.push(d)
    y.push(r - at(rf, fi))
    factorCols.forEach((c, j) => at(cols, j).push(at(c, fi)))
  })
  const fit = ols(y, cols)
  const n = y.length
  const betas = Object.fromEntries(names.map((f, i) => [f, at(fit.slopes, i)]))

  const rollingDates = used.slice(ROLLING_WINDOW - 1)
  const rollingFits = rollingSlopes(y, cols, ROLLING_WINDOW)
  const rollingBetas = Object.fromEntries(
    names.map((f, i) => [f, rollingFits.map((s) => at(s, i))]),
  )

  const total = sampleVariance(y)
  let systematic = 0
  for (let i = 0; i < names.length; i++) {
    for (let j = 0; j < names.length; j++) {
      systematic +=
        at(fit.slopes, i).estimate *
        at(fit.slopes, j).estimate *
        sampleCovariance(at(cols, i), at(cols, j))
    }
  }
  const idiosyncratic = fit.rss / (n - 1)
  const ann = PERIODS_PER_YEAR
  return {
    factors: names,
    observations: n,
    startDate: at(used, 0),
    endDate: at(used, n - 1),
    intercept: fit.intercept,
    alphaAnnual: fit.intercept.estimate * ann,
    betas,
    r2: fit.r2,
    rolling: { dates: rollingDates, betas: rollingBetas },
    risk: {
      total: total * ann,
      systematic: systematic * ann,
      idiosyncratic: idiosyncratic * ann,
      systematicShare: total === 0 ? null : systematic / total,
      idiosyncraticShare: total === 0 ? null : idiosyncratic / total,
    },
  }
}
