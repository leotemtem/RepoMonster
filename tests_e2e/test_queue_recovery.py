from __future__ import annotations

from harness import DEFAULT_REQUIRED_CI, EndToEndTestCase


class WorkerCrashTests(EndToEndTestCase):
    settings = {"WEBHOOK_LOCK_TIMEOUT_SECONDS": "1"}

    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)

    def test_crash_after_creating_the_check_run_continues_it(self) -> None:
        self.github.after_next("POST", r"/check-runs$", self.kill_worker)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(
            lambda: self.processes["worker"].poll() is not None,
            description="the worker to die",
        )
        self.start_worker("worker2")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "success")

    def test_crash_after_publishing_does_not_review_again(self) -> None:
        self.github.after_next("PATCH", r"/check-runs/\d+$", self.kill_worker)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(
            lambda: self.processes["worker"].poll() is not None,
            description="the worker to die",
        )
        self.start_worker("worker2")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["history"], ["in_progress", "success"])

    def test_job_that_keeps_killing_workers_ends_with_a_published_failure(
        self,
    ) -> None:
        self.send_webhook_after_arming_kills(3)
        self.wait_for_delivery("open", status="failed")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")
        self.assertEqual(
            self.query("SELECT attempts FROM webhook_deliveries"),
            [(3,)],
        )
        self.assertEqual(self.github.hooks, [])

    def send_webhook_after_arming_kills(self, count: int) -> None:
        names = [f"worker{index}" for index in range(2, count + 2)]

        def kill_and_replace() -> None:
            self.kill_worker(self.current_worker)
            self.current_worker = names.pop(0)
            self.start_worker(self.current_worker)

        self.current_worker = "worker"
        for _ in range(count):
            self.github.after_next("GET", r"/pulls/7/files$", kill_and_replace)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")


class TerminalFailureTests(EndToEndTestCase):
    def test_failure_before_any_check_run_still_publishes_one(self) -> None:
        for _ in range(3):
            self.github.fail_next("GET", r"/pulls/7$", status=500)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open", status="failed")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")

    def test_publishing_the_failure_retries_through_a_github_outage(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        for _ in range(3):
            self.github.fail_next("GET", r"/pulls/7/files$", status=500)
        for _ in range(2):
            self.github.fail_next("PATCH", r"/check-runs/\d+$", status=503)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open", status="failed")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")


class RetryScheduleTests(EndToEndTestCase):
    settings = {
        "WEBHOOK_RETRY_BASE_SECONDS": "30",
        "WEBHOOK_RETRY_MAX_SECONDS": "900",
    }

    def seconds_until_retry(self) -> float:
        rows = self.wait_for(
            lambda: self.query(
                "SELECT extract(epoch FROM available_at - now())::float "
                "FROM webhook_deliveries WHERE status = 'pending' AND attempts = 1 "
                "AND last_error IS NOT NULL"
            ),
            description="the failure to be recorded",
        )
        return rows[0][0]

    def test_failure_backs_off(self) -> None:
        self.github.fail_next("GET", r"/pulls/7$", status=502)

        self.send_webhook("pull_request", self.pull_request_payload())

        self.assertGreater(self.seconds_until_retry(), 45)

    def test_retry_after_extends_the_delay(self) -> None:
        self.github.fail_next("GET", r"/pulls/7$", status=429, retry_after="600")

        self.send_webhook("pull_request", self.pull_request_payload())

        self.assertGreater(self.seconds_until_retry(), 500)


class DatabaseOutageTests(EndToEndTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.addCleanup(self.set_database_available, True)

    def test_idle_worker_survives_an_outage(self) -> None:
        self.set_database_available(False)
        self.wait_for(
            lambda: "database unavailable" in self.logs(),
            description="the worker to notice the outage",
        )
        self.set_database_available(True)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertIsNone(self.processes["worker"].poll())
        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")

    def test_outage_after_publishing_does_not_review_again(self) -> None:
        self.github.after_next(
            "PATCH",
            r"/check-runs/\d+$",
            lambda: self.set_database_available(False),
        )

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(
            lambda: "database unavailable" in self.logs(),
            description="the worker to notice the outage",
        )
        self.set_database_available(True)
        self.wait_for_delivery("open")

        self.assertIsNone(self.processes["worker"].poll())
        [run] = self.github.check_runs
        self.assertEqual(run["history"], ["in_progress", "success"])
