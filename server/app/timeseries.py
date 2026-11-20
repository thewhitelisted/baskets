"""Pure series logic: total return, FX conversion, bar finality, overlap checks, ended detection.

Nothing here does I/O; every function is deterministic in its arguments.
"""

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Yahoo quotes some listings in a minor unit. Each maps to (ISO currency, divisor).
MINOR_UNITS: dict[str, tuple[str, int]] = {
    "GBp": ("GBP", 100),
    "ZAc": ("ZAR", 100),
    "ILA": ("ILS", 100),
    "USX": ("USD", 100),
}

ENDED_AFTER_MISSED_SESSIONS = 5
OVERLAP_DAYS = 7
OVERLAP_RTOL = 1e-4
OVERLAP_DIV_ATOL = 1e-6
# A bar is final once its session closed this long ago (closing auctions settle, funds post NAV).
FINALITY_DELAY = timedelta(hours=1)


class DataQualityError(ValueError):
    """Upstream returned bars that violate the series invariants."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily bar: split-adjusted close, dividend on its ex-date, split ratio (0 = none)."""

    date: date
    close: float
    dividend: float
    split: float


def validate_bars(bars: Sequence[Bar]) -> None:
    """Raise DataQualityError unless bars are non-empty, strictly ascending, finite, close > 0."""
    if not bars:
        raise DataQualityError("no bars")
    for i, bar in enumerate(bars):
        if i > 0 and bar.date <= bars[i - 1].date:
            raise DataQualityError(f"dates not strictly ascending at {bar.date.isoformat()}")
        if not (math.isfinite(bar.close) and bar.close > 0):
            raise DataQualityError(f"non-positive or non-finite close {bar.close} on {bar.date}")
        if not (math.isfinite(bar.dividend) and bar.dividend >= 0):
            raise DataQualityError(f"invalid dividend {bar.dividend} on {bar.date}")
        if not (math.isfinite(bar.split) and bar.split >= 0):
            raise DataQualityError(f"invalid split {bar.split} on {bar.date}")


def total_return(bars: Sequence[Bar], seed: tuple[float, float] | None = None) -> list[float]:
    """Total-return index: TR_0 = Close_0, TR_t = TR_{t-1} * (Close_t + Div_t) / Close_{t-1}.

    `seed` = (TR, Close) of the bar immediately before `bars`, to extend an existing series;
    extending must give exactly the values a full recompute would.
    """
    out: list[float] = []
    if seed is None:
        if not bars:
            return out
        prev_tr, prev_close = bars[0].close, bars[0].close
        out.append(prev_tr)
        rest = bars[1:]
    else:
        prev_tr, prev_close = seed
        rest = bars
    for bar in rest:
        prev_tr = prev_tr * (bar.close + bar.dividend) / prev_close
        prev_close = bar.close
        out.append(prev_tr)
    return out


def usd_conversion(currency: str) -> tuple[str, int]:
    """(ISO currency of the quote, divisor to get from the quote unit to that currency)."""
    return MINOR_UNITS.get(currency, (currency, 1))


def fx_pair_id(iso_currency: str) -> str:
    return f"{iso_currency}USD=X"


def convert_to_usd(
    dates: Sequence[date],
    values: Sequence[float],
    divisor: int,
    fx_dates: Sequence[date],
    fx_values: Sequence[float],
) -> tuple[list[date], list[float]]:
    """value / divisor * (latest FX close on or before each date).

    Dates before the first FX close cannot be converted and are omitted.
    """
    if len(dates) != len(values) or len(fx_dates) != len(fx_values):
        raise ValueError("dates and values differ in length")
    out_dates: list[date] = []
    out_values: list[float] = []
    for d, v in zip(dates, values, strict=True):
        i = bisect.bisect_right(fx_dates, d) - 1
        if i < 0:
            continue
        out_dates.append(d)
        out_values.append(v / divisor * fx_values[i])
    return out_dates, out_values


def last_final_date(now: datetime, tz: str, session_close: time) -> date:
    """Latest exchange-local date whose daily bar is complete at instant `now` (tz-aware)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(tz))
    final_at = datetime.combine(local.date(), session_close, ZoneInfo(tz)) + FINALITY_DELAY
    return local.date() if local >= final_at else local.date() - timedelta(days=1)


def drop_unfinished(bars: Sequence[Bar], cutoff: date) -> list[Bar]:
    return [b for b in bars if b.date <= cutoff]


def overlap_start(last_cached: date) -> date:
    return last_cached - timedelta(days=OVERLAP_DAYS)


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Outcome of comparing a refetched window with the cache: append `new_bars` or refetch."""

    new_bars: tuple[Bar, ...]
    refetch_reason: str | None


def plan_incremental_merge(cached: Sequence[Bar], window: Sequence[Bar], start: date) -> MergePlan:
    """Decide how to merge a window refetched from `start` into the cache.

    `cached` is the cached tail: every cached bar dated on or after `start`. Full refetch when
    the overlap (start .. last cached date) disagrees: missing or extra dates, close or
    dividend mismatch, or a split not already cached.
    """
    if not cached:
        raise ValueError("cached tail is empty")
    if any(b.date < start for b in cached) or any(b.date < start for b in window):
        raise ValueError("bars before the window start")
    if not window:
        return MergePlan((), "upstream returned no bars in the refresh window")
    last_cached = cached[-1].date
    cached_in_window = {b.date: b for b in cached}
    fetched_overlap = {b.date: b for b in window if b.date <= last_cached}
    if set(cached_in_window) != set(fetched_overlap):
        missing = sorted(set(cached_in_window) - set(fetched_overlap))
        extra = sorted(set(fetched_overlap) - set(cached_in_window))
        return MergePlan(
            (), f"overlap dates differ (missing {_fmt(missing)}, unexpected {_fmt(extra)})"
        )
    cached_splits = {b.date for b in cached if b.split > 0}
    for bar in window:
        if bar.split > 0 and bar.date not in cached_splits:
            return MergePlan((), f"new split {bar.split} on {bar.date.isoformat()}")
    for d, new in fetched_overlap.items():
        old = cached_in_window[d]
        if not math.isclose(new.close, old.close, rel_tol=OVERLAP_RTOL):
            return MergePlan((), f"close changed on {d.isoformat()}: {old.close} -> {new.close}")
        if not math.isclose(
            new.dividend, old.dividend, rel_tol=OVERLAP_RTOL, abs_tol=OVERLAP_DIV_ATOL
        ):
            return MergePlan(
                (), f"dividend changed on {d.isoformat()}: {old.dividend} -> {new.dividend}"
            )
    return MergePlan(tuple(b for b in window if b.date > last_cached), None)


def _fmt(ds: list[date]) -> str:
    return ",".join(d.isoformat() for d in ds[:3]) + ("..." if len(ds) > 3 else "") or "none"


def missed_sessions(last_date: date, reference_dates: Sequence[date]) -> int:
    """Number of reference (SPY) trading days strictly after `last_date`."""
    return len(reference_dates) - bisect.bisect_right(reference_dates, last_date)


def is_ended(last_date: date, reference_dates: Sequence[date], missing_upstream: bool) -> bool:
    return (
        missing_upstream
        or missed_sessions(last_date, reference_dates) >= ENDED_AFTER_MISSED_SESSIONS
    )
