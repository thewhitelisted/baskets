import type { SeriesEntry, SharesEntry, Successor } from '../../api/types'
import type { Basket } from '../basket'
import { isIsoDate } from '../dates'
import {
  alignToCalendar,
  resolveStartIndex,
  validateSeries,
  type Column,
} from './align'
import { driftWeights, portfolioGrowth } from './drift'
import { AnalyticsError } from './errors'
import { at } from './numeric'
import { simpleReturns } from './returns'
import { alignDailyRf } from './rf'
import {
  equalTargets,
  invvolTargets,
  mcapTargets,
  redistributeCustom,
  sharesAsOf,
  signOf,
  trailingSigma,
  type Sign,
} from './targets'

export type SeriesInput = Pick<
  SeriesEntry,
  'dates' | 'close' | 'status' | 'lastDate' | 'successor'
>

export type BacktestInput = {
  basket: Basket
  /** Master calendar: SPY trading dates, ascending. */
  calendar: readonly string[]
  /** Every holding, every successor reachable from a holding, and the benchmark. */
  series: Readonly<Record<string, SeriesInput>>
  rf: { dates: readonly string[]; annualRate: readonly number[] }
  /** Required for scheme "mcap": every holding and reachable successor. */
  shares?: Readonly<Record<string, SharesEntry>>
}

export type Weights = Record<string, number>

export type RebalanceRecord = {
  date: string
  weights: Weights
  cash: number
  /** mcap only: holdings whose shares figure was not yet known on this date. */
  lookAhead: string[]
}

/** Amounts are fractions of portfolio value at that close, before the event. */
export type BacktestEvent =
  | {
      id: string
      date: string
      kind: 'cash_out'
      details: {
        price: number
        weight: number
        /** null: cashed out at the last close; a number: the deal price per share. */
        cashPerShare: number | null
        /** Cash received. */
        cashValue: number
      }
    }
  | {
      id: string
      date: string
      kind: 'conversion'
      details: Successor & {
        id: string
        price: number
        successorPrice: number
        weight: number
        successorValue: number
        cashValue: number
        /** True when the successor was already held and the two slots merged. */
        merged: boolean
      }
    }
  | {
      id: string
      date: string
      kind: 'slot_removed'
      details: {
        /** Basket holdings whose slot this was. */
        holdings: string[]
        /** Custom scheme: the slot's target weight; null for rule-based schemes. */
        customWeight: number | null
        /** Custom scheme: holdings that received the weight; empty means it stays cash. */
        redistributedTo: string[]
      }
    }

export type BacktestWarning = {
  date: string
  code: 'invvol_median_sigma' | 'invvol_equal_weight'
  ids: string[]
  message: string
}

export type BacktestResult = {
  /** Calendar dates from the start date to the last calendar date. */
  dates: string[]
  /** NAV at each close; nav[0] = 1. */
  nav: number[]
  /** returns[i] is the portfolio return from dates[i] to dates[i + 1]. */
  returns: number[]
  /** Daily rf over the same periods as returns. */
  rf: number[]
  /** Benchmark simple returns over the same periods as returns. */
  benchmarkReturns: number[]
  /** Cash weight at each close, after events and any rebalance. */
  cash: number[]
  rebalances: RebalanceRecord[]
  latest: { date: string; weights: Weights; cash: number }
  events: BacktestEvent[]
  warnings: BacktestWarning[]
}

export const MAX_SUCCESSOR_DEPTH = 5

/** True when `date` is the first calendar date of a new period relative to `prev`. */
export function isRebalanceDate(
  prev: string,
  date: string,
  freq: Basket['rebalance'],
): boolean {
  switch (freq) {
    case 'none':
      return false
    case 'monthly':
      return prev.slice(0, 7) !== date.slice(0, 7)
    case 'quarterly': {
      const q = (d: string): string =>
        `${d.slice(0, 4)}Q${String(Math.floor((Number(d.slice(5, 7)) - 1) / 3))}`
      return q(prev) !== q(date)
    }
    case 'annual':
      return prev.slice(0, 4) !== date.slice(0, 4)
  }
}

