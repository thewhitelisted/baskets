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
