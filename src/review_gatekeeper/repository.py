from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Protocol
from urllib import request as urlrequest

from .models import ReviewProfile, ReviewRequest, StandardDocument, StandardRule
from .url_safety import require_http_url


class StandardsRepository(Protocol):
    """Storage boundary used by the review service."""

    def load_profile(
        self, profile_id: str, request: ReviewRequest | None = None
    ) -> ReviewProfile: ...

    def retrieve(
        self, request: ReviewRequest, profile: ReviewProfile, limit: int = 12
    ) -> list[StandardDocument]: ...


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbeddingProvider:
    """Adapter for embedding services that expose POST /embeddings."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int = 1536,
        api_key: str | None = None,
    ) -> None:
        self.endpoint = f"{require_http_url(base_url, setting_name='EMBEDDING_BASE_URL')}/embeddings"
        self.model = model
        self.model_id = model
        self.dimensions = dimensions
        self.api_key = api_key

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleEmbeddingProvider":
        return cls(
            base_url=os.environ["EMBEDDING_BASE_URL"],
            model=os.environ["EMBEDDING_MODEL"],
            dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
            api_key=os.getenv("EMBEDDING_API_KEY"),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urlrequest.Request(
            self.endpoint, data=payload, headers=headers, method="POST"
        )
        with urlrequest.urlopen(  # nosec B310  # noqa: S310 - Embedding endpoint URL scheme is validated.
            req, timeout=30
        ) as response:
            body = json.loads(response.read())
        vectors = [
            item["embedding"] for item in sorted(body["data"], key=lambda x: x["index"])
        ]
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"Embedding service returned {len(vector)} dimensions; expected {self.dimensions}"
                )
        return vectors


class PostgresStandardsRepository:
    """Production RAG repository: metadata filtering, then pgvector ranking."""

    def __init__(
        self,
        database_url: str,
        embedding_provider: EmbeddingProvider,
        profiles_root: Path,
        tenant_key: str | None = None,
    ) -> None:
        self.database_url = database_url
        self.embedding_provider = embedding_provider
        self.profiles_root = profiles_root
        self.tenant_key = tenant_key

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install database dependencies with: pip install -e '.[db]'"
            ) from exc
        return psycopg.connect(self.database_url)

    def load_profile(
        self, profile_id: str, request: ReviewRequest | None = None
    ) -> ReviewProfile:
        # Profiles are operational policy, not retrievable standards content.
        review_settings = {}
        if request is not None:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT review_settings, model_profile
                    FROM repositories
                    WHERE repository_key = %s AND enabled = true
                    """,
                    (request.resolved_repository_key(),),
                )
                row = cursor.fetchone()
            if row:
                review_settings = dict(row[0])
                if profile_id == "default":
                    profile_id = row[1]
        path = self.profiles_root / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Unknown profile: {profile_id}")
        payload = json.loads(path.read_text())
        payload["required_ci"] = review_settings.get("required_ci", [])
        if "require_task_reference" in review_settings:
            payload["require_task_reference_for_code_changes"] = review_settings[
                "require_task_reference"
            ]
        if "require_test_evidence" in review_settings:
            payload["require_test_evidence_when_code_changes"] = review_settings[
                "require_test_evidence"
            ]
        if "auto_block" in review_settings:
            payload["auto_block"] = review_settings["auto_block"]
        return ReviewProfile.from_dict(payload)

    def retrieve(
        self, request: ReviewRequest, profile: ReviewProfile, limit: int = 12
    ) -> list[StandardDocument]:
        query_vector = self.embedding_provider.embed([self._retrieval_query(request)])[
            0
        ]
        vector_literal = _vector_literal(
            query_vector, self.embedding_provider.dimensions
        )
        language = (request.inferred_language() or "").lower() or None
        framework = (request.framework or "").lower() or None
        task_keys = [item.value for item in request.task_references]
        repository_key = request.resolved_repository_key()

        sql = """
            SELECT
                d.id, d.scope, d.language, d.framework, d.title, d.tags,
                d.source_urls, c.content,
                1 - (c.embedding <=> %s::vector) AS similarity
            FROM standard_chunks AS c
            JOIN standard_documents AS d ON d.id = c.document_id
            WHERE d.enabled = true
              AND d.scope = %s
              AND (
                    (d.scope = 'public' AND EXISTS (
                        SELECT 1
                        FROM repositories AS repository
                        JOIN repository_pack_selections AS selection
                          ON selection.repository_id = repository.id
                        WHERE repository.repository_key = %s
                          AND selection.enabled = true
                          AND selection.pack_id = d.pack_id
                          AND selection.pack_version = d.pack_version
                    ))
                 OR (d.scope = 'company' AND d.tenant_key = %s)
                 OR (d.scope = 'repo' AND d.repository_key = %s)
                 OR (d.scope = 'task' AND d.repository_key = %s AND d.task_key = ANY(%s::text[]))
              )
              AND (
                    d.language IS NULL
                    OR (%s::text IS NOT NULL AND lower(d.language) = %s::text)
              )
              AND (
                    d.framework IS NULL
                    OR (%s::text IS NOT NULL AND lower(d.framework) = %s::text)
              )
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
        """
        with self._connect() as connection, connection.cursor() as cursor:
            rows = []
            # Query each scope separately so pgvector can use distance ordering while
            # preserving task > repo > company > public precedence.
            for scope in profile.retrieval_order:
                cursor.execute(
                    sql,
                    (
                        vector_literal,
                        scope,
                        repository_key,
                        self.tenant_key,
                        repository_key,
                        repository_key,
                        task_keys,
                        language,
                        language,
                        framework,
                        framework,
                        vector_literal,
                        limit,
                    ),
                )
                rows.extend(cursor.fetchall())
            rows = rows[:limit]
            document_ids = list(dict.fromkeys(row[0] for row in rows))
            rules = self._load_rules(cursor, document_ids)

        documents: dict[str, StandardDocument] = {}
        for row in rows:
            document_id = row[0]
            if document_id not in documents:
                documents[document_id] = StandardDocument(
                    id=document_id,
                    scope=row[1],
                    language=row[2],
                    framework=row[3],
                    title=row[4],
                    tags=list(row[5]),
                    source_links=list(row[6]),
                    rules=rules.get(document_id, []),
                    retrieved_chunks=[],
                )
            documents[document_id].retrieved_chunks.append(row[7])
        return list(documents.values())

    @staticmethod
    def _load_rules(cursor, document_ids: list[str]) -> dict[str, list[StandardRule]]:
        if not document_ids:
            return {}
        cursor.execute(
            """
            SELECT document_id, id, severity, gate, title, rationale, check_statements
            FROM standard_rules
            WHERE document_id = ANY(%s)
            ORDER BY document_id, id
            """,
            (document_ids,),
        )
        result: dict[str, list[StandardRule]] = {}
        for row in cursor.fetchall():
            result.setdefault(row[0], []).append(
                StandardRule.from_dict(
                    {
                        "id": row[1],
                        "severity": row[2],
                        "gate": row[3],
                        "title": row[4],
                        "rationale": row[5] or "",
                        "check_statements": row[6],
                    }
                )
            )
        return result

    @staticmethod
    def _retrieval_query(request: ReviewRequest) -> str:
        changed_paths = "\n".join(item.path for item in request.changed_files[:50])
        return "\n".join(
            (
                f"Change title: {request.title}",
                f"Change description: {request.description}",
                f"Language: {request.inferred_language() or 'unknown'}",
                f"Framework: {request.framework or 'unknown'}",
                f"Changed paths:\n{changed_paths}",
            )
        )


