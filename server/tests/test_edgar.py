"""EDGAR parsing, point-in-time shares and the HTTP client (via httpx.MockTransport).

Input space: no facts; one fact; amendments re-reporting the same value later; late filings of
an older period; several facts on one filing date; equal (end, filed) ties; malformed and
non-positive facts; HTTP 200/404/429/5xx, invalid JSON, non-object JSON, transport errors.
"""

import bisect
import itertools
from datetime import date, timedelta

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.edgar import (
    FACTS_URL,
    MIN_INTERVAL_S,
    TICKERS_URL,
    EdgarClient,
    SharesFact,
    parse_shares_facts,
    parse_ticker_map,
    point_in_time_shares,
)
from app.providers.base import NotFoundError, RateLimitedError, UpstreamError

D = date(2020, 1, 1)


def fact(end: int, filed: int, value: float, accn: str = "a") -> SharesFact:
    return SharesFact(D + timedelta(days=end), D + timedelta(days=filed), accn, value)


def lookup(known: list[date], values: list[float], t: date) -> float | None:
    i = bisect.bisect_right(known, t) - 1
    return None if i < 0 else values[i]


def reference(facts: list[SharesFact], t: date) -> float | None:
    visible = [f for f in facts if f.filed <= t]
    if not visible:
        return None
    return max(visible, key=lambda f: (f.end, f.filed, f.accn, f.value)).value


facts_strategy = st.lists(
    st.builds(
        fact,
        end=st.integers(0, 40),
        filed=st.integers(0, 60),
        value=st.sampled_from([100.0, 200.0, 300.0]),
        accn=st.sampled_from(["a", "b", "c"]),
    ),
    max_size=12,
)


@given(facts_strategy)
def test_point_in_time_matches_brute_force_without_look_ahead(facts: list[SharesFact]) -> None:
    known, values = point_in_time_shares(facts)
    assert known == sorted(set(known))
    assert all(a != b for a, b in itertools.pairwise(values))
    for day in range(-1, 62):
        t = D + timedelta(days=day)
        assert lookup(known, values, t) == reference(facts, t)


def test_point_in_time_examples() -> None:
    assert point_in_time_shares([]) == ([], [])
    facts = [
        fact(end=10, filed=20, value=100.0),
        fact(end=10, filed=30, value=100.0),  # 10-K/A re-reporting the same value
        fact(end=40, filed=50, value=90.0),
        fact(end=5, filed=55, value=999.0),  # late filing about an older period
    ]
    known, values = point_in_time_shares(facts)
    assert known == [D + timedelta(days=20), D + timedelta(days=50)]
    assert values == [100.0, 90.0]


def companyfacts(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": rows}}}}}


def test_parse_shares_facts() -> None:
    rows: list[dict[str, object]] = [
        {"end": "2020-01-31", "val": 5, "accn": "x", "filed": "2020-02-10", "form": "10-Q"},
        {"end": "2020-04-30", "val": 0, "accn": "y", "filed": "2020-05-10"},
    ]
    assert parse_shares_facts(companyfacts(rows)) == [
        SharesFact(date(2020, 1, 31), date(2020, 2, 10), "x", 5.0)
    ]
    assert parse_shares_facts({"facts": {}}) == []
    assert parse_shares_facts({}) == []


@pytest.mark.parametrize(
    "payload",
    [
        companyfacts([{"end": "2020-01-31", "val": 5, "accn": "x"}]),
        companyfacts([{"end": "bad", "val": 5, "accn": "x", "filed": "2020-01-01"}]),
        companyfacts([{"end": "2020-01-31", "val": "many", "accn": "x", "filed": "2020-01-01"}]),
        {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": {}}}}}},
    ],
)
def test_parse_shares_facts_rejects_malformed(payload: dict[str, object]) -> None:
    with pytest.raises(UpstreamError):
        parse_shares_facts(payload)


def test_parse_ticker_map() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
        "1": {"cik_str": 1067983, "ticker": "BRK.B", "title": "Berkshire"},
    }
    assert parse_ticker_map(payload) == {"AAPL": 320193, "BRK-B": 1067983}
    for bad in ({}, {"0": {"ticker": "X"}}, {"0": "nope"}):
        with pytest.raises(UpstreamError):
            parse_ticker_map(bad)


class Recorder:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def client_for(handler: Recorder) -> EdgarClient:
    return EdgarClient(
        "Example Org ops", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )


def test_client_sends_user_agent_and_parses() -> None:
    handler = Recorder(httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "A"}}))
    client = client_for(handler)
    assert client.ticker_map() == {"A": 1}
    assert handler.requests[0].headers["User-Agent"] == "Example Org ops"
    assert str(handler.requests[0].url) == TICKERS_URL
    assert client.calls == 1


def test_client_facts_url() -> None:
    handler = Recorder(httpx.Response(200, json=companyfacts([])))
    client_for(handler).shares_facts(320193)
    assert str(handler.requests[0].url) == FACTS_URL.format(cik=320193)
    assert str(handler.requests[0].url).endswith("CIK0000320193.json")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404), NotFoundError),
        (httpx.Response(429), RateLimitedError),
        (httpx.Response(403), UpstreamError),
        (httpx.Response(503), UpstreamError),
        (httpx.Response(200, content=b"not json"), UpstreamError),
        (httpx.Response(200, json=[1, 2]), UpstreamError),
        (httpx.ConnectError("down"), UpstreamError),
    ],
)
def test_client_errors(response: httpx.Response | Exception, error: type[Exception]) -> None:
    with pytest.raises(error):
        client_for(Recorder(response)).ticker_map()


def test_client_spaces_requests() -> None:
    now = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    client = EdgarClient(
        "ua",
        transport=httpx.MockTransport(Recorder(httpx.Response(200, json=companyfacts([])))),
        clock=lambda: now[0],
        sleep=sleep,
    )
    for _ in range(3):
        client.shares_facts(1)
    assert waits == pytest.approx([MIN_INTERVAL_S, MIN_INTERVAL_S])
    assert 1 / MIN_INTERVAL_S * 2 <= 10, "two processes together must stay within SEC's 10 req/s"


def test_client_requires_user_agent() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        EdgarClient("  ")
