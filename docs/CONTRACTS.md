# Contracts

## Data API (server → web)
Base URL: `VITE_API_BASE` (prod `https://baskets-api.christopherjlee.ca`, dev `http://localhost:8800`). All GET, JSON, UTF-8. Dates are ISO `YYYY-MM-DD`, ascending, unique. Numbers are finite JSON numbers (never NaN/Infinity). CORS allows `https://baskets.christopherjlee.ca` and `http://localhost:5173`.

Errors: non-2xx bodies are `{"error":{"code":string,"message":string}}`. 400 bad params, 404 unknown route, 429 rate limited, 500 internal. Per-id failures inside 200 responses go in an `errors` map (codes: `not_found`, `upstream_error`, `fetch_limit`, `unsupported`).

### Symbol ids
- Yahoo symbols: `AAPL`, `7203.T`, `BTC-USD`, `EURUSD=X`, `GC=F`, `^TNX`. Server upper-cases Yahoo ids.
- `pm:<clobTokenId>` — Polymarket YES-outcome token. close = price in [0,1].
- `fred:<SERIES_ID>` — FRED series level (may be ≤ 0). Overlay / risk-free only; never holdable.

### GET /v1/health
`{"status":"ok","version":string,"db":"ok"|"not_configured"|"error"}`

### GET /v1/search?q=<1..64 chars>&limit=<1..25, default 10>
`{"results":[{"id":string,"name":string,"kind":Kind,"exchange":string|null,"currency":string|null}]}`
Kind = `equity|etf|index|crypto|fx|future|mutualfund|polymarket|macro`. Merges Yahoo search and Polymarket market search (one result per YES token).

### GET /v1/series?ids=<comma list, 1..50 unique>&base=USD
`base` only `USD` in v1 (else 400).
`{"base":"USD","series":{[id]:{"id":string,"dates":string[],"close":number[],"currency":string,"converted":boolean,"source":"yahoo"|"polymarket"|"fred","asOf":string}},"errors":{[id]:{"code":string,"message":string}}}`
- Yahoo closes are split- and dividend-adjusted (total return). Non-USD listings converted to USD with the daily `<CCY>USD=X` close forward-filled onto the asset's dates; `converted:true`, `currency` = original currency. Minor units (GBp, ZAc, ILA) divided by 100 first.
- `dates.length == close.length >= 1`. Yahoo/Polymarket closes ≥ 0.
- At most 10 ids not already cached are fetched per request; the rest get `fetch_limit` (client retries those).
- Each requested id appears in exactly one of `series` or `errors`.

### GET /v1/meta?ids=<comma list, 1..50 unique>
`{"meta":{[id]:Meta},"errors":{...}}`
Meta = `{"id","name","kind":Kind,"assetClass":"equity"|"fixed_income"|"commodity"|"crypto"|"fx"|"prediction"|"macro"|"multi_asset","sector":string|null,"industry":string|null,"country":string|null,"currency":string,"marketCap":number|null,"sharesOutstanding":number|null,"fundSectorWeights":{[sector]:number}|null,"polymarket":{"question":string,"outcome":string,"endDate":string|null,"resolved":boolean,"resolvedValue":0|1|null}|null}`
`fundSectorWeights` (ETFs/funds only) are decimals summing to ~1.

### GET /v1/factors?model=ff5mom&region=us|developed
`{"model":"ff5mom","region":string,"factors":["Mkt-RF","SMB","HML","RMW","CMA","Mom"],"dates":string[],"values":{[factor or "RF"]:number[]}}` — daily decimal returns (Ken French percent ÷ 100). All arrays same length as `dates`.

### GET /v1/rf
`{"id":"fred:DGS3MO","dates":string[],"annualRate":number[]}` — decimal annual yield. Daily rf = (1+annualRate)^(1/252) − 1.

## Basket (web; persisted in localStorage and share URL)
Share URL: `https://baskets.christopherjlee.ca/#/b/<lz-string compressToEncodedURIComponent(JSON.stringify(basket))>`.

