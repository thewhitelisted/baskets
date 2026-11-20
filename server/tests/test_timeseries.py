"""Pure series logic.

Input space considered: empty / one / many bars; zero and positive dividends; dividends on
the first bar; seeds at every split point; FX dates before, equal to, after and between asset
dates; FX starting after the asset; minor units; finality exactly at, before and after the
session close + delay, across DST and timezones; overlap windows with missing, extra,
changed (inside / outside tolerance) bars, new and known splits; reference gaps of 4/5/6.
"""

import math
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.timeseries import (
    ENDED_AFTER_MISSED_SESSIONS,
    OVERLAP_RTOL,
    Bar,
    DataQualityError,
    convert_to_usd,
    drop_unfinished,
    fx_pair_id,
    is_ended,
    last_final_date,
    missed_sessions,
    overlap_start,
    plan_incremental_merge,
    total_return,
    usd_conversion,
    validate_bars,
)

D0 = date(2026, 3, 2)


def bars_from(closes: list[float], divs: list[float] | None = None) -> list[Bar]:
    divs = divs or [0.0] * len(closes)
    return [
        Bar(D0 + timedelta(days=i), c, d, 0.0)
        for i, (c, d) in enumerate(zip(closes, divs, strict=True))
    ]


prices = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
dividends = st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=50.0, allow_nan=False))
series = st.lists(st.tuples(prices, dividends), min_size=1, max_size=60).map(
    lambda rows: bars_from([c for c, _ in rows], [d for _, d in rows])
)


def reference_tr(bars: list[Bar]) -> list[Fraction]:
    """TR_t = Close_0 * prod_{k<=t} (Close_k + Div_k) / Close_{k-1}, in exact arithmetic."""
    out = [Fraction(bars[0].close)]
    for k in range(1, len(bars)):
        growth = (Fraction(bars[k].close) + Fraction(bars[k].dividend)) / Fraction(
            bars[k - 1].close
        )
        out.append(out[-1] * growth)
    return out


# total return ------------------------------------------------------------------------------


@given(series)
def test_total_return_matches_exact_product(bars: list[Bar]) -> None:
    got = total_return(bars)
    want = reference_tr(bars)
    assert len(got) == len(bars)
    for g, w in zip(got, want, strict=True):
        assert math.isclose(g, float(w), rel_tol=1e-9)


@given(series, st.data())
def test_incremental_append_equals_full_recompute(bars: list[Bar], data: st.DataObject) -> None:
    k = data.draw(st.integers(min_value=1, max_value=len(bars)))
    head = total_return(bars[:k])
    tail = total_return(bars[k:], seed=(head[-1], bars[k - 1].close))
    assert head + tail == total_return(bars)


def test_total_return_examples() -> None:
    assert total_return([]) == []
    assert total_return(bars_from([50.0])) == [50.0]
    # Dividend on the first bar does not count; 2 + 0.5 paid on the ex-date of bar 2.
    assert total_return(bars_from([10.0, 10.0, 8.0], [1.0, 0.0, 2.0])) == [10.0, 10.0, 10.0]


def test_total_return_without_dividends_is_the_close() -> None:
    closes = [3.0, 4.5, 2.25, 9.0]
    assert total_return(bars_from(closes)) == pytest.approx(closes, rel=1e-12)


# validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bars", "message"),
    [
        ([], "no bars"),
        ([Bar(D0, 0.0, 0.0, 0.0)], "non-positive"),
        ([Bar(D0, -1.0, 0.0, 0.0)], "non-positive"),
        ([Bar(D0, math.nan, 0.0, 0.0)], "non-finite"),
        ([Bar(D0, math.inf, 0.0, 0.0)], "non-finite"),
        ([Bar(D0, 1.0, -0.1, 0.0)], "dividend"),
        ([Bar(D0, 1.0, math.nan, 0.0)], "dividend"),
        ([Bar(D0, 1.0, 0.0, -2.0)], "split"),
        ([Bar(D0, 1.0, 0.0, 0.0), Bar(D0, 1.0, 0.0, 0.0)], "ascending"),
        ([Bar(D0, 1.0, 0.0, 0.0), Bar(D0 - timedelta(days=1), 1.0, 0.0, 0.0)], "ascending"),
    ],
)
def test_validate_bars_rejects(bars: list[Bar], message: str) -> None:
    with pytest.raises(DataQualityError, match=message):
        validate_bars(bars)


def test_validate_bars_accepts_good_bars() -> None:
    validate_bars(bars_from([1.0, 2.0], [0.0, 0.5]))


