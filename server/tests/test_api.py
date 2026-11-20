"""Routes end to end through the app with a fake upstream (no network).

Input space: cached / uncached / unknown / failing / unsupported ids, 0..12 uncached ids per
request (fetch_limit boundary 10/11), rate limiting mid-request, USD / non-USD / minor-unit
listings, FX starting before and after the asset, stale FX, ended by gap, corporate actions
(merger, cash-out, effective before data), EDGAR renames; malformed parameters; CORS from
allowed and other origins; per-IP rate limit boundary; unhandled exceptions.
"""

import json
import math
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.corporate_actions import CorporateAction
from app.edgar import DISABLED_MESSAGE
from app.main import ALLOWED_ORIGINS, create_app
from app.providers.base import (
    NotFoundError,
    RateLimitedError,
    SearchResult,
    UnsupportedError,
    UpstreamError,
)
from app.service import FETCH_LIMIT, META_TTL, Service
from app.timeseries import total_return
from tests.conftest import Clock, MakeService, SecWorld
from tests.fakes import FakeProvider, make_history, make_meta, weekdays

AAPL = make_history([10.0, 11.0, 12.0, 12.5], dividends={2: 0.5})


def assert_series_contract(body: dict[str, Any], requested: list[str]) -> None:
    assert body["base"] == "USD"
    assert set(body["series"]) | set(body["errors"]) == set(requested)
    assert not set(body["series"]) & set(body["errors"])
    for id, entry in body["series"].items():
        assert entry["id"] == id
        assert len(entry["dates"]) == len(entry["close"]) >= 1
        assert entry["dates"] == sorted(set(entry["dates"]))
        for d in entry["dates"]:
            date.fromisoformat(d)
        assert all(isinstance(v, float) and math.isfinite(v) and v >= 0 for v in entry["close"])
        assert entry["lastDate"] == entry["dates"][-1]
        assert entry["status"] in ("active", "ended")
        assert (entry["successor"] is None) or entry["status"] == "ended"
        assert entry["adjustment"]["method"] in ("total_return", "futures_roll", "none")
        assert isinstance(entry["stale"], bool)
    for error in body["errors"].values():
        assert set(error) == {"code", "message"}
        assert error["code"] in ("not_found", "upstream_error", "fetch_limit", "unsupported")


def get_series(client: TestClient, ids: list[str]) -> dict[str, Any]:
    response = client.get("/v1/series", params={"ids": ",".join(ids)})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert_series_contract(body, ids)
    return body


# series ------------------------------------------------------------------------------------


def test_series_fetches_once_then_serves_cache(client: TestClient, provider: FakeProvider) -> None:
    provider.world["AAPL"] = AAPL
    body = get_series(client, ["AAPL"])
    entry = body["series"]["AAPL"]
    assert entry["dates"] == [b.date.isoformat() for b in AAPL.bars]
    assert entry["close"] == total_return(AAPL.bars)
    assert entry["close"][-1] == pytest.approx(12.5 * (12.0 + 0.5) / 12.0)
    assert {
        k: entry[k] for k in ("currency", "converted", "source", "stale", "status", "successor")
    } == {
        "currency": "USD",
        "converted": False,
        "source": "yahoo",
        "stale": False,
        "status": "active",
        "successor": None,
    }
    assert entry["adjustment"] == {"method": "total_return", "backAdjustedFrom": None}
    assert entry["asOf"] == "2027-01-04T12:00:00+00:00"
    calls = list(provider.calls)
    assert ("history", "AAPL") in calls
    assert ("history", "SPY") in calls
    response = client.get("/v1/series", params={"ids": "aapl"})
    assert response.json()["series"]["AAPL"] == entry
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert provider.calls == calls


def test_series_non_total_return_kind_serves_close(
    client: TestClient, provider: FakeProvider
) -> None:
    provider.world["GC=F"] = make_history([1800.0, 1810.0], kind="future", dividends={1: 5.0})
    entry = get_series(client, ["GC=F"])["series"]["GC=F"]
    assert entry["close"] == [1800.0, 1810.0]
    assert entry["adjustment"] == {"method": "none", "backAdjustedFrom": None}


