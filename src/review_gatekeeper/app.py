from __future__ import annotations

import hmac
import json
import os
from typing import Any

from .models import ReviewRequest
from .insights import OpenAICompatibleInsightProvider
from .database import project_root
from .github import SUPPORTED_PULL_REQUEST_ACTIONS, verify_webhook_signature
from .jobs import WebhookQueue
from .providers import normalize_gitlab_event
from .repository import OpenAICompatibleEmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService

try:
    from fastapi import FastAPI, Header, HTTPException, Request
except ImportError:  # pragma: no cover
    FastAPI = None
    Header = None
    HTTPException = None
    Request = Any


def create_app():
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("Install optional web dependencies with: pip install -e '.[web]'")

    root = project_root()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required; runtime standards retrieval uses PostgreSQL")
    insight_provider = (
        OpenAICompatibleInsightProvider.from_environment()
        if os.getenv("INSIGHT_BASE_URL")
        else None
    )
    service = ReviewService(
        PostgresStandardsRepository(
            database_url=database_url,
            embedding_provider=OpenAICompatibleEmbeddingProvider.from_environment(),
            profiles_root=root / "profiles",
            tenant_key=os.getenv("TENANT_KEY"),
        ),
        insight_provider=insight_provider,
    )
    webhook_queue = WebhookQueue(database_url)
    app = FastAPI(title="Review Gatekeeper", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        with service.repository._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM standard_pack_versions WHERE status = 'ready'"
            )
            ready_packs = int(cursor.fetchone()[0])
        return {"status": "ready", "standard_packs": ready_packs}

    @app.post("/review")
    async def review(
        payload: dict[str, Any],
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        api_key = os.getenv("REPOMONSTER_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=503, detail="Direct review endpoint is disabled")
        expected = f"Bearer {api_key}"
        if not hmac.compare_digest(expected, authorization):
            raise HTTPException(status_code=401, detail="Invalid API credentials")
        request = ReviewRequest.from_dict(payload)
        return service.review(request).to_dict()

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_github_delivery: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> dict[str, Any]:
        body = await request.body()
        if len(body) > int(os.getenv("MAX_WEBHOOK_BYTES", "2000000")):
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(status_code=503, detail="GitHub webhook integration is not configured")
        if not verify_webhook_signature(body, secret, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
        if not x_github_event or not x_github_delivery:
            raise HTTPException(status_code=400, detail="Missing GitHub webhook headers")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Webhook payload must be an object")
        if x_github_event == "ping":
            return {"received": True, "queued": False, "event": "ping"}
        if x_github_event != "pull_request":
            return {"received": True, "queued": False, "event": x_github_event}
        if payload.get("action") not in SUPPORTED_PULL_REQUEST_ACTIONS:
            return {
                "received": True,
                "queued": False,
                "event": x_github_event,
                "action": payload.get("action"),
            }
        queued = webhook_queue.enqueue(
            provider="github",
            delivery_id=x_github_delivery,
            event_name=x_github_event,
            payload=payload,
        )
        return {
            "received": True,
            "queued": queued,
            "duplicate": not queued,
            "delivery_id": x_github_delivery,
        }

    @app.post("/webhooks/gitlab")
    async def gitlab_webhook(
        request: Request,
        x_gitlab_event: str = Header(default=""),
    ) -> dict[str, Any]:
        payload = await request.json()
        normalized = normalize_gitlab_event(x_gitlab_event, payload)
        return {
            "received": True,
            "normalized_event": normalized.to_dict(),
            "note": "Fetch changed files and linked task context before submitting to /review.",
        }

    return app
