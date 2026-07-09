from __future__ import annotations

import unittest

from review_gatekeeper.models import (
    Finding,
    FindingImpact,
    InsightRecommendation,
    InsightResult,
    ReviewProfile,
    ReviewRequest,
    Severity,
)
from review_gatekeeper.service import ReviewService


class _Repository:
    def __init__(self, mode: str, minimum_impact: str = "significant") -> None:
        self.profile = ReviewProfile.from_dict(
            {
                "id": "test",
                "name": "Test",
                "required_description_sections": [
                    "Problem",
                    "Approach",
                    "Acceptance Criteria",
                    "Test Evidence",
                ],
                "require_task_reference_for_code_changes": False,
                "require_test_evidence_when_code_changes": True,
                "max_warnings_for_ready": 10,
                "large_change_line_threshold": 400,
                "retrieval_order": [],
                "auto_block": {
                    "mode": mode,
                    "require_poor_documentation": True,
                    "minimum_model_impact": minimum_impact,
                },
            }
        )

    def load_profile(self, _profile_id, _request):
        return self.profile

    def retrieve(self, _request, _profile):
        return []


class _InsightProvider:
    def __init__(
        self,
        *,
        recommendation: InsightRecommendation = InsightRecommendation.REQUEST_CHANGES,
        evidence: list[str] | None = None,
    ) -> None:
        self.result = InsightResult(
            findings=[
                Finding(
                    severity=Severity.WARNING,
                    category="correctness",
                    title="Implementation does not match the approach",
                    detail="The implementation violates the documented behavior.",
                    evidence=["app/service.py:42"] if evidence is None else evidence,
                    rule_id="implementation-contract",
                    impact=FindingImpact.SIGNIFICANT,
                )
            ],
            recommendation=recommendation,
            recommendation_reason="The author should correct the implementation.",
        )

    def generate(self, _review_brief: str) -> InsightResult:
        return self.result


def _request(*, large: bool = True) -> ReviewRequest:
    return ReviewRequest.from_dict(
        {
            "provider": "github",
            "change_kind": "pull_request",
            "repository": "acme/api",
            "external_id": "42",
            "title": "Change service behavior",
            "description": (
                "## Problem\nBug.\n\n"
                "## Approach\nFix it.\n\n"
                "## Acceptance Criteria\nIt works.\n\n"
                "## Test Evidence\nTests pass."
            ),
            "changed_files": [
                {
                    "path": "app/service.py",
                    "additions": 450 if large else 20,
                    "deletions": 0,
                },
                {"path": "tests/test_service.py", "additions": 10, "deletions": 0},
            ],
        }
    )


class AutoBlockPolicyTests(unittest.TestCase):
    def test_shadow_mode_records_match_without_changing_gate(self) -> None:
        result = ReviewService(
            _Repository("shadow"), insight_provider=_InsightProvider()
        ).review(_request())

        self.assertEqual(result.gate_state.value, "ready_for_human_review")
        self.assertTrue(
            any(item.rule_id == "auto-block-shadow" for item in result.findings)
        )

    def test_enforce_mode_blocks_matching_review(self) -> None:
        result = ReviewService(
            _Repository("enforce"), insight_provider=_InsightProvider()
        ).review(_request())

        self.assertEqual(result.gate_state.value, "blocked")
        self.assertTrue(
            any(item.rule_id == "auto-block-enforced" for item in result.findings)
        )
        self.assertEqual(result.insight_recommendation.value, "request_changes")
        self.assertEqual(result.to_dict()["insight_recommendation"], "request_changes")

    def test_good_pr_documentation_does_not_match_composite_policy(self) -> None:
        result = ReviewService(
            _Repository("enforce"), insight_provider=_InsightProvider()
        ).review(_request(large=False))

        self.assertEqual(result.gate_state.value, "ready_for_human_review")
        self.assertFalse(any(item.category == "policy" for item in result.findings))

    def test_model_recommendation_must_request_changes(self) -> None:
        result = ReviewService(
            _Repository("enforce"),
            insight_provider=_InsightProvider(
                recommendation=InsightRecommendation.READY
            ),
        ).review(_request())

        self.assertEqual(result.gate_state.value, "ready_for_human_review")

    def test_significant_finding_requires_evidence(self) -> None:
        result = ReviewService(
            _Repository("enforce"),
            insight_provider=_InsightProvider(evidence=[]),
        ).review(_request())

        self.assertEqual(result.gate_state.value, "ready_for_human_review")

    def test_blocking_threshold_rejects_significant_finding(self) -> None:
        result = ReviewService(
            _Repository("enforce", minimum_impact="blocking"),
            insight_provider=_InsightProvider(),
        ).review(_request())

        self.assertEqual(result.gate_state.value, "ready_for_human_review")
        self.assertFalse(any(item.category == "policy" for item in result.findings))


if __name__ == "__main__":
    unittest.main()