def test_each_id_in_exactly_one_map(client: TestClient, provider: FakeProvider) -> None:
    provider.world["AAPL"] = AAPL
    provider.errors["DOWN"] = UpstreamError("Yahoo 500")
    provider.errors["CL=F"] = UnsupportedError("non-positive close")
    ids = ["AAPL", "NOPE", "DOWN", "CL=F", "pm:12345", "fred:DGS10"]
    body = get_series(client, ids)
    assert list(body["series"]) == ["AAPL"]
    assert {k: v["code"] for k, v in body["errors"].items()} == {
        "NOPE": "not_found",
        "DOWN": "upstream_error",
        "CL=F": "unsupported",
        "pm:12345": "unsupported",
        "fred:DGS10": "unsupported",
    }
    assert body["errors"]["pm:12345"]["message"] == "Polymarket ids are not supported yet"
    assert body["errors"]["fred:DGS10"]["message"] == "FRED ids are not supported yet"


@pytest.mark.parametrize(
    ("uncached", "limited"), [(FETCH_LIMIT, 0), (FETCH_LIMIT + 1, 1), (FETCH_LIMIT + 2, 2)]
)
def test_fetch_limit(
    client: TestClient, provider: FakeProvider, uncached: int, limited: int
) -> None:
    ids = [f"S{i}" for i in range(uncached)]
    for id in ids:
        provider.world[id] = make_history([1.0, 2.0])
    response = client.get("/v1/series", params={"ids": ",".join(ids)})
    body = response.json()
    assert_series_contract(body, ids)
    assert [id for id, e in body["errors"].items() if e["code"] == "fetch_limit"] == ids[
        FETCH_LIMIT:
    ]
    assert len(body["series"]) == uncached - limited
    assert response.headers["cache-control"] == ("no-store" if limited else "public, max-age=3600")
    if limited:
        retry = get_series(client, ids)
        assert retry["errors"] == {}


def test_fetch_limit_ignores_cached_ids_and_counts_failures(
    client: TestClient, provider: FakeProvider
) -> None:
    provider.world["AAPL"] = AAPL
    get_series(client, ["AAPL"])
    ids = ["AAPL"] + [f"MISSING{i}" for i in range(FETCH_LIMIT + 1)]
    body = get_series(client, ids)
    assert "AAPL" in body["series"]
    codes = [body["errors"][i]["code"] for i in ids[1:]]
    assert codes == ["not_found"] * FETCH_LIMIT + ["fetch_limit"]


def test_rate_limit_stops_further_fetches(client: TestClient, provider: FakeProvider) -> None:
    provider.world["AAPL"] = AAPL
    get_series(client, ["AAPL"])
    provider.errors["A1"] = RateLimitedError("429")
    provider.world["A2"] = make_history([1.0])
    before = len(provider.calls)
    body = get_series(client, ["A1", "A2", "AAPL"])
    assert body["errors"]["A1"]["code"] == "upstream_error"
    assert body["errors"]["A2"] == {
        "code": "upstream_error",
        "message": "upstream is rate limiting; retry later",
    }
    assert "AAPL" in body["series"]
    assert provider.calls[before:] == [("history", "A1")]


def test_series_bad_params(client: TestClient) -> None:
    cases = {
        "/v1/series?ids=AAPL&base=EUR": "base must be USD",
        "/v1/series": "ids: Field required",
        "/v1/series?ids=": "empty id",
        "/v1/series?ids=AAPL,aapl": "duplicate id",
        "/v1/series?ids=A%20B": "invalid symbol id",
        "/v1/series?ids=" + ",".join(f"S{i}" for i in range(51)): "at most 50",
    }
    for url, message in cases.items():
        response = client.get(url)
        assert response.status_code == 400, url
        body = response.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == "bad_request"
        assert message in body["error"]["message"], (url, body)
        assert "detail" not in response.text