# FX and minor units ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("GBp", ("GBP", 100)),
        ("ZAc", ("ZAR", 100)),
        ("ILA", ("ILS", 100)),
        ("USX", ("USD", 100)),
        ("JPY", ("JPY", 1)),
        ("USD", ("USD", 1)),
        ("GBP", ("GBP", 1)),
    ],
)
def test_usd_conversion_units(currency: str, expected: tuple[str, int]) -> None:
    assert usd_conversion(currency) == expected


def test_fx_pair_id() -> None:
    assert fx_pair_id("JPY") == "JPYUSD=X"


def test_convert_to_usd_forward_fills_and_drops_dates_before_first_rate() -> None:
    d = [date(2026, 1, i) for i in (1, 2, 5, 6, 9)]
    fx_dates = [date(2026, 1, 2), date(2026, 1, 6), date(2026, 1, 7)]
    out_dates, out_values = convert_to_usd(d, [100.0] * 5, 100, fx_dates, [1.2, 1.3, 1.4])
    assert out_dates == d[1:]
    assert out_values == pytest.approx([1.2, 1.2, 1.3, 1.4])


def test_convert_to_usd_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="length"):
        convert_to_usd([D0], [1.0, 2.0], 1, [], [])


day_offsets = st.lists(st.integers(min_value=0, max_value=60), unique=True).map(sorted)


@given(day_offsets, day_offsets, st.integers(min_value=1, max_value=100))
def test_convert_to_usd_matches_linear_scan(a: list[int], f: list[int], divisor: int) -> None:
    dates = [D0 + timedelta(days=i) for i in a]
    values = [float(i + 1) for i in a]
    fx_dates = [D0 + timedelta(days=i) for i in f]
    fx_values = [1.0 + i / 100 for i in f]
    got = convert_to_usd(dates, values, divisor, fx_dates, fx_values)
    want_d: list[date] = []
    want_v: list[float] = []
    for d, v in zip(dates, values, strict=True):
        rates = [r for fd, r in zip(fx_dates, fx_values, strict=True) if fd <= d]
        if rates:
            want_d.append(d)
            want_v.append(v / divisor * rates[-1])
    assert got == (want_d, want_v)


# finality ----------------------------------------------------------------------------------


NY = "America/New_York"


@pytest.mark.parametrize(
    ("now", "tz", "close", "expected"),
    [
        # 16:00 close + 1h delay = 17:00 local. EST is UTC-5.
        (datetime(2026, 11, 20, 21, 59, tzinfo=UTC), NY, time(16), date(2026, 11, 19)),
        (datetime(2026, 11, 20, 22, 0, tzinfo=UTC), NY, time(16), date(2026, 11, 20)),
        # 03:00 UTC is 22:00 ET the previous local day, after that day's close.
        (datetime(2026, 11, 20, 3, 0, tzinfo=UTC), NY, time(16), date(2026, 11, 19)),
        # EDT (UTC-4) after the March DST switch: 17:00 local = 21:00 UTC.
        (datetime(2026, 3, 9, 20, 59, tzinfo=UTC), NY, time(16), date(2026, 3, 8)),
        (datetime(2026, 3, 9, 21, 0, tzinfo=UTC), NY, time(16), date(2026, 3, 9)),
        # Tokyo 15:30 close: final from 16:30 JST = 07:30 UTC.
        (datetime(2026, 11, 20, 7, 29, tzinfo=UTC), "Asia/Tokyo", time(15, 30), date(2026, 11, 19)),
        (datetime(2026, 11, 20, 7, 30, tzinfo=UTC), "Asia/Tokyo", time(15, 30), date(2026, 11, 20)),
        # Crypto session ends 23:59 UTC: today's bar is never final the same UTC day.
        (datetime(2026, 11, 20, 23, 59, tzinfo=UTC), "UTC", time(23, 59), date(2026, 11, 19)),
        (datetime(2026, 11, 21, 0, 59, tzinfo=UTC), "UTC", time(23, 59), date(2026, 11, 20)),
    ],
)
def test_last_final_date(now: datetime, tz: str, close: time, expected: date) -> None:
    assert last_final_date(now, tz, close) == expected


def test_last_final_date_requires_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        last_final_date(datetime(2026, 1, 1), NY, time(16))


def test_drop_unfinished() -> None:
    bars = bars_from([1.0, 2.0, 3.0])
    assert drop_unfinished(bars, bars[1].date) == bars[:2]


# incremental merge -------------------------------------------------------------------------


def window_of(bars: list[Bar], start: date) -> list[Bar]:
    return [b for b in bars if b.date >= start]


def test_overlap_start_is_seven_days_back() -> None:
    assert overlap_start(date(2026, 3, 10)) == date(2026, 3, 3)


