"""SEC EDGAR: ticker -> CIK map and point-in-time shares outstanding.

SEC requires a User-Agent naming the requester with contact details. It is read from the
SEC_USER_AGENT environment variable; without it, EDGAR is never called.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.providers.base import NotFoundError, RateLimitedError, UpstreamError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# SEC allows 10 requests/second per client. The API server and the refresh job are separate
# processes, so each keeps to half of that.
MIN_INTERVAL_S = 0.2
DISABLED_MESSAGE = "EDGAR is disabled: SEC_USER_AGENT is not set on the server"


@dataclass(frozen=True, slots=True)
class SharesFact:
    end: date
    filed: date
    accn: str
    value: float


def point_in_time_shares(facts: Sequence[SharesFact]) -> tuple[list[date], list[float]]:
    """(knownDates, shares): the value in force from each filing date on, without look-ahead.

    The value in force at date t is the fact with the latest period end among facts filed on
    or before t (ties: later filing, larger accession number, larger value). An entry is
    emitted on each filing date where that value changes.
    """
    known: list[date] = []
    values: list[float] = []
    best: tuple[date, date, str, float] | None = None
    ordered = sorted(facts, key=lambda f: f.filed)
    for i, fact in enumerate(ordered):
        rank = (fact.end, fact.filed, fact.accn, fact.value)
        if best is None or rank > best:
            best = rank
        last_of_day = i + 1 == len(ordered) or ordered[i + 1].filed != fact.filed
        if last_of_day and (not values or values[-1] != best[3]):
            known.append(fact.filed)
            values.append(best[3])
    return known, values


def parse_shares_facts(companyfacts: Mapping[str, Any]) -> list[SharesFact]:
    """Facts of dei:EntityCommonStockSharesOutstanding (unit `shares`); [] when absent."""
    try:
        units = companyfacts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]
    except (KeyError, TypeError):
        return []
    rows = units.get("shares", [])
    if not isinstance(rows, list):
        raise UpstreamError("EDGAR companyfacts: 'shares' is not a list")
    facts: list[SharesFact] = []
    for row in rows:
        try:
            value = float(row["val"])
            fact = SharesFact(
                date.fromisoformat(row["end"]),
                date.fromisoformat(row["filed"]),
                str(row["accn"]),
                value,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise UpstreamError(f"EDGAR companyfacts: malformed fact {row!r}") from e
        if value > 0:
            facts.append(fact)
    return facts


def parse_ticker_map(payload: Mapping[str, Any]) -> dict[str, int]:
    """company_tickers.json -> {ticker (Yahoo style, upper-case): CIK}."""
    out: dict[str, int] = {}
    try:
        for row in payload.values():
            ticker = str(row["ticker"]).upper().replace(".", "-")
            out[ticker] = int(row["cik_str"])
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise UpstreamError("EDGAR company_tickers.json has an unexpected shape") from e
    if not out:
        raise UpstreamError("EDGAR company_tickers.json is empty")
    return out


class EdgarClient:
    """Rate-limited EDGAR HTTP client. Constructed only when a User-Agent is configured."""

    def __init__(
        self,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("SEC user agent must not be empty")
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30,
            transport=transport,
        )
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.calls = 0

    def ticker_map(self) -> dict[str, int]:
        return parse_ticker_map(self._get_json(TICKERS_URL))

    def shares_facts(self, cik: int) -> list[SharesFact]:
        return parse_shares_facts(self._get_json(FACTS_URL.format(cik=cik)))

    def _get_json(self, url: str) -> Mapping[str, Any]:
        with self._lock:
            wait = self._next_at - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._next_at = self._clock() + MIN_INTERVAL_S
            self.calls += 1
        try:
            response = self._client.get(url)
        except httpx.HTTPError as e:
            raise UpstreamError(f"EDGAR request failed: {e}") from e
        if response.status_code == 404:
            raise NotFoundError(f"EDGAR has no document at {url}")
        if response.status_code == 429:
            raise RateLimitedError("EDGAR rate-limited the request")
        if response.status_code != 200:
            raise UpstreamError(f"EDGAR returned HTTP {response.status_code} for {url}")
        try:
            payload = response.json()
        except ValueError as e:
            raise UpstreamError(f"EDGAR returned invalid JSON for {url}") from e
        if not isinstance(payload, dict):
            raise UpstreamError(f"EDGAR returned a non-object for {url}")
        return payload