def test_non_usd_listing_is_converted(client: TestClient, provider: FakeProvider) -> None:
    dates = weekdays(date(2026, 1, 5), 4)
    provider.world["7203.T"] = make_history(
        [1000.0, 1100.0, 1200.0, 1300.0], currency="JPY", dates=dates
    )
    # FX starts on the asset's second day and has no rate on its third: forward-filled.
    provider.world["JPYUSD=X"] = make_history([0.01, 0.02], kind="fx", dates=[dates[1], dates[3]])
    entry = get_series(client, ["7203.T"])["series"]["7203.T"]
    assert entry["dates"] == [d.isoformat() for d in dates[1:]]
    assert entry["close"] == pytest.approx([1100.0 * 0.01, 1200.0 * 0.01, 1300.0 * 0.02])
    assert (entry["currency"], entry["converted"]) == ("JPY", True)


def test_minor_unit_listing(client: TestClient, provider: FakeProvider) -> None:
    provider.world["VOD.L"] = make_history([7000.0, 7100.0], currency="GBp")
    provider.world["GBPUSD=X"] = make_history([1.25, 1.5], kind="fx")
    entry = get_series(client, ["VOD.L"])["series"]["VOD.L"]
    assert entry["close"] == pytest.approx([70.0 * 1.25, 71.0 * 1.5])
    assert (entry["currency"], entry["converted"]) == ("GBp", True)


def test_us_cents_listing_needs_no_fx(client: TestClient, provider: FakeProvider) -> None:
    provider.world["ZC=F"] = make_history([450.0, 460.0], currency="USX", kind="future")
    entry = get_series(client, ["ZC=F"])["series"]["ZC=F"]
    assert entry["close"] == pytest.approx([4.5, 4.6])
    assert (entry["currency"], entry["converted"]) == ("USX", True)
    assert not any(id.endswith("=X") for _, id in provider.calls)


def test_fx_failures(client: TestClient, provider: FakeProvider, service: Service) -> None:
    provider.world["SAP.DE"] = make_history([100.0], currency="EUR", dates=[date(2026, 1, 5)])
    body = get_series(client, ["SAP.DE"])
    assert body["errors"]["SAP.DE"]["code"] == "upstream_error"
    assert "FX rate EURUSD=X unavailable" in body["errors"]["SAP.DE"]["message"]
    provider.world["EURUSD=X"] = make_history([1.1], kind="fx", dates=[date(2026, 1, 6)])
    body = get_series(client, ["SAP.DE"])
    assert body["errors"]["SAP.DE"]["code"] == "unsupported"
    provider.world["CHF.SW"] = make_history([100.0], currency="CHF")
    provider.world["CHFUSD=X"] = make_history([1.1], kind="fx", currency="EUR")
    assert "expected USD" in get_series(client, ["CHF.SW"])["errors"]["CHF.SW"]["message"]


def test_stale_flags_propagate(
    client: TestClient, provider: FakeProvider, service: Service, clock: Clock
) -> None:
    provider.world["VOD.L"] = make_history([7000.0], currency="GBp")
    provider.world["GBPUSD=X"] = make_history([1.25], kind="fx")
    assert get_series(client, ["VOD.L"])["series"]["VOD.L"]["stale"] is False
    service.store.set_refresh_state(
        "GBPUSD=X", status="active", stale=True, missing_upstream=False, error="x", now=clock()
    )
    assert get_series(client, ["VOD.L"])["series"]["VOD.L"]["stale"] is True


def test_reference_unavailable(client: TestClient, provider: FakeProvider) -> None:
    del provider.world["SPY"]
    provider.world["AAPL"] = AAPL
    body = get_series(client, ["AAPL"])
    assert body["errors"]["AAPL"]["code"] == "upstream_error"
    assert "reference series SPY unavailable" in body["errors"]["AAPL"]["message"]


def ended_world(provider: FakeProvider) -> None:
    spy_dates = [b.date for b in provider.world["SPY"].bars]
    provider.world["OLD"] = make_history([5.0, 6.0], dates=spy_dates[-7:-5])  # 5 SPY days missed
    provider.world["LIVE"] = make_history([5.0, 6.0], dates=spy_dates[-6:-4])  # 4 missed


