import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Statsig Webhook Receiver", version="1.0.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("statsig-webhook")


class HandshakePayload(BaseModel):
    verification_code: str


def _load_secret() -> str:
    secret = os.getenv("STATSIG_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("Missing STATSIG_WEBHOOK_SECRET environment variable")
    return secret


def _compute_sha256_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/statsig/webhook")
async def statsig_webhook(
    request: Request,
    x_statsig_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw_body = await request.body()

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

    # Handshake path: echo the verification_code if present.
    verification_code = payload.get("verification_code")
    if isinstance(verification_code, str) and verification_code:
        return JSONResponse(status_code=200, content={"verification_code": verification_code})

    # Event path: parse and log the event data from Statsig.
    event_type = payload.get("type", "unknown")
    event_data = payload.get("data", {})

    logger.info("Received Statsig event", extra={"event_type": event_type, "event_data": event_data})

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "received_type": event_type,
            "received_data": event_data,
        },
    )
