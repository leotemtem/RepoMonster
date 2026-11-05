from __future__ import annotations

from harness import (
    APP_ID,
    DEFAULT_REQUIRED_CI,
    HEAD_SHA,
    EndToEndTestCase,
    repository_config,
)


class WaitingForCiTests(EndToEndTestCase):
    use_model = True

    def test_pending_check_waits_then_reviews_when_it_succeeds(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI, status="in_progress", conclusion=None)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["status"], "in_progress")
        self.assertIsNone(run["conclusion"])
        self.assertIn(DEFAULT_REQUIRED_CI, run["output"]["summary"])
        self.assertEqual(self.github.model_calls, 0)
        self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.send_webhook(
            "check_run", self.check_run_event(DEFAULT_REQUIRED_CI), delivery="done"
        )
        self.wait_for_delivery("done")

        [run] = self.github.check_runs
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.github.model_calls, 1)

    def test_missing_check_waits_then_blocks_when_it_fails(self) -> None:
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")
        self.assertEqual(self.github.check_runs[0]["status"], "in_progress")

        self.github.set_ci(DEFAULT_REQUIRED_CI, conclusion="failure")
        self.send_webhook(
            "check_run",
            self.check_run_event(DEFAULT_REQUIRED_CI, pull_numbers=(7,)),
            delivery="red",
        )
        self.wait_for_delivery("red")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")

    def test_status_event_triggers_the_review(self) -> None:
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.github.statuses[HEAD_SHA] = [
            {"context": DEFAULT_REQUIRED_CI, "state": "success"}
        ]
        _, ignored = self.send_webhook(
            "status",
            {**self.check_run_event("x"), "sha": HEAD_SHA, "state": "pending"},
        )
        self.assertFalse(ignored["queued"])
        event = {
            **self.check_run_event("x"),
            "sha": HEAD_SHA,
            "context": DEFAULT_REQUIRED_CI,
            "state": "success",
        }
        self.send_webhook("status", event, delivery="status")
        self.wait_for_delivery("status")

        [run] = self.github.check_runs
        self.assertEqual(run["status"], "completed")

    def test_own_check_run_does_not_trigger_a_review(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        own = self.check_run_event("RepoMonster review gate", app_id=int(APP_ID))
        _, body = self.send_webhook("check_run", own)

        self.assertFalse(body["queued"])
        self.assertEqual(self.github.model_calls, 1)

    def test_several_checks_produce_one_review(self) -> None:
        self.github.default_files[".repomonster.yml"] = repository_config(
            ["lint", "test"]
        )
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.github.set_ci("lint")
        self.send_webhook("check_run", self.check_run_event("lint"), delivery="lint")
        self.wait_for_delivery("lint")
        self.assertEqual(self.github.check_runs[0]["status"], "in_progress")
        self.assertIn("test", self.github.check_runs[0]["output"]["summary"])
        self.assertEqual(self.github.model_calls, 0)

        self.github.set_ci("test")
        self.send_webhook("check_run", self.check_run_event("test"), delivery="test")
        self.wait_for_delivery("test")
        self.assertEqual(self.github.model_calls, 1)

        self.send_webhook("check_run", self.check_run_event("test"), delivery="again")
        self.wait_for_delivery("again", status="ignored")
        self.github.set_ci("docs")
        self.send_webhook("check_run", self.check_run_event("docs"), delivery="other")
        self.wait_for_delivery("other", status="ignored")

        self.assertEqual(self.github.model_calls, 1)
        self.assertEqual(len(self.github.check_runs), 1)

    def test_rerun_with_a_new_result_reviews_again(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI, conclusion="failure")
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")
        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")

        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.send_webhook(
            "check_run", self.check_run_event(DEFAULT_REQUIRED_CI), delivery="rerun"
        )
        self.wait_for_delivery("rerun")

        self.assertEqual(len(self.github.check_runs), 2)
        self.assertEqual(self.github.model_calls, 2)


class CiWaitTimeoutTests(EndToEndTestCase):
    settings = {"CI_WAIT_TIMEOUT_SECONDS": "2"}
    use_model = True

    def test_check_that_never_finishes_escalates_to_a_maintainer(self) -> None:
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.wait_for(
            lambda: self.github.check_runs[0]["status"] == "completed",
            description="the wait to time out",
        )

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn(DEFAULT_REQUIRED_CI, run["output"]["text"])
        self.assertEqual(self.github.model_calls, 0)
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"),
            [("manual_escalation",)],
        )

    def test_timer_does_nothing_once_the_review_has_finished(self) -> None:
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.send_webhook(
            "check_run", self.check_run_event(DEFAULT_REQUIRED_CI), delivery="done"
        )
        self.wait_for_delivery("done")

        timers = self.wait_for(
            lambda: self.query(
                "SELECT delivery_id FROM webhook_deliveries "
                "WHERE event_name = 'repomonster_ci_wait' AND status = 'ignored'"
            ),
            description="the timer to fire",
        )
        self.assertEqual(len(timers), 1)
        self.assertEqual(len(self.github.check_runs), 1)
        self.assertEqual(self.github.model_calls, 1)
