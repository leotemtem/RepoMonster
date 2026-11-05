from __future__ import annotations

from harness import DEFAULT_REQUIRED_CI, EndToEndTestCase


class FailClosedTests(EndToEndTestCase):
    use_model = True

    def test_model_failure_escalates_without_passing_the_gate(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(self.github.model_calls, 1)
        self.assertEqual(run["conclusion"], "action_required")
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"),
            [("manual_escalation",)],
        )

    def test_exhausted_retries_do_not_pass_the_gate(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        for _ in range(3):
            self.github.fail_next("GET", r"/pulls/7/files$", status=500)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open", status="failed")

        self.wait_for(
            lambda: self.github.check_runs[0]["status"] == "completed",
            description="the terminal failure to publish",
        )
        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")


class PullRequestStateTests(EndToEndTestCase):
    def test_draft_is_not_reviewed(self) -> None:
        self.github.add_pull_request(draft=True)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "neutral")
        self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

    def test_delivery_for_an_old_commit_is_ignored(self) -> None:
        payload = self.pull_request_payload()
        payload["pull_request"] = {
            **payload["pull_request"],
            "head": {"sha": "b" * 40, "ref": "fix-drops"},
        }

        self.send_webhook("pull_request", payload, delivery="old")
        self.wait_for_delivery("old", status="ignored")

        self.assertEqual(self.github.check_runs, [])


class ModelFailureWithErrorsTests(EndToEndTestCase):
    use_model = True

    def test_deterministic_errors_show_as_blocked(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.add_pull_request(body="Fixes #42")

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertEqual(self.github.model_calls, 1)
        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"), [("blocked",)]
        )
