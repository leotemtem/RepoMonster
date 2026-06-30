from __future__ import annotations

import os
from typing import Any

from .models import ReviewRequest
from .insights import OpenAICompatibleInsightProvider
from .database import project_root
from .providers import normalize_github_event, normalize_gitlab_event
from .repository import OpenAICompatibleEmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService

try:
    from fastapi import FastAPI, Header, Request
except ImportError:  # pragma: no cover
    FastAPI = None
    Header = None
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
    async def review(payload: dict[str, Any]) -> dict[str, Any]:
        request = ReviewRequest.from_dict(payload)
        return service.review(request).to_dict()

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
    ) -> dict[str, Any]:
        payload = await request.json()
        normalized = normalize_github_event(x_github_event, payload)
        return {
            "received": True,
            "normalized_event": normalized.to_dict(),
            "note": "Fetch changed files and linked task context before submitting to /review.",
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
