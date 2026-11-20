// Cross-checks against tools/reference/generate_fixtures.py (numpy, pandas,
// scikit-learn, statsmodels). Every number is compared with
// |a - b| <= 1e-9 * max(|a|, |b|) + 1e-12 unless a looser bound is stated.
import { describe, expect, it } from 'vitest'
import type { FactorsResponse, SharesEntry } from '../../api/types'
import { parseBasket } from '../basket'
import { runBacktest, type BacktestResult, type SeriesInput } from './backtest'
import {
  exposures,
  riskEffectiveN,
  weightConcentration,
  type ExposureMeta,
} from './concentration'
import { factorRegression } from './factors'
import { basketRisk, type Estimator } from './risk'
import { performanceStats } from './stats'
import { expectClose, readFixture } from './testing'
import { at } from './numeric'

type Market = {
  calendar: string[]
  series: Record<string, SeriesInput>
  rf: { dates: string[]; annualRate: number[] }
  factors: FactorsResponse
  shares: Record<string, SharesEntry>
  meta: Record<string, ExposureMeta>
}

type Scenario = {
  name: string
  basket: unknown
  expected: {
    backtest: Record<string, unknown> & { events: Record<string, unknown>[] }
    stats: unknown
    risk: Record<
      Estimator,
      (Record<string, unknown> & { ids: string[] }) | null
    >
    concentration: unknown
    factors?: unknown
    exposures?: unknown
  }
}

const market = readFixture('market') as Market
const names = readFixture('index') as string[]

/** Reshapes TS events to the reference's flat layout. */
function flatEvents(result: BacktestResult): Record<string, unknown>[] {
  return result.events.map((e) => {
    const base = { id: e.id, date: e.date, kind: e.kind }
    switch (e.kind) {
      case 'cash_out':
        return {
          ...base,
          weight: e.details.weight,
          cashPerShare: e.details.cashPerShare,
          cashValue: e.details.cashValue,
        }
      case 'conversion':
        return {
          ...base,
          weight: e.details.weight,
          successorValue: e.details.successorValue,
          cashValue: e.details.cashValue,
          merged: e.details.merged,
        }
      case 'slot_removed':
        return { ...base, ...e.details }
    }
  })
}

/** Permutes a reference matrix/vector keyed by `from` ids into `to` order. */
function reorder<T>(xs: T[], from: string[], to: string[]): T[] {
  return to.map((id) => at(xs, from.indexOf(id)))
}

describe.each(names)('reference fixture %s', (name) => {
  const scenario = readFixture(name) as Scenario
  const basket = parseBasket(scenario.basket)
  const result = runBacktest({
    basket,
    calendar: market.calendar,
    series: market.series,
    rf: market.rf,
    shares: market.shares,
  })
  const exp = scenario.expected

  it('backtest matches', () => {
    const { events, ...rest } = exp.backtest
    expectClose(
      {
        dates: result.dates,
        nav: result.nav,
        returns: result.returns,
        rf: result.rf,
        benchmarkReturns: result.benchmarkReturns,
        cash: result.cash,
        rebalances: result.rebalances,
        latest: result.latest,
        warnings: result.warnings.map(({ date, code, ids }) => ({
          date,
          code,
          ids,
        })),
      },
      rest,
    )
    expectClose(flatEvents(result), events, '$.events')
  })

  it('performance stats match', () => {
    expectClose(performanceStats(result), exp.stats)
  })

  it.each(['sample', 'ledoit_wolf', 'ewma'] as const)(
    'risk (%s) matches',
    (estimator) => {
      const e = exp.risk[estimator]
      if (e === null) {
        expect(Object.keys(result.latest.weights)).toHaveLength(0)
        return
      }
      const r = basketRisk({
        calendar: market.calendar,
        series: market.series,
        weights: result.latest.weights,
        estimator,
      })
      const from = e.ids
      const to = r.ids
      expect([...to].sort()).toEqual([...from].sort())
      const cov = reorder(e.covariance as number[][], from, to).map((row) =>
        reorder(row, from, to),
      )
      const corr = reorder(e.correlation as (number | null)[][], from, to).map(
        (row) => reorder(row, from, to),
      )
      const contributions = e.contributions as Record<string, number[]> | null
      expectClose(
        {
          observations: r.observations,
          startDate: r.startDate,
          covariance: r.covariance,
          correlation: r.correlation,
          vols: r.vols,
          sigmaP: r.sigmaP,
          contributions: r.contributions,
          diversificationRatio: r.diversificationRatio,
          riskEffectiveN:
            r.contributions === null
              ? null
              : riskEffectiveN(r.contributions.pctCtr),
          shrinkage: r.shrinkage,
        },
        {
          observations: e.observations,
          startDate: e.startDate,
          covariance: cov,
          correlation: corr,
          vols: reorder(e.vols as number[], from, to),
          sigmaP: e.sigmaP,
          contributions:
            contributions === null
              ? null
              : {
                  mctr: reorder(contributions.mctr ?? [], from, to),
                  ctr: reorder(contributions.ctr ?? [], from, to),
                  pctCtr: reorder(contributions.pctCtr ?? [], from, to),
                },
          diversificationRatio: e.diversificationRatio,
          riskEffectiveN: e.riskEffectiveN,
          shrinkage: e.shrinkage,
        },
      )
    },
  )

  it('weight concentration matches', () => {
    expectClose(
      weightConcentration(Object.values(result.latest.weights)),
      exp.concentration,
    )
  })

  if (exp.factors !== undefined) {
    it('factor regression matches statsmodels', () => {
      expectClose(
        factorRegression({
          dates: result.dates,
          returns: result.returns,
          factorData: market.factors,
        }),
        { factors: market.factors.factors, ...(exp.factors as object) },
      )
    })
  }

  if (exp.exposures !== undefined) {
    it('exposures match', () => {
      const ex = exposures(result.latest.weights, market.meta)
      const asMap = (list: { key: string; net: number; gross: number }[]) =>
        Object.fromEntries(
          list.map(({ key, net, gross }) => [key, { net, gross }]),
        )
      expectClose(
        {
          assetClass: asMap(ex.assetClass),
          sector: asMap(ex.sector),
          country: asMap(ex.country),
          currency: asMap(ex.currency),
        },
        exp.exposures,
      )
    })
  }
})
