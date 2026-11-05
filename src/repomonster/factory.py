from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .database import project_root
from .insights import (
    AnthropicInsightProvider,
    InsightProvider,
    OpenAICompatibleInsightProvider,
)
from .repository import EmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


def _insight_provider_from_environment() -> InsightProvider | None:
    provider = os.getenv("INSIGHT_PROVIDER")
    if provider == "anthropic":
        return AnthropicInsightProvider.from_environment()
    if provider not in {None, "", "openai_compatible"}:
        raise ValueError("INSIGHT_PROVIDER must be anthropic or openai_compatible")
    if provider == "openai_compatible" and not os.getenv("INSIGHT_BASE_URL"):
        raise ValueError("INSIGHT_PROVIDER=openai_compatible requires INSIGHT_BASE_URL")
    if os.getenv("INSIGHT_BASE_URL"):
        return OpenAICompatibleInsightProvider.from_environment()
    return None


def build_review_service(
    pool: ConnectionPool, embedding_provider: EmbeddingProvider
) -> ReviewService:
    return ReviewService(
        PostgresStandardsRepository(
            pool=pool,
            embedding_provider=embedding_provider,
            profiles_root=project_root() / "profiles",
            tenant_key=os.getenv("TENANT_KEY"),
        ),
        insight_provider=_insight_provider_from_environment(),
    )
