"""Read-time lifecycle resolution: status, successor and review state of a cached id."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.corporate_actions import CorporateAction

Status = Literal["active", "ended"]
SuccessorSource = Literal["edgar_rename", "corporate_actions"]


@dataclass(frozen=True, slots=True)
class Successor:
    id: str | None
    ratio: float
    cash_per_share: float
    effective_date: date
    source: SuccessorSource


@dataclass(frozen=True, slots=True)
class Lifecycle:
    status: Status
    successor: Successor | None
    reviewed: bool
    # Bars after this date are not served (a corporate action ended the id).
    serve_through: date | None


def resolve_lifecycle(
    data_status: Status,
    last_date: date,
    action: CorporateAction | None,
    edgar_successor: str | None,
) -> Lifecycle:
    """Corporate actions override everything; otherwise an ended id may carry an EDGAR rename.

    `data_status` is what the bars say (ended detection); `last_date` the last cached bar.
    """
    if action is not None:
        successor = Successor(
            action.successor,
            action.ratio,
            action.cash_per_share,
            action.effective_date,
            "corporate_actions",
        )
        return Lifecycle("ended", successor, True, action.effective_date)
    if data_status == "active":
        return Lifecycle("active", None, True, None)
    if edgar_successor is not None:
        return Lifecycle(
            "ended", Successor(edgar_successor, 1.0, 0.0, last_date, "edgar_rename"), True, None
        )
    return Lifecycle("ended", None, False, None)


def rename_successor(id: str, cik_tickers: Sequence[str]) -> str | None:
    """Current ticker of the id's CIK when it is unambiguous and differs from `id`.

    `cik_tickers` are the tickers SEC currently lists for the stored CIK. If `id` is still
    among them, or SEC lists several others, no rename is inferred.
    """
    if id in cik_tickers:
        return None
    if len(cik_tickers) != 1:
        return None
    return cik_tickers[0]


def tickers_by_cik(ticker_to_cik: Mapping[str, int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for ticker, cik in sorted(ticker_to_cik.items()):
        out.setdefault(cik, []).append(ticker)
    return out
