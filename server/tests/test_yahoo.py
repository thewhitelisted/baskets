"""YahooProvider logic with yfinance replaced by in-memory stand-ins (no network).

Covers: metadata validation, finality cutoff, data-quality rejection, error translation,
chunked download with per-id probing, backoff and give-up on rate limiting, meta mapping and
asset-class rules, search mapping.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest
import yfinance as yf
from yfinance.exceptions import (
    YFDataException,
    YFPricesMissingError,
    YFRateLimitError,
    YFTzMissingError,
)

from app.providers import yahoo
from app.providers.base import (
    Kind,
    NotFoundError,
    ProviderError,
    RateLimitedError,
    UnsupportedError,
    UpstreamError,
)
from app.providers.yahoo import YahooProvider, asset_class_for, bars_from_frame, kind_for, translate
from app.timeseries import Bar

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)  # 10:00 New York: today's bar is not final
TPERIOD = {"regular": {"end": pd.Timestamp("2026-11-20 16:00", tz="America/New_York")}}
META = {
    "instrumentType": "EQUITY",
    "currency": "USD",
    "exchangeTimezoneName": "America/New_York",
    "currentTradingPeriod": TPERIOD,
}


def frame(
    rows: list[tuple[str, float, float, float]], tz: str | None = "America/New_York"
) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    if tz:
        index = index.tz_localize(tz)
    return pd.DataFrame(
        {
            "Close": [r[1] for r in rows],
            "Dividends": [r[2] for r in rows],
            "Stock Splits": [r[3] for r in rows],
        },
        index=index,
    )


ROWS = [
    ("2026-11-18", 10.0, 0.0, 0.0),
    ("2026-11-19", 11.0, 0.5, 0.0),
    ("2026-11-20", 11.5, 0.0, 0.0),
]


class FakeTicker:
    histories: ClassVar[dict[str, pd.DataFrame | Exception]] = {}
    metas: ClassVar[dict[str, dict[str, Any]]] = {}
    infos: ClassVar[dict[str, dict[str, Any] | Exception]] = {}
    funds: ClassVar[dict[str, object]] = {}
    history_calls: ClassVar[list[tuple[str, dict[str, object]]]] = []

    def __init__(self, id: str, session: object = None) -> None:
        self.id = id

    def history(self, **kwargs: object) -> pd.DataFrame:
        FakeTicker.history_calls.append((self.id, kwargs))
        result = self.histories[self.id]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def history_metadata(self) -> dict[str, Any]:
        return self.metas.get(self.id, META)

    @property
    def info(self) -> dict[str, Any]:
        result = self.infos[self.id]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def funds_data(self) -> object:
        result = self.funds[self.id]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_yf(monkeypatch: pytest.MonkeyPatch) -> type[FakeTicker]:
    FakeTicker.histories, FakeTicker.metas, FakeTicker.infos, FakeTicker.funds = {}, {}, {}, {}
    FakeTicker.history_calls = []
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    return FakeTicker


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(tmp_path: Path, sleeps: list[float]) -> YahooProvider:
    return YahooProvider(tmp_path / "yf", now=lambda: NOW, sleep=sleeps.append)


# pure helpers ------------------------------------------------------------------------------


def test_kind_for() -> None:
    assert kind_for("MUTUALFUND") == "mutualfund"
    with pytest.raises(UnsupportedError, match="OPTION"):
        kind_for("OPTION")


@pytest.mark.parametrize(
    ("id", "kind", "category", "positions", "expected"),
    [
        ("AAPL", "equity", None, None, "equity"),
        ("BTC-USD", "crypto", None, None, "crypto"),
        ("EURUSD=X", "fx", None, None, "fx"),
        ("^TNX", "index", None, None, "fixed_income"),
        ("^GSPC", "index", None, None, "equity"),
        ("ES=F", "future", None, None, "equity"),
        ("ZN=F", "future", None, None, "fixed_income"),
        ("6E=F", "future", None, None, "fx"),
        ("GC=F", "future", None, None, "commodity"),
        ("SPY", "etf", "Large Blend", {"stockPosition": 0.99}, "equity"),
        ("TLT", "etf", "Long Government", {"bondPosition": 0.995}, "fixed_income"),
        ("GLD", "etf", "Commodities Focused", {"otherPosition": 1.0}, "commodity"),
        ("AOR", "etf", "Allocation", {"stockPosition": 0.49, "bondPosition": 0.49}, "multi_asset"),
        ("X", "mutualfund", None, None, "multi_asset"),
        ("Y", "etf", None, {"stockPosition": 0.5}, "equity"),
        ("Z", "etf", None, {"bondPosition": 0.5}, "fixed_income"),
    ],
)
def test_asset_class_for(
    id: str, kind: Kind, category: str | None, positions: dict[str, float] | None, expected: str
) -> None:
    assert asset_class_for(id, kind, category, positions) == expected


def test_bars_from_frame_uses_local_dates_and_skips_nan() -> None:
    f = frame(
        [("2026-11-18", 10.0, float("nan"), float("nan")), ("2026-11-19", float("nan"), 0.0, 0.0)],
        tz="Asia/Tokyo",
    )
    assert bars_from_frame(f) == [Bar(date(2026, 11, 18), 10.0, 0.0, 0.0)]


def test_translate() -> None:
    assert isinstance(translate(YFRateLimitError(), "X"), RateLimitedError)
    assert isinstance(translate(YFTzMissingError("X"), "X"), NotFoundError)
    assert isinstance(translate(YFPricesMissingError("X", ""), "X"), NotFoundError)
    assert isinstance(translate(ConnectionError("reset"), "X"), UpstreamError)
    not_found = UpstreamError("x")
    assert translate(not_found, "X") is not_found

    class HttpError(Exception):
        response = type("R", (), {"status_code": 404})()

    assert isinstance(translate(HttpError(), "X"), NotFoundError)


# fetch_history -----------------------------------------------------------------------------


def test_fetch_history_drops_unfinished_bar(
    provider: YahooProvider, fake_yf: type[FakeTicker]
) -> None:
    fake_yf.histories["AAPL"] = frame(ROWS)
    history = provider.fetch_history("AAPL")
    assert [b.date for b in history.bars] == [date(2026, 11, 18), date(2026, 11, 19)]
    assert (history.currency, history.kind, history.tz, history.adjustment) == (
        "USD",
        "equity",
        "America/New_York",
        "total_return",
    )
    assert history.session_close.hour == 16
    _, kwargs = fake_yf.history_calls[0]
    assert kwargs["period"] == "max"
    assert kwargs["auto_adjust"] is False
    assert kwargs["actions"] is True
    assert kwargs["repair"] is False


@pytest.mark.parametrize(
    ("meta", "message"),
    [
        ({**META, "currency": None}, "no currency"),
        ({**META, "exchangeTimezoneName": ""}, "no exchange timezone"),
        (
            {k: v for k, v in META.items() if k != "currentTradingPeriod"},
            "no regular trading period",
        ),
    ],
)
def test_fetch_history_rejects_bad_metadata(
    provider: YahooProvider, fake_yf: type[FakeTicker], meta: dict[str, Any], message: str
) -> None:
    fake_yf.histories["AAPL"] = frame(ROWS)
    fake_yf.metas["AAPL"] = meta
    with pytest.raises(UpstreamError, match=message):
        provider.fetch_history("AAPL")


def test_fetch_history_errors(provider: YahooProvider, fake_yf: type[FakeTicker]) -> None:
    fake_yf.histories.update(
        {
            "GONE": YFTzMissingError("GONE"),
            "BUSY": YFRateLimitError(),
            "TODAY": frame(ROWS[2:]),
            "NEG": frame([("2020-04-20", -37.6, 0.0, 0.0)]),
            "OPT": frame(ROWS),
        }
    )
    fake_yf.metas["OPT"] = {**META, "instrumentType": "OPTION"}
    with pytest.raises(NotFoundError):
        provider.fetch_history("GONE")
    with pytest.raises(RateLimitedError):
        provider.fetch_history("BUSY")
    with pytest.raises(NotFoundError, match="no completed daily bars"):
        provider.fetch_history("TODAY")
    with pytest.raises(UnsupportedError, match="non-positive"):
        provider.fetch_history("NEG")
    with pytest.raises(UnsupportedError, match="OPTION"):
        provider.fetch_history("OPT")


# fetch_recent ------------------------------------------------------------------------------


def download_frame(per_id: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.concat({k: v for k, v in per_id.items()}, axis=1, names=["Ticker", "Price"])


class FakeDownload:
    def __init__(self, results: list[pd.DataFrame | None]) -> None:
        self.results = results
        self.tickers: list[list[str]] = []
        self.calls: list[dict[str, object]] = []

    def __call__(self, tickers: list[str], **kwargs: object) -> pd.DataFrame | None:
        self.tickers.append(tickers)
        self.calls.append(kwargs)
        return self.results.pop(0)


def bars_of(result: tuple[Bar, ...] | ProviderError) -> tuple[Bar, ...]:
    assert isinstance(result, tuple), result
    return result


START = {"AAPL": date(2026, 11, 12), "MSFT": date(2026, 11, 18)}
CUT = {"AAPL": date(2026, 11, 19), "MSFT": date(2026, 11, 19)}


def test_fetch_recent_batches_and_filters(
    provider: YahooProvider, fake_yf: type[FakeTicker], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [("2026-11-12", 9.0, 0.0, 0.0), *ROWS]
    download = FakeDownload(
        [download_frame({"AAPL": frame(rows, tz=None), "MSFT": frame(rows, tz=None)})]
    )
    monkeypatch.setattr(yf, "download", download)
    result = provider.fetch_recent(START, CUT)
    assert [b.date for b in bars_of(result["AAPL"])] == [
        date(2026, 11, 12),
        date(2026, 11, 18),
        date(2026, 11, 19),
    ]
    assert [b.date for b in bars_of(result["MSFT"])] == [date(2026, 11, 18), date(2026, 11, 19)]
    call = download.calls[0]
    assert sorted(download.tickers[0]) == ["AAPL", "MSFT"]
    assert call["start"] == "2026-11-12"
    assert call["auto_adjust"] is False
    assert call["actions"] is True
    assert call["repair"] is False
    assert call["group_by"] == "ticker"
    assert call["session"] is not None
    assert fake_yf.history_calls == []


def test_fetch_recent_chunks(
    provider: YahooProvider, fake_yf: type[FakeTicker], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"S{i:02d}" for i in range(yahoo.CHUNK_SIZE + 1)]
    frames = {id: frame(ROWS[:2], tz=None) for id in ids}
    download = FakeDownload(
        [
            download_frame({i: frames[i] for i in ids[:-1]}),
            download_frame({ids[-1]: frames[ids[-1]]}),
        ]
    )
    monkeypatch.setattr(yf, "download", download)
    result = provider.fetch_recent(
        dict.fromkeys(ids, date(2026, 11, 18)), dict.fromkeys(ids, date(2026, 11, 19))
    )
    assert [len(t) for t in download.tickers] == [yahoo.CHUNK_SIZE, 1]
    assert all(len(bars_of(v)) == 2 for v in result.values())


def test_fetch_recent_probes_missing_ids(
    provider: YahooProvider, fake_yf: type[FakeTicker], monkeypatch: pytest.MonkeyPatch
) -> None:
    download = FakeDownload([download_frame({"AAPL": frame(ROWS, tz=None)})])
    monkeypatch.setattr(yf, "download", download)
    fake_yf.histories["MSFT"] = YFPricesMissingError("MSFT", "")
    result = provider.fetch_recent(START, CUT)
    assert isinstance(result["MSFT"], NotFoundError)
    assert [c[0] for c in fake_yf.history_calls] == ["MSFT"]
    fake_yf.histories["MSFT"] = ConnectionError("reset")
    download.results.append(download_frame({"AAPL": frame(ROWS, tz=None)}))
    assert isinstance(provider.fetch_recent(START, CUT)["MSFT"], UpstreamError)
    fake_yf.histories["MSFT"] = frame(ROWS)
    download.results.append(None)
    fake_yf.histories["AAPL"] = frame(ROWS)
    result = provider.fetch_recent(START, CUT)
    assert len(bars_of(result["MSFT"])) == 2
    assert len(bars_of(result["AAPL"])) == 2


def test_fetch_recent_backs_off_then_succeeds(
    provider: YahooProvider,
    fake_yf: type[FakeTicker],
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = download_frame({"AAPL": frame(ROWS, tz=None).iloc[0:0]})
    download = FakeDownload([empty, download_frame({"AAPL": frame(ROWS, tz=None)})])
    monkeypatch.setattr(yf, "download", download)
    fake_yf.histories["AAPL"] = YFRateLimitError()
    result = provider.fetch_recent({"AAPL": START["AAPL"]}, CUT)
    assert sleeps == [yahoo.BACKOFF_S[0]]
    assert isinstance(result["AAPL"], tuple)


def test_fetch_recent_gives_up_after_backoff(
    provider: YahooProvider,
    fake_yf: type[FakeTicker],
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"S{i:02d}" for i in range(yahoo.CHUNK_SIZE + 1)]
    monkeypatch.setattr(yf, "download", lambda *a, **k: None)
    for id in ids:
        fake_yf.histories[id] = YFRateLimitError()
    result = provider.fetch_recent(
        dict.fromkeys(ids, date(2026, 11, 18)), dict.fromkeys(ids, date(2026, 11, 19))
    )
    assert sleeps == list(yahoo.BACKOFF_S)
    assert all(isinstance(v, RateLimitedError) for v in result.values())
    assert "skipped" in str(result[ids[-1]])
    assert len(fake_yf.history_calls) == len(yahoo.BACKOFF_S) + 1


def test_fetch_recent_rejects_bad_data(
    provider: YahooProvider, fake_yf: type[FakeTicker], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        yf,
        "download",
        FakeDownload([download_frame({"AAPL": frame([("2026-11-18", 0.0, 0.0, 0.0)], tz=None)})]),
    )
    assert isinstance(provider.fetch_recent({"AAPL": START["AAPL"]}, CUT)["AAPL"], UnsupportedError)


# meta and search ---------------------------------------------------------------------------


class Funds:
    sector_weightings: ClassVar[dict[str, float]] = {"technology": 0.7, "healthcare": 0.3}
    asset_classes: ClassVar[dict[str, float]] = {"stockPosition": 0.98}


def test_fetch_meta(provider: YahooProvider, fake_yf: type[FakeTicker]) -> None:
    fake_yf.infos["SPY"] = {
        "quoteType": "ETF",
        "longName": "SPDR",
        "currency": "USD",
        "sharesOutstanding": 9.0,
        "marketCap": None,
        "category": "Large Blend",
    }
    fake_yf.funds["SPY"] = Funds()
    meta = provider.fetch_meta("SPY")
    assert (
        meta.kind,
        meta.asset_class,
        meta.fund_sector_weights,
        meta.shares_outstanding,
        meta.market_cap,
    ) == ("etf", "equity", {"technology": 0.7, "healthcare": 0.3}, 9.0, None)
    fake_yf.infos["NOFUND"] = {"quoteType": "MUTUALFUND", "shortName": "Fund", "currency": "USD"}
    fake_yf.funds["NOFUND"] = YFDataException("No Fund data found.")
    nofund = provider.fetch_meta("NOFUND")
    assert (nofund.name, nofund.fund_sector_weights, nofund.asset_class) == (
        "Fund",
        None,
        "multi_asset",
    )
    fake_yf.infos["NONE"] = {"trailingPegRatio": None}
    with pytest.raises(NotFoundError):
        provider.fetch_meta("NONE")
    fake_yf.infos["NOCCY"] = {"quoteType": "EQUITY"}
    with pytest.raises(UpstreamError, match="no currency"):
        provider.fetch_meta("NOCCY")
    fake_yf.infos["BUSY"] = YFRateLimitError()
    with pytest.raises(RateLimitedError):
        provider.fetch_meta("BUSY")


def test_search(provider: YahooProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    quotes = [
        {"symbol": "tm", "longname": "Toyota", "quoteType": "EQUITY", "exchDisp": "NYSE"},
        {"symbol": "TOYOTA-OPT", "quoteType": "OPTION"},
        {"shortname": "no symbol", "quoteType": "EQUITY"},
        {"symbol": "7203.T", "shortname": "TOYOTA", "quoteType": "EQUITY", "exchange": "JPX"},
    ]

    class FakeSearch:
        def __init__(self, query: str, **kwargs: object) -> None:
            assert kwargs["news_count"] == 0
            assert kwargs["max_results"] == 5
            self.quotes = quotes

    monkeypatch.setattr(yf, "Search", FakeSearch)
    results = provider.search("toyota", 5)
    assert [(r.id, r.name, r.exchange) for r in results] == [
        ("TM", "Toyota", "NYSE"),
        ("7203.T", "TOYOTA", "JPX"),
    ]

    def broken(query: str, **kwargs: object) -> None:
        raise YFRateLimitError()

    monkeypatch.setattr(yf, "Search", broken)
    with pytest.raises(RateLimitedError):
        provider.search("toyota", 5)
