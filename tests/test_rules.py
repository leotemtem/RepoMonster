from __future__ import annotations

import unittest
from pathlib import Path

from review_gatekeeper.models import ChangedFile, ReviewProfile, ReviewRequest
from review_gatekeeper.repository import BundledPackRepository
from review_gatekeeper.rules import RuleEngine
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
                        "deletions": 3,
                    }
                ],
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
                        "deletions": 9,
                    },
                    {
                        "path": "tests/test_auth.py",
                        "language": "python",
                        "additions": 18,
                        "deletions": 2,
                    },
                ],
            }
        )

        result = build_service().review(request)
        self.assertEqual(result.gate_state.value, "blocked")
        self.assertTrue(
            any(item.category == "traceability" for item in result.findings)
        )

    def test_source_paths_receive_task_and_test_evidence_checks(self) -> None:
        profile = ReviewProfile.from_dict({"id": "default", "name": "Default"})
        for path in (
            "src/docs/handler.py",
            "docs/example.ts",
            "src/testing_utils/helper.py",
            "testing_utils/helper.py",
            "src/testimony.py",
            "testimonials/service.py",
        ):
            with self.subTest(path=path):
                request = ReviewRequest.from_dict(
                    {
                        "provider": "github",
                        "change_kind": "pull_request",
                        "repository": "acme/api",
                        "external_id": "1",
                        "changed_files": [{"path": path}],
                    }
                )
                findings = RuleEngine().evaluate(request, profile, [])
                self.assertTrue(
                    any(item.category == "traceability" for item in findings)
                )
                self.assertTrue(
                    any(item.rule_id == "pr-test-evidence" for item in findings)
                )
                self.assertTrue(
                    any(
                        item.category == "testing" and item.severity.value == "warning"
                        for item in findings
                    )
                )


class FileClassificationTests(unittest.TestCase):
    def test_classification_uses_directory_segments_and_file_names(self) -> None:
        cases = [
            ("src/docs/handler.py", False, False),
            ("docs/example.ts", False, False),
            ("src/testing_utils/helper.py", False, False),
            ("testing_utils/helper.py", False, False),
            ("src/testimony.py", False, False),
            ("testimonials/service.py", False, False),
            ("src/contest/main.py", False, False),
            ("README.MD", False, True),
            ("docs/guide.mdx", False, True),
            ("guide.rst", False, True),
            ("requirements.txt", False, False),
            ("src/test/main.py", True, False),
            ("tests/main.py", True, False),
            ("src/tests/main.py", True, False),
            ("src/__tests__/main.ts", True, False),
            ("tests_e2e/harness.py", True, False),
            ("tests_integration/fixture.py", True, False),
            ("test_main.py", True, False),
            ("src/test_main.py", True, False),
            ("src/main_test.py", True, False),
            ("src/main_test.go", True, False),
            ("src/main.spec.ts", True, False),
            ("src/main.spec.tsx", True, False),
            ("src/main.test.ts", True, False),
            ("src/main.test.tsx", True, False),
            ("src/main.spec.js", True, False),
            ("src/main.test.jsx", True, False),
            ("src/main.test.json", False, False),
            ("tests/README.md", True, True),
        ]
        for path, is_test, is_documentation in cases:
            with self.subTest(path=path):
                changed = ChangedFile(path)
                self.assertEqual(changed.is_test_file(), is_test)
                self.assertEqual(changed.is_documentation_file(), is_documentation)
                self.assertEqual(
                    changed.is_source_file(), not (is_test or is_documentation)
                )


if __name__ == "__main__":
    unittest.main()
