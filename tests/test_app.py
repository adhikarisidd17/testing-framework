from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_handshake_echoes_verification_code() -> None:
    response = client.post("/statsig/webhook", json={"verification_code": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "abc123"}


def test_event_payload_is_parsed() -> None:
    payload = {
        "type": "experiment_assignment",
        "data": {"user_id": "u-1", "experiment": "new_homepage", "variant": "treatment"},
    }
    response = client.post("/statsig/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["received_type"] == "experiment_assignment"
    assert response.json()["received_data"]["experiment"] == "new_homepage"
