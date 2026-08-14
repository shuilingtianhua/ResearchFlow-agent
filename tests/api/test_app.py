from fastapi.testclient import TestClient

from researchflow.api import create_app
from researchflow.settings import Settings


def test_health_endpoint() -> None:
    client = TestClient(create_app(Settings(environment="test")))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ResearchFlow Agent",
        "environment": "test",
    }
