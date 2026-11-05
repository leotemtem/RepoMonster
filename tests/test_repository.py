from __future__ import annotations

import unittest
from pathlib import Path

from repomonster.models import ReviewProfile, ReviewRequest
from repomonster.repository import (
    OpenAICompatibleEmbeddingProvider,
    PostgresStandardsRepository,
    _vector_literal,
)


class _EmbeddingProvider:
    model_id = "test-embedding"
    dimensions = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class _Cursor:
    def __init__(self) -> None:
        self.result = []
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params) -> None:
        self.executions.append((sql, params))
        if "FROM standard_rules" in sql:
            self.result = [
                (
                    "public.python-fastapi@1",
                    "clarity",
                    "warning",
                    False,
                    "Prefer clear code",
                    "Reviewability matters",
                    ["Naming exposes intent"],
                )
            ]
        elif params[1] == "public":
            self.result = [
                (
                    "public.python-fastapi@1",
                    "public",
                    "python",
                    "fastapi",
                    "Python + FastAPI",
                    ["python", "fastapi"],
                    ["https://fastapi.tiangolo.com/"],
                    "Use explicit response models for public APIs.",
                    0.91,
                )
            ]
        else:
            self.result = []

    def fetchall(self):
        return self.result


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance


class PostgresRepositoryTests(unittest.TestCase):
    def test_embedding_provider_rejects_non_http_base_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "EMBEDDING_BASE_URL must be an http"):
            OpenAICompatibleEmbeddingProvider("file:///tmp/embedding", "test-model")

    def test_vector_literal_rejects_dimension_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            _vector_literal([0.1], 3)

    def test_nullable_retrieval_filters_have_explicit_postgres_types(self) -> None:
        connection = _Connection()
        repository = PostgresStandardsRepository(
            pool=_Pool(connection),
            embedding_provider=_EmbeddingProvider(),
            profiles_root=Path("profiles"),
        )
        request = ReviewRequest.from_dict(
            {
                "provider": "github",
                "change_kind": "pull_request",
                "repository": "acme/api",
                "external_id": "2",
                "title": "Documentation only",
                "description": "Update documentation",
                "changed_files": [{"path": "README.md"}],
            }
        )
        profile = ReviewProfile.from_dict(
            {"id": "default", "name": "Default", "retrieval_order": ["repo"]}
        )

        repository.retrieve(request, profile)

        sql, params = connection.cursor_instance.executions[0]
        self.assertIn("ANY(%s::text[])", sql)
        self.assertEqual(sql.count("%s::text IS NOT NULL"), 2)
        self.assertIsNone(params[7])
        self.assertIsNone(params[9])


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


if __name__ == "__main__":
    unittest.main()
