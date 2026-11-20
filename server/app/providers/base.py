"""Provider protocol: everything routes and the refresh job need from a data source.

A provider owns a set of ids (decided by `app.ids.source_of`) and fetches raw bars, metadata
and search results for them. Storage, total-return computation, FX conversion and lifecycle
(stale / ended / successor) are handled generically outside providers.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Literal, Protocol

from app.timeseries import Bar

Source = Literal["yahoo", "polymarket", "fred"]
Kind = Literal[
    "equity", "etf", "index", "crypto", "fx", "future", "mutualfund", "polymarket", "macro"
]
AssetClass = Literal[
    "equity", "fixed_income", "commodity", "crypto", "fx", "prediction", "macro", "multi_asset"
]
AdjustmentMethod = Literal["total_return", "futures_roll", "none"]
ErrorCode = Literal["not_found", "upstream_error", "fetch_limit", "unsupported"]


class ProviderError(Exception):
    code: ErrorCode = "upstream_error"


class NotFoundError(ProviderError):
    """Upstream has no data for this id (unknown, or no longer returned)."""

    code: ErrorCode = "not_found"


class UpstreamError(ProviderError):
    """Upstream failed (network, server error, bad payload); retrying later may succeed."""

    code: ErrorCode = "upstream_error"


class RateLimitedError(UpstreamError):
    """Upstream rate-limited us; callers back off and stop issuing requests."""


class UnsupportedError(ProviderError):
    code: ErrorCode = "unsupported"


@dataclass(frozen=True, slots=True)
class History:
    """A full history as fetched on first sight of an id."""

    bars: tuple[Bar, ...]
    currency: str
    kind: Kind
    tz: str
    session_close: time
    adjustment: AdjustmentMethod
    back_adjusted_from: date | None


@dataclass(frozen=True, slots=True)
class Meta:
    id: str
    name: str
    kind: Kind
    asset_class: AssetClass
    sector: str | None
    industry: str | None
    country: str | None
    currency: str
    market_cap: float | None
    shares_outstanding: float | None
    fund_sector_weights: Mapping[str, float] | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    id: str
    name: str
    kind: Kind
    exchange: str | None
    currency: str | None


class Provider(Protocol):
    source: Source

    @property
    def upstream_calls(self) -> int:
        """HTTP requests issued to the upstream so far (for refresh reports and tests)."""
        ...

    def fetch_history(self, id: str) -> History:
        """Full history of `id`, final bars only. Raises ProviderError."""
        ...

    def fetch_recent(
        self, starts: Mapping[str, date], cutoffs: Mapping[str, date]
    ) -> dict[str, tuple[Bar, ...] | ProviderError]:
        """Bars from starts[id] through cutoffs[id] (inclusive) for every id, batched upstream.

        Returns one entry per requested id: its bars, or the ProviderError for that id
        (RateLimitedError when backing off did not get it through).
        """
        ...

    def fetch_meta(self, id: str) -> Meta:
        """Raises ProviderError."""
        ...

    def search(self, query: str, limit: int) -> Sequence[SearchResult]:
        """Raises ProviderError."""
        ...
