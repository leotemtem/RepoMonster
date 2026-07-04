from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from review_gatekeeper.insights import InsightResponseError, OpenAICompatibleInsightProvider


class InsightProviderTests(unittest.TestCase):
    def test_generate_requests_structured_output_with_configured_budget(self) -> None:
        provider = OpenAICompatibleInsightProvider(
            "http://model.test/v1",
            "test-model",
            timeout_seconds=900,
            max_tokens=8192,
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "findings": [
                                        {
                                            "severity": "warning",
                                            "category": "correctness",
                                            "title": "Contract mismatch",
                                            "detail": "The implementation changes case.",
                                            "evidence": ["app/main.py:12"],
                                            "rule_id": "contract-match",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            }
        ).encode()

        with patch(
            "review_gatekeeper.insights.urlrequest.urlopen", return_value=response
        ) as open_url:
            findings = provider.generate("Review this change")

        request_payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 900)
        self.assertEqual(request_payload["max_tokens"], 8192)
        self.assertFalse(request_payload["stream"])
        self.assertEqual(request_payload["response_format"]["type"], "json_schema")
        self.assertEqual(findings[0].title, "Contract mismatch")

    def test_environment_configures_timeout_and_generation_budget(self) -> None:
        with patch.dict(
            os.environ,
            {
                "INSIGHT_BASE_URL": "http://model.test/v1",
                "INSIGHT_MODEL": "test-model",
                "INSIGHT_TIMEOUT_SECONDS": "900",
                "INSIGHT_MAX_TOKENS": "16384",
            },
        ):
            provider = OpenAICompatibleInsightProvider.from_environment()

        self.assertEqual(provider.timeout_seconds, 900)
        self.assertEqual(provider.max_tokens, 16384)

    def test_reasoning_without_final_content_has_actionable_error(self) -> None:
        provider = OpenAICompatibleInsightProvider(
            "http://model.test/v1", "test-model"
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "An unfinished analysis",
                        }
                    }
                ]
            }
        ).encode()

        with patch(
            "review_gatekeeper.insights.urlrequest.urlopen", return_value=response
        ), self.assertRaisesRegex(
            InsightResponseError, "producing reasoning but no final answer"
        ):
            provider.generate("Review this change")

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
