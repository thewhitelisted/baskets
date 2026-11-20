// Response types for the data API, per docs/CONTRACTS.md including Amendment A.

export type IsoDate = string

export type ErrorCode =
  'not_found' | 'upstream_error' | 'fetch_limit' | 'unsupported'

export type IdError = { code: ErrorCode; message: string }

export type IdErrors = Record<string, IdError>

/** Body of every non-2xx response. */
export type ApiErrorBody = { error: { code: string; message: string } }

export type Kind =
  | 'equity'
  | 'etf'
  | 'index'
  | 'crypto'
  | 'fx'
  | 'future'
  | 'mutualfund'
  | 'polymarket'
  | 'macro'

export type AssetClass =
  | 'equity'
  | 'fixed_income'
  | 'commodity'
  | 'crypto'
  | 'fx'
  | 'prediction'
  | 'macro'
  | 'multi_asset'

export type HealthResponse = {
  status: 'ok'
  version: string
  db: 'ok' | 'not_configured' | 'error'
}

export type SearchResult = {
  id: string
  name: string
  kind: Kind
  exchange: string | null
  currency: string | null
}

export type SearchResponse = { results: SearchResult[] }

export type Successor = {
  /** null: a cash-out at a deal price (ratio is 0; each share becomes cashPerShare USD). */
  id: string | null
  ratio: number
  cashPerShare: number
  effectiveDate: IsoDate
  source: 'edgar_rename' | 'corporate_actions'
}

export type Adjustment = {
  method: 'total_return' | 'futures_roll' | 'none'
  backAdjustedFrom: IsoDate | null
}

export type SeriesEntry = {
  id: string
  dates: IsoDate[]
  close: number[]
  currency: string
  converted: boolean
  source: 'yahoo' | 'polymarket' | 'fred'
  /** ISO 8601 UTC timestamp of the last successful fetch or refresh. */
  asOf: string
  stale: boolean
  status: 'active' | 'ended'
  lastDate: IsoDate
  /** Non-null only when status is "ended". */
  successor: Successor | null
  adjustment: Adjustment
}

export type SeriesResponse = {
  base: 'USD'
  series: Record<string, SeriesEntry>
  errors: IdErrors
}

export type PolymarketMeta = {
  question: string
  outcome: string
  endDate: IsoDate | null
  resolved: boolean
  resolvedValue: 0 | 1 | null
}

export type Meta = {
  id: string
  name: string
  kind: Kind
  assetClass: AssetClass
  sector: string | null
  industry: string | null
  country: string | null
  currency: string
  marketCap: number | null
  sharesOutstanding: number | null
  fundSectorWeights: Record<string, number> | null
  polymarket: PolymarketMeta | null
}

export type MetaResponse = { meta: Record<string, Meta>; errors: IdErrors }

export type FactorName = 'Mkt-RF' | 'SMB' | 'HML' | 'RMW' | 'CMA' | 'Mom'

export type FactorsResponse = {
  model: 'ff5mom'
  region: string
  factors: FactorName[]
  dates: IsoDate[]
  values: Record<FactorName | 'RF', number[]>
}

export type RfResponse = {
  id: 'fred:DGS3MO'
  dates: IsoDate[]
  annualRate: number[]
}

export type SharesEntry = {
  source: 'edgar' | 'current'
  knownDates: IsoDate[]
  shares: number[]
}

export type SharesResponse = {
  shares: Record<string, SharesEntry>
  errors: IdErrors
}

export type EndedEntry = {
  id: string
  lastDate: IsoDate
  successor: Successor | null
  reviewed: boolean
}

export type EndedResponse = { ended: EndedEntry[] }