function validateLifecycle(id: string, s: SeriesInput): void {
  const fail = (why: string): never => {
    throw new AnalyticsError('invalid_series', `series ${id}: ${why}`)
  }
  if (s.lastDate !== s.dates[s.dates.length - 1])
    fail('lastDate is not the last date')
  const succ = s.successor
  if (succ === null) return
  if (s.status !== 'ended') fail('successor on a series that has not ended')
  if (succ.id === null && succ.ratio !== 0)
    fail('a cash-out successor (id null) must have ratio 0')
  if (!(Number.isFinite(succ.ratio) && succ.ratio >= 0))
    fail('successor ratio must be finite and >= 0')
  if (!(Number.isFinite(succ.cashPerShare) && succ.cashPerShare >= 0)) {
    fail('successor cashPerShare must be finite and >= 0')
  }
  if (!isIsoDate(succ.effectiveDate) || succ.effectiveDate < s.lastDate) {
    fail('successor effectiveDate must be an ISO date on or after lastDate')
  }
}

/** Holdings plus every successor reachable from them, validating each series and chain length. */
function collectIds(
  holdings: readonly string[],
  series: BacktestInput['series'],
): string[] {
  const out = new Set<string>()
  for (const h of holdings) {
    let id = h
    for (let hops = 0; ; hops++) {
      const s = series[id]
      if (s === undefined)
        throw new AnalyticsError(
          'missing_series',
          `no series supplied for ${id}`,
        )
      out.add(id)
      const next = s.successor?.id ?? null
      if (next === null) break
      if (hops === MAX_SUCCESSOR_DEPTH) {
        throw new AnalyticsError(
          'successor_chain_too_long',
          `successor chain from ${h} is longer than ${String(MAX_SUCCESSOR_DEPTH)} links or loops`,
        )
      }
      id = next
    }
  }
  return [...out]
}

type Slot = {
  /** Basket holdings merged into this slot. */
  holdings: string[]
  /** Id currently held for this slot. */
  id: string
  sign: Sign
  custom: number
  cashedOut: boolean
}

