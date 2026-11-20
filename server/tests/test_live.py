"""Live smoke tests against Yahoo and SEC. Skipped by default; run with `uv run pytest -m live`.

The EDGAR test needs SEC_USER_AGENT (a descriptive agent with contact details, per SEC policy).
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.edgar import EdgarClient, point_in_time_shares
from app.providers.yahoo import YahooProvider
from app.service import Service
from app.store import Store
from app.timeseries import OVERLAP_RTOL, overlap_start, plan_incremental_merge

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def yahoo(tmp_path_factory: pytest.TempPathFactory) -> YahooProvider:
    return YahooProvider(tmp_path_factory.mktemp("yf"))


def test_yahoo_history_and_incremental_overlap_agree(yahoo: YahooProvider) -> None:
    ids = ["AAPL", "SPY", "7203.T", "VOD.L", "BTC-USD"]
    histories = {id: yahoo.fetch_history(id) for id in ids}
    assert len(histories["AAPL"].bars) > 10_000
    assert histories["VOD.L"].currency == "GBp"
    assert histories["7203.T"].currency == "JPY"
    today = datetime.now(UTC).date()
    assert all(h.bars[-1].date <= today for h in histories.values())
    # Pretend the cache ended 3 bars ago: the refetched window must agree with it.
    cached = {id: h.bars[:-3] for id, h in histories.items()}
    starts = {id: overlap_start(c[-1].date) for id, c in cached.items()}
    cutoffs = {id: h.bars[-1].date for id, h in histories.items()}
    before = yahoo.upstream_calls
    windows = yahoo.fetch_recent(starts, cutoffs)
    print(f"fetch_recent of {len(ids)} ids: {yahoo.upstream_calls - before} upstream calls")
    for id in ids:
        window = windows[id]
        assert isinstance(window, tuple), window
        tail = [b for b in cached[id] if b.date >= starts[id]]
        plan = plan_incremental_merge(tail, window, starts[id])
        assert plan.refetch_reason is None, (id, plan.refetch_reason)
        expected = histories[id].bars[-3:]
        assert [(b.date, b.close, b.split) for b in plan.new_bars] == [
            (b.date, b.close, b.split) for b in expected
        ]
        # Yahoo's dividend values can differ in the 5th significant digit between requests
        # (seen on VOD.L); the overlap check tolerates that.
        for new, old in zip(plan.new_bars, expected, strict=True):
            assert new.dividend == pytest.approx(old.dividend, rel=OVERLAP_RTOL)


def test_yahoo_unknown_symbol(yahoo: YahooProvider) -> None:
    from app.providers.base import NotFoundError

    with pytest.raises(NotFoundError):
        yahoo.fetch_history("ZZZZNOTREAL")


def test_yahoo_meta_and_search(yahoo: YahooProvider) -> None:
    spy = yahoo.fetch_meta("SPY")
    assert spy.kind == "etf"
    assert spy.asset_class == "equity"
    assert spy.fund_sector_weights is not None
    assert abs(sum(spy.fund_sector_weights.values()) - 1) < 0.02
    assert yahoo.fetch_meta("TLT").asset_class == "fixed_income"
    assert any(r.id == "AAPL" for r in yahoo.search("apple", 5))


def test_service_converts_live(yahoo: YahooProvider, tmp_path: Path) -> None:
    store = Store(tmp_path / "live.db")
    store.init()
    service = Service(store, {"yahoo": yahoo}, None, {}, lambda: datetime.now(UTC))
    result = service.series(["7203.T", "VOD.L"])
    assert result.errors == {}
    for entry in result.values.values():
        assert entry["converted"]
        assert entry["status"] == "active"
        assert entry["close"][-1] > 0


@pytest.mark.skipif(not os.environ.get("SEC_USER_AGENT"), reason="SEC_USER_AGENT not set")
def test_edgar_live() -> None:
    client = EdgarClient(os.environ["SEC_USER_AGENT"])
    tickers = client.ticker_map()
    assert tickers["AAPL"] == 320193
    known, values = point_in_time_shares(client.shares_facts(320193))
    assert len(known) > 10
    assert known[-1] > datetime.now(UTC).date() - timedelta(days=200)
    assert all(v > 1e9 for v in values)
