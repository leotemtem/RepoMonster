from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repository import EmbeddingProvider, _vector_literal


@dataclass(frozen=True, slots=True)
class PackDocument:
    path: Path
    title: str
    tags: list[str]
    rules: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StandardPack:
    root: Path
    manifest: dict[str, Any]
    documents: list[PackDocument]
    content_checksum: str

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return str(self.manifest["version"])


def load_pack(manifest_path: Path) -> StandardPack:
    manifest = json.loads(manifest_path.read_text())
    required = {"id", "version", "name", "description", "documents", "sources"}
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"{manifest_path}: missing fields {', '.join(missing)}")

    root = manifest_path.parent
    documents: list[PackDocument] = []
    digest = hashlib.sha256(manifest_path.read_bytes())
    for item in manifest["documents"]:
        path = root / item["path"]
        if not path.is_file():
            raise ValueError(f"{manifest_path}: document does not exist: {item['path']}")
        digest.update(item["path"].encode())
        digest.update(path.read_bytes())
        documents.append(
            PackDocument(
                path=path,
                title=item["title"],
                tags=list(item.get("tags", [])),
                rules=list(item.get("rules", [])),
            )
        )
    return StandardPack(root, manifest, documents, digest.hexdigest())


def discover_packs(root: Path) -> list[StandardPack]:
    return [load_pack(path) for path in sorted(root.glob("*/*/manifest.json"))]


def chunk_markdown(markdown: str) -> list[tuple[str | None, str]]:
    """Split authored standards at headings while retaining heading context."""
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    body: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^#{1,3}\s+(.+)$", line)
        if match:
            content = "\n".join(body).strip()
            if content:
                chunks.append((heading, content))
            heading = match.group(1).strip()
            body = []
        else:
            body.append(line)
    content = "\n".join(body).strip()
    if content:
        chunks.append((heading, content))
    return chunks or [(None, markdown.strip())]


class PackSynchronizer:
    def __init__(self, connection, embedding_provider: EmbeddingProvider) -> None:
        self.connection = connection
        self.embedding_provider = embedding_provider

    def sync(self, packs: list[StandardPack], force: bool = False) -> list[str]:
        changed: list[str] = []
        for pack in packs:
            if not force and self._is_current(pack):
                continue
            self._sync_pack(pack)
            changed.append(f"{pack.id}@{pack.version}")
        return changed

    def _is_current(self, pack: StandardPack) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT content_checksum, embedding_model, embedding_dimensions, status
                FROM standard_pack_versions
                WHERE pack_id = %s AND version = %s
                """,
                (pack.id, pack.version),
            )
            row = cursor.fetchone()
        return bool(
            row
            and row[0] == pack.content_checksum
            and row[1] == self.embedding_provider.model_id
            and row[2] == self.embedding_provider.dimensions
            and row[3] == "ready"
        )

    def _sync_pack(self, pack: StandardPack) -> None:
        manifest = pack.manifest
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO standard_packs (id, name, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    updated_at = now()
                """,
                (pack.id, manifest["name"], manifest["description"]),
            )
            cursor.execute(
                """
                INSERT INTO standard_pack_versions (
                    pack_id, version, language, framework, supported_versions,
                    manifest, content_checksum, embedding_model,
                    embedding_dimensions, status
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 'indexing')
                ON CONFLICT (pack_id, version) DO UPDATE SET
                    language = EXCLUDED.language,
                    framework = EXCLUDED.framework,
                    supported_versions = EXCLUDED.supported_versions,
                    manifest = EXCLUDED.manifest,
                    content_checksum = EXCLUDED.content_checksum,
                    embedding_model = EXCLUDED.embedding_model,
                    embedding_dimensions = EXCLUDED.embedding_dimensions,
                    status = 'indexing',
                    updated_at = now()
                """,
                (
                    pack.id,
                    pack.version,
                    manifest.get("language"),
                    manifest.get("framework"),
                    manifest.get("supported_versions"),
                    json.dumps(manifest),
                    pack.content_checksum,
                    self.embedding_provider.model_id,
                    self.embedding_provider.dimensions,
                ),
            )

        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE standard_documents
                SET enabled = false, updated_at = now()
                WHERE pack_id = %s AND pack_version = %s
                """,
                (pack.id, pack.version),
            )

        for document in pack.documents:
            self._sync_document(pack, document)

        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE standard_pack_versions
                SET status = 'ready', updated_at = now()
                WHERE pack_id = %s AND version = %s
                """,
                (pack.id, pack.version),
            )

    def _sync_document(self, pack: StandardPack, document: PackDocument) -> None:
        manifest = pack.manifest
        relative_path = document.path.relative_to(pack.root).as_posix()
        logical_key = f"pack:{pack.id}:{relative_path}"
        document_id = f"{logical_key}@{pack.version}"
        body = document.path.read_text()
        checksum = hashlib.sha256(body.encode()).hexdigest()
        chunks = chunk_markdown(body)
        vectors = self.embedding_provider.embed([content for _, content in chunks])
        source_urls = [item["url"] for item in manifest["sources"]]
        language = manifest.get("language")
        framework = manifest.get("framework")
        stack_key = f"{language or 'any'}/{framework or 'any'}"

        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO standard_documents (
                    id, logical_key, version, scope, pack_id, pack_version,
                    language, framework, stack_key, source_path, title,
                    body_markdown, source_urls, tags, content_checksum, metadata
                ) VALUES (
                    %s, %s, %s, 'public', %s, %s, %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s, '{}'::jsonb
                )
                ON CONFLICT (id) DO UPDATE SET
                    logical_key = EXCLUDED.logical_key,
                    version = EXCLUDED.version,
                    pack_id = EXCLUDED.pack_id,
                    pack_version = EXCLUDED.pack_version,
                    language = EXCLUDED.language,
                    framework = EXCLUDED.framework,
                    stack_key = EXCLUDED.stack_key,
                    source_path = EXCLUDED.source_path,
                    title = EXCLUDED.title,
                    body_markdown = EXCLUDED.body_markdown,
                    source_urls = EXCLUDED.source_urls,
                    tags = EXCLUDED.tags,
                    content_checksum = EXCLUDED.content_checksum,
                    enabled = true,
                    updated_at = now()
                """,
                (
                    document_id,
                    logical_key,
                    pack.version,
                    pack.id,
                    pack.version,
                    language,
                    framework,
                    stack_key,
                    relative_path,
                    document.title,
                    body,
                    json.dumps(source_urls),
                    json.dumps(document.tags),
                    checksum,
                ),
            )
            cursor.execute("DELETE FROM standard_chunks WHERE document_id = %s", (document_id,))
            cursor.execute("DELETE FROM standard_rules WHERE document_id = %s", (document_id,))
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
            for rule in document.rules:
                cursor.execute(
                    """
                    INSERT INTO standard_rules (
                        id, document_id, severity, gate, title, rationale,
                        check_statements, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, '{}'::jsonb)
                    """,
                    (
                        rule["id"],
                        document_id,
                        rule["severity"],
                        bool(rule.get("gate", False)),
                        rule["title"],
                        rule.get("rationale"),
                        json.dumps(rule.get("check_statements", [])),
                    ),
                )
