"""Yahoo Finance via yfinance.

History is fetched with `auto_adjust=False, actions=True` and stored as split-adjusted Close
plus dividends (Yahoo's Adj Close is not used: it rescales all history after every
dividend). `repair` stays off: see the PR for the measurement behind that choice.
"""

import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import yfinance as yf
from curl_cffi import requests as curl_requests
from yfinance.exceptions import (
    YFDataException,
    YFInvalidPeriodError,
    YFPricesMissingError,
    YFRateLimitError,
    YFTickerMissingError,
)

from app.providers.base import (
    AdjustmentMethod,
    AssetClass,
    History,
    Kind,
    Meta,
    NotFoundError,
    ProviderError,
    RateLimitedError,
    SearchResult,
    Source,
    UnsupportedError,
    UpstreamError,
)
from app.timeseries import Bar, DataQualityError, last_final_date, validate_bars

log = logging.getLogger(__name__)

KINDS: dict[str, Kind] = {
    "EQUITY": "equity",
    "ETF": "etf",
    "INDEX": "index",
    "CRYPTOCURRENCY": "crypto",
    "CURRENCY": "fx",
    "FUTURE": "future",
    "MUTUALFUND": "mutualfund",
}
TOTAL_RETURN_KINDS: frozenset[Kind] = frozenset({"equity", "etf", "mutualfund"})
# Financial futures by root symbol; any other future is a commodity.
FUTURE_ROOT_CLASSES: dict[str, AssetClass] = {
    **dict.fromkeys(["ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K", "NKD"], "equity"),
    **dict.fromkeys(["ZN", "ZB", "ZF", "ZT", "UB", "TN", "SR3", "ZQ", "GE"], "fixed_income"),
    **dict.fromkeys(["6E", "6J", "6B", "6A", "6C", "6S", "6M", "6N", "DX"], "fx"),
    **dict.fromkeys(["BTC", "ETH", "MBT", "MET"], "crypto"),
}
# Yahoo's Treasury yield indices; every other index is treated as equity.
YIELD_INDICES = frozenset({"^IRX", "^FVX", "^TNX", "^TYX"})

CHUNK_SIZE = 25
DOWNLOAD_THREADS = 4
BACKOFF_S = (30.0, 120.0, 300.0)
TIMEOUT_S = 30


