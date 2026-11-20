"""SQLite (WAL) cache. Bars of a cached id are only ever appended to or replaced by a history
covering at least the same date range; nothing is evicted."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

from app.lifecycle import Status
from app.providers.base import AdjustmentMethod, History, Kind, Source
from app.timeseries import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    currency TEXT NOT NULL,
    tz TEXT NOT NULL,
    session_close TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    back_adjusted_from TEXT,
    cik INTEGER,
    status TEXT NOT NULL CHECK (status IN ('active', 'ended')),
    missing_upstream INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    refresh_error TEXT,
    edgar_successor TEXT,
    first_cached_at TEXT NOT NULL,
    last_refresh_at TEXT NOT NULL,
    last_success_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars (
    id TEXT NOT NULL REFERENCES instruments(id),
    date TEXT NOT NULL,
    close REAL NOT NULL,
    dividend REAL NOT NULL,
    split REAL NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (id, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS documents (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
) WITHOUT ROWID;
"""


class HistoryShrinkError(RuntimeError):
    """A replacement history would drop cached dates."""


@dataclass(frozen=True, slots=True)
class Instrument:
    id: str
    source: Source
    kind: Kind
    currency: str
    tz: str
    session_close: time
    adjustment: AdjustmentMethod
    back_adjusted_from: date | None
    cik: int | None
    status: Status
    missing_upstream: bool
    stale: bool
    refresh_error: str | None
    edgar_successor: str | None
    first_cached_at: datetime
    last_refresh_at: datetime
    last_success_at: datetime


@dataclass(frozen=True, slots=True)
class StoredBar:
    """A cached bar and its served value before FX (total-return index, or the close)."""

    bar: Bar
    value: float


