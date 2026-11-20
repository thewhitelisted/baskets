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
  app/main.py         FastAPI app and routes
  tests/              pytest suite
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
uv run pytest                                         # tests
uv run ruff check && uv run ruff format --check       # lint + format check
uv run mypy                                           # type check (strict)
```

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
