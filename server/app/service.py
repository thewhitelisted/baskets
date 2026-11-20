"""Cache-first data service behind the routes: series, meta, shares, search, ended."""

import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, TypedDict

from app.corporate_actions import CorporateAction
from app.edgar import DISABLED_MESSAGE, EdgarClient, SharesFact, point_in_time_shares
from app.ids import is_us_listing, source_of
from app.lifecycle import Lifecycle, Status, Successor, resolve_lifecycle
from app.providers.base import (
    ErrorCode,
    History,
    Meta,
    NotFoundError,
    Provider,
    ProviderError,
    RateLimitedError,
    Source,
    UnsupportedError,
    UpstreamError,
)
from app.store import Instrument, Store
from app.timeseries import (
    convert_to_usd,
    fx_pair_id,
    is_ended,
    total_return,
    usd_conversion,
)

log = logging.getLogger(__name__)

FETCH_LIMIT = 10
REFERENCE_ID = "SPY"
META_TTL = timedelta(days=7)
SHARES_TTL = timedelta(days=7)
SEC_TICKERS_TTL = timedelta(days=7)
SEARCH_TTL = timedelta(days=1)
TRANSIENT_CODES: frozenset[ErrorCode] = frozenset({"fetch_limit", "upstream_error"})
UNSUPPORTED_SOURCES: dict[Source, str] = {
    "polymarket": "Polymarket ids are not supported yet",
    "fred": "FRED ids are not supported yet",
}


class ErrorItem(TypedDict):
    code: ErrorCode
    message: str


class SuccessorJson(TypedDict):
    id: str | None
    ratio: float
    cashPerShare: float
    effectiveDate: str
    source: str


class AdjustmentJson(TypedDict):
    method: str
    backAdjustedFrom: str | None


class SeriesJson(TypedDict):
    id: str
    dates: list[str]
    close: list[float]
    currency: str
    converted: bool
    source: str
    asOf: str
    stale: bool
    status: str
    lastDate: str
    successor: SuccessorJson | None
    adjustment: AdjustmentJson


class SharesJson(TypedDict):
    source: Literal["edgar", "current"]
    knownDates: list[str]
    shares: list[float]


class EndedJson(TypedDict):
    id: str
    lastDate: str
    successor: SuccessorJson | None
    reviewed: bool


class SearchJson(TypedDict):
    id: str
    name: str
    kind: str
    exchange: str | None
    currency: str | None


class MetaJson(TypedDict):
    id: str
    name: str
    kind: str
    assetClass: str
    sector: str | None
    industry: str | None
    country: str | None
    currency: str
    marketCap: float | None
    sharesOutstanding: float | None
    fundSectorWeights: dict[str, float] | None
    polymarket: None


@dataclass
class PerIdResult[T]:
    values: dict[str, T] = field(default_factory=dict)
    errors: dict[str, ErrorItem] = field(default_factory=dict)

    def fail(self, id: str, error: ProviderError) -> None:
        self.errors[id] = {"code": error.code, "message": str(error)}

    @property
    def cacheable(self) -> bool:
        """False when a retry could give a different answer (fetch_limit, upstream_error)."""
        return not any(e["code"] in TRANSIENT_CODES for e in self.errors.values())


class FetchBudget:
    """At most FETCH_LIMIT ids per request may trigger upstream fetches; once upstream
    rate-limits, no further fetches are attempted in this request."""

    def __init__(self, limit: int = FETCH_LIMIT) -> None:
        self._remaining = limit
        self._spent: set[str] = set()
        self.rate_limited = False

    def take(self, id: str) -> None:
        if self.rate_limited:
            raise RateLimitedError("upstream is rate limiting; retry later")
        if id in self._spent:
            return
        if self._remaining == 0:
            raise FetchLimitError(f"more than {FETCH_LIMIT} uncached ids; retry this id")
        self._remaining -= 1
        self._spent.add(id)

    def note(self, error: ProviderError) -> None:
        if isinstance(error, RateLimitedError):
            self.rate_limited = True


class FetchLimitError(ProviderError):
    code: ErrorCode = "fetch_limit"


