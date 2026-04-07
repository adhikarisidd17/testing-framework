import hashlib
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Statsig Webhook Receiver", version="1.0.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("statsig-webhook")

STATSIG_EXPERIMENTS_URL = "https://statsigapi.net/console/v1/experiments/"


def _load_secret() -> str:
    secret = os.getenv("STATSIG_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("Missing STATSIG_WEBHOOK_SECRET environment variable")
    return secret


def _compute_sha256_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _extract_verification_code(payload: dict[str, Any]) -> str | None:
    direct = payload.get("verification_code")
    if isinstance(direct, str) and direct:
        return direct

    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("verification_code")
        if isinstance(nested, str) and nested:
            return nested

    return None


def _extract_target_experiment_name(handshake_payload: dict[str, Any]) -> str | None:
    top_level_name = handshake_payload.get("name")
    if isinstance(top_level_name, str) and top_level_name:
        return top_level_name

    data = handshake_payload.get("data")
    if isinstance(data, dict):
        candidate_keys = ["name", "experiment_name", "experimentName"]
        for key in candidate_keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value

    return None


def _select_experiment(api_payload: Any, target_name: str | None) -> dict[str, Any] | None:
    experiments: list[dict[str, Any]] = []

    if isinstance(api_payload, list):
        experiments = [item for item in api_payload if isinstance(item, dict)]
    elif isinstance(api_payload, dict):
        nested = api_payload.get("experiments")
        if isinstance(nested, list):
            experiments = [item for item in nested if isinstance(item, dict)]
        else:
            experiments = [api_payload]

    if not experiments:
        return None

    if target_name:
        for experiment in experiments:
            if experiment.get("name") == target_name:
                return experiment

    return experiments[0]


def _find_group_description(groups: Any, group_name: str) -> str:
    if not isinstance(groups, list):
        return "N/A"

    for group in groups:
        if not isinstance(group, dict):
            continue
        if group.get("name") == group_name:
            description = group.get("description")
            if isinstance(description, str) and description:
                return description
            return "N/A"

    return "N/A"


def _build_experiment_url(project_id: str | None, experiment_name: str) -> str:
    safe_name = quote(experiment_name, safe="")
    if project_id:
        return f"https://console.statsig.com/{project_id}/experiments/{safe_name}/summary"
    return f"https://console.statsig.com/PROJECT_ID/experiments/{safe_name}/summary"


def _sanitize_hypothesis(hypothesis: Any) -> str:
    if not isinstance(hypothesis, str) or not hypothesis:
        return "N/A"
    # Known issue workaround: strip stray backticks in hypothesis text.
    return hypothesis.replace("`", "")


def _format_slack_message(experiment: dict[str, Any], project_id: str | None) -> str:
    name = experiment.get("name") if isinstance(experiment.get("name"), str) else "N/A"
    hypothesis = _sanitize_hypothesis(experiment.get("hypothesis"))
    team = experiment.get("team") if isinstance(experiment.get("team"), str) else "N/A"

    primary_metrics = experiment.get("primaryMetrics")
    primary_metric_name = "N/A"
    if isinstance(primary_metrics, list) and primary_metrics:
        first_metric = primary_metrics[0]
        if isinstance(first_metric, dict):
            metric_name = first_metric.get("name")
            if isinstance(metric_name, str) and metric_name:
                primary_metric_name = metric_name

    groups = experiment.get("groups")
    control_description = _find_group_description(groups, "Control")
    test_description = _find_group_description(groups, "Test")

    experiment_url = _build_experiment_url(project_id, name)

    return (
        "🚀 Experiment Started 🚀\n\n"
        f"*Experiment Name*\n{name}\n\n"
        f"*Hypothesis:* {hypothesis}\n\n"
        f"*Primary metric*: {primary_metric_name}\\\n\n"
        f"*Team*: {team}\n\n"
        "*Baseline*\\\n\n"
        f"{control_description}\n\n"
        "*Variation*\\\n\n"
        f"{test_description}\n\n"
        f"View Experiment → {experiment_url}"
    )


async def _fetch_statsig_experiment(handshake_payload: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("STATSIG_CONSOLE_API_KEY")
    if not api_key:
        logger.warning("STATSIG_CONSOLE_API_KEY is not set; skipping console API fetch")
        return None

    headers = {"STATSIG-API-KEY": api_key}
    target_name = _extract_target_experiment_name(handshake_payload)

    logger.info("Calling Statsig experiments API", extra={"url": STATSIG_EXPERIMENTS_URL, "target_name": target_name})

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(STATSIG_EXPERIMENTS_URL, headers=headers)
        response.raise_for_status()
        api_payload: Any = response.json()

    return _select_experiment(api_payload, target_name)


async def _handle_statsig_webhook(request: Request, x_statsig_signature: str | None) -> JSONResponse:
    raw_body = await request.body()
    logger.info("Incoming webhook", extra={"path": request.url.path, "content_length": len(raw_body)})

    # Optional request signing verification.
    require_signature = os.getenv("REQUIRE_SIGNATURE", "false").lower() == "true"
    if require_signature:
        if not x_statsig_signature:
            raise HTTPException(status_code=401, detail="Missing signature header")
        expected = _compute_sha256_signature(_load_secret(), raw_body)
        if not hmac.compare_digest(expected, x_statsig_signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    logger.info("Received Statsig payload", extra={"payload": payload})

    verification_code = _extract_verification_code(payload)
    require_verification_code = os.getenv("REQUIRE_VERIFICATION_CODE", "false").lower() == "true"

    if verification_code:
        print(f"Received Statsig verification_code: {verification_code}")
    elif require_verification_code:
        logger.warning(
            "Request missing verification_code and REQUIRE_VERIFICATION_CODE=true; skipping Statsig API call",
            extra={"path": request.url.path, "payload_keys": list(payload.keys())},
        )
    else:
        logger.info(
            "verification_code missing but optional; continuing with Statsig API call",
            extra={"path": request.url.path, "payload_keys": list(payload.keys())},
        )

    should_fetch_experiment = bool(verification_code) or not require_verification_code
    if should_fetch_experiment:
        try:
            experiment = await _fetch_statsig_experiment(payload)
            if experiment:
                project_id = os.getenv("STATSIG_PROJECT_ID")
                slack_message = _format_slack_message(experiment, project_id)
                print("\n--- Slack Message Preview ---")
                print(slack_message)
                print("--- End Slack Message Preview ---\n")
            else:
                logger.warning("No experiment data available from Statsig console API")
        except httpx.HTTPError as exc:
            logger.exception("Failed to fetch experiment data from Statsig console API: %s", exc)

    if verification_code:
        return JSONResponse(status_code=200, content={"verification_code": verification_code})

    event_type = payload.get("type", "unknown")
    event_data = payload.get("data", {})

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "received_type": event_type,
            "received_data": event_data,
            "debug": "verification_code optional path used" if not require_verification_code else "verification_code required but missing",
        },
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/statsig/webhook")
async def statsig_webhook(
    request: Request,
    x_statsig_signature: str | None = Header(default=None),
) -> JSONResponse:
    return await _handle_statsig_webhook(request, x_statsig_signature)


@app.post("/slack/events")
async def slack_events_alias(
    request: Request,
    x_statsig_signature: str | None = Header(default=None),
) -> JSONResponse:
    """Alias endpoint for integrations that POST to /slack/events."""
    return await _handle_statsig_webhook(request, x_statsig_signature)
