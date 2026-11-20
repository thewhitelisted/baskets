"""In-memory upstream for tests: a mutable 'world' of histories the fake provider serves."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta

from app.providers.base import (
    History,
    Kind,
    Meta,
    NotFoundError,
    ProviderError,
    SearchResult,
    Source,
)
from app.timeseries import Bar

SPY_START = date(2026, 1, 2)
SPY_LENGTH = 200


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_history(
    closes: Sequence[float],
    start: date | None = None,
    dividends: Mapping[int, float] | None = None,
    splits: Mapping[int, float] | None = None,
    kind: Kind = "equity",
    currency: str = "USD",
    dates: Sequence[date] | None = None,
) -> History:
    """Bars on `dates`, else weekdays from `start`, else the last len(closes) SPY days."""
    if dates is not None:
        ds = list(dates)
    elif start is not None:
        ds = weekdays(start, len(closes))
    else:
        ds = weekdays(SPY_START, SPY_LENGTH)[-len(closes) :]
    dividends = dividends or {}
    splits = splits or {}
    bars = tuple(
        Bar(d, c, dividends.get(i, 0.0), splits.get(i, 0.0))
        for i, (d, c) in enumerate(zip(ds, closes, strict=True))
    )
    return History(
        bars=bars,
        currency=currency,
        kind=kind,
        tz="America/New_York",
        session_close=time(16, 0),
        adjustment="total_return" if kind in ("equity", "etf", "mutualfund") else "none",
        back_adjusted_from=None,
    )


def extend(
    history: History, closes: Sequence[float], dividends: Mapping[int, float] | None = None
) -> History:
    """`history` plus new weekday bars after its last date."""
    last = history.bars[-1].date
    ds = weekdays(last + timedelta(days=1), len(closes))
    dividends = dividends or {}
    new = tuple(
        Bar(d, c, dividends.get(i, 0.0), 0.0)
        for i, (d, c) in enumerate(zip(ds, closes, strict=True))
    )
    return replace(history, bars=history.bars + new)


def make_meta(
    id: str, kind: Kind = "equity", shares: float | None = 1000.0, currency: str = "USD"
) -> Meta:
    return Meta(
        id=id,
        name=f"{id} Inc.",
        kind=kind,
        asset_class="equity",
        sector="Technology" if kind == "equity" else None,
        industry=None,
        country="United States",
        currency=currency,
        market_cap=1e9,
        shares_outstanding=shares,
        fund_sector_weights={"technology": 0.6, "healthcare": 0.4} if kind == "etf" else None,
    )


@dataclass
class FakeProvider:
    source: Source = "yahoo"
    world: dict[str, History] = field(default_factory=dict)
    errors: dict[str, ProviderError] = field(default_factory=dict)
    recent_errors: dict[str, ProviderError] = field(default_factory=dict)
    metas: dict[str, Meta | ProviderError] = field(default_factory=dict)
    search_results: list[SearchResult] | ProviderError = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def upstream_calls(self) -> int:
        return len(self.calls)

    def fetch_history(self, id: str) -> History:
        self.calls.append(("history", id))
        if id in self.errors:
            raise self.errors[id]
        if id not in self.world:
            raise NotFoundError(f"no data for {id}")
        return self.world[id]

    def fetch_recent(
        self, starts: Mapping[str, date], cutoffs: Mapping[str, date]
    ) -> dict[str, tuple[Bar, ...] | ProviderError]:
        out: dict[str, tuple[Bar, ...] | ProviderError] = {}
        for id, start in starts.items():
            self.calls.append(("recent", id))
            if id in self.recent_errors:
                out[id] = self.recent_errors[id]
            elif id not in self.world:
                out[id] = NotFoundError(f"no data for {id}")
            else:
                out[id] = tuple(b for b in self.world[id].bars if start <= b.date <= cutoffs[id])
        return out

    def fetch_meta(self, id: str) -> Meta:
        self.calls.append(("meta", id))
        meta = self.metas.get(id)
        if meta is None:
            raise NotFoundError(f"no meta for {id}")
        if isinstance(meta, ProviderError):
            raise meta
        return meta

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.calls.append(("search", query))
        if isinstance(self.search_results, ProviderError):
            raise self.search_results
        return self.search_results[:limit]
