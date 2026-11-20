"""Symbol id parsing and normalisation (request boundary)."""

import re

from app.providers.base import Source

MAX_IDS = 50
_YAHOO = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^&]{0,31}$")
_PM = re.compile(r"^pm:[0-9]{1,100}$")
_FRED = re.compile(r"^fred:[A-Z0-9_]{1,40}$")


class InvalidIdsError(ValueError):
    pass


def normalize_id(raw: str) -> str:
    """Canonical id: Yahoo symbols upper-cased; `pm:` and `fred:` prefixed ids validated as-is."""
    if raw.startswith("pm:"):
        if not _PM.fullmatch(raw):
            raise InvalidIdsError(f"invalid Polymarket id {raw!r}")
        return raw
    if raw.startswith("fred:"):
        if not _FRED.fullmatch(raw):
            raise InvalidIdsError(f"invalid FRED id {raw!r}")
        return raw
    upper = raw.upper()
    if not _YAHOO.fullmatch(upper):
        raise InvalidIdsError(f"invalid symbol id {raw!r}")
    return upper


def parse_id_list(raw: str) -> list[str]:
    """Comma list of 1..MAX_IDS ids, unique after normalisation, in request order."""
    parts = raw.split(",")
    if len(parts) > MAX_IDS:
        raise InvalidIdsError(f"at most {MAX_IDS} ids per request, got {len(parts)}")
    ids: list[str] = []
    for part in parts:
        if part == "":
            raise InvalidIdsError("empty id in list")
        normalized = normalize_id(part)
        if normalized in ids:
            raise InvalidIdsError(f"duplicate id {normalized!r}")
        ids.append(normalized)
    return ids


def source_of(id: str) -> Source:
    if id.startswith("pm:"):
        return "polymarket"
    if id.startswith("fred:"):
        return "fred"
    return "yahoo"


def is_us_listing(id: str) -> bool:
    """Yahoo convention: listings outside the US carry an exchange suffix after a dot."""
    return source_of(id) == "yahoo" and "." not in id and "=" not in id and "^" not in id
