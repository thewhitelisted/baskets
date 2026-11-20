"""Parse and validate server/data/corporate_actions.toml.

The file is curated by hand: mergers, cash-outs and renames that automatic detection misses.
"""

import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.ids import InvalidIdsError, normalize_id, source_of

_REQUIRED = {"id", "effective_date"}
_ALLOWED = {"id", "successor", "ratio", "cash_per_share", "effective_date", "note"}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CorporateActionsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CorporateAction:
    id: str
    successor: str | None
    ratio: float
    cash_per_share: float
    effective_date: date
    note: str


def parse_corporate_actions(text: str) -> dict[str, CorporateAction]:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CorporateActionsError(f"invalid TOML: {e}") from e
    unknown_tables = set(doc) - {"action"}
    if unknown_tables:
        raise CorporateActionsError(f"unknown top-level keys: {sorted(unknown_tables)}")
    entries = doc.get("action", [])
    if not isinstance(entries, list):
        raise CorporateActionsError("'action' must be an array of tables ([[action]])")
    actions: dict[str, CorporateAction] = {}
    for n, entry in enumerate(entries, start=1):
        action = _parse_entry(n, entry)
        if action.id in actions:
            raise CorporateActionsError(f"action #{n}: duplicate id {action.id!r}")
        actions[action.id] = action
    return actions


def load_corporate_actions(path: Path) -> dict[str, CorporateAction]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CorporateActionsError(f"cannot read {path}: {e}") from e
    try:
        return parse_corporate_actions(text)
    except CorporateActionsError as e:
        raise CorporateActionsError(f"{path}: {e}") from e


def _parse_entry(n: int, entry: object) -> CorporateAction:
    where = f"action #{n}"
    if not isinstance(entry, dict):
        raise CorporateActionsError(f"{where}: must be a table")
    missing = _REQUIRED - set(entry)
    if missing:
        raise CorporateActionsError(f"{where}: missing {sorted(missing)}")
    unknown = set(entry) - _ALLOWED
    if unknown:
        raise CorporateActionsError(f"{where}: unknown keys {sorted(unknown)}")

    id_ = _yahoo_id(where, "id", entry["id"])
    where = f"action #{n} ({id_})"
    successor = _yahoo_id(where, "successor", entry["successor"]) if "successor" in entry else None
    if successor == id_:
        raise CorporateActionsError(f"{where}: successor equals id")

    ratio = _number(where, "ratio", entry.get("ratio", 0))
    if successor is not None and ratio <= 0:
        raise CorporateActionsError(f"{where}: ratio must be > 0 when successor is set")
    if successor is None and ratio != 0:
        raise CorporateActionsError(f"{where}: ratio must be omitted or 0 without a successor")

    if successor is None and "cash_per_share" not in entry:
        raise CorporateActionsError(f"{where}: cash_per_share is required without a successor")
    cash = _number(where, "cash_per_share", entry.get("cash_per_share", 0))
    if cash < 0:
        raise CorporateActionsError(f"{where}: cash_per_share must be >= 0")

    effective = _effective_date(where, entry["effective_date"])

    note = entry.get("note", "")
    if not isinstance(note, str):
        raise CorporateActionsError(f"{where}: note must be a string")
    return CorporateAction(id_, successor, ratio, cash, effective, note)


def _effective_date(where: str, value: object) -> date:
    """A "YYYY-MM-DD" string (as in the contract example) or a TOML local date."""
    if type(value) is date:
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise CorporateActionsError(f"{where}: effective_date: {e}") from e
    raise CorporateActionsError(f'{where}: effective_date must be a date "YYYY-MM-DD"')


def _yahoo_id(where: str, key: str, value: object) -> str:
    if not isinstance(value, str):
        raise CorporateActionsError(f"{where}: {key} must be a string")
    try:
        normalized = normalize_id(value)
    except InvalidIdsError as e:
        raise CorporateActionsError(f"{where}: {key}: {e}") from e
    if source_of(normalized) != "yahoo":
        raise CorporateActionsError(f"{where}: {key} must be a Yahoo symbol")
    return normalized


def _number(where: str, key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CorporateActionsError(f"{where}: {key} must be a number")
    if not math.isfinite(value):
        raise CorporateActionsError(f"{where}: {key} must be finite")
    return float(value)