def test_merge_appends_new_bars_when_overlap_matches() -> None:
    full = bars_from([10.0 + i for i in range(12)])
    cached = full[:8]
    start = overlap_start(cached[-1].date)
    plan = plan_incremental_merge(window_of(cached, start), window_of(full, start), start)
    assert plan.refetch_reason is None
    assert plan.new_bars == tuple(full[8:])


def test_merge_with_no_new_bars_appends_nothing() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    start = overlap_start(cached[-1].date)
    plan = plan_incremental_merge(window_of(cached, start), window_of(cached, start), start)
    assert plan == plan.__class__((), None)


def _plan_with(fetched_overlap: list[Bar], cached: list[Bar]) -> str | None:
    start = overlap_start(cached[-1].date)
    return plan_incremental_merge(
        window_of(cached, start), window_of(fetched_overlap, start), start
    ).refetch_reason


def test_merge_refetches_on_missing_overlap_date() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    fetched = cached[:5] + cached[6:]
    assert "missing 2026-03-07" in (_plan_with(fetched, cached) or "")


def test_merge_refetches_on_missing_last_cached_bar() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    assert "missing" in (_plan_with(cached[:-1], cached) or "")


def test_merge_refetches_on_extra_overlap_date() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    fewer = cached[:5] + cached[6:]
    reason = _plan_with(cached, fewer)
    assert reason is not None
    assert "unexpected 2026-03-07" in reason


@pytest.mark.parametrize(
    ("factor", "refetch"),
    [(1 + OVERLAP_RTOL / 2, False), (1 + OVERLAP_RTOL * 2, True), (0.5, True)],
)
def test_merge_close_tolerance(factor: float, refetch: bool) -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    fetched = [*cached[:-2], Bar(cached[-2].date, cached[-2].close * factor, 0.0, 0.0), cached[-1]]
    reason = _plan_with(fetched, cached)
    assert (reason is not None) == refetch
    if refetch:
        assert reason is not None
        assert "close changed" in reason


def test_merge_refetches_on_new_dividend_in_overlap() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    fetched = [*cached[:-1], Bar(cached[-1].date, cached[-1].close, 0.3, 0.0)]
    assert "dividend changed" in (_plan_with(fetched, cached) or "")


def test_merge_refetches_on_new_split_after_cache() -> None:
    cached = bars_from([10.0 + i for i in range(8)])
    new = Bar(cached[-1].date + timedelta(days=1), 5.0, 0.0, 2.0)
    assert "new split" in (_plan_with([*cached, new], cached) or "")


def test_merge_ignores_split_already_cached() -> None:
    cached = [*bars_from([10.0 + i for i in range(7)]), Bar(D0 + timedelta(days=7), 5.0, 0.0, 2.0)]
    new = Bar(cached[-1].date + timedelta(days=1), 5.5, 0.0, 0.0)
    start = overlap_start(cached[-1].date)
    plan = plan_incremental_merge(window_of(cached, start), window_of([*cached, new], start), start)
    assert plan.refetch_reason is None
    assert plan.new_bars == (new,)


def test_merge_refetches_on_empty_window() -> None:
    cached = bars_from([10.0, 11.0])
    plan = plan_incremental_merge(cached, [], cached[0].date)
    assert plan.refetch_reason == "upstream returned no bars in the refresh window"


def test_merge_rejects_bad_arguments() -> None:
    cached = bars_from([10.0, 11.0])
    with pytest.raises(ValueError, match="empty"):
        plan_incremental_merge([], cached, cached[0].date)
    with pytest.raises(ValueError, match="before the window start"):
        plan_incremental_merge(cached, cached, cached[1].date)


# ended detection ---------------------------------------------------------------------------


REF = [date(2026, 3, i) for i in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]


@pytest.mark.parametrize(
    ("last", "missed"),
    [
        (date(2026, 3, 13), 0),
        (date(2026, 3, 20), 0),
        (date(2026, 3, 9), 4),
        (date(2026, 3, 8), 5),
        (date(2026, 3, 6), 5),
        (date(2026, 3, 5), 6),
        (date(2026, 1, 1), 10),
    ],
)
def test_missed_sessions(last: date, missed: int) -> None:
    assert missed_sessions(last, REF) == missed
    assert is_ended(last, REF, False) == (missed >= ENDED_AFTER_MISSED_SESSIONS)


def test_missing_upstream_is_ended_regardless_of_gap() -> None:
    assert is_ended(date(2026, 3, 13), REF, True)
    assert not is_ended(date(2026, 3, 13), REF, False)
