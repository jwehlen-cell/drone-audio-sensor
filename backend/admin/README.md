# Admin UI

A small FastAPI service that serves two HTML pages:

- **Status** (`/`) — live device connection state pulled from Redis, joined with the registered Firestore device doc; plus recent drone detection events from Firestore.
- **Registered phones** (`/registered`) — every device document, with buttons to drive lifecycle state transitions (active/lost/revoked/wipe_requested).

Designed to scale to zero: `min_instances = 0` on Cloud Run. The hot path is a sequence of Firestore + Redis reads on each request; no background work runs idle.

## Configuration

All env vars are `ADMIN_`-prefixed:

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_GCP_PROJECT_ID` | empty | Required when not on Cloud Run |
| `ADMIN_FIRESTORE_DATABASE` | `(default)` | |
| `ADMIN_DEVICES_COLLECTION` | `devices` | |
| `ADMIN_DETECTIONS_COLLECTION` | `detections` | |
| `ADMIN_REDIS_HOST` | `127.0.0.1` | Memorystore host |
| `ADMIN_STALE_WARNING_SECONDS` | `30` | Yellow-dot threshold on status page |
| `ADMIN_STALE_OFFLINE_SECONDS` | `300` | Red-dot threshold |
| `ADMIN_RECENT_DETECTIONS_WINDOW_SECONDS` | `3600` | Detection list time window |
| `ADMIN_PORT` | `8080` | |
| `ADMIN_ALLOW_UNAUTHENTICATED` | unset | Set `true` ONLY for local dev |

## Authentication

The admin service is deployed with `allowUnauthenticatedInvocations = false`. Cloud Run will reject any request without an IAM-validated identity token (`roles/run.invoker`).

When fronted by IAP or Cloud Run + IAM, the request reaches the app with the `X-Goog-Authenticated-User-Email` header — that's what the FastAPI dependency uses to identify the caller.

For local development without auth:

```
ADMIN_ALLOW_UNAUTHENTICATED=true python -m admin.main
```

**Do not set `ADMIN_ALLOW_UNAUTHENTICATED=true` in any non-local environment.**

TODO: replace the placeholder dependency with an IAP session integration so the auth model is end-to-end verified, not just trusted from the Cloud Run header.

## Local development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=src
export ADMIN_GCP_PROJECT_ID=your-project
export ADMIN_REDIS_HOST=127.0.0.1
export ADMIN_ALLOW_UNAUTHENTICATED=true
python -m admin.main
# Browse to http://localhost:8080
```

## Container build

```
docker build -f backend/admin/Dockerfile -t drone-admin:dev .
```

## How state transitions get enforced

The admin UI is the user-facing path for state changes, but the rules
themselves live in `state_machine.py` (a copy is also vendored into the
gateway, since the gateway enforces the rules on the audio path).

| Current state    | Admin can move to             | Notes |
|------------------|------------------------------|-------|
| `active`         | `lost`, `revoked`, `wipe_requested` | wipe requires extra confirm |
| `lost`           | `active`, `revoked`, `wipe_requested` | wipe requires extra confirm |
| `revoked`        | `active`                      | extra confirm |
| `wipe_requested` | (none)                        | gateway flips to `wipe_sent` on next connect |
| `wipe_sent`      | (none — terminal)             | |

The set-state endpoint uses a Firestore transaction so the
`wipe_requested → wipe_sent` flip from the gateway can't race with an
admin click.