def test_ended_by_gap(client: TestClient, provider: FakeProvider) -> None:
    ended_world(provider)
    body = get_series(client, ["OLD", "LIVE"])
    assert body["series"]["OLD"]["status"] == "ended"
    assert body["series"]["OLD"]["successor"] is None
    assert body["series"]["LIVE"]["status"] == "active"
    response = client.get("/v1/ended")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.json() == {
        "ended": [
            {
                "id": "OLD",
                "lastDate": body["series"]["OLD"]["lastDate"],
                "successor": None,
                "reviewed": False,
            }
        ]
    }


def test_corporate_actions(make_service: MakeService, provider: FakeProvider) -> None:
    ended_world(provider)
    provider.world["BOUGHT"] = make_history([5.0, 6.0, 7.0])
    provider.world["EARLY"] = make_history([5.0], start=date(2026, 2, 2))
    third = provider.world["BOUGHT"].bars[1].date
    actions = {
        "OLD": CorporateAction("OLD", None, 0.0, 6.5, date(2026, 12, 1), "cash"),
        "BOUGHT": CorporateAction("BOUGHT", "SPY", 0.25, 1.0, third, "merger"),
        "EARLY": CorporateAction("EARLY", None, 0.0, 1.0, date(2026, 1, 1), "before data"),
    }
    with TestClient(create_app(lambda: make_service(actions=actions))) as client:
        body = get_series(client, ["OLD", "BOUGHT", "EARLY"])
        bought = body["series"]["BOUGHT"]
        assert bought["status"] == "ended"
        assert bought["lastDate"] == third.isoformat()
        assert len(bought["dates"]) == 2
        assert bought["successor"] == {
            "id": "SPY",
            "ratio": 0.25,
            "cashPerShare": 1.0,
            "effectiveDate": third.isoformat(),
            "source": "corporate_actions",
        }
        assert body["series"]["OLD"]["successor"]["id"] is None
        assert body["errors"]["EARLY"]["code"] == "unsupported"
        ended = {e["id"]: e for e in client.get("/v1/ended").json()["ended"]}
        assert set(ended) == {"OLD", "BOUGHT"}
        assert ended["BOUGHT"]["reviewed"]
        assert ended["OLD"]["reviewed"]
        assert ended["BOUGHT"]["lastDate"] == third.isoformat()


def test_edgar_rename_successor_is_served(
    client: TestClient, provider: FakeProvider, service: Service
) -> None:
    ended_world(provider)
    get_series(client, ["OLD"])
    service.store.set_edgar_successor("OLD", "NEW")
    entry = get_series(client, ["OLD"])["series"]["OLD"]
    assert entry["successor"] == {
        "id": "NEW",
        "ratio": 1.0,
        "cashPerShare": 0.0,
        "effectiveDate": entry["lastDate"],
        "source": "edgar_rename",
    }
    assert client.get("/v1/ended").json()["ended"][0]["reviewed"] is True


def test_series_json_has_no_non_finite_numbers(client: TestClient, provider: FakeProvider) -> None:
    provider.world["AAPL"] = AAPL
    text = client.get("/v1/series", params={"ids": "AAPL"}).text
    json.loads(text, parse_constant=lambda c: pytest.fail(f"non-finite constant {c}"))


# meta --------------------------------------------------------------------------------------


def test_meta(client: TestClient, provider: FakeProvider, clock: Clock) -> None:
    provider.metas["AAPL"] = make_meta("AAPL")
    provider.metas["SPY"] = make_meta("SPY", kind="etf")
    provider.metas["DOWN"] = UpstreamError("boom")
    response = client.get("/v1/meta", params={"ids": "AAPL,SPY,NOPE,DOWN,pm:1"})
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["meta"]["AAPL"] == {
        "id": "AAPL",
        "name": "AAPL Inc.",
        "kind": "equity",
        "assetClass": "equity",
        "sector": "Technology",
        "industry": None,
        "country": "United States",
        "currency": "USD",
        "marketCap": 1e9,
        "sharesOutstanding": 1000.0,
        "fundSectorWeights": None,
        "polymarket": None,
    }
    assert body["meta"]["SPY"]["fundSectorWeights"] == {"technology": 0.6, "healthcare": 0.4}
    assert {k: v["code"] for k, v in body["errors"].items()} == {
        "NOPE": "not_found",
        "DOWN": "upstream_error",
        "pm:1": "unsupported",
    }
    calls = len(provider.calls)
    client.get("/v1/meta", params={"ids": "AAPL"})
    assert len(provider.calls) == calls
    clock.value += META_TTL
    provider.metas["AAPL"] = make_meta("AAPL", shares=2000.0)
    assert (
        client.get("/v1/meta", params={"ids": "AAPL"}).json()["meta"]["AAPL"]["sharesOutstanding"]
        == 2000.0
    )
    clock.value += META_TTL
    provider.metas["AAPL"] = UpstreamError("down")
    assert (
        client.get("/v1/meta", params={"ids": "AAPL"}).json()["meta"]["AAPL"]["sharesOutstanding"]
        == 2000.0
    )