export function runBacktest(input: BacktestInput): BacktestResult {
  const { basket, calendar, series } = input
  validateSeries(
    'calendar',
    calendar,
    calendar.map(() => 0),
    { nonNegative: true },
  )
  const holdingIds = basket.holdings.map((h) => h.id)
  const ids = collectIds(holdingIds, series)
  const benchSeries = series[basket.benchmark]
  if (benchSeries === undefined) {
    throw new AnalyticsError(
      'missing_series',
      `no series supplied for benchmark ${basket.benchmark}`,
    )
  }

  const prices = new Map<string, Column>()
  for (const id of new Set([...ids, basket.benchmark])) {
    const s = series[id] as SeriesInput
    validateSeries(id, s.dates, s.close, { nonNegative: true })
    validateLifecycle(id, s)
    prices.set(id, alignToCalendar(calendar, s.dates, s.close))
  }
  const priceCol = (id: string): Column => prices.get(id) as Column
  const returnCols = new Map<string, Column>()
  const returnCol = (id: string): Column => {
    let r = returnCols.get(id)
    if (r === undefined) {
      r = simpleReturns(priceCol(id))
      returnCols.set(id, r)
    }
    return r
  }

  if (basket.scheme === 'mcap') {
    const missing = ids.filter((id) => input.shares?.[id] === undefined)
    if (missing.length > 0) {
      throw new AnalyticsError(
        'missing_shares',
        `market-cap weighting needs shares data for: ${missing.join(', ')}`,
      )
    }
  }

  const start = resolveStartIndex(
    calendar,
    [...holdingIds, basket.benchmark].map((id) => ({
      id,
      values: priceCol(id),
    })),
    basket.start,
  )
  const rfCol = alignDailyRf(calendar, input.rf)

  /** Calendar index at whose close an ended series is converted or cashed out; Infinity if never. */
  const eventIndex = new Map<string, number>()
  for (const id of ids) {
    const s = series[id] as SeriesInput
    if (s.status !== 'ended') {
      eventIndex.set(id, Infinity)
      continue
    }
    const when = s.successor?.effectiveDate ?? s.lastDate
    const i = calendar.findIndex((d) => d >= when)
    eventIndex.set(id, i < 0 ? Infinity : i)
  }

  const price = (id: string, t: number): number => {
    const p = at(priceCol(id), t)
    if (p === null) {
      throw new AnalyticsError(
        'missing_data',
        `no price for ${id} on ${at(calendar, t)}`,
      )
    }
    return p
  }

  let slots: Slot[] = basket.holdings.map((h) => ({
    holdings: [h.id],
    id: h.id,
    sign: signOf(h.weight),
    custom: h.weight,
    cashedOut: false,
  }))
  let positions = new Map<string, number>()
  let cash = 1
  const events: BacktestEvent[] = []
  const warnings: BacktestWarning[] = []
  const rebalances: RebalanceRecord[] = []

  /**
   * Applies every conversion and cash-out due at close t and returns the
   * portfolio's growth factor from them. Positions are kept in units of the
   * pre-event portfolio value and renormalized at the end.
   */
  const processEvents = (t: number): number => {
    const date = at(calendar, t)
    let converted = false
    for (;;) {
      const slot = slots.find(
        (s) => !s.cashedOut && (eventIndex.get(s.id) as number) <= t,
      )
      if (slot === undefined) break
      const id = slot.id
      const succ = (series[id] as SeriesInput).successor
      const p = price(id, t)
      const w = positions.get(id) ?? 0
      positions.delete(id)
      // Drift sends a position whose price falls to 0 to weight 0, and rebalances refuse to buy one.
      if (p === 0 && w !== 0)
        throw new Error(`invariant: non-zero weight ${String(w)} on zero price`)
      const succId = succ?.id ?? null
      if (succ === null || succId === null) {
        // No successor: cash at the last close. Successor id null: cash at the deal price.
        const deal = succ?.cashPerShare ?? null
        const cashValue = deal === null ? w : p === 0 ? 0 : (w * deal) / p
        if (deal !== null) converted = true
        cash += cashValue
        slot.cashedOut = true
        events.push({
          id,
          date,
          kind: 'cash_out',
          details: { price: p, weight: w, cashPerShare: deal, cashValue },
        })
        continue
      }
      const ps = price(succId, t)
      const successorValue = p === 0 ? 0 : (w * succ.ratio * ps) / p
      const cashValue = p === 0 ? 0 : (w * succ.cashPerShare) / p
      converted = true
      positions.set(succId, (positions.get(succId) ?? 0) + successorValue)
      cash += cashValue
      const host = slots.find(
        (s) => s !== slot && !s.cashedOut && s.id === succId,
      )
      if (host === undefined) {
        slot.id = succId
      } else {
        if (basket.scheme !== 'custom' && host.sign !== slot.sign) {
          throw new AnalyticsError(
            'slot_sign_conflict',
            `${id} converts into ${succId} on ${date} but they are held on opposite sides`,
          )
        }
        host.holdings.push(...slot.holdings)
        host.custom += slot.custom
        slots = slots.filter((s) => s !== slot)
      }
      events.push({
        id,
        date,
        kind: 'conversion',
        details: {
          ...succ,
          id: succId,
          price: p,
          successorPrice: ps,
          weight: w,
          successorValue,
          cashValue,
          merged: host !== undefined,
        },
      })
    }
    if (!converted) return 1
    let growth = cash
    for (const v of positions.values()) growth += v
    if (!(growth > 0)) {
      throw new AnalyticsError(
        'nav_nonpositive',
        `portfolio value falls to zero or below on ${date}`,
      )
    }
    for (const [k, v] of positions) positions.set(k, v / growth)
    cash /= growth
    return growth
  }

  const rebalance = (t: number): void => {
    const date = at(calendar, t)
    const removed = slots.map((s) => s.cashedOut)
    if (removed.some(Boolean)) {
      const custom = basket.scheme === 'custom'
      const survivors = slots.filter((s) => !s.cashedOut)
      for (const s of slots.filter((x) => x.cashedOut)) {
        const sameSide = survivors
          .filter((x) => x.custom * s.custom > 0)
          .map((x) => x.id)
        events.push({
          id: s.id,
          date,
          kind: 'slot_removed',
          details: {
            holdings: s.holdings,
            customWeight: custom ? s.custom : null,
            redistributedTo: custom ? sameSide : [],
          },
        })
      }
      if (custom) {
        const next = redistributeCustom(
          slots.map((s) => s.custom),
          removed,
        )
        slots.forEach((s, i) => (s.custom = at(next, i)))
      }
      slots = survivors
    }

    const live = slots.map((s) => s.id)
    const signs = slots.map((s) => s.sign)
    let targets: number[] = []
    const lookAhead: string[] = []
    if (slots.length > 0) {
      switch (basket.scheme) {
        case 'custom':
          targets = slots.map((s) => s.custom)
          break
        case 'equal':
          targets = equalTargets(signs, basket.leverage)
          break
        case 'invvol': {
          const sigmas = live.map((id) => {
            const { sigma } = trailingSigma(
              returnCol(id),
              t,
              basket.invvolLookback,
            )
            if (sigma === 0) {
              throw new AnalyticsError(
                'zero_volatility',
                `${id} has zero trailing volatility on ${date}; inverse-vol weight is undefined`,
              )
            }
            return sigma
          })
          const r = invvolTargets(signs, sigmas, basket.leverage)
          targets = r.weights
          if (r.fallback.kind === 'median') {
            const short = r.fallback.indices.map((i) => at(live, i))
            warnings.push({
              date,
              code: 'invvol_median_sigma',
              ids: short,
              message: `fewer than 20 returns for ${short.join(', ')}; used the median volatility of the others`,
            })
          } else if (r.fallback.kind === 'equal') {
            warnings.push({
              date,
              code: 'invvol_equal_weight',
              ids: live,
              message: 'no holding has 20 returns; used equal weights',
            })
          }
          break
        }
        case 'mcap': {
          const caps = live.map((id) => {
            const sh = sharesAsOf(input.shares?.[id] as SharesEntry, date)
            if (sh.lookAhead) lookAhead.push(id)
            return price(id, t) * sh.shares
          })
          targets = mcapTargets(signs, caps, basket.leverage)
          break
        }
      }
    }
    live.forEach((id, i) => {
      if (at(targets, i) !== 0 && price(id, t) === 0) {
        throw new AnalyticsError(
          'zero_price_allocation',
          `rebalance on ${date} targets ${String(at(targets, i))} of ${id}, whose price is 0`,
        )
      }
    })
    positions = new Map(live.map((id, i) => [id, at(targets, i)]))
    cash = 1 - targets.reduce((a, b) => a + b, 0)
    rebalances.push({
      date,
      weights: Object.fromEntries(positions),
      cash,
      lookAhead,
    })
  }

  /** Earliest calendar index at which a live slot has an event due. */
  const nextDue = (): number =>
    Math.min(
      ...slots
        .filter((s) => !s.cashedOut)
        .map((s) => eventIndex.get(s.id) ?? Infinity),
    )

  processEvents(start)
  rebalance(start)
  let due = nextDue()
  // The daily loop works on arrays mirroring `positions`, synced around events and rebalances.
  let held: string[] = []
  let heldReturns: Column[] = []
  let w: number[] = []
  const sync = (): void => {
    held = [...positions.keys()]
    heldReturns = held.map(returnCol)
    w = held.map((id) => positions.get(id) ?? NaN)
  }
  sync()

  const dates = calendar.slice(start)
  const nav = [1]
  const returns: number[] = []
  const rfOut: number[] = []
  const cashOut = [cash]
  const benchReturns = returnCol(basket.benchmark)
  const benchOut: number[] = []

  for (let t = start + 1; t < calendar.length; t++) {
    const date = at(calendar, t)
    const rf = at(rfCol, t)
    if (rf === null)
      throw new AnalyticsError(
        'missing_data',
        `no risk-free rate on or before ${date}`,
      )
    const r = heldReturns.map((col, i) => {
      const x = col[t]
      if (x === null || x === undefined)
        throw new AnalyticsError(
          'missing_data',
          `no return for ${at(held, i)} on ${date}`,
        )
      return x
    })
    const growth = portfolioGrowth(w, cash, r, rf)
    if (!(growth > 0)) {
      throw new AnalyticsError(
        'nav_nonpositive',
        `portfolio value falls to zero or below on ${date}`,
      )
    }
    const drifted = driftWeights(w, cash, r, rf)
    w = drifted.weights
    cash = drifted.cash
    let eventGrowth = 1
    const rebalancing = isRebalanceDate(
      at(calendar, t - 1),
      date,
      basket.rebalance,
    )
    if (t >= due || rebalancing)
      positions = new Map(held.map((id, i) => [id, at(w, i)]))
    if (t >= due) eventGrowth = processEvents(t)
    const ret = growth * eventGrowth - 1
    returns.push(ret)
    nav.push(at(nav, nav.length - 1) * (1 + ret))
    rfOut.push(rf)
    const rb = at(benchReturns, t)
    if (rb === null)
      throw new AnalyticsError('missing_data', `no benchmark return on ${date}`)
    benchOut.push(rb)
    if (rebalancing) rebalance(t)
    if (t >= due || rebalancing) {
      sync()
      due = nextDue()
    }
    cashOut.push(cash)
  }

  return {
    dates,
    nav,
    returns,
    rf: rfOut,
    benchmarkReturns: benchOut,
    cash: cashOut,
    rebalances,
    latest: {
      date: at(dates, dates.length - 1),
      weights: Object.fromEntries(held.map((id, i) => [id, at(w, i)])),
      cash,
    },
    events,
    warnings,
  }
}
