import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.store import HistoryShrinkError, Store
from tests.fakes import extend, make_history

NOW = datetime(2027, 1, 4, tzinfo=UTC)


def test_init_enables_wal_and_check(tmp_path: Path) -> None:
    store = Store(tmp_path / "sub" / "x.db")
    assert not store.check()
    store.init()
    assert store.check()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_check_false_without_schema(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    sqlite3.connect(path).close()
    assert not Store(path).check()


def test_check_false_for_garbage_file(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    path.write_bytes(b"not a database" * 100)
    assert not Store(path).check()


def test_history_round_trip(store: Store) -> None:
    history = make_history([1.0, 2.0, 3.0], dividends={1: 0.5}, splits={2: 2.0})
    store.insert_history("AAPL", "yahoo", history, [1.0, 2.5, 3.75], "active", 320193, NOW)
    stored = store.get_bars("AAPL")
    assert [s.bar for s in stored] == list(history.bars)
    assert [s.value for s in stored] == [1.0, 2.5, 3.75]
    instrument = store.get_instrument("AAPL")
    assert instrument is not None
    assert (instrument.cik, instrument.status, instrument.stale, instrument.last_success_at) == (
        320193,
        "active",
        False,
        NOW,
    )
    assert store.get_bars("AAPL", since=history.bars[1].date)[0].bar == history.bars[1]
    assert store.last_date("AAPL") == history.bars[-1].date
    assert store.last_date("AAPL", through=history.bars[1].date) == history.bars[1].date
    assert store.last_date("MISSING") is None
    assert store.get_instrument("MISSING") is None


def test_insert_twice_fails(store: Store) -> None:
    history = make_history([1.0])
    store.insert_history("X", "yahoo", history, [1.0], "active", None, NOW)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_history("X", "yahoo", history, [1.0], "active", None, NOW)


def test_append_must_follow_last_bar(store: Store) -> None:
    history = make_history([1.0, 2.0])
    store.insert_history("X", "yahoo", history, [1.0, 2.0], "active", None, NOW)
    with pytest.raises(ValueError, match="must start after"):
        store.append_bars("X", history.bars[1:], [2.0])
    longer = extend(history, [3.0])
    store.append_bars("X", longer.bars[2:], [3.0])
    assert store.last_date("X") == longer.bars[-1].date
    with pytest.raises(KeyError):
        store.append_bars("MISSING", longer.bars[2:], [3.0])


def test_replace_refuses_to_lose_cached_dates(store: Store) -> None:
    history = make_history([1.0, 2.0, 3.0])
    store.insert_history("X", "yahoo", history, [1.0, 2.0, 3.0], "active", None, NOW)
    for shorter in (history.bars[1:], history.bars[:-1]):
        with pytest.raises(HistoryShrinkError):
            store.replace_history(
                "X",
                make_history([b.close for b in shorter], dates=[b.date for b in shorter]),
                [1.0] * 2,
                NOW,
            )
    assert len(store.get_bars("X")) == 3
    longer = extend(history, [4.0])
    rescaled = make_history([0.5, 1.0, 1.5, 2.0], dates=[b.date for b in longer.bars])
    store.replace_history("X", rescaled, [0.5, 1.0, 1.5, 2.0], NOW)
    assert [s.bar.close for s in store.get_bars("X")] == [0.5, 1.0, 1.5, 2.0]
    with pytest.raises(KeyError):
        store.replace_history("MISSING", rescaled, [1.0] * 4, NOW)


def test_failed_transaction_rolls_back(store: Store) -> None:
    history = make_history([1.0, 2.0])
    with pytest.raises(ValueError, match="differ in length"):
        store.insert_history("X", "yahoo", history, [1.0], "active", None, NOW)
    assert store.get_instrument("X") is None


def test_refresh_state_keeps_last_success_when_stale(store: Store) -> None:
    store.insert_history("X", "yahoo", make_history([1.0]), [1.0], "active", None, NOW)
    later = datetime(2027, 1, 5, tzinfo=UTC)
    store.set_refresh_state(
        "X", status="ended", stale=True, missing_upstream=False, error="boom", now=later
    )
    x = store.get_instrument("X")
    assert x is not None
    assert (x.status, x.stale, x.refresh_error, x.last_refresh_at, x.last_success_at) == (
        "ended",
        True,
        "boom",
        later,
        NOW,
    )
    store.set_refresh_state(
        "X", status="active", stale=False, missing_upstream=True, error=None, now=later
    )
    x = store.get_instrument("X")
    assert x is not None
    assert (x.stale, x.missing_upstream, x.last_success_at) == (False, True, later)


def test_documents_round_trip_and_reject_nan(store: Store) -> None:
    assert store.get_document("meta", "X") is None
    store.put_document("meta", "X", {"a": 1}, NOW)
    store.put_document("meta", "X", {"a": 2}, NOW)
    doc = store.get_document("meta", "X")
    assert doc is not None
    assert doc.body == {"a": 2}
    assert doc.fetched_at == NOW
    with pytest.raises(ValueError, match="Out of range float"):
        store.put_document("meta", "Y", {"a": float("nan")}, NOW)