def test_meta_fetch_limit(client: TestClient, provider: FakeProvider) -> None:
    ids = [f"S{i}" for i in range(FETCH_LIMIT + 1)]
    for id in ids:
        provider.metas[id] = make_meta(id)
    body = client.get("/v1/meta", params={"ids": ",".join(ids)}).json()
    assert list(body["errors"]) == [ids[-1]]
    assert body["errors"][ids[-1]]["code"] == "fetch_limit"


# shares ------------------------------------------------------------------------------------


def test_shares_without_edgar(client: TestClient, provider: FakeProvider) -> None:
    provider.metas.update(
        {
            "AAPL": make_meta("AAPL"),
            "7203.T": make_meta("7203.T"),
            "SPY": make_meta("SPY", kind="etf"),
            "BTC-USD": make_meta("BTC-USD", kind="crypto"),
            "NOSH.L": make_meta("NOSH.L", shares=None),
        }
    )
    body = client.get("/v1/shares", params={"ids": "AAPL,7203.T,SPY,BTC-USD,NOSH.L,pm:1"}).json()
    assert body["shares"] == {
        "7203.T": {"source": "current", "knownDates": ["2027-01-04"], "shares": [1000.0]},
        "SPY": {"source": "current", "knownDates": ["2027-01-04"], "shares": [1000.0]},
    }
    assert body["errors"]["AAPL"] == {"code": "unsupported", "message": DISABLED_MESSAGE}
    assert body["errors"]["BTC-USD"]["code"] == "unsupported"
    assert body["errors"]["NOSH.L"]["code"] == "not_found"
    assert body["errors"]["pm:1"]["code"] == "unsupported"


def sec_facts(*rows: tuple[str, str, float]) -> dict[str, Any]:
    return {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [{"end": e, "filed": f, "val": v, "accn": f} for e, f, v in rows]
                    }
                }
            }
        }
    }


def test_shares_with_edgar(
    make_service: MakeService, provider: FakeProvider, sec: SecWorld
) -> None:
    provider.metas.update(
        {"AAPL": make_meta("AAPL"), "MSFT": make_meta("MSFT"), "TINY": make_meta("TINY")}
    )
    sec.tickers = {"AAPL": 320193, "MSFT": 789019}
    sec.facts = {
        320193: sec_facts(("2020-03-28", "2020-04-30", 4.3e9), ("2020-06-27", "2020-07-30", 4.2e9))
    }
    with TestClient(create_app(lambda: make_service(edgar=True))) as client:
        body = client.get("/v1/shares", params={"ids": "AAPL,MSFT,TINY"}).json()
        assert body["shares"]["AAPL"] == {
            "source": "edgar",
            "knownDates": ["2020-04-30", "2020-07-30"],
            "shares": [4.3e9, 4.2e9],
        }
        # MSFT: SEC knows the CIK but has no facts (404); TINY: not in the SEC map.
        assert body["shares"]["MSFT"]["source"] == "current"
        assert body["shares"]["TINY"]["source"] == "current"
        before = len(sec.requests)
        assert client.get("/v1/shares", params={"ids": "AAPL,MSFT"}).json()["errors"] == {}
        assert len(sec.requests) == before
        assert all(r.headers["User-Agent"] == "test-agent" for r in sec.requests)


