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

STATSIG_EXPERIMENTS_BASE_URL = "https://statsigapi.net/console/v1/experiments"
DEFAULT_SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/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"


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


def _extract_experiment_id(payload: dict[str, Any]) -> str | None:
    """Extract experiment identifier from varied Statsig webhook payload shapes."""

    preferred_keys = {"experimentID", "experimentId", "experiment_id", "id"}

    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            # 1) Prefer explicit id-like keys first.
            for key in preferred_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate

            # 2) Statsig metadata payload fallback: metadata.name.
            metadata = value.get("metadata")
            if isinstance(metadata, dict):
                meta_name = metadata.get("name")
                if isinstance(meta_name, str) and meta_name:
                    return meta_name

            # 3) Generic name fallback when this object clearly references experiments.
            value_type = value.get("type") or value.get("eventName")
            if isinstance(value_type, str) and "experiment" in value_type.lower():
                name_candidate = value.get("name")
                if isinstance(name_candidate, str) and name_candidate:
                    return name_candidate

            for nested in value.values():
                found = walk(nested)
                if found:
                    return found

        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found

        return None

    return walk(payload)


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




def _normalize_experiment_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize Statsig API payload variants to expected experiment shape."""
    # Common wrappers: {"data": {...}} or {"data": [{...}]}
    candidate: Any = raw
    data = raw.get("data")
    if isinstance(data, dict):
        candidate = data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        candidate = data[0]

    if not isinstance(candidate, dict):
        return raw

    normalized = dict(candidate)

    # Normalize alternate key names.
    if "primaryMetrics" not in normalized and isinstance(normalized.get("primary_metrics"), list):
        normalized["primaryMetrics"] = normalized["primary_metrics"]
    if "groups" not in normalized and isinstance(normalized.get("variants"), list):
        normalized["groups"] = normalized["variants"]

    return normalized

def _build_slack_payload(experiment: dict[str, Any]) -> dict[str, Any]:
    # Send the experiment as JSON text payload to Slack.
    return {"text": json.dumps(experiment, indent=2, default=str)}


async def _send_slack_message(slack_payload: dict[str, Any]) -> bool:
    """Send JSON payload to Slack Incoming Webhook if configured."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", DEFAULT_SLACK_WEBHOOK_URL)
    if not webhook_url:
        logger.info("SLACK_WEBHOOK_URL is not set; skipping Slack send and printing preview")
        return False

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(webhook_url, json=slack_payload)
        response.raise_for_status()

    logger.info("Sent Slack message successfully")
    return True


async def _fetch_statsig_experiment(handshake_payload: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("STATSIG_CONSOLE_API_KEY","console-2tvTdXLIF121Z8BlQlFwBcACslpQdeqAJ3jvbg7Y81B")
    if not api_key:
        logger.warning("STATSIG_CONSOLE_API_KEY is not set; skipping console API fetch")
        return None

    experiment_id = _extract_experiment_id(handshake_payload)
    logger.info("Extracted experiment id from payload", extra={"experiment_id": experiment_id})
    if not experiment_id:
        logger.warning("No experiment id found in payload; skipping console API fetch")
        return None

    url = f"{STATSIG_EXPERIMENTS_BASE_URL}/{quote(experiment_id, safe='')}"
    headers = {
        "statsig-api-key": api_key,
        "statsig-api-version": os.getenv("STATSIG_API_VERSION", "20240601"),
        "content-type": "application/json",
    }

    logger.info("Calling Statsig experiment API", extra={"url": url, "experiment_id": experiment_id})

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        api_payload: Any = response.json()

    if isinstance(api_payload, dict):
        print("\n--- Raw Statsig Experiment API Payload ---")
        print(json.dumps(api_payload, indent=2, default=str))
        print("--- End Raw Statsig Experiment API Payload ---\n")
        return _normalize_experiment_payload(api_payload)
    return None


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
                # Keep payload JSON-oriented (no Block Kit).
                if "hypothesis" in experiment:
                    experiment["hypothesis"] = _sanitize_hypothesis(experiment.get("hypothesis"))
                slack_message = _build_slack_payload(experiment)
                message_sent = await _send_slack_message(slack_message)
                if not message_sent:
                    print("\n--- Slack Message Preview ---")
                    print(json.dumps(slack_message, indent=2, default=str))
                    print("--- End Slack Message Preview ---\n")
            else:
                logger.warning("No experiment data available from Statsig console API")
        except httpx.HTTPError as exc:
            logger.exception("Failed to fetch/send Statsig->Slack flow: %s", exc)

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
