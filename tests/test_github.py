from __future__ import annotations

import base64
import unittest

from review_gatekeeper.config import load_repository_config
from review_gatekeeper.github import (
    GitHubReviewProcessor,
    _decode_github_content,
    _github_issue_numbers,
    _select_stack,
    verify_webhook_signature,
)
from review_gatekeeper.jobs import WebhookJob
from review_gatekeeper.models import (
    ChangedFile,
    Finding,
    FindingImpact,
    GateState,
    InsightRecommendation,
    ReviewRequest,
    ReviewResult,
    Severity,
)


class GitHubIntegrationTests(unittest.TestCase):
    def test_webhook_signature_matches_github_test_vector(self) -> None:
        self.assertTrue(
            verify_webhook_signature(
                b"Hello, World!",
                "It's a Secret to Everybody",
                "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
            )
        )
        self.assertFalse(
            verify_webhook_signature(b"tampered", "secret", "sha256=not-valid")
        )

    def test_github_base64_content_accepts_wrapped_lines(self) -> None:
        encoded = base64.b64encode(b"version: 1\n").decode()
        wrapped = encoded[:5] + "\n" + encoded[5:]
        self.assertEqual(
            _decode_github_content({"encoding": "base64", "content": wrapped}),
            "version: 1\n",
        )

    def test_stack_is_selected_from_changed_paths(self) -> None:
        config = load_repository_config(
            """
version: 1
knowledge: {}
stacks:
  - paths: [backend/**]
    language: python
    framework: fastapi
    packs: [python@1.0.0, fastapi@1.0.0]
  - paths: [frontend/**]
    language: typescript
    framework: node
    packs: [typescript@1.0.0, node@1.0.0]
"""
        )
        language, framework, packs = _select_stack(
            config,
            [
                ChangedFile("backend/app.py", additions=80),
                ChangedFile("frontend/app.ts", additions=5),
            ],
        )
        self.assertEqual((language, framework), ("python", "fastapi"))
        self.assertEqual(
            packs,
            ["python@1.0.0", "fastapi@1.0.0", "typescript@1.0.0", "node@1.0.0"],
        )

    def test_local_github_issue_references_are_deduplicated(self) -> None:
        text = "Fixes #42; context: https://github.com/acme/api/issues/42 and #77"
        self.assertEqual(_github_issue_numbers(text, "acme/api"), [42, 77])

    def test_stale_pull_request_delivery_is_not_reviewed(self) -> None:
        client = _FakeGitHubClient(head_sha="new-head")
        queue = _FakeQueue()
        processor = GitHubReviewProcessor(
            database_url="unused",
            queue=queue,
            client=client,
            embedding_provider=object(),
            review_service=object(),
        )

        processed = processor.process(_pull_request_job(head_sha="old-head"))

        self.assertFalse(processed)
        self.assertEqual(client.created_checks, [])

    def test_draft_pull_request_gets_neutral_check(self) -> None:
        client = _FakeGitHubClient(head_sha="same-head", draft=True)
        queue = _FakeQueue()
        processor = GitHubReviewProcessor(
            database_url="unused",
            queue=queue,
            client=client,
            embedding_provider=object(),
            review_service=object(),
        )

        processed = processor.process(_pull_request_job(head_sha="same-head"))

        self.assertTrue(processed)
        self.assertEqual(queue.external_result_id, "9001")
        self.assertEqual(client.updated_checks[0]["conclusion"], "neutral")

    def test_review_persistence_records_model_recommendation_and_impact(self) -> None:
        connection = _PersistenceConnection()
        processor = GitHubReviewProcessor(
            database_url="unused",
            queue=_FakeQueue(),
            client=_FakeGitHubClient(head_sha="head"),
            embedding_provider=object(),
            review_service=object(),
        )
        processor._connect = lambda: connection
        request = ReviewRequest.from_dict(
            {
                "provider": "github",
                "change_kind": "pull_request",
                "repository": "acme/api",
                "repository_key": "github:https://github.com:123",
                "external_id": "7",
                "title": "Change",
                "description": "Description",
                "metadata": {"head_sha": "head"},
            }
        )
        result = ReviewResult(
            gate_state=GateState.BLOCKED,
            summary="blocked",
            findings=[
                Finding(
                    severity=Severity.WARNING,
                    category="correctness",
                    title="Mismatch",
                    detail="The implementation does not match.",
                    evidence=["app.py:1"],
                    impact=FindingImpact.SIGNIFICANT,
                )
            ],
            applied_profile="default",
            retrieved_documents=[],
            llm_review_brief="brief",
            insight_recommendation=InsightRecommendation.REQUEST_CHANGES,
            insight_recommendation_reason="Author changes are required.",
        )

        processor._persist_result(request, result)

        review_insert = next(
            item
            for item in connection.cursor_instance.executions
            if "INSERT INTO review_runs" in item[0]
        )
        finding_insert = next(
            item
            for item in connection.cursor_instance.executions
            if "INSERT INTO review_findings" in item[0]
        )
        self.assertEqual(review_insert[1][-2], "request_changes")
        self.assertEqual(review_insert[1][-1], "Author changes are required.")
        self.assertEqual(finding_insert[1][2], "significant")


class _FakeQueue:
    external_result_id: str | None = None

    def set_external_result_id(self, job_id: int, external_result_id: str) -> None:
        self.external_result_id = external_result_id


class _FakeGitHubClient:
    def __init__(self, *, head_sha: str, draft: bool = False) -> None:
        self.head_sha = head_sha
        self.draft = draft
        self.created_checks: list[dict] = []
        self.updated_checks: list[dict] = []

    def installation_token(self, installation_id: int, repository_id: int) -> str:
        return "installation-token"

    def get_repository(self, full_name: str, token: str) -> dict:
        return {
            "id": 123,
            "full_name": "acme/api",
            "default_branch": "main",
            "html_url": "https://github.com/acme/api",
        }

    def get_pull_request(self, full_name: str, number: int, token: str) -> dict:
        return {
            "number": 7,
            "state": "open",
            "draft": self.draft,
            "head": {"sha": self.head_sha},
            "html_url": "https://github.com/acme/api/pull/7",
        }

    def create_check_run(self, full_name: str, token: str, **payload) -> dict:
        self.created_checks.append(payload)
        return {"id": 9001}

    def update_check_run(
        self, full_name: str, check_run_id: int, token: str, **payload
    ) -> dict:
        self.updated_checks.append(payload)
        return {"id": check_run_id}


class _PersistenceCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple]] = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params) -> None:
        self.executions.append((sql, params))
        if "SELECT id FROM repositories" in sql:
            self.result = (11,)
        elif "INSERT INTO review_runs" in sql:
            self.result = (22,)
        else:
            self.result = None

    def fetchone(self):
        return self.result


class _PersistenceConnection:
    def __init__(self) -> None:
        self.cursor_instance = _PersistenceCursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance


def _pull_request_job(*, head_sha: str) -> WebhookJob:
    return WebhookJob(
        id=1,
        provider="github",
        delivery_id="delivery-1",
        event_name="pull_request",
        attempts=1,
        payload={
            "action": "opened",
            "installation": {"id": 22},
            "repository": {
                "id": 123,
                "full_name": "acme/api",
                "html_url": "https://github.com/acme/api",
            },
            "pull_request": {
                "number": 7,
                "title": "Change",
                "head": {"sha": head_sha, "ref": "feature"},
                "base": {"ref": "main"},
            },
        },
    )


if __name__ == "__main__":
    unittest.main()
