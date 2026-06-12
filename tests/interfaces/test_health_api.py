from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.interfaces.http.routes import register_routes


class FakeHealthService:
    def dependency_health(self):
        return {
            "sqlite": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "ollama": {"status": "error"},
        }


def build_client() -> TestClient:
    app = FastAPI()
    app.state.services = {"health": FakeHealthService()}
    register_routes(app)
    return TestClient(app)


def test_root_redirects_to_management_fe():
    client = build_client()

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/static/index.html"


def test_health_returns_process_status_ok():
    client = build_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dependency_health_delegates_to_health_service():
    client = build_client()

    response = client.get("/health/dependencies")

    assert response.status_code == 200
    assert response.json() == {
        "sqlite": {"status": "ok"},
        "qdrant": {"status": "ok"},
        "ollama": {"status": "error"},
    }
