from __future__ import annotations

import unittest
from pathlib import Path

from review_gatekeeper.models import ReviewRequest
from review_gatekeeper.repository import BundledPackRepository
from review_gatekeeper.service import ReviewService


def build_service() -> ReviewService:
    project_root = Path(__file__).resolve().parents[1]
    return ReviewService(
        BundledPackRepository(
            packs_root=project_root / "standard-packs",
            profiles_root=project_root / "profiles",
        )
    )


class ReviewGatekeeperTests(unittest.TestCase):
    def test_ready_for_human_review_when_description_and_tests_exist(self) -> None:
        request = ReviewRequest.from_dict(
            {
                "provider": "github",
                "change_kind": "pull_request",
                "repository": "acme/payments-api",
                "external_id": "184",
                "title": "Add idempotent payment capture endpoint",
                "description": (
                    "## Problem\nDuplicate requests cause duplicate captures.\n\n"
                    "## Approach\nPersist the first successful result and return it.\n\n"
                    "## Acceptance Criteria\nRepeated requests with the same key are idempotent.\n\n"
                    "## Test Evidence\nAdded API and service tests.\n"
                ),
                "framework": "fastapi",
                "task_references": [{"kind": "jira", "value": "PAY-142"}],
                "changed_files": [
                    {
                        "path": "app/api/routes/capture.py",
                        "language": "python",
                        "additions": 30,
                        "deletions": 5
                    },
                    {
                        "path": "tests/api/test_capture.py",
                        "language": "python",
                        "additions": 40,
                        "deletions": 0
                    }
                ]
            }
        )

        result = build_service().review(request)
        self.assertEqual(result.gate_state.value, "ready_for_human_review")

    def test_blocked_when_required_description_sections_are_missing(self) -> None:
        request = ReviewRequest.from_dict(
            {
                "provider": "github",
                "change_kind": "pull_request",
                "repository": "acme/payments-api",
                "external_id": "185",
                "title": "Quick fix",
                "description": "Fix bug.",
                "framework": "fastapi",
                "task_references": [{"kind": "jira", "value": "PAY-143"}],
                "changed_files": [
                    {
                        "path": "app/services/capture.py",
                        "language": "python",
                        "additions": 12,
                        "deletions": 3
                    }
                ]
            }
        )

        result = build_service().review(request)
        self.assertEqual(result.gate_state.value, "blocked")
        self.assertTrue(any(item.category == "description" for item in result.findings))

    def test_blocked_when_task_reference_is_missing(self) -> None:
        request = ReviewRequest.from_dict(
            {
                "provider": "gitlab",
                "change_kind": "merge_request",
                "repository": "acme/platform",
                "external_id": "55",
                "title": "Refactor auth middleware",
                "description": (
                    "## Problem\nAuth path is duplicated.\n\n"
                    "## Approach\nCentralize middleware.\n\n"
                    "## Acceptance Criteria\nHandlers share one auth path.\n\n"
                    "## Test Evidence\nUpdated middleware tests.\n"
                ),
                "framework": "fastapi",
                "changed_files": [
                    {
                        "path": "app/middleware/auth.py",
                        "language": "python",
                        "additions": 22,
                        "deletions": 9
                    },
                    {
                        "path": "tests/test_auth.py",
                        "language": "python",
                        "additions": 18,
                        "deletions": 2
                    }
                ]
            }
        )

        result = build_service().review(request)
        self.assertEqual(result.gate_state.value, "blocked")
        self.assertTrue(any(item.category == "traceability" for item in result.findings))


if __name__ == "__main__":
    unittest.main()
