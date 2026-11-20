import {
  mean,
  num,
  PERIODS_PER_YEAR,
  sampleCovariance,
  sampleStd,
  sampleVariance,
  str,
} from './numeric'

export const ROLLING_WINDOW = 252

export type Drawdown = {
  /** nav / running peak - 1 at every date (<= 0). */
  series: number[]
  /** Most negative drawdown, or 0. */
  max: number
  /** Dates are null when there is no drawdown. Peak is the first date the peak was reached. */
  peakDate: string | null
  troughDate: string | null
  /** First date after the trough at which NAV regains the peak; null if not yet. */
  recoveryDate: string | null
}

export type BenchmarkStats = {
  /** OLS slope of (r - rf) on (r_b - rf). */
  beta: number | null
  /** OLS intercept times 252. */
  alpha: number | null
  trackingError: number | null
  informationRatio: number | null
  correlation: number | null
}

export type PerformanceStats = {
  cumulativeReturn: number
  cagr: number | null
  annualVol: number | null
  sharpe: number | null
  sortino: number | null
  calmar: number | null
  drawdown: Drawdown
  rolling: { dates: string[]; return: number[]; vol: number[] }
  calendarYears: {
    year: number
    startDate: string
    endDate: string
    return: number
  }[]
  benchmark: BenchmarkStats
}

export type StatsInput = {
  /** NAV dates; returns[i] is the period ending at dates[i + 1]. */
  dates: readonly string[]
  nav: readonly number[]
  returns: readonly number[]
  rf: readonly number[]
  benchmarkReturns: readonly number[]
}

const ANN = Math.sqrt(PERIODS_PER_YEAR)

export function drawdown(
  dates: readonly string[],
  nav: readonly number[],
): Drawdown {
  const series: number[] = []
  let peak = num(nav, 0)
  let peakIdx = 0
  let max = 0
  let troughIdx = -1
  let peakAtTrough = 0
  for (let i = 0; i < nav.length; i++) {
    const v = num(nav, i)
    if (v > peak) {
      peak = v
      peakIdx = i
    }
    const dd = v / peak - 1
    series.push(dd)
    if (dd < max) {
      max = dd
      troughIdx = i
      peakAtTrough = peakIdx
    }
  }
  if (troughIdx < 0)
    return {
      series,
      max: 0,
      peakDate: null,
      troughDate: null,
      recoveryDate: null,
    }
  const peakValue = num(nav, peakAtTrough)
  let recoveryDate: string | null = null
  for (let i = troughIdx + 1; i < nav.length; i++) {
    if (num(nav, i) >= peakValue) {
      recoveryDate = str(dates, i)
      break
    }
  }
  return {
    series,
    max,
    peakDate: str(dates, peakAtTrough),
    troughDate: str(dates, troughIdx),
    recoveryDate,
  }
}

function benchmarkStats(
  returns: readonly number[],
  rf: readonly number[],
  bench: readonly number[],
): BenchmarkStats {
  const none = {
    beta: null,
    alpha: null,
    trackingError: null,
    informationRatio: null,
    correlation: null,
  }
  if (returns.length < 2) return none
  const y = returns.map((r, i) => r - num(rf, i))
  const x = bench.map((r, i) => r - num(rf, i))
  const varX = sampleVariance(x)
  const beta = varX === 0 ? null : sampleCovariance(x, y) / varX
  const alpha =
    beta === null ? null : (mean(y) - beta * mean(x)) * PERIODS_PER_YEAR
  const active = returns.map((r, i) => r - num(bench, i))
  const te = sampleStd(active) * ANN
  const sdP = sampleStd(returns)
  const sdB = sampleStd(bench)
  return {
    beta,
    alpha,
    trackingError: te,
    informationRatio: te === 0 ? null : (mean(active) * PERIODS_PER_YEAR) / te,
    correlation:
      sdP === 0 || sdB === 0
        ? null
        : sampleCovariance(returns, bench) / (sdP * sdB),
  }
}

export function performanceStats(input: StatsInput): PerformanceStats {
  const { dates, nav, returns, rf, benchmarkReturns } = input
  const n = returns.length
  if (
    nav.length !== dates.length ||
    n !== dates.length - 1 ||
    rf.length !== n ||
    benchmarkReturns.length !== n
  ) {
    throw new RangeError('stats input arrays have inconsistent lengths')
  }
  const first = num(nav, 0)
  const last = num(nav, nav.length - 1)
  const cumulativeReturn = last / first - 1
  const cagr = n === 0 ? null : Math.pow(last / first, PERIODS_PER_YEAR / n) - 1
  const annualVol = n < 2 ? null : sampleStd(returns) * ANN

  const excess = returns.map((r, i) => r - num(rf, i))
  let sharpe: number | null = null
  if (n >= 2) {
    const sd = sampleStd(excess)
    sharpe = sd === 0 ? null : (mean(excess) / sd) * ANN
  }
  let sortino: number | null = null
  if (n >= 1) {
    let ss = 0
    for (const e of excess) if (e < 0) ss += e * e
    const downside = Math.sqrt(ss / n)
    sortino = downside === 0 ? null : (mean(excess) / downside) * ANN
  }

  const dd = drawdown(dates, nav)
  const calmar = cagr === null || dd.max === 0 ? null : cagr / -dd.max

  const rolling = {
    dates: [] as string[],
    return: [] as number[],
    vol: [] as number[],
  }
  for (let k = ROLLING_WINDOW; k < nav.length; k++) {
    rolling.dates.push(str(dates, k))
    rolling.return.push(num(nav, k) / num(nav, k - ROLLING_WINDOW) - 1)
    rolling.vol.push(sampleStd(returns, k - ROLLING_WINDOW, k) * ANN)
  }

  const calendarYears: PerformanceStats['calendarYears'] = []
  let base = 0
  let yearStart = 0
  for (let i = 0; i < dates.length; i++) {
    const year = str(dates, i).slice(0, 4)
    if (i + 1 < dates.length && str(dates, i + 1).slice(0, 4) === year) continue
    calendarYears.push({
      year: Number(year),
      startDate: str(dates, yearStart),
      endDate: str(dates, i),
      return: num(nav, i) / num(nav, base) - 1,
    })
    base = i
    yearStart = i + 1
  }

  return {
    cumulativeReturn,
    cagr,
    annualVol,
    sharpe,
    sortino,
    calmar,
    drawdown: dd,
    rolling,
    calendarYears,
    benchmark: benchmarkStats(returns, rf, benchmarkReturns),
  }
}
