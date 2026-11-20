"""Refresh job: every transition of the id lifecycle (see the PR's state-by-event table)."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app import refresh
from app.corporate_actions import CorporateAction
from app.providers.base import NotFoundError, ProviderError, RateLimitedError, UpstreamError
from app.service import META_TTL, Service
from app.store import Instrument
from app.timeseries import Bar, total_return
from tests.conftest import Clock, MakeService, SecWorld
from tests.fakes import FakeProvider, extend, make_history, make_meta

AAPL = make_history([10.0, 11.0, 12.0, 12.5], dividends={2: 0.5})


def seed(service: Service, provider: FakeProvider, **histories: object) -> None:
    for id, history in histories.items():
        provider.world[id] = history  # type: ignore[assignment]
    result = service.series(list(histories))
    assert result.errors == {}


def instrument(service: Service, id: str) -> Instrument:
    found = service.store.get_instrument(id)
    assert found is not None
    return found


def advance(provider: FakeProvider, *ids: str, n: int = 1) -> None:
    for id in ids:
        last = provider.world[id].bars[-1].close
        provider.world[id] = extend(provider.world[id], [last + i + 1 for i in range(n)])


def assert_cache_matches_world(service: Service, provider: FakeProvider, id: str) -> None:
    stored = service.store.get_bars(id)
    assert [s.bar for s in stored] == list(provider.world[id].bars)
    assert [s.value for s in stored] == pytest.approx(
        total_return(provider.world[id].bars), rel=1e-12
    )


def test_new_bars_are_appended_with_continuing_total_return(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    provider.world["AAPL"] = extend(AAPL, [13.0, 13.5, 14.0], dividends={1: 0.25})
    advance(provider, "SPY", n=3)
    report = refresh.run(service)
    assert report.appended_bars == 6
    assert report.full_refetches == {}
    assert report.failed == {}
    assert report.upstream_calls == {"yahoo": 2}
    assert_cache_matches_world(service, provider, "AAPL")
    aapl = instrument(service, "AAPL")
    assert (aapl.status, aapl.stale, aapl.missing_upstream, aapl.refresh_error) == (
        "active",
        False,
        False,
        None,
    )


def test_no_new_bars(service: Service, provider: FakeProvider) -> None:
    seed(service, provider, AAPL=AAPL)
    report = refresh.run(service)
    assert report.appended_bars == 0
    assert report.failed == {}
    assert instrument(service, "AAPL").status == "active"


def test_new_split_triggers_full_refetch(service: Service, provider: FakeProvider) -> None:
    seed(service, provider, AAPL=AAPL)
    halved = make_history([c / 2 for c in (10.0, 11.0, 12.0, 12.5)], dividends={2: 0.25})
    split_day = extend(halved, [6.5]).bars[-1]
    provider.world["AAPL"] = replace(halved, bars=(*halved.bars, replace(split_day, split=2.0)))
    advance(provider, "SPY")
    report = refresh.run(service)
    assert report.full_refetches["AAPL"].startswith("new split 2.0 on")
    assert_cache_matches_world(service, provider, "AAPL")
    assert not instrument(service, "AAPL").stale


def test_late_dividend_in_overlap_triggers_full_refetch(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    bars = list(AAPL.bars)
    bars[-1] = replace(bars[-1], dividend=0.1)
    provider.world["AAPL"] = replace(AAPL, bars=tuple(bars))
    report = refresh.run(service)
    assert "dividend changed" in report.full_refetches["AAPL"]
    assert_cache_matches_world(service, provider, "AAPL")


@pytest.mark.parametrize("error", [UpstreamError("Yahoo 503"), RateLimitedError("429")])
def test_upstream_failure_marks_stale_and_keeps_cache(
    service: Service, provider: FakeProvider, error: UpstreamError
) -> None:
    seed(service, provider, AAPL=AAPL)
    before = service.store.get_bars("AAPL")
    provider.recent_errors["AAPL"] = error
    report = refresh.run(service)
    assert report.failed == {"AAPL": str(error)}
    aapl = instrument(service, "AAPL")
    assert (aapl.stale, aapl.status, aapl.refresh_error) == (True, "active", str(error))
    assert service.store.get_bars("AAPL") == before
    entry = service.series(["AAPL"]).values["AAPL"]
    assert entry["stale"] is True
    del provider.recent_errors["AAPL"]
    refresh.run(service)
    assert not instrument(service, "AAPL").stale


def test_failed_full_refetch_marks_stale(service: Service, provider: FakeProvider) -> None:
    seed(service, provider, AAPL=AAPL)
    bars = list(AAPL.bars)
    bars[-1] = replace(bars[-1], close=99.0)
    provider.world["AAPL"] = replace(AAPL, bars=tuple(bars))
    provider.errors["AAPL"] = UpstreamError("boom")
    report = refresh.run(service)
    assert report.failed["AAPL"] == "full refetch failed: boom"
    assert instrument(service, "AAPL").stale
    assert service.store.get_bars("AAPL")[-1].bar.close == 12.5


def test_full_refetch_that_would_drop_history_is_refused(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    shorter = replace(AAPL, bars=tuple(replace(b, close=b.close * 2) for b in AAPL.bars[1:]))
    provider.world["AAPL"] = shorter
    report = refresh.run(service)
    assert "does not cover cached" in report.failed["AAPL"]
    assert len(service.store.get_bars("AAPL")) == len(AAPL.bars)
    assert instrument(service, "AAPL").stale


def test_not_returned_upstream_is_ended_then_resumes(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    gone = provider.world.pop("AAPL")
    report = refresh.run(service)
    assert report.missing_upstream == ["AAPL"]
    assert report.failed == {}
    assert report.unresolved_ended == ["AAPL"]
    aapl = instrument(service, "AAPL")
    assert (aapl.status, aapl.missing_upstream, aapl.stale) == ("ended", True, False)
    assert service.series(["AAPL"]).values["AAPL"]["status"] == "ended"
    provider.world["AAPL"] = extend(gone, [13.0])
    advance(provider, "SPY")
    refresh.run(service)
    aapl = instrument(service, "AAPL")
    assert (aapl.status, aapl.missing_upstream) == ("active", False)


def test_gap_of_five_reference_days_ends_and_resuming_reactivates(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    advance(provider, "SPY", n=4)
    refresh.run(service)
    assert instrument(service, "AAPL").status == "active"
    advance(provider, "SPY")
    refresh.run(service)
    assert instrument(service, "AAPL").status == "ended"
    assert [e["id"] for e in service.ended()] == ["AAPL"]
    provider.world["AAPL"] = extend(provider.world["AAPL"], [13.0] * 5)
    refresh.run(service)
    assert instrument(service, "AAPL").status == "active"
    assert service.ended() == []


def test_reference_is_cached_when_missing(
    service: Service, provider: FakeProvider, clock: Clock
) -> None:
    provider.world["AAPL"] = AAPL
    service.store.insert_history(
        "AAPL", "yahoo", AAPL, total_return(AAPL.bars), "active", None, clock()
    )
    report = refresh.run(service)
    assert service.store.get_instrument("SPY") is not None
    assert report.ids == 2


def test_id_without_provider_is_reported(
    service: Service, provider: FakeProvider, clock: Clock
) -> None:
    seed(service, provider, AAPL=AAPL)
    service.store.insert_history(
        "pm:1", "polymarket", make_history([0.5], kind="equity"), [0.5], "active", None, clock()
    )
    report = refresh.run(service)
    assert report.failed == {"pm:1": "no provider for source polymarket"}
    assert instrument(service, "pm:1").stale


def test_meta_ttl_refresh(service: Service, provider: FakeProvider, clock: Clock) -> None:
    seed(service, provider, AAPL=AAPL, MSFT=AAPL, NVDA=AAPL)
    for id in ("AAPL", "MSFT", "NVDA"):
        provider.metas[id] = make_meta(id)
    service.meta(["AAPL", "MSFT", "NVDA"])
    report = refresh.run(service)
    assert report.meta_refreshed == 0
    clock.value += META_TTL
    provider.metas["AAPL"] = make_meta("AAPL", shares=5.0)
    provider.metas["MSFT"] = RateLimitedError("429")
    report = refresh.run(service)
    assert report.meta_refreshed == 1
    assert report.meta_failed == {"MSFT": "429", "NVDA": "skipped: upstream rate limited"}
    assert service.meta(["AAPL"]).values["AAPL"]["sharesOutstanding"] == 5.0


def test_rename_detection(make_service: MakeService, provider: FakeProvider, sec: SecWorld) -> None:
    sec.tickers = {"OLD": 1, "AMBI": 2, "LIVE": 3, "BOUGHT": 4}
    service = make_service(
        edgar=True,
        actions={"BOUGHT": CorporateAction("BOUGHT", None, 0.0, 1.0, AAPL.bars[-1].date, "")},
    )
    seed(service, provider, OLD=AAPL, AMBI=AAPL, LIVE=AAPL, BOUGHT=AAPL, LATE=AAPL)
    assert instrument(service, "OLD").cik == 1
    assert instrument(service, "LATE").cik is None
    for id in ("OLD", "AMBI", "BOUGHT"):
        del provider.world[id]
    sec.tickers = {"NEW": 1, "AMBI-A": 2, "AMBI-B": 2, "LIVE": 3, "LATE": 5, "BOUGHT2": 4}
    report = refresh.run(service)
    assert report.renames == {"OLD": "NEW"}
    assert report.edgar_note is None
    assert report.unresolved_ended == ["AMBI"]
    assert report.upstream_calls["edgar"] == 1
    assert instrument(service, "OLD").edgar_successor == "NEW"
    assert instrument(service, "AMBI").edgar_successor is None
    assert instrument(service, "BOUGHT").edgar_successor is None
    assert instrument(service, "LATE").cik == 5
    ended = {e["id"]: e for e in service.ended()}
    assert ended["OLD"]["successor"] is not None
    assert ended["OLD"]["successor"]["source"] == "edgar_rename"
    assert ended["BOUGHT"]["successor"] is not None
    assert ended["BOUGHT"]["successor"]["source"] == "corporate_actions"
    # OLD trades again: the rename no longer applies.
    provider.world["OLD"] = AAPL
    refresh.run(service)
    old = instrument(service, "OLD")
    assert (old.status, old.edgar_successor) == ("active", None)


def test_rename_detection_without_edgar_is_reported(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    report = refresh.run(service)
    assert report.edgar_note is not None
    assert "SEC_USER_AGENT is not set" in report.edgar_note


def test_rename_detection_with_sec_outage(
    make_service: MakeService, provider: FakeProvider, sec: SecWorld
) -> None:
    service = make_service(edgar=True)
    seed(service, provider, AAPL=AAPL)
    sec.status = 503
    report = refresh.run(service)
    assert report.edgar_note is not None
    assert "SEC ticker map unavailable" in report.edgar_note


def test_report_lines_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    service: Service,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(service, provider, AAPL=AAPL)
    monkeypatch.setattr(refresh, "build_service", lambda _settings: service)
    assert refresh.main() == 0
    out = capsys.readouterr().out
    assert "upstream calls: {'yahoo': 2}" in out
    assert "unresolved ended ids (add to corporate_actions.toml): []" in out
    provider.recent_errors["AAPL"] = NotFoundError("gone")
    provider.recent_errors["SPY"] = UpstreamError("down")
    assert refresh.main() == 1
    out = capsys.readouterr().out
    assert "failed (serving stale cache): 1" in out
    assert "  SPY: down" in out
    assert "no longer returned by upstream: ['AAPL']" in out


def test_refetch_window_starts_seven_days_before_last_bar(
    service: Service, provider: FakeProvider
) -> None:
    seed(service, provider, AAPL=AAPL)
    seen: dict[str, object] = {}
    original = provider.fetch_recent

    def spy(
        starts: Mapping[str, date], cutoffs: Mapping[str, date]
    ) -> dict[str, tuple[Bar, ...] | ProviderError]:
        seen.update(starts)
        return original(starts, cutoffs)

    provider.fetch_recent = spy  # type: ignore[method-assign]
    refresh.run(service)
    assert seen["AAPL"] == AAPL.bars[-1].date - timedelta(days=7)
