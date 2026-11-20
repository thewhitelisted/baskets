"""Nightly refresh: `python -m app.refresh`.

Incrementally refreshes every cached id, refreshes expired metadata, re-evaluates ended
status, checks EDGAR for renames, and prints a report. Exits 1 if any id could not be
refreshed (it keeps serving its last good cache with stale=true).
"""

import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.bootstrap import build_service
from app.config import Settings
from app.edgar import DISABLED_MESSAGE
from app.ids import is_us_listing
from app.lifecycle import Status, rename_successor, tickers_by_cik
from app.providers.base import NotFoundError, Provider, ProviderError, RateLimitedError
from app.service import META_TTL, REFERENCE_ID, Service, served_values
from app.store import HistoryShrinkError, Instrument
from app.timeseries import (
    Bar,
    is_ended,
    last_final_date,
    overlap_start,
    plan_incremental_merge,
    total_return,
)

log = logging.getLogger(__name__)


@dataclass
class Report:
    ids: int = 0
    appended_bars: int = 0
    full_refetches: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    missing_upstream: list[str] = field(default_factory=list)
    meta_refreshed: int = 0
    meta_failed: dict[str, str] = field(default_factory=dict)
    renames: dict[str, str] = field(default_factory=dict)
    edgar_note: str | None = None
    unresolved_ended: list[str] = field(default_factory=list)
    upstream_calls: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"ids refreshed: {self.ids}",
            f"bars appended: {self.appended_bars}",
            f"upstream calls: {self.upstream_calls}",
            f"full refetches: {len(self.full_refetches)}",
            *[f"  {k}: {v}" for k, v in sorted(self.full_refetches.items())],
            f"failed (serving stale cache): {len(self.failed)}",
            *[f"  {k}: {v}" for k, v in sorted(self.failed.items())],
            f"no longer returned by upstream: {sorted(self.missing_upstream)}",
            f"meta refreshed: {self.meta_refreshed}, failed: {len(self.meta_failed)}",
            *[f"  {k}: {v}" for k, v in sorted(self.meta_failed.items())],
            f"EDGAR renames: {dict(sorted(self.renames.items()))}",
        ]
        if self.edgar_note:
            out.append(f"EDGAR: {self.edgar_note}")
        out.append(
            f"unresolved ended ids (add to corporate_actions.toml): {sorted(self.unresolved_ended)}"
        )
        return out


def run(service: Service) -> Report:
    report = Report()
    service.store.init()
    calls_before = {s: p.upstream_calls for s, p in service.providers.items()}
    edgar_before = service.edgar.calls if service.edgar is not None else 0

    instruments = service.store.list_instruments()
    if instruments and all(i.id != REFERENCE_ID for i in instruments):
        service.ensure_cached(REFERENCE_ID, service.providers["yahoo"])
        instruments = service.store.list_instruments()
    report.ids = len(instruments)

    outcomes: dict[str, tuple[bool, bool, str | None]] = {}
    for source, provider in service.providers.items():
        mine = [i for i in instruments if i.source == source]
        outcomes.update(_refresh_bars(service, provider, mine, report))
    for instrument in instruments:
        if instrument.source not in service.providers:
            message = f"no provider for source {instrument.source}"
            outcomes[instrument.id] = (True, instrument.missing_upstream, message)
            report.failed[instrument.id] = message

    reference = service.store.get_dates(REFERENCE_ID)
    for instrument in instruments:
        stale, missing, error = outcomes[instrument.id]
        last = service.store.last_date(instrument.id)
        assert last is not None
        status: Status = "ended" if is_ended(last, reference, missing) else "active"
        service.store.set_refresh_state(
            instrument.id,
            status=status,
            stale=stale,
            missing_upstream=missing,
            error=error,
            now=service.now(),
        )

    _refresh_meta(service, report)
    _check_renames(service, report)

    for entry in service.ended():
        if not entry["reviewed"]:
            report.unresolved_ended.append(entry["id"])
    report.upstream_calls = {
        s: p.upstream_calls - calls_before[s] for s, p in service.providers.items()
    }
    if service.edgar is not None:
        report.upstream_calls["edgar"] = service.edgar.calls - edgar_before
    return report