class BundledPackRepository:
    """Test/dev adapter that reads bundled packs without performing vector search."""

    def __init__(self, packs_root: Path, profiles_root: Path) -> None:
        self.packs_root = packs_root
        self.profiles_root = profiles_root

    def load_profile(
        self, profile_id: str, request: ReviewRequest | None = None
    ) -> ReviewProfile:
        path = self.profiles_root / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Unknown profile: {profile_id}")
        return ReviewProfile.from_dict(json.loads(path.read_text()))

    def list_documents(self) -> list[StandardDocument]:
        from .packs import chunk_markdown, discover_packs

        documents = []
        for pack in discover_packs(self.packs_root):
            source_links = [item["url"] for item in pack.manifest["sources"]]
            for document in pack.documents:
                documents.append(
                    StandardDocument(
                        id=f"pack:{pack.id}:{document.path.name}@{pack.version}",
                        scope="public",
                        language=pack.manifest.get("language"),
                        framework=pack.manifest.get("framework"),
                        title=document.title,
                        tags=document.tags,
                        source_links=source_links,
                        rules=[StandardRule.from_dict(item) for item in document.rules],
                        retrieved_chunks=[
                            content
                            for _, content in chunk_markdown(document.path.read_text())
                        ],
                    )
                )
        return documents

    def retrieve(
        self, request: ReviewRequest, profile: ReviewProfile, limit: int = 12
    ) -> list[StandardDocument]:
        language = (request.inferred_language() or "").lower()
        framework = (request.framework or "").lower()
        selected = {item.rsplit("@", 1)[0] for item in request.standard_packs}
        ranked: list[tuple[int, StandardDocument]] = []
        for document in self.list_documents():
            if document.scope not in profile.retrieval_order:
                continue
            if document.language and language and document.language.lower() != language:
                continue
            if (
                document.framework
                and framework
                and document.framework.lower() != framework
            ):
                continue
            pack_id = document.id.split(":", 2)[1]
            if selected and pack_id not in selected:
                continue
            ranked.append((profile.retrieval_order.index(document.scope), document))
        ranked.sort(key=lambda item: (item[0], item[1].id))
        return [document for _, document in ranked[:limit]]


def _vector_literal(vector: list[float], expected_dimensions: int) -> str:
    if len(vector) != expected_dimensions:
        raise ValueError(
            f"Expected {expected_dimensions} embedding dimensions, got {len(vector)}"
        )
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("Embedding contains a non-finite value")
    return "[" + ",".join(format(value, ".12g") for value in vector) + "]"
