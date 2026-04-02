# Statsig Webhook Receiver (Local + GCP Cloud Run)

This project exposes a public webhook endpoint that:

1. Handles Statsig handshake verification by echoing a `verification_code`.
2. Parses and acknowledges non-handshake event payloads.
3. Runs locally.
4. Deploys as a container to Google Cloud Run.

## Endpoint behavior

- `POST /statsig/webhook`
  - If request JSON includes `verification_code`, response is:
    ```json
    { "verification_code": "<same-code>" }
    ```
  - Otherwise, parses event fields like `type` and `data` and responds with a JSON acknowledgment.

- `GET /healthz`
  - Simple health check.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8080
```

Test locally:

```bash
curl -X POST http://localhost:8080/statsig/webhook \
  -H 'Content-Type: application/json' \
  -d '{"verification_code":"test-123"}'
```

Expected output:

```json
{"verification_code":"test-123"}
```

## Make local endpoint public (for Statsig callback)

Use a tunnel (example with ngrok):

```bash
ngrok http 8080
```

Then set your Statsig webhook URL to:

```text
https://<ngrok-id>.ngrok-free.app/statsig/webhook
```

## Deploy to GCP Cloud Run

Set project variables:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export SERVICE="statsig-webhook"
export IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:v1"
```

Build container image:

```bash
gcloud builds submit --config cloudbuild.yaml --substitutions _IMAGE=${IMAGE}
```

Deploy to Cloud Run:

```bash
gcloud run deploy ${SERVICE} \
  --image ${IMAGE} \
  --platform managed \
  --region ${REGION} \
  --allow-unauthenticated \
  --port 8080
```

Get URL:

```bash
gcloud run services describe ${SERVICE} --region ${REGION} --format='value(status.url)'
```

Set Statsig webhook endpoint to:

```text
https://<cloud-run-url>/statsig/webhook
```

## Optional: signature verification

If you use a shared secret, set:

- `REQUIRE_SIGNATURE=true`
- `STATSIG_WEBHOOK_SECRET=<your-secret>`

And send `X-Statsig-Signature` as HMAC SHA256 of raw body in format `sha256=<hex_digest>`.

## Run tests

```bash
pip install pytest httpx
pytest -q
```
