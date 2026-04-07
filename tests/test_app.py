import os

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
    async def fake_send(_payload):
        return False
    webhook_app._send_slack_message = fake_send

    response = client.post("/statsig/webhook", json={"verification_code": "v-123"})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "v-123"}

    console_output = capsys.readouterr().out
    assert "--- Slack Message Preview ---" in console_output
    assert ":rocket: Experiment Started :rocket:" in console_output
    assert '"type": "header"' in console_output
    assert '"text": "Experiment started: new_homepage"' in console_output
    assert '"action_id": "view_experiment"' in console_output
    assert "Hypothesis: submission." in console_output
    assert "Baseline" in console_output
    assert "Variation" in console_output


def test_format_slack_message_returns_block_kit_structure() -> None:
    experiment = {
        "name": "expmt_usp_submit",
        "hypothesis": "submission.`",
        "primaryMetrics": [{"name": "Checkout completion"}],
        "team": "UCL",
        "groups": [
            {"name": "Control", "description": "Control description"},
            {"name": "Test", "description": "Test description"},
        ],
    }

    message = webhook_app._format_slack_message(experiment, "jQzdlAawJ6o27HMDlUbxv")
    blocks = message["blocks"]

    assert message["text"] == "Experiment started: expmt_usp_submit"
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == ":rocket: Experiment Started :rocket:"
    assert blocks[1]["text"]["text"] == "expmt_usp_submit"
    assert "Hypothesis: submission." in blocks[2]["text"]["text"]
    assert "Primary metric: Checkout completion" in blocks[2]["text"]["text"]
    assert "Team: UCL" in blocks[2]["text"]["text"]
    assert blocks[4]["text"]["text"] == "Baseline\nControl description"
    assert blocks[5]["text"]["text"] == "Variation\nTest description"
    assert (
        blocks[7]["elements"][0]["url"]
        == "https://console.statsig.com/jQzdlAawJ6o27HMDlUbxv/experiments/expmt_usp_submit/summary"
    )


def test_slack_events_alias_works_for_handshake() -> None:
    response = client.post("/slack/events", json={"verification_code": "alias-1"})
    assert response.status_code == 200
    assert response.json() == {"verification_code": "alias-1"}


def test_non_handshake_includes_optional_debug_hint() -> None:
    response = client.post("/slack/events", json={"type": "event", "data": {"a": 1}})
    assert response.status_code == 200
    assert response.json()["debug"] == "verification_code optional path used"


def test_non_handshake_with_required_verification_code() -> None:
    os.environ["REQUIRE_VERIFICATION_CODE"] = "true"
    try:
        response = client.post("/slack/events", json={"type": "event", "data": {"a": 1}})
        assert response.status_code == 200
        assert response.json()["debug"] == "verification_code required but missing"
    finally:
        os.environ.pop("REQUIRE_VERIFICATION_CODE", None)



def test_extract_experiment_id_from_nested_payload() -> None:
    payload = {"data": {"experiment_id": "webhook_test"}}
    assert webhook_app._extract_experiment_id(payload) == "webhook_test"


def test_extract_experiment_id_from_top_level_payload() -> None:
    payload = {"id": "exp_123"}
    assert webhook_app._extract_experiment_id(payload) == "exp_123"



def test_extract_experiment_id_from_statsig_metadata_payload() -> None:
    payload = {
        "data": [
            {
                "user": {"name": "Nicolaus Benadet", "email": "nicolaus.benadet@samblagroup.com"},
                "timestamp": 1775551877023,
                "eventName": "statsig::config_change",
                "metadata": {
                    "projectName": "Sambla Group AB",
                    "projectID": "jQzdlAawJ6o27HMDlUbxv",
                    "type": "Experiment",
                    "name": "webhook_test",
                    "description": "Started Experiment",
                },
            }
        ]
    }
    assert webhook_app._extract_experiment_id(payload) == "webhook_test"



def test_normalize_experiment_payload_from_data_wrapper() -> None:
    raw = {"data": {"name": "webhook_test", "hypothesis": "h1", "team": "Growth"}}
    normalized = webhook_app._normalize_experiment_payload(raw)
    assert normalized["name"] == "webhook_test"


def test_normalize_experiment_payload_from_data_list_wrapper() -> None:
    raw = {"data": [{"name": "webhook_test", "primary_metrics": [{"name": "signup"}]}]}
    normalized = webhook_app._normalize_experiment_payload(raw)
    assert normalized["name"] == "webhook_test"
    assert normalized["primaryMetrics"][0]["name"] == "signup"
