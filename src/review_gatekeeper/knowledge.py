from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .config import RepositoryConfig
from .packs import chunk_markdown
from .repository import EmbeddingProvider, _vector_literal


@dataclass(frozen=True, slots=True)
class RepositorySource:
    path: str
    title: str
    content: str
    source_sha: str
    source_type: str
    language: str | None = None
    framework: str | None = None


class RepositoryKnowledgeSynchronizer:
    """Persists trusted base-branch configuration and repository RAG sources."""

    def __init__(self, connection, embedding_provider: EmbeddingProvider) -> None:
        self.connection = connection
        self.embedding_provider = embedding_provider

    def sync(
        self,
        *,
        provider: str,
        provider_base_url: str,
        external_id: str,
        full_name: str,
        repository_key: str,
        default_branch: str,
        config_sha: str,
        config: RepositoryConfig,
        sources: list[RepositorySource],
        installation_id: int | None = None,
    ) -> int:
        repository_id = self._upsert_repository(
            provider,
            provider_base_url,
            external_id,
            full_name,
            repository_key,
            default_branch,
            config_sha,
            installation_id,
            config,
        )
        self._sync_pack_selections(repository_id, config)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE standard_documents
                SET enabled = false, updated_at = now()
                WHERE repository_key = %s AND scope = 'repo'
                """,
                (repository_key,),
            )
            cursor.execute(
                """
                UPDATE repository_standard_sources
                SET status = 'pending'
                WHERE repository_id = %s
                """,
                (repository_id,),
            )
        for source in sources:
            self._sync_source(repository_id, repository_key, source, scope="repo")
        return repository_id

    def sync_task_sources(
        self, repository_key: str, task_key: str, sources: list[RepositorySource]
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM repositories WHERE repository_key = %s AND enabled = true",
                (repository_key,),
            )
            row = cursor.fetchone()
        if not row:
            raise ValueError(f"Repository has not been onboarded: {repository_key}")
        repository_id = int(row[0])
        for source in sources:
            self._sync_source(
                repository_id,
                repository_key,
                source,
                scope="task",
                task_key=task_key,
            )

    def _upsert_repository(
        self,
        provider: str,
        provider_base_url: str,
        external_id: str,
        full_name: str,
        repository_key: str,
        default_branch: str,
        config_sha: str,
        installation_id: int | None,
        config: RepositoryConfig,
    ) -> int:
        review_settings = {
            "required_ci": config.required_ci,
            "require_task_reference": config.require_task_reference,
            "require_test_evidence": config.require_test_evidence,
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO repositories (
                    installation_id, provider, provider_base_url, external_id,
                    repository_key, full_name, default_branch, config_sha,
                    review_settings, model_profile
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (provider, provider_base_url, external_id) DO UPDATE SET
                    installation_id = EXCLUDED.installation_id,
                    repository_key = EXCLUDED.repository_key,
                    full_name = EXCLUDED.full_name,
                    default_branch = EXCLUDED.default_branch,
                    config_sha = EXCLUDED.config_sha,
                    review_settings = EXCLUDED.review_settings,
                    model_profile = EXCLUDED.model_profile,
                    enabled = true,
                    updated_at = now()
                RETURNING id
                """,
                (
                    installation_id,
                    provider,
                    provider_base_url,
                    external_id,
                    repository_key,
                    full_name,
                    default_branch,
                    config_sha,
                    json.dumps(review_settings),
                    config.model_profile,
                ),
            )
            return int(cursor.fetchone()[0])

    def _sync_pack_selections(self, repository_id: int, config: RepositoryConfig) -> None:
        selections = [pack.rsplit("@", 1) for pack in config.selected_packs]
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE repository_pack_selections SET enabled = false WHERE repository_id = %s",
                (repository_id,),
            )
            for priority, (pack_id, version) in enumerate(selections):
                cursor.execute(
                    """
                    INSERT INTO repository_pack_selections (
                        repository_id, pack_id, pack_version, priority, enabled
                    ) VALUES (%s, %s, %s, %s, true)
                    ON CONFLICT (repository_id, pack_id) DO UPDATE SET
                        pack_version = EXCLUDED.pack_version,
                        priority = EXCLUDED.priority,
                        enabled = true
                    """,
                    (repository_id, pack_id, version, priority),
                )

    def _sync_source(
        self,
        repository_id: int,
        repository_key: str,
        source: RepositorySource,
        *,
        scope: str,
        task_key: str | None = None,
    ) -> None:
        if scope not in {"repo", "task"}:
            raise ValueError(f"Unsupported repository knowledge scope: {scope}")
        checksum = hashlib.sha256(source.content.encode()).hexdigest()
        namespace = f"task:{task_key}" if scope == "task" else "repo"
        logical_key = f"{namespace}:{repository_key}:{source.path}"
        document_id = f"{logical_key}@{source.source_sha}"
        chunks = chunk_markdown(source.content)
        vectors = self.embedding_provider.embed([content for _, content in chunks])
        stack_key = f"{source.language or 'any'}/{source.framework or 'any'}"

        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE standard_documents
                SET enabled = false, updated_at = now()
                WHERE logical_key = %s AND id <> %s
                """,
                (logical_key, document_id),
            )
            cursor.execute(
                """
                INSERT INTO standard_documents (
                    id, logical_key, version, scope, repository_key, task_key, language,
                    framework, stack_key, source_path, title, body_markdown,
                    content_checksum, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    body_markdown = EXCLUDED.body_markdown,
                    content_checksum = EXCLUDED.content_checksum,
                    metadata = EXCLUDED.metadata,
                    enabled = true,
                    updated_at = now()
                """,
                (
                    document_id,
                    logical_key,
                    source.source_sha,
                    scope,
                    repository_key,
                    task_key,
                    source.language,
                    source.framework,
                    stack_key,
                    source.path,
                    source.title,
                    source.content,
                    checksum,
                    json.dumps({"source_type": source.source_type}),
                ),
            )
            cursor.execute("DELETE FROM standard_chunks WHERE document_id = %s", (document_id,))
            for ordinal, ((heading, content), vector) in enumerate(
                zip(chunks, vectors, strict=True)
            ):
                cursor.execute(
                    """
                    INSERT INTO standard_chunks (
                        document_id, ordinal, heading, content, content_checksum,
                        token_count, embedding_model, embedding_dimensions, embedding
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                    (
                        document_id,
                        ordinal,
                        heading,
                        content,
                        hashlib.sha256(content.encode()).hexdigest(),
                        len(content.split()),
                        self.embedding_provider.model_id,
                        self.embedding_provider.dimensions,
                        _vector_literal(vector, self.embedding_provider.dimensions),
                    ),
                )
            if scope == "repo":
                cursor.execute(
                    """
                    INSERT INTO repository_standard_sources (
                        repository_id, source_path, source_sha, content_checksum,
                        document_id, status, indexed_at
                    ) VALUES (%s, %s, %s, %s, %s, 'ready', now())
                    ON CONFLICT (repository_id, source_path) DO UPDATE SET
                        source_sha = EXCLUDED.source_sha,
                        content_checksum = EXCLUDED.content_checksum,
                        document_id = EXCLUDED.document_id,
                        status = 'ready',
                        indexed_at = now()
                    """,
                    (repository_id, source.path, source.source_sha, checksum, document_id),
                )