class CountingSession(curl_requests.Session[curl_requests.Response]):
    """curl_cffi session that counts the HTTP requests yfinance makes through it."""

    def __init__(self) -> None:
        super().__init__(impersonate="chrome")
        self.calls = 0

    # Signature mirrors curl_cffi Session.request, which is untyped in its arguments.
    def request(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls += 1
        return super().request(*args, **kwargs)


def kind_for(quote_type: object) -> Kind:
    kind = KINDS.get(str(quote_type))
    if kind is None:
        raise UnsupportedError(f"Yahoo instrument type {quote_type!r} is not supported")
    return kind


def adjustment_for(kind: Kind) -> AdjustmentMethod:
    return "total_return" if kind in TOTAL_RETURN_KINDS else "none"


def asset_class_for(
    id: str, kind: Kind, category: str | None, positions: Mapping[str, float] | None
) -> AssetClass:
    """Asset class from Yahoo fields. Funds: >= 50% stock -> equity, >= 50% bonds -> fixed
    income, a commodities category -> commodity, else multi_asset."""
    if kind == "equity":
        return "equity"
    if kind == "crypto":
        return "crypto"
    if kind == "fx":
        return "fx"
    if kind == "index":
        return "fixed_income" if id in YIELD_INDICES else "equity"
    if kind == "future":
        return FUTURE_ROOT_CLASSES.get(id.split("=")[0], "commodity")
    positions = positions or {}
    if positions.get("stockPosition", 0.0) >= 0.5:
        return "equity"
    if positions.get("bondPosition", 0.0) >= 0.5:
        return "fixed_income"
    if category is not None and "commodit" in category.lower():
        return "commodity"
    return "multi_asset"


def bars_from_frame(frame: pd.DataFrame) -> list[Bar]:
    """Rows with a Close become bars dated by their exchange-local date."""
    bars: list[Bar] = []
    for ts, row in frame.iterrows():
        close = float(row["Close"])
        if math.isnan(close):
            continue
        stamp = cast(pd.Timestamp, ts)
        bars.append(
            Bar(
                date=stamp.date(),
                close=close,
                dividend=_zero_if_nan(row["Dividends"]),
                split=_zero_if_nan(row["Stock Splits"]),
            )
        )
    return bars


def _zero_if_nan(value: object) -> float:
    number = float(cast(float, value))
    return 0.0 if math.isnan(number) else number


def translate(e: Exception, id: str) -> ProviderError:
    if isinstance(e, ProviderError):
        return e
    if isinstance(e, YFRateLimitError):
        return RateLimitedError(f"Yahoo rate-limited the request for {id}")
    if isinstance(e, YFTickerMissingError | YFPricesMissingError):
        return NotFoundError(f"Yahoo has no data for {id}: {e}")
    if isinstance(e, YFInvalidPeriodError):
        return UpstreamError(f"Yahoo rejected the period for {id}: {e}")
    response = getattr(e, "response", None)
    if getattr(response, "status_code", None) == 404:
        return NotFoundError(f"Yahoo has no data for {id}")
    return UpstreamError(f"Yahoo request for {id} failed: {type(e).__name__}: {e}")


class YahooProvider:
    source: Source = "yahoo"

    def __init__(
        self,
        cache_dir: Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache_dir))
        yf.config.debug.hide_exceptions = False
        self._session = CountingSession()
        self._now = now
        self._sleep = sleep

    @property
    def upstream_calls(self) -> int:
        return self._session.calls

    def fetch_history(self, id: str) -> History:
        try:
            ticker = yf.Ticker(id, session=self._session)
            frame = ticker.history(
                period="max", auto_adjust=False, actions=True, repair=False, timeout=TIMEOUT_S
            )
            metadata = ticker.history_metadata
        except Exception as e:
            raise translate(e, id) from e
        kind = kind_for(metadata.get("instrumentType"))
        currency = metadata.get("currency")
        tz = metadata.get("exchangeTimezoneName")
        if not isinstance(currency, str) or not currency:
            raise UpstreamError(f"Yahoo metadata for {id} has no currency")
        if not isinstance(tz, str) or not tz:
            raise UpstreamError(f"Yahoo metadata for {id} has no exchange timezone")
        session_close = _session_close(metadata, id, tz)
        cutoff = last_final_date(self._now(), tz, session_close)
        bars = [b for b in bars_from_frame(frame) if b.date <= cutoff]
        if not bars:
            raise NotFoundError(f"Yahoo returned no completed daily bars for {id}")
        try:
            validate_bars(bars)
        except DataQualityError as e:
            raise UnsupportedError(f"Yahoo data for {id} is unusable: {e}") from e
        return History(
            bars=tuple(bars),
            currency=currency,
            kind=kind,
            tz=tz,
            session_close=session_close,
            adjustment=adjustment_for(kind),
            back_adjusted_from=None,
        )

    def fetch_recent(
        self, starts: Mapping[str, date], cutoffs: Mapping[str, date]
    ) -> dict[str, tuple[Bar, ...] | ProviderError]:
        results: dict[str, tuple[Bar, ...] | ProviderError] = {}
        ordered = sorted(starts, key=lambda i: (starts[i], i))
        rate_limited = False
        for chunk in _chunks(ordered, CHUNK_SIZE):
            if rate_limited:
                for id in chunk:
                    results[id] = RateLimitedError("skipped: Yahoo rate-limited this refresh")
                continue
            for attempt in range(len(BACKOFF_S) + 1):
                try:
                    results.update(self._fetch_chunk(chunk, starts, cutoffs))
                    break
                except RateLimitedError:
                    if attempt == len(BACKOFF_S):
                        rate_limited = True
                        for id in chunk:
                            results[id] = RateLimitedError(
                                "Yahoo kept rate-limiting after backing off"
                            )
                        break
                    log.warning("Yahoo rate limit; backing off %.0fs", BACKOFF_S[attempt])
                    self._sleep(BACKOFF_S[attempt])
        return results

    def _fetch_chunk(
        self, chunk: Sequence[str], starts: Mapping[str, date], cutoffs: Mapping[str, date]
    ) -> dict[str, tuple[Bar, ...] | ProviderError]:
        start = min(starts[i] for i in chunk)
        frame = yf.download(
            list(chunk),
            start=start.isoformat(),
            actions=True,
            auto_adjust=False,
            repair=False,
            progress=False,
            threads=min(DOWNLOAD_THREADS, len(chunk)),
            group_by="ticker",
            multi_level_index=True,
            session=self._session,
            timeout=TIMEOUT_S,
        )
        out: dict[str, tuple[Bar, ...] | ProviderError] = {}
        tickers = set() if frame is None else set(frame.columns.get_level_values(0))
        for id in chunk:
            bars = bars_from_frame(frame[id]) if frame is not None and id in tickers else []
            if not bars:
                # yf.download logs per-ticker failures instead of raising them; fetch this id
                # alone to learn whether it is rate limiting, an outage or missing data.
                try:
                    bars = self._probe(id, starts[id])
                except RateLimitedError:
                    raise
                except ProviderError as e:
                    out[id] = e
                    continue
            window = tuple(b for b in bars if starts[id] <= b.date <= cutoffs[id])
            try:
                validate_bars(window)
            except DataQualityError as e:
                out[id] = (
                    NotFoundError(f"Yahoo returned no bars for {id} since {starts[id]}")
                    if not window
                    else UnsupportedError(f"Yahoo data for {id} is unusable: {e}")
                )
                continue
            out[id] = window
        return out

    def _probe(self, id: str, start: date) -> list[Bar]:
        try:
            frame = yf.Ticker(id, session=self._session).history(
                start=start.isoformat(),
                auto_adjust=False,
                actions=True,
                repair=False,
                timeout=TIMEOUT_S,
            )
        except Exception as e:
            error = translate(e, id)
            if isinstance(error, NotFoundError):
                return []
            raise error from e
        return bars_from_frame(frame)

    def fetch_meta(self, id: str) -> Meta:
        try:
            ticker = yf.Ticker(id, session=self._session)
            info = ticker.info
        except Exception as e:
            raise translate(e, id) from e
        quote_type = info.get("quoteType")
        if quote_type is None:
            raise NotFoundError(f"Yahoo has no metadata for {id}")
        kind = kind_for(quote_type)
        currency = info.get("currency")
        if not isinstance(currency, str) or not currency:
            raise UpstreamError(f"Yahoo metadata for {id} has no currency")
        sector_weights: dict[str, float] | None = None
        positions: Mapping[str, float] | None = None
        if kind in ("etf", "mutualfund"):
            try:
                funds = ticker.funds_data
                sector_weights = {k: float(v) for k, v in funds.sector_weightings.items()} or None
                positions = funds.asset_classes
            except YFDataException:
                log.info("Yahoo has no fund data for %s", id)
            except Exception as e:
                raise translate(e, id) from e
        category = info.get("category")
        return Meta(
            id=id,
            name=str(info.get("longName") or info.get("shortName") or id),
            kind=kind,
            asset_class=asset_class_for(
                id, kind, category if isinstance(category, str) else None, positions
            ),
            sector=_str_or_none(info.get("sector")),
            industry=_str_or_none(info.get("industry")),
            country=_str_or_none(info.get("country")),
            currency=currency,
            market_cap=_positive_or_none(info.get("marketCap")),
            shares_outstanding=_positive_or_none(info.get("sharesOutstanding")),
            fund_sector_weights=sector_weights,
        )

    def search(self, query: str, limit: int) -> list[SearchResult]:
        try:
            quotes = yf.Search(
                query,
                max_results=limit,
                news_count=0,
                lists_count=0,
                include_cb=False,
                recommended=0,
                session=self._session,
                timeout=TIMEOUT_S,
                raise_errors=True,
            ).quotes
        except Exception as e:
            raise translate(e, f"search {query!r}") from e
        results: list[SearchResult] = []
        for quote in quotes:
            kind = KINDS.get(str(quote.get("quoteType")))
            symbol = quote.get("symbol")
            if kind is None or not isinstance(symbol, str):
                continue
            results.append(
                SearchResult(
                    id=symbol.upper(),
                    name=str(quote.get("longname") or quote.get("shortname") or symbol),
                    kind=kind,
                    exchange=_str_or_none(quote.get("exchDisp") or quote.get("exchange")),
                    currency=_str_or_none(quote.get("currency")),
                )
            )
        return results[:limit]


def _session_close(metadata: Mapping[str, Any], id: str, tz: str) -> dtime:
    try:
        end = metadata["currentTradingPeriod"]["regular"]["end"]
        close: dtime = pd.Timestamp(end).tz_convert(tz).time()
        return close
    except (KeyError, TypeError, ValueError) as e:
        raise UpstreamError(f"Yahoo metadata for {id} has no regular trading period") from e


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None
