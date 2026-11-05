from __future__ import annotations

import hmac
import json
import os
from typing import TYPE_CHECKING, Any

from .database import project_root
from .github import should_queue_event, verify_webhook_signature
from .insights import OpenAICompatibleInsightProvider
from .jobs import WebhookQueue
from .models import ReviewRequest
from .repository import OpenAICompatibleEmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService

if TYPE_CHECKING:
    from fastapi import Request
else:
    # FastAPI resolves route annotations at runtime. This must be the real class, or
    # `request: Request` becomes a required query parameter and every webhook gets a 422.
    try:
        from fastapi import Request
    except ImportError:  # pragma: no cover
        Request = Any

FastAPI: Any
Header: Any
HTTPException: Any
run_in_threadpool: Any

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.concurrency import run_in_threadpool
except ImportError:  # pragma: no cover
    FastAPI = None
    Header = None
    HTTPException = None
    run_in_threadpool = None


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject oversized request bodies while they arrive, before they fill memory."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.max_bytes:
            await self._reject(send)
            return
        seen = 0

        async def limited_receive() -> Any:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":"Request body is too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_app() -> Any:
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError(
            "Install optional web dependencies with: pip install -e '.[web]'"
        )

    root = project_root()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is required; runtime standards retrieval uses PostgreSQL"
        )
    insight_provider = (
        OpenAICompatibleInsightProvider.from_environment()
        if os.getenv("INSIGHT_BASE_URL")
        else None
    )
    repository = PostgresStandardsRepository(
        database_url=database_url,
        embedding_provider=OpenAICompatibleEmbeddingProvider.from_environment(),
        profiles_root=root / "profiles",
        tenant_key=os.getenv("TENANT_KEY"),
    )
    service = ReviewService(repository, insight_provider=insight_provider)
    webhook_queue = WebhookQueue(database_url)
    app = FastAPI(title="Review Gatekeeper", version="0.1.0")
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=int(os.getenv("MAX_WEBHOOK_BYTES", "2000000"))
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # A plain def runs in the threadpool, so a slow database cannot stall the event loop.
    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        with repository._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM standard_pack_versions WHERE status = 'ready'"
            )
            ready_packs = int(cursor.fetchone()[0])
        return {"status": "ready", "standard_packs": ready_packs}

    @app.post("/review")
    async def review(
        http_request: Request,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        api_key = os.getenv("REPOMONSTER_API_KEY", "")
        if not api_key:
            raise HTTPException(
                status_code=503, detail="Direct review endpoint is disabled"
            )
        expected = f"Bearer {api_key}"
        if not hmac.compare_digest(expected, authorization):
            raise HTTPException(status_code=401, detail="Invalid API credentials")
        try:
            payload = json.loads(await http_request.body())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Review payload must be an object"
            )
        result = await run_in_threadpool(
            service.review, ReviewRequest.from_dict(payload)
        )
        return result.to_dict()

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_github_delivery: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> dict[str, Any]:
        body = await request.body()
        secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(
                status_code=503, detail="GitHub webhook integration is not configured"
            )
        if not verify_webhook_signature(body, secret, x_hub_signature_256):
            raise HTTPException(
                status_code=401, detail="Invalid GitHub webhook signature"
            )
        if not x_github_event or not x_github_delivery:
            raise HTTPException(
                status_code=400, detail="Missing GitHub webhook headers"
            )
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Webhook payload must be an object"
            )
        if x_github_event == "ping":
            return {"received": True, "queued": False, "event": "ping"}
        if not should_queue_event(
            x_github_event, payload, os.getenv("GITHUB_APP_ID", "").strip()
        ):
            return {
                "received": True,
                "queued": False,
                "event": x_github_event,
                "action": payload.get("action"),
            }
        queued = await run_in_threadpool(
            webhook_queue.enqueue,
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

    return app
