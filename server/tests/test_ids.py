import pytest

from app.ids import InvalidIdsError, is_us_listing, normalize_id, parse_id_list, source_of


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        ("7203.t", "7203.T"),
        ("btc-usd", "BTC-USD"),
        ("eurusd=x", "EURUSD=X"),
        ("gc=f", "GC=F"),
        ("^tnx", "^TNX"),
        ("m&m.ns", "M&M.NS"),
        ("pm:123456", "pm:123456"),
        ("fred:DGS10", "fred:DGS10"),
    ],
)
def test_normalize_id(raw: str, expected: str) -> None:
    assert normalize_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " AAPL",
        "AAPL ",
        "A B",
        "AAPL;",
        "a" * 33,
        ".AAPL",
        "pm:",
        "pm:abc",
        "PM:1",
        "fred:dgs10",
        "fred:",
        "ÄPPLE",
        "AAPL\n",
    ],
)
def test_normalize_id_rejects(raw: str) -> None:
    with pytest.raises(InvalidIdsError):
        normalize_id(raw)


def test_parse_id_list_keeps_order() -> None:
    assert parse_id_list("msft,aapl,pm:1") == ["MSFT", "AAPL", "pm:1"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty id"),
        ("AAPL,", "empty id"),
        ("AAPL,,MSFT", "empty id"),
        ("aapl,AAPL", "duplicate id 'AAPL'"),
        (",".join(f"S{i}" for i in range(51)), "at most 50"),
    ],
)
def test_parse_id_list_rejects(raw: str, message: str) -> None:
    with pytest.raises(InvalidIdsError, match=message):
        parse_id_list(raw)


def test_parse_id_list_accepts_fifty() -> None:
    assert len(parse_id_list(",".join(f"S{i}" for i in range(50)))) == 50


def test_source_and_us_listing() -> None:
    assert source_of("pm:1") == "polymarket"
    assert source_of("fred:X") == "fred"
    assert source_of("AAPL") == "yahoo"
    assert is_us_listing("BRK-B")
    assert not is_us_listing("7203.T")
    assert not is_us_listing("EURUSD=X")
    assert not is_us_listing("^GSPC")
    assert not is_us_listing("pm:1")
