from datetime import date

import pytest

from app.corporate_actions import CorporateAction
from app.lifecycle import Lifecycle, Successor, rename_successor, resolve_lifecycle, tickers_by_cik

LAST = date(2026, 6, 1)
MERGER = CorporateAction("OLD", "NEW", 0.8, 12.5, date(2026, 6, 3), "")
CASH_OUT = CorporateAction("OLD", None, 0.0, 40.0, date(2026, 6, 2), "")


def test_active_without_action() -> None:
    assert resolve_lifecycle("active", LAST, None, None) == Lifecycle("active", None, True, None)


def test_active_ignores_stored_edgar_successor() -> None:
    assert resolve_lifecycle("active", LAST, None, "NEW").successor is None


def test_ended_without_successor_is_unreviewed_cash_out() -> None:
    assert resolve_lifecycle("ended", LAST, None, None) == Lifecycle("ended", None, False, None)


def test_ended_with_edgar_rename() -> None:
    assert resolve_lifecycle("ended", LAST, None, "NEW") == Lifecycle(
        "ended", Successor("NEW", 1.0, 0.0, LAST, "edgar_rename"), True, None
    )


@pytest.mark.parametrize("data_status", ["active", "ended"])
@pytest.mark.parametrize("edgar", [None, "OTHER"])
def test_corporate_action_overrides_everything(data_status: str, edgar: str | None) -> None:
    lifecycle = resolve_lifecycle(data_status, LAST, MERGER, edgar)  # type: ignore[arg-type]
    assert lifecycle == Lifecycle(
        "ended",
        Successor("NEW", 0.8, 12.5, date(2026, 6, 3), "corporate_actions"),
        True,
        MERGER.effective_date,
    )


def test_cash_out_action_has_successor_without_id() -> None:
    lifecycle = resolve_lifecycle("ended", LAST, CASH_OUT, None)
    assert lifecycle.successor == Successor(None, 0.0, 40.0, date(2026, 6, 2), "corporate_actions")
    assert lifecycle.reviewed


@pytest.mark.parametrize(
    ("tickers", "expected"),
    [
        ([], None),
        (["OLD"], None),
        (["OLD", "NEW"], None),
        (["NEW"], "NEW"),
        (["NEW", "NEW2"], None),
    ],
)
def test_rename_successor(tickers: list[str], expected: str | None) -> None:
    assert rename_successor("OLD", tickers) == expected


def test_tickers_by_cik_groups_sorted() -> None:
    assert tickers_by_cik({"GOOGL": 1, "GOOG": 1, "AAPL": 2}) == {1: ["GOOG", "GOOGL"], 2: ["AAPL"]}
