from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Service
from app.store import Store


def test_health_reports_ok_with_working_db(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "ok", "version": "0.1.0", "db": "ok"}


def test_health_reports_db_error_when_database_unusable(tmp_path: Path, service: Service) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    service.store = Store(blocker / "baskets.db")  # parent is a file: init fails
    with TestClient(create_app(lambda: service)) as c:
        assert c.get("/v1/health").json()["db"] == "error"


def test_health_reports_db_error_when_database_corrupted(
    client: TestClient, service: Service
) -> None:
    service.store.path.write_bytes(b"garbage" * 1000)
    assert client.get("/v1/health").json()["db"] == "error"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_and_schema_are_not_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "unknown route"}}


def test_trailing_slash_is_not_redirected(client: TestClient) -> None:
    response = client.get("/v1/health/", follow_redirects=False)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_health_rejects_non_get_methods(client: TestClient, method: str) -> None:
    response = client.request(method, "/v1/health")
    assert response.status_code == 405
    if method != "HEAD":
        assert response.json() == {
            "error": {"code": "method_not_allowed", "message": "Method Not Allowed"}
        }
