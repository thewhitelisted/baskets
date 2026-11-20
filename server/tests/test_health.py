from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_ok_with_db_not_configured() -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok", "version": "0.1.0", "db": "not_configured"}
