# Baskets

Build custom baskets and benchmarks from US and international stocks and ETFs, crypto,
rates, commodities, FX and Polymarket contracts, then see their performance, risk, risk
contribution, concentration and factor exposure.

- **Web** (`web/`): React + Vite + TypeScript, served from GitHub Pages at
  <https://baskets.christopherjlee.ca>. All analytics run client-side.
- **Data API** (`server/`): Python FastAPI at <https://baskets-api.christopherjlee.ca>
  (local dev port 8800). It only fetches, caches and serves data.

The interface between the two halves, and the basket format, is defined in
[`docs/CONTRACTS.md`](docs/CONTRACTS.md).

## Layout

```
server/               FastAPI data API (Python >= 3.12, managed with uv)
  app/main.py         FastAPI app, routes, error format, CORS, rate limits
  app/service.py      cache-first series / meta / shares / search / ended logic
  app/timeseries.py   pure series logic: total return, FX, finality, overlap, ended
  app/store.py        SQLite (WAL) cache
  app/providers/      Provider protocol (base.py) and Yahoo provider (yahoo.py)
  app/edgar.py        SEC EDGAR ticker map and point-in-time shares
  app/refresh.py      nightly refresh job (python -m app.refresh)
  data/corporate_actions.toml  curated mergers / cash-outs / renames
  tests/              pytest suite (test_live.py hits real services; skipped by default)
web/                  Vite + React + TypeScript single-page app
  src/config.ts       API base URL (VITE_API_BASE, default http://localhost:8800)
  src/lib/analytics/  pure analytics functions
  src/api/            API client
  src/pages/          routed pages
  public/CNAME        GitHub Pages custom domain
docs/CONTRACTS.md     API and basket contracts
.github/workflows/    CI
```

## Development

### Server

Dependencies are managed with [uv](https://docs.astral.sh/uv/); `uv sync` creates
`server/.venv` from `server/uv.lock` using the Python version in `server/.python-version`.

```sh
cd server
uv sync                                               # install
uv run uvicorn app.main:app --reload --port 8800      # run at http://localhost:8800
uv run python -m app.refresh                          # refresh every cached id, print a report
uv run pytest                                         # tests (no network)
uv run pytest -m live -s                              # live smoke tests against Yahoo / SEC
uv run ruff check && uv run ruff format --check       # lint + format check
uv run mypy                                           # type check (strict)
```

Environment:

- `BASKETS_DB`: SQLite path (default `server/data/baskets.db`; gitignored). yfinance keeps
  its timezone cache in `yfinance-cache/` next to it.
- `SEC_USER_AGENT`: User-Agent for SEC EDGAR, which SEC requires to name the requester and a
  contact (for example `"Organisation Name admin@domain"`). Unset, EDGAR features are off:
  `/v1/shares` answers `unsupported` for US-listed equities and the refresh job skips rename
  detection, saying so in its report.

The refresh job exits 1 when any id could not be refreshed (those ids keep serving their last
good data with `stale: true`). An invalid `data/corporate_actions.toml` stops both the API and
the job at startup. Behind a reverse proxy, run uvicorn with
`--proxy-headers --forwarded-allow-ips 127.0.0.1` so rate limits apply per client IP.

### Web

Requires Node 24 (`web/.nvmrc`).

```sh
cd web
npm ci                  # install from package-lock.json
npm run dev             # dev server at http://localhost:5173
npm run typecheck       # tsc --noEmit
npm run lint            # eslint
npm run format:check    # prettier --check (npm run format to fix)
npm test                # vitest run
npm run build           # production build into web/dist
```

Set `VITE_API_BASE` (for example in `web/.env.local`) to point the app at a different API;
it must be an absolute http(s) URL without a trailing slash.