```ts
type Basket = {
  v: 1;
  name: string;                                  // 1..80 chars
  holdings: { id: string; weight: number }[];    // 1..50, unique ids, no fred: ids. weight = signed decimal (0.25 = 25%, negative = short). Magnitude used only when scheme = "custom"; other schemes use only the sign (0 → long).
  scheme: "custom" | "equal" | "mcap" | "invvol";
  invvolLookback: number;                        // trading days, 20..756, default 63
  rebalance: "none" | "monthly" | "quarterly" | "annual";
  leverage: number;                              // 0 < leverage ≤ 10, default 1. Non-custom schemes: target weights scaled so Σ|w| = leverage. Ignored for custom.
  benchmark: string;                             // symbol id, default "SPY"
  overlays: string[];                            // 0..5 pm:/fred:/yahoo ids drawn on charts
  start: string | null;                          // ISO date, or null = earliest date all holdings have data
};
```
Custom weights are used as entered; cash = 1 − Σw earns daily rf (negative cash = borrowing at rf).

## Amendment A — corporate events, data quality, shares (supersedes conflicting text above)

### Additional fields on every `/v1/series` entry
- `"stale": boolean` — true when the latest refresh attempt for this id failed and the data is the last good cache.
- `"status": "active" | "ended"` — `ended` = no longer trades (delisted, renamed, acquired, or Polymarket market resolved).
- `"lastDate": string` — equals the last element of `dates`.
- `"successor": null | {"id": string, "ratio": number, "cashPerShare": number, "effectiveDate": string, "source": "edgar_rename" | "corporate_actions"}` — only non-null when `status = "ended"`. Each old share becomes `ratio` successor shares plus `cashPerShare` (in USD) cash at the close of `effectiveDate` (≥ `lastDate`). Renames are `ratio: 1, cashPerShare: 0`.
- `"adjustment": {"method": "total_return" | "futures_roll" | "none", "backAdjustedFrom": string | null}` — `total_return` for Yahoo equities/ETFs/funds; `futures_roll` for continuous futures (task 2B): before `backAdjustedFrom` each detected roll day's return is neutralized; from `backAdjustedFrom` on, the series is ratio back-adjusted from captured front/next contract closes. `none` otherwise.

### Ended detection
- A Yahoo id becomes `ended` when it has had no new bar for ≥ 5 consecutive SPY trading days, or Yahoo stops returning it after it was cached. It returns to `active` if bars resume.
- Renames: when an ended id's stored SEC CIK now maps to a different current ticker, `successor` = that ticker with `source: "edgar_rename"`.
- Mergers / other actions: entries in `server/data/corporate_actions.toml` override everything:
  ```toml
  [[action]]
  id = "OLD"
  successor = "NEW"        # omit for pure cash-out
  ratio = 0.8              # successor shares per old share (omit or 0 if no successor)
  cash_per_share = 12.5    # USD
  effective_date = "2027-03-14"
  note = "free text"
  ```
- An ended id with no successor is a cash-out at its last close.
- A resolved Polymarket market is `ended` with no successor; its last close is exactly 0 or 1.

### GET /v1/shares?ids=<comma list, 1..50 unique>
`{"shares":{[id]:{"source":"edgar"|"current","knownDates":string[],"shares":number[]}},"errors":{...}}`
- `edgar`: one entry per reported value, `knownDates` = SEC filing date (when the value became public), ascending. Use the latest value with `knownDate ≤ t` at date t (no look-ahead).
- `current`: a single value from Yahoo's current shares outstanding, `knownDates = [today]`. Using it for past dates is look-ahead; the client must flag it.

### GET /v1/ended
`{"ended":[{"id":string,"lastDate":string,"successor":Successor|null,"reviewed":boolean}]}` — every cached id with `status = "ended"`. `reviewed = false` when an ended Yahoo id has neither an EDGAR rename nor a corporate_actions entry (needs a human to add one; treated as cash-out meanwhile).

## Clarifications to the contract and Amendment A (server task 2A)
These state what the server does where the text above is silent or ambiguous.