@dataclass(frozen=True, slots=True)
class Document:
    body: Any
    fetched_at: datetime


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise RuntimeError(f"could not enable WAL on {self.path} (got {mode})")
            conn.executescript(SCHEMA)

    def check(self) -> bool:
        """True when the database opens and has the schema."""
        if not self.path.exists():
            return False
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM instruments LIMIT 1").fetchall()
                conn.execute("SELECT 1 FROM bars LIMIT 1").fetchall()
                conn.execute("SELECT 1 FROM documents LIMIT 1").fetchall()
        except sqlite3.Error:
            return False
        return True

    # instruments -------------------------------------------------------------------------

    def get_instrument(self, id: str) -> Instrument | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM instruments WHERE id = ?", (id,)).fetchone()
        return None if row is None else _instrument(dict(row))

    def list_instruments(self) -> list[Instrument]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM instruments ORDER BY id").fetchall()
        return [_instrument(dict(row)) for row in rows]

    def insert_history(
        self,
        id: str,
        source: Source,
        history: History,
        values: Sequence[float],
        status: Status,
        cik: int | None,
        now: datetime,
    ) -> None:
        """Cache a new id. Fails if the id is already cached."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO instruments (id, source, kind, currency, tz, session_close,"
                " adjustment, back_adjusted_from, cik, status, first_cached_at, last_refresh_at,"
                " last_success_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    id,
                    source,
                    history.kind,
                    history.currency,
                    history.tz,
                    history.session_close.isoformat(),
                    history.adjustment,
                    _iso(history.back_adjusted_from),
                    cik,
                    status,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            _insert_bars(conn, id, history.bars, values)

    def replace_history(
        self, id: str, history: History, values: Sequence[float], now: datetime
    ) -> None:
        """Replace all bars of a cached id; refuses if any cached date range would be lost."""
        with self._transaction() as conn:
            first, last = conn.execute(
                "SELECT MIN(date), MAX(date) FROM bars WHERE id = ?", (id,)
            ).fetchone()
            if first is None:
                raise KeyError(id)
            new_first, new_last = (
                history.bars[0].date.isoformat(),
                history.bars[-1].date.isoformat(),
            )
            if new_first > first or new_last < last:
                raise HistoryShrinkError(
                    f"{id}: refetched history {new_first}..{new_last} does not cover cached"
                    f" {first}..{last}"
                )
            conn.execute("DELETE FROM bars WHERE id = ?", (id,))
            _insert_bars(conn, id, history.bars, values)
            conn.execute(
                "UPDATE instruments SET currency = ?, kind = ?, tz = ?, session_close = ?,"
                " adjustment = ?, back_adjusted_from = ?, last_refresh_at = ? WHERE id = ?",
                (
                    history.currency,
                    history.kind,
                    history.tz,
                    history.session_close.isoformat(),
                    history.adjustment,
                    _iso(history.back_adjusted_from),
                    now.isoformat(),
                    id,
                ),
            )

    def append_bars(self, id: str, bars: Sequence[Bar], values: Sequence[float]) -> None:
        with self._transaction() as conn:
            (last,) = conn.execute("SELECT MAX(date) FROM bars WHERE id = ?", (id,)).fetchone()
            if last is None:
                raise KeyError(id)
            if bars and bars[0].date.isoformat() <= last:
                raise ValueError(f"{id}: appended bars must start after {last}")
            _insert_bars(conn, id, bars, values)

    def set_refresh_state(
        self,
        id: str,
        *,
        status: Status,
        stale: bool,
        missing_upstream: bool,
        error: str | None,
        now: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE instruments SET status = ?, stale = ?, missing_upstream = ?,"
                " refresh_error = ?, last_refresh_at = ?,"
                " last_success_at = CASE WHEN ? THEN last_success_at ELSE ? END WHERE id = ?",
                (
                    status,
                    int(stale),
                    int(missing_upstream),
                    error,
                    now.isoformat(),
                    int(stale),
                    now.isoformat(),
                    id,
                ),
            )

    def set_cik(self, id: str, cik: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE instruments SET cik = ? WHERE id = ?", (cik, id))

    def set_edgar_successor(self, id: str, successor: str | None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE instruments SET edgar_successor = ? WHERE id = ?", (successor, id))

    # bars --------------------------------------------------------------------------------

    def get_bars(self, id: str, since: date | None = None) -> list[StoredBar]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, close, dividend, split, value FROM bars WHERE id = ? AND date >= ?"
                " ORDER BY date",
                (id, (since or date.min).isoformat()),
            ).fetchall()
        return [StoredBar(Bar(date.fromisoformat(d), c, dv, s), v) for d, c, dv, s, v in rows]

    def last_bar(self, id: str) -> StoredBar | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT date, close, dividend, split, value FROM bars WHERE id = ?"
                " ORDER BY date DESC LIMIT 1",
                (id,),
            ).fetchone()
        if row is None:
            return None
        d, c, dv, s, v = row
        return StoredBar(Bar(date.fromisoformat(d), c, dv, s), v)

    def last_date(self, id: str, through: date | None = None) -> date | None:
        with self._connect() as conn:
            (last,) = conn.execute(
                "SELECT MAX(date) FROM bars WHERE id = ? AND date <= ?",
                (id, (through or date.max).isoformat()),
            ).fetchone()
        return None if last is None else date.fromisoformat(last)

    def get_dates(self, id: str) -> list[date]:
        with self._connect() as conn:
            rows = conn.execute("SELECT date FROM bars WHERE id = ? ORDER BY date", (id,))
            return [date.fromisoformat(d) for (d,) in rows]

    # documents (meta, shares, search results, SEC ticker map) ------------------------------

    def get_document(self, kind: str, key: str) -> Document | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body, fetched_at FROM documents WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        if row is None:
            return None
        return Document(json.loads(row[0]), datetime.fromisoformat(row[1]))

    def put_document(self, kind: str, key: str, body: object, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO documents (kind, key, body, fetched_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (kind, key) DO UPDATE SET body = excluded.body,"
                " fetched_at = excluded.fetched_at",
                (kind, key, json.dumps(body, allow_nan=False), now.isoformat()),
            )


def utcnow() -> datetime:
    return datetime.now(UTC)


def _insert_bars(
    conn: sqlite3.Connection, id: str, bars: Sequence[Bar], values: Sequence[float]
) -> None:
    if len(bars) != len(values):
        raise ValueError("bars and values differ in length")
    conn.executemany(
        "INSERT INTO bars (id, date, close, dividend, split, value) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (id, b.date.isoformat(), b.close, b.dividend, b.split, v)
            for b, v in zip(bars, values, strict=True)
        ],
    )


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def _instrument(row: dict[str, Any]) -> Instrument:
    return Instrument(
        id=row["id"],
        source=cast(Source, row["source"]),
        kind=cast(Kind, row["kind"]),
        currency=row["currency"],
        tz=row["tz"],
        session_close=time.fromisoformat(row["session_close"]),
        adjustment=cast(AdjustmentMethod, row["adjustment"]),
        back_adjusted_from=(
            None
            if row["back_adjusted_from"] is None
            else date.fromisoformat(row["back_adjusted_from"])
        ),
        cik=row["cik"],
        status=cast(Status, row["status"]),
        missing_upstream=bool(row["missing_upstream"]),
        stale=bool(row["stale"]),
        refresh_error=row["refresh_error"],
        edgar_successor=row["edgar_successor"],
        first_cached_at=datetime.fromisoformat(row["first_cached_at"]),
        last_refresh_at=datetime.fromisoformat(row["last_refresh_at"]),
        last_success_at=datetime.fromisoformat(row["last_success_at"]),
    )