class Service:
    def __init__(
        self,
        store: Store,
        providers: Mapping[Source, Provider],
        edgar: EdgarClient | None,
        corporate_actions: Mapping[str, CorporateAction],
        now: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.providers = providers
        self.edgar = edgar
        self.corporate_actions = corporate_actions
        self.now = now
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # series ------------------------------------------------------------------------------

    def series(self, ids: Sequence[str]) -> PerIdResult[SeriesJson]:
        result: PerIdResult[SeriesJson] = PerIdResult()
        budget = FetchBudget()
        for id in ids:
            try:
                provider = self._provider(id)
                instrument = self.store.get_instrument(id)
                if instrument is None:
                    budget.take(id)
                    instrument = self.ensure_cached(id, provider)
                result.values[id] = self._series_entry(instrument)
            except ProviderError as e:
                budget.note(e)
                result.fail(id, e)
        return result

    def ensure_cached(self, id: str, provider: Provider) -> Instrument:
        """Fetch and cache the full history of `id` unless already cached."""
        with self._lock(id):
            cached = self.store.get_instrument(id)
            if cached is not None:
                return cached
            history = provider.fetch_history(id)
            if id == REFERENCE_ID:
                reference = [b.date for b in history.bars]
            else:
                reference = self._reference_dates()
            status: Status = (
                "ended" if is_ended(history.bars[-1].date, reference, False) else "active"
            )
            self.store.insert_history(
                id,
                provider.source,
                history,
                served_values(history),
                status,
                self._cik_for_new(id),
                self.now(),
            )
        instrument = self.store.get_instrument(id)
        assert instrument is not None
        return instrument

    def _reference_dates(self) -> list[date]:
        try:
            self.ensure_cached(REFERENCE_ID, self.providers["yahoo"])
        except ProviderError as e:
            raise _upstream(e, f"reference series {REFERENCE_ID} unavailable: {e}") from e
        return self.store.get_dates(REFERENCE_ID)

    def lifecycle(self, instrument: Instrument, last_date: date) -> Lifecycle:
        return resolve_lifecycle(
            instrument.status,
            last_date,
            self.corporate_actions.get(instrument.id),
            instrument.edgar_successor,
        )

    def _series_entry(self, instrument: Instrument) -> SeriesJson:
        id = instrument.id
        stored = self.store.get_bars(id)
        lifecycle = self.lifecycle(instrument, stored[-1].bar.date)
        if lifecycle.serve_through is not None:
            stored = [s for s in stored if s.bar.date <= lifecycle.serve_through]
            if not stored:
                raise UnsupportedError(
                    f"corporate action for {id} is effective before its first cached bar"
                )
        dates = [s.bar.date for s in stored]
        values = [s.value for s in stored]
        stale = instrument.stale
        iso, divisor = usd_conversion(instrument.currency)
        converted = instrument.currency != "USD"
        if iso != "USD":
            fx = self._fx(iso)
            stale = stale or fx.stale
            fx_bars = self.store.get_bars(fx.id)
            dates, values = convert_to_usd(
                dates, values, divisor, [s.bar.date for s in fx_bars], [s.value for s in fx_bars]
            )
            if not dates:
                raise UnsupportedError(
                    f"no {fx.id} rate on or before {id}'s last date; cannot convert to USD"
                )
        elif divisor != 1:
            values = [v / divisor for v in values]
        assert all(math.isfinite(v) and v > 0 for v in values), f"non-finite value in {id}"
        return {
            "id": id,
            "dates": [d.isoformat() for d in dates],
            "close": values,
            "currency": instrument.currency,
            "converted": converted,
            "source": instrument.source,
            "asOf": instrument.last_success_at.isoformat(),
            "stale": stale,
            "status": lifecycle.status,
            "lastDate": dates[-1].isoformat(),
            "successor": successor_json(lifecycle.successor),
            "adjustment": {
                "method": instrument.adjustment,
                "backAdjustedFrom": _iso(instrument.back_adjusted_from),
            },
        }

    def _fx(self, iso: str) -> Instrument:
        pair = fx_pair_id(iso)
        try:
            fx = self.ensure_cached(pair, self.providers["yahoo"])
        except ProviderError as e:
            raise _upstream(e, f"FX rate {pair} unavailable: {e}") from e
        if fx.currency != "USD":
            raise UpstreamError(f"{fx.id} is quoted in {fx.currency}, expected USD")
        return fx

    # meta --------------------------------------------------------------------------------

    def meta(self, ids: Sequence[str]) -> PerIdResult[MetaJson]:
        result: PerIdResult[MetaJson] = PerIdResult()
        budget = FetchBudget()
        for id in ids:
            try:
                result.values[id] = self._meta(id, budget)
            except ProviderError as e:
                budget.note(e)
                result.fail(id, e)
        return result

    def _meta(self, id: str, budget: FetchBudget) -> MetaJson:
        provider = self._provider(id)
        doc = self.store.get_document("meta", id)
        if doc is not None and self.now() - doc.fetched_at < META_TTL:
            body: MetaJson = doc.body
            return body
        try:
            budget.take(id)
            fresh = meta_json(provider.fetch_meta(id))
        except (FetchLimitError, UpstreamError) as e:
            if doc is None:
                raise
            log.warning("serving expired meta for %s: %s", id, e)
            expired: MetaJson = doc.body
            return expired
        self.store.put_document("meta", id, fresh, self.now())
        return fresh

    def refresh_meta(self, id: str) -> None:
        """Refetch meta for a cached document (refresh job). Raises ProviderError."""
        fresh = meta_json(self._provider(id).fetch_meta(id))
        self.store.put_document("meta", id, fresh, self.now())

    # shares ------------------------------------------------------------------------------

    def shares(self, ids: Sequence[str]) -> PerIdResult[SharesJson]:
        result: PerIdResult[SharesJson] = PerIdResult()
        budget = FetchBudget()
        for id in ids:
            try:
                result.values[id] = self._shares(id, budget)
            except ProviderError as e:
                budget.note(e)
                result.fail(id, e)
        return result

    def _shares(self, id: str, budget: FetchBudget) -> SharesJson:
        meta = self._meta(id, budget)
        if meta["kind"] not in ("equity", "etf", "mutualfund"):
            raise UnsupportedError(f"{id} is a {meta['kind']}; it has no shares outstanding")
        if meta["kind"] == "equity" and is_us_listing(id):
            if self.edgar is None:
                raise UnsupportedError(DISABLED_MESSAGE)
            edgar = self._edgar_shares(id, budget)
            if edgar is not None:
                return edgar
        shares = meta["sharesOutstanding"]
        if shares is None:
            raise NotFoundError(f"no shares outstanding available for {id}")
        return {
            "source": "current",
            "knownDates": [self.now().date().isoformat()],
            "shares": [shares],
        }

    def _edgar_shares(self, id: str, budget: FetchBudget) -> SharesJson | None:
        """Point-in-time EDGAR shares, or None when SEC has none for this ticker."""
        assert self.edgar is not None
        doc = self.store.get_document("shares", id)
        if doc is not None and self.now() - doc.fetched_at < SHARES_TTL:
            cached: SharesJson | None = doc.body
            return cached
        try:
            budget.take(id)
            cik = self.sec_tickers().get(id)
            facts: list[SharesFact] = []
            if cik is not None:
                try:
                    facts = self.edgar.shares_facts(cik)
                except NotFoundError:
                    facts = []
        except (FetchLimitError, UpstreamError) as e:
            if doc is None:
                raise
            log.warning("serving expired EDGAR shares for %s: %s", id, e)
            expired: SharesJson | None = doc.body
            return expired
        fresh: SharesJson | None = None
        if facts:
            known, values = point_in_time_shares(facts)
            fresh = {
                "source": "edgar",
                "knownDates": [d.isoformat() for d in known],
                "shares": values,
            }
        self.store.put_document("shares", id, fresh, self.now())
        return fresh

    # SEC ticker map ----------------------------------------------------------------------

    def sec_tickers(self, max_age: timedelta = SEC_TICKERS_TTL) -> dict[str, int]:
        """Ticker -> CIK, refetched when older than `max_age`. Raises ProviderError."""
        if self.edgar is None:
            raise UnsupportedError(DISABLED_MESSAGE)
        doc = self.store.get_document("sec", "company_tickers")
        if doc is not None and self.now() - doc.fetched_at < max_age:
            cached: dict[str, int] = doc.body
            return cached
        try:
            fresh = self.edgar.ticker_map()
        except UpstreamError as e:
            if doc is None:
                raise
            log.warning("using expired SEC ticker map: %s", e)
            expired: dict[str, int] = doc.body
            return expired
        self.store.put_document("sec", "company_tickers", fresh, self.now())
        return fresh

    def _cik_for_new(self, id: str) -> int | None:
        if source_of(id) != "yahoo" or not is_us_listing(id):
            return None
        if self.edgar is None:
            return None
        try:
            return self.sec_tickers().get(id)
        except ProviderError as e:
            log.warning("could not look up CIK for %s; rename detection needs it: %s", id, e)
            return None

    # search ------------------------------------------------------------------------------

    def search(self, query: str, limit: int) -> list[SearchJson]:
        """Results of every provider that searches (Yahoo only for now). Raises ProviderError."""
        key = f"{limit}:{query.casefold()}"
        doc = self.store.get_document("search", key)
        if doc is not None and self.now() - doc.fetched_at < SEARCH_TTL:
            cached: list[SearchJson] = doc.body
            return cached
        try:
            found = self.providers["yahoo"].search(query, limit)
        except UpstreamError as e:
            if doc is None:
                raise
            log.warning("serving expired search results for %r: %s", query, e)
            expired: list[SearchJson] = doc.body
            return expired
        fresh: list[SearchJson] = [
            {
                "id": r.id,
                "name": r.name,
                "kind": r.kind,
                "exchange": r.exchange,
                "currency": r.currency,
            }
            for r in found
        ]
        self.store.put_document("search", key, fresh, self.now())
        return fresh

    # ended -------------------------------------------------------------------------------

    def ended(self) -> list[EndedJson]:
        out: list[EndedJson] = []
        for instrument in self.store.list_instruments():
            last = self.store.last_date(instrument.id)
            assert last is not None, f"{instrument.id} has no bars"
            lifecycle = self.lifecycle(instrument, last)
            if lifecycle.status != "ended":
                continue
            if lifecycle.serve_through is not None:
                served_last = self.store.last_date(instrument.id, lifecycle.serve_through)
                if served_last is None:
                    log.error("corporate action for %s precedes all its bars", instrument.id)
                    continue
                last = served_last
            out.append(
                {
                    "id": instrument.id,
                    "lastDate": last.isoformat(),
                    "successor": successor_json(lifecycle.successor),
                    "reviewed": lifecycle.reviewed,
                }
            )
        return out

    # helpers -----------------------------------------------------------------------------

    def _provider(self, id: str) -> Provider:
        source = source_of(id)
        provider = self.providers.get(source)
        if provider is None:
            raise UnsupportedError(UNSUPPORTED_SOURCES.get(source, f"no provider for {source}"))
        return provider

    def _lock(self, id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(id, threading.Lock())


def served_values(history: History) -> list[float]:
    """Values served before FX: the total-return index, or the close itself."""
    if history.adjustment == "total_return":
        return total_return(history.bars)
    return [b.close for b in history.bars]


def successor_json(successor: Successor | None) -> SuccessorJson | None:
    if successor is None:
        return None
    return {
        "id": successor.id,
        "ratio": successor.ratio,
        "cashPerShare": successor.cash_per_share,
        "effectiveDate": successor.effective_date.isoformat(),
        "source": successor.source,
    }


def meta_json(meta: Meta) -> MetaJson:
    return {
        "id": meta.id,
        "name": meta.name,
        "kind": meta.kind,
        "assetClass": meta.asset_class,
        "sector": meta.sector,
        "industry": meta.industry,
        "country": meta.country,
        "currency": meta.currency,
        "marketCap": meta.market_cap,
        "sharesOutstanding": meta.shares_outstanding,
        "fundSectorWeights": (
            None if meta.fund_sector_weights is None else dict(meta.fund_sector_weights)
        ),
        "polymarket": None,
    }


def _upstream(cause: ProviderError, message: str) -> UpstreamError:
    """An upstream_error for a dependency that failed, keeping rate limiting visible."""
    return (
        RateLimitedError(message) if isinstance(cause, RateLimitedError) else UpstreamError(message)
    )


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()
