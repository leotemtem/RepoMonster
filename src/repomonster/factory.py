from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .database import project_root
from .insights import OpenAICompatibleInsightProvider
from .repository import EmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


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
        insight_provider=(
            OpenAICompatibleInsightProvider.from_environment()
            if os.getenv("INSIGHT_BASE_URL")
            else None
        ),
    )
