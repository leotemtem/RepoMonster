from __future__ import annotations

import unittest

from review_gatekeeper.models import ReviewProfile, ReviewRequest
from review_gatekeeper.rules import RuleEngine


class RequiredCiRuleTests(unittest.TestCase):
    def test_missing_repository_required_check_blocks_review(self) -> None:
        request = ReviewRequest.from_dict(
            {
                "provider": "github",
                "change_kind": "pull_request",
                "repository": "acme/api",
                "external_id": "1",
                "title": "Change",
                "description": "Change",
                "metadata": {"ci_checks": {"test": "success"}},
            }
        )
        profile = ReviewProfile.from_dict(
            {
                "id": "default",
                "name": "Default",
                "required_ci": ["test", "lint"],
                "require_task_reference_for_code_changes": False,
                "require_test_evidence_when_code_changes": False,
            }
        )

        findings = RuleEngine().evaluate(request, profile, [])

        self.assertTrue(any(item.category == "ci" and "lint" in item.title for item in findings))


if __name__ == "__main__":
    unittest.main()