def test_shares_edgar_outage(
    make_service: MakeService, provider: FakeProvider, sec: SecWorld
) -> None:
    provider.metas["AAPL"] = make_meta("AAPL")
    sec.status = 503
    with TestClient(create_app(lambda: make_service(edgar=True))) as client:
        assert (
            client.get("/v1/shares", params={"ids": "AAPL"}).json()["errors"]["AAPL"]["code"]
            == "upstream_error"
        )


# search ------------------------------------------------------------------------------------


def test_search(client: TestClient, provider: FakeProvider, clock: Clock) -> None:
    provider.search_results = [
        SearchResult("TM", "Toyota Motor", "equity", "NYSE", None),
        SearchResult("7203.T", "Toyota Motor", "equity", "Tokyo", "JPY"),
    ]
    response = client.get("/v1/search", params={"q": "toyota", "limit": 1})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.json() == {
        "results": [
            {
                "id": "TM",
                "name": "Toyota Motor",
                "kind": "equity",
                "exchange": "NYSE",
                "currency": None,
            }
        ]
    }
    calls = len(provider.calls)
    client.get("/v1/search", params={"q": "TOYOTA", "limit": 1})
    assert len(provider.calls) == calls
    provider.search_results = UpstreamError("down")
    assert client.get("/v1/search", params={"q": "toyota", "limit": 1}).status_code == 200
    clock.value += timedelta(days=2)
    assert client.get("/v1/search", params={"q": "toyota", "limit": 1}).status_code == 200
    response = client.get("/v1/search", params={"q": "honda"})
    assert response.status_code == 502
    assert response.json() == {"error": {"code": "upstream_error", "message": "down"}}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "x" * 65},
        {"q": "a", "limit": 0},
        {"q": "a", "limit": 26},
        {"q": "a", "limit": "x"},
    ],
)
def test_search_bad_params(client: TestClient, params: dict[str, Any]) -> None:
    response = client.get("/v1/search", params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize(
    "params", [{"q": "x" * 64}, {"q": "a", "limit": 25}, {"q": "a", "limit": 1}]
)
def test_search_param_boundaries_accepted(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/v1/search", params=params).status_code == 200


# cross-cutting -----------------------------------------------------------------------------


def test_unknown_route(client: TestClient) -> None:
    response = client.get("/v2/series")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "unknown route"}}


@pytest.mark.parametrize(
    "path",
    ["/v1/series?ids=A", "/v1/meta?ids=A", "/v1/shares?ids=A", "/v1/search?q=a", "/v1/ended"],
)
def test_data_routes_reject_post(client: TestClient, path: str) -> None:
    response = client.post(path)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_cors(client: TestClient, provider: FakeProvider) -> None:
    for origin in ALLOWED_ORIGINS:
        response = client.get("/v1/health", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
        preflight = client.options(
            "/v1/series", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-methods"] == "GET"
    other = client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    denied = client.options(
        "/v1/series",
        headers={"Origin": ALLOWED_ORIGINS[0], "Access-Control-Request-Method": "POST"},
    )
    assert denied.status_code == 400
    assert "access-control-allow-credentials" not in denied.headers


def test_rate_limit_per_ip(client: TestClient) -> None:
    for _ in range(60):
        assert client.get("/v1/search", params={"q": "a"}).status_code == 200
    response = client.get("/v1/search", params={"q": "a"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert client.get("/v1/ended").status_code == 200


def test_unhandled_error_is_contract_500(service: Service, provider: FakeProvider) -> None:
    provider.errors["BOOM"] = RuntimeError("bug")  # type: ignore[assignment]
    with TestClient(create_app(lambda: service), raise_server_exceptions=False) as client:
        response = client.get("/v1/series", params={"ids": "BOOM"})
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal", "message": "internal server error"}}


def test_not_found_error_message_kept(client: TestClient, provider: FakeProvider) -> None:
    provider.errors["GONE"] = NotFoundError("Yahoo has no data for GONE")
    assert get_series(client, ["GONE"])["errors"]["GONE"] == {
        "code": "not_found",
        "message": "Yahoo has no data for GONE",
    }
