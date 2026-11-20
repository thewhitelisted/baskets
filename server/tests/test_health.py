import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_ok_with_db_not_configured() -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok", "version": "0.1.0", "db": "not_configured"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_and_schema_are_not_served(path: str) -> None:
    assert client.get(path).status_code == 404


def test_trailing_slash_is_not_redirected() -> None:
    response = client.get("/v1/health/", follow_redirects=False)

    assert response.status_code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_health_rejects_non_get_methods(method: str) -> None:
    assert client.request(method, "/v1/health").status_code == 405
