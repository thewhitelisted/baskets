from datetime import date
from pathlib import Path

import pytest

from app.config import CORPORATE_ACTIONS
from app.corporate_actions import (
    CorporateAction,
    CorporateActionsError,
    load_corporate_actions,
    parse_corporate_actions,
)

CONTRACT_EXAMPLE = """
[[action]]
id = "OLD"
successor = "NEW"        # omit for pure cash-out
ratio = 0.8              # successor shares per old share (omit or 0 if no successor)
cash_per_share = 12.5    # USD
effective_date = "2027-03-14"
note = "free text"
"""


def test_contract_example_parses() -> None:
    assert parse_corporate_actions(CONTRACT_EXAMPLE) == {
        "OLD": CorporateAction("OLD", "NEW", 0.8, 12.5, date(2027, 3, 14), "free text")
    }


def test_committed_file_is_valid() -> None:
    assert load_corporate_actions(CORPORATE_ACTIONS) == {}


def test_empty_document() -> None:
    assert parse_corporate_actions("") == {}


def test_cash_out_rename_and_toml_date_and_case() -> None:
    actions = parse_corporate_actions(
        """
        [[action]]
        id = "gone"
        cash_per_share = 0
        effective_date = 2027-01-05

        [[action]]
        id = "brk.a"
        successor = "brk-a2"
        ratio = 1
        effective_date = "2027-01-06"
        """
    )
    assert actions["GONE"] == CorporateAction("GONE", None, 0.0, 0.0, date(2027, 1, 5), "")
    assert actions["BRK.A"] == CorporateAction("BRK.A", "BRK-A2", 1.0, 0.0, date(2027, 1, 6), "")


def entry(**fields: str) -> str:
    body = {"id": '"OLD"', "successor": '"NEW"', "ratio": "1", "effective_date": '"2027-03-14"'}
    body.update(fields)
    return "[[action]]\n" + "\n".join(f"{k} = {v}" for k, v in body.items() if v != "<omit>")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[action]\n", "invalid TOML"),
        ("other = 1\n", "unknown top-level keys"),
        ("action = 1\n", "array of tables"),
        ("action = [1]\n", "must be a table"),
        (entry(id="<omit>"), "missing \\['id'\\]"),
        (entry(effective_date="<omit>"), "missing \\['effective_date'\\]"),
        (entry(colour='"red"'), "unknown keys \\['colour'\\]"),
        (entry(id="5"), "id must be a string"),
        (entry(id='"bad id"'), "invalid symbol id"),
        (entry(id='"pm:123"'), "must be a Yahoo symbol"),
        (entry(successor='"OLD"'), "successor equals id"),
        (entry(ratio="0"), "ratio must be > 0"),
        (entry(ratio="-1"), "ratio must be > 0"),
        (entry(ratio='"1"'), "ratio must be a number"),
        (entry(ratio="true"), "ratio must be a number"),
        (entry(ratio="nan"), "ratio must be finite"),
        (entry(ratio="inf"), "ratio must be finite"),
        (entry(successor="<omit>", ratio="0.5", cash_per_share="1"), "ratio must be omitted or 0"),
        (entry(successor="<omit>", ratio="<omit>"), "cash_per_share is required"),
        (entry(cash_per_share="-0.01"), "cash_per_share must be >= 0"),
        (entry(effective_date='"2027-3-14"'), "effective_date must be a date"),
        (entry(effective_date='"2027-02-30"'), "effective_date"),
        (entry(effective_date="2027-03-14T00:00:00"), "effective_date must be a date"),
        (entry(effective_date="20270314"), "effective_date must be a date"),
        (entry(note="1"), "note must be a string"),
        (entry() + "\n" + entry(), "duplicate id 'OLD'"),
    ],
)
def test_invalid_files_are_rejected(text: str, message: str) -> None:
    with pytest.raises(CorporateActionsError, match=message):
        parse_corporate_actions(text)


def test_load_reports_path(tmp_path: Path) -> None:
    path = tmp_path / "ca.toml"
    path.write_text(entry(ratio="0"))
    with pytest.raises(CorporateActionsError, match=r"ca\.toml: action #1 \(OLD\): ratio"):
        load_corporate_actions(path)
    with pytest.raises(CorporateActionsError, match="cannot read"):
        load_corporate_actions(tmp_path / "missing.toml")
