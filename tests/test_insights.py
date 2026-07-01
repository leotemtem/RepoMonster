from __future__ import annotations

import unittest

from review_gatekeeper.insights import OpenAICompatibleInsightProvider


class InsightProviderTests(unittest.TestCase):
    def test_structured_finding_is_normalized(self) -> None:
        finding = OpenAICompatibleInsightProvider._finding(
            {
                "severity": "warning",
                "category": "maintainability",
                "title": "Boundary is unclear",
                "detail": "The route performs persistence directly.",
                "evidence": ["app/routes/payments.py:42"],
                "rule_id": "repo-route-boundary",
            }
        )

        self.assertEqual(finding.severity.value, "warning")
        self.assertEqual(finding.rule_id, "repo-route-boundary")

    def test_model_severity_is_case_insensitive(self) -> None:
        finding = OpenAICompatibleInsightProvider._finding(
            {
                "severity": " ERROR ",
                "category": "correctness",
                "title": "Validation gap",
                "detail": "A boundary case is not handled.",
                "evidence": ["app/main.py"],
                "rule_id": None,
            }
        )

        self.assertEqual(finding.severity.value, "error")

    def test_scalar_evidence_is_treated_as_one_item(self) -> None:
        finding = OpenAICompatibleInsightProvider._finding(
            {
                "severity": "info",
                "category": "correctness",
                "title": "Task is implemented",
                "detail": "The endpoint matches the acceptance criteria.",
                "evidence": "app/main.py",
                "rule_id": None,
            }
        )

        self.assertEqual(finding.evidence, ["app/main.py"])

    def test_null_evidence_is_treated_as_empty(self) -> None:
        finding = OpenAICompatibleInsightProvider._finding(
            {
                "severity": "info",
                "category": "clarity",
                "title": "Clear implementation",
                "detail": "No specific source location was supplied.",
                "evidence": None,
                "rule_id": None,
            }
        )

        self.assertEqual(finding.evidence, [])


if __name__ == "__main__":
    unittest.main()
