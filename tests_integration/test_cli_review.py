from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class LocalReviewCliIntegrationTests(unittest.TestCase):
    def test_example_review_request_runs_through_local_cli(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root / "src")
        environment["REPOMONSTER_ROOT"] = str(root)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "review_gatekeeper.cli",
                str(root / "examples" / "review_request.json"),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            text=True,
        )

        payload = json.loads(completed.stdout)
        self.assertEqual(payload["gate_state"], "ready_for_human_review")
        self.assertEqual(payload["applied_profile"], "default")
        self.assertIn(
            "pack:fastapi:review-guidance.md@1.0.0", payload["retrieved_documents"]
        )
        self.assertIn(
            "pack:python:review-guidance.md@1.0.0", payload["retrieved_documents"]
        )
        self.assertTrue(
            any(
                finding["category"] == "documentation"
                for finding in payload["findings"]
            )
        )
        self.assertIn("(no diff excerpts supplied)", payload["llm_review_brief"])


if __name__ == "__main__":
    unittest.main()
