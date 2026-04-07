from fastapi.testclient import TestClient

import app as webhook_app

client = TestClient(webhook_app.app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_handshake_echoes_verification_code() -> None:
    response = client.post("/statsig/webhook", json={"verification_code": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "abc123"}


def test_handshake_echoes_nested_verification_code() -> None:
    response = client.post("/statsig/webhook", json={"data": {"verification_code": "nested-456"}})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "nested-456"}


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


def test_handshake_fetches_experiment_and_prints_slack_message(capsys) -> None:
    async def fake_fetch(_payload):
        return {
            "name": "new_homepage",
            "hypothesis": "submission.`",
            "primaryMetrics": [{"name": "signup_conversion"}],
            "team": "Growth",
            "groups": [
                {"name": "Control", "description": "Current homepage"},
                {"name": "Test", "description": "New homepage variant"},
            ],
        }

    webhook_app._fetch_statsig_experiment = fake_fetch

    response = client.post("/statsig/webhook", json={"verification_code": "v-123"})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "v-123"}

    console_output = capsys.readouterr().out
    assert "--- Slack Message Preview ---" in console_output
    assert "🚀 Experiment Started 🚀" in console_output
    assert "*Hypothesis:* submission." in console_output
    assert "*Baseline*" in console_output
    assert "*Variation*" in console_output