**Errors and caching**
- Non-2xx bodies are always `{"error":{"code","message"}}`. Codes: `bad_request` (400: invalid, empty, duplicate or more than 50 ids; `base` other than `USD`; out-of-range `q`/`limit`), `not_found` (404 unknown route, including trailing-slash variants), `method_not_allowed` (405), `rate_limited` (429), `internal` (500), `upstream_error` (502, `/v1/search` only, when Yahoo fails and no cached results exist).
- Ids are compared after normalisation (Yahoo ids upper-cased), so `aapl,AAPL` is a duplicate. Response keys are normalised ids.
- `Cache-Control: public, max-age=3600` on 200 data responses, except `no-store` when the body contains a per-id `fetch_limit` or `upstream_error` (a retry may succeed). `/v1/health` is `no-store`.
- Rate limits per client IP: 120 requests/minute on `/v1/series`, `/v1/meta`, `/v1/shares`, `/v1/ended`; 60/minute on `/v1/search`. Behind a reverse proxy, uvicorn must run with `--proxy-headers --forwarded-allow-ips 127.0.0.1` so the client IP is used.
- The `fetch_limit` budget (at most 10 ids per request that need an upstream fetch) also applies to `/v1/meta` and `/v1/shares`. The `<CCY>USD=X` FX pair and `SPY` (the ended-detection reference) are fetched when first needed and do not count toward it.
- `/v1/health` reports `db: "ok" | "error"`; `not_configured` is no longer produced (the database path defaults to `server/data/baskets.db`, overridable with `BASKETS_DB`).

**Series**
- `asOf` is the ISO 8601 UTC timestamp of the last successful fetch or refresh of the id.
- Only completed daily bars are stored: a bar dated D (exchange-local) is kept once one hour has passed since the exchange's regular session close on D.
- `USX` (US cents, e.g. `ZC=F`) is a minor unit like `GBp`/`ZAc`/`ILA`: divided by 100, `converted: true`, no FX pair.
- Dates before the first available `<CCY>USD=X` close are omitted (the FX rate cannot be forward-filled backwards), so a converted series may start later than the listing. If no date has a rate, the id gets `unsupported`.
- A converted series is `stale` if either the listing or its FX pair is stale.
- Yahoo closes must be finite and > 0. An id with any non-positive close (e.g. `CL=F` on 2020-04-20) gets `unsupported` naming the bar.
- Until task 2B, futures, indices, FX and crypto are served with `adjustment.method: "none"` (raw close); `total_return` applies to Yahoo `EQUITY`, `ETF` and `MUTUALFUND` instruments.
- Instrument types other than equity, ETF, index, crypto, FX, future and mutual fund (e.g. options) get `unsupported`.

**Lifecycle**
- An id Yahoo stops returning is `ended` but not `stale` (the refresh succeeded in learning that).
- A `corporate_actions.toml` entry makes the id `ended` even if Yahoo still returns bars (ticker reuse), and the series is served only through `effective_date`, so `lastDate ≤ effectiveDate`. An entry whose `effective_date` precedes all cached bars yields `unsupported`.
- A pure cash-out entry (no `successor`) is served as `successor: {"id": null, "ratio": 0, "cashPerShare": <cash_per_share>, "effectiveDate": ..., "source": "corporate_actions"}`; `Successor.id` is therefore `string | null`. `cash_per_share` is required when `successor` is omitted; `effective_date` may be a quoted `"YYYY-MM-DD"` string or a TOML date. The file is validated at API and refresh-job start; an invalid file stops both.
- An EDGAR rename's `effectiveDate` is the old id's `lastDate`. A rename is inferred only when the stored CIK now maps to exactly one ticker and that ticker is not the old id; otherwise the id stays unreviewed.
- The 5-SPY-day rule can flag listings on exchanges with long holidays (e.g. Lunar New Year closures) as `ended` until their bars resume.

**Shares and meta**
- `edgar` is used for US-listed equities (Yahoo symbols without an exchange suffix) for which SEC has `dei:EntityCommonStockSharesOutstanding` facts; others use `current`. Without `SEC_USER_AGENT` on the server, US-listed equities get `unsupported` with a message saying EDGAR is disabled.
- `edgar` entries: the value in force at date t is the fact with the latest period end among facts filed on or before t (ties: later filing, larger accession number, larger value); an entry is emitted on each filing date where that value changes. `current` uses today's UTC date.
- `assetClass` rules: equity → `equity`; crypto → `crypto`; FX → `fx`; Treasury yield indices (`^IRX`, `^FVX`, `^TNX`, `^TYX`) → `fixed_income`, other indices → `equity`; financial futures by root (e.g. `ES`, `NQ` → `equity`; `ZN`, `ZB`, `SR3` → `fixed_income`; `6E`, `DX` → `fx`; `BTC` → `crypto`), other futures → `commodity`; ETFs and funds → `equity` if ≥ 50% stock, `fixed_income` if ≥ 50% bonds, `commodity` if Yahoo's category mentions commodities, else `multi_asset`.
- `/v1/search` `currency` is always `null` for Yahoo results (Yahoo search does not return it).
