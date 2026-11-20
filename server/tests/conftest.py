from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.corporate_actions import CorporateAction
from app.edgar import EdgarClient
from app.main import create_app
from app.service import Service
from app.store import Store
from tests.fakes import SPY_LENGTH, FakeProvider, make_history

SPY_CLOSES = [100.0 + i * 0.1 for i in range(SPY_LENGTH)]


@dataclass
class Clock:
    value: datetime = datetime(2027, 1, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@dataclass
class SecWorld:
    """Fake SEC endpoints served through httpx.MockTransport."""

    tickers: dict[str, int] = field(default_factory=dict)
    facts: dict[int, Any] = field(default_factory=dict)
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path == "/files/company_tickers.json":
            body = {
                str(i): {"cik_str": cik, "ticker": t, "title": t}
                for i, (t, cik) in enumerate(self.tickers.items())
            }
            return httpx.Response(200, json=body)
        for cik, payload in self.facts.items():
            if request.url.path == f"/api/xbrl/companyfacts/CIK{cik:010d}.json":
                return httpx.Response(200, json=payload)
        return httpx.Response(404)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "baskets.db")
    s.init()
    return s


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(world={"SPY": make_history(SPY_CLOSES, kind="etf")})


@pytest.fixture
def sec() -> SecWorld:
    return SecWorld()


MakeService = Callable[..., Service]


@pytest.fixture
def make_service(store: Store, provider: FakeProvider, clock: Clock, sec: SecWorld) -> MakeService:
    def build(edgar: bool = False, actions: dict[str, CorporateAction] | None = None) -> Service:
        client = (
            EdgarClient(
                "test-agent", transport=httpx.MockTransport(sec.handler), sleep=lambda _: None
            )
            if edgar
            else None
        )
        return Service(store, {"yahoo": provider}, client, actions or {}, clock)

    return build


@pytest.fixture
def service(make_service: MakeService) -> Service:
    return make_service()


@pytest.fixture
def client(service: Service) -> Iterator[TestClient]:
    with TestClient(create_app(lambda: service)) as c:
        yield c