def _refresh_bars(
    service: Service, provider: Provider, instruments: Sequence[Instrument], report: Report
) -> dict[str, tuple[bool, bool, str | None]]:
    """Per id: (stale, missing_upstream, error message)."""
    now = service.now()
    starts: dict[str, date] = {}
    cutoffs: dict[str, date] = {}
    for instrument in instruments:
        last = service.store.last_date(instrument.id)
        assert last is not None
        starts[instrument.id] = overlap_start(last)
        cutoffs[instrument.id] = last_final_date(now, instrument.tz, instrument.session_close)
    fetched = provider.fetch_recent(starts, cutoffs) if starts else {}

    outcomes: dict[str, tuple[bool, bool, str | None]] = {}
    for instrument in instruments:
        id = instrument.id
        window = fetched[id]
        if isinstance(window, NotFoundError):
            report.missing_upstream.append(id)
            outcomes[id] = (False, True, str(window))
            continue
        if isinstance(window, ProviderError):
            report.failed[id] = str(window)
            outcomes[id] = (True, instrument.missing_upstream, str(window))
            continue
        cached = [s.bar for s in service.store.get_bars(id, since=starts[id])]
        plan = plan_incremental_merge(cached, window, starts[id])
        if plan.refetch_reason is None:
            _append(service, instrument, plan.new_bars)
            report.appended_bars += len(plan.new_bars)
            outcomes[id] = (False, False, None)
            continue
        report.full_refetches[id] = plan.refetch_reason
        outcomes[id] = _full_refetch(service, provider, id, report)
    return outcomes


def _append(service: Service, instrument: Instrument, bars: Sequence[Bar]) -> None:
    if not bars:
        return
    if instrument.adjustment == "total_return":
        last = service.store.last_bar(instrument.id)
        assert last is not None
        values = total_return(bars, seed=(last.value, last.bar.close))
    else:
        values = [b.close for b in bars]
    service.store.append_bars(instrument.id, bars, values)


def _full_refetch(
    service: Service, provider: Provider, id: str, report: Report
) -> tuple[bool, bool, str | None]:
    try:
        history = provider.fetch_history(id)
        service.store.replace_history(id, history, served_values(history), service.now())
    except (ProviderError, HistoryShrinkError) as e:
        report.failed[id] = f"full refetch failed: {e}"
        return (True, False, f"full refetch failed: {e}")
    return (False, False, None)


def _refresh_meta(service: Service, report: Report) -> None:
    now = service.now()
    rate_limited = False
    for instrument in service.store.list_instruments():
        doc = service.store.get_document("meta", instrument.id)
        if doc is None or now - doc.fetched_at < META_TTL:
            continue
        if rate_limited:
            report.meta_failed[instrument.id] = "skipped: upstream rate limited"
            continue
        try:
            service.refresh_meta(instrument.id)
            report.meta_refreshed += 1
        except ProviderError as e:
            rate_limited = isinstance(e, RateLimitedError)
            report.meta_failed[instrument.id] = str(e)


def _check_renames(service: Service, report: Report) -> None:
    if service.edgar is None:
        report.edgar_note = f"rename detection skipped: {DISABLED_MESSAGE}"
        log.warning(report.edgar_note)
        return
    try:
        tickers = service.sec_tickers(max_age=timedelta(0))
    except ProviderError as e:
        report.edgar_note = f"rename detection skipped: SEC ticker map unavailable: {e}"
        log.warning(report.edgar_note)
        return
    by_cik = tickers_by_cik(tickers)
    for instrument in service.store.list_instruments():
        id = instrument.id
        if instrument.status == "active":
            if instrument.cik is None and is_us_listing(id) and id in tickers:
                service.store.set_cik(id, tickers[id])
            if instrument.edgar_successor is not None:
                service.store.set_edgar_successor(id, None)
            continue
        if instrument.cik is None or id in service.corporate_actions:
            continue
        successor = rename_successor(id, by_cik.get(instrument.cik, []))
        if successor != instrument.edgar_successor:
            service.store.set_edgar_successor(id, successor)
        if successor is not None:
            report.renames[id] = successor


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = run(build_service(Settings.from_env()))
    print("\n".join(report.lines()))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
