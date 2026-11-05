from __future__ import annotations

import os
import signal
import threading

from harness import (
    DEFAULT_REQUIRED_CI,
    FULL_NAME,
    INSTALLATION_ID,
    EndToEndTestCase,
)

from review_gatekeeper.jobs import LostJobLock, WebhookQueue


class FrozenWorkerTests(EndToEndTestCase):
    settings = {"WEBHOOK_LOCK_TIMEOUT_SECONDS": "1"}

    def test_worker_that_lost_its_lock_cannot_overwrite_a_newer_result(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)

        def freeze_and_invalidate_the_evidence() -> None:
            os.kill(self.processes["worker"].pid, signal.SIGSTOP)
            self.github.pulls[7]["body"] = "Fixes #42"

        self.github.after_next(
            "GET", r"/pulls/7/files$", freeze_and_invalidate_the_evidence
        )
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(
            lambda: self.github.pulls[7]["body"] == "Fixes #42",
            description="the worker to freeze",
        )

        self.start_worker("worker2")
        self.wait_for_delivery("open")
        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")

        os.kill(self.processes["worker"].pid, signal.SIGCONT)
        self.wait_for(
            lambda: "lost its lock" in self.logs(),
            description="the first worker to stop",
        )

        [run] = self.github.check_runs
        self.assertEqual(run["history"], ["in_progress", "action_required"])
        self.assertEqual(self.delivery_status("open"), "completed")
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"), [("blocked",)]
        )

    def test_heartbeat_keeps_a_slow_job_with_its_worker(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.start_worker("worker2")
        self.github.after_next(
            "GET", r"/pulls/7/files$", lambda: threading.Event().wait(3)
        )

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])
        [run] = self.github.check_runs
        self.assertEqual(run["history"], ["in_progress", "success"])


class SerializationTests(EndToEndTestCase):
    def test_one_repository_runs_one_job_at_a_time(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.add_pull_request(8)
        self.start_worker("worker2")
        holding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            holding.set()
            release.wait(30)

        self.github.after_next("GET", r"/pulls/\d+/files$", hold)
        self.send_webhook("pull_request", self.pull_request_payload(7), delivery="one")
        self.wait_for(holding.is_set, description="the first job to start")
        self.send_webhook("pull_request", self.pull_request_payload(8), delivery="two")

        threading.Event().wait(1)
        self.assertEqual(self.delivery_status("two"), "pending")

        release.set()
        self.wait_for_delivery("one")
        self.wait_for_delivery("two")

    def test_embedding_does_not_hold_a_transaction_open(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        holding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            holding.set()
            release.wait(30)

        self.github.after_next("POST", r"/embeddings$", hold)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(holding.is_set, description="the embedding call to start")

        self.assertEqual(
            self.query(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            ),
            [(0,)],
        )

        release.set()
        self.wait_for_delivery("open")


class QueueOwnershipTests(EndToEndTestCase):
    start_default_worker = False

    def setUp(self) -> None:
        super().setUp()
        self.queue = WebhookQueue(
            self.database_url, max_attempts=3, lock_timeout_seconds=900
        )

    def enqueue(self, delivery: str, repository_id: int = 1) -> None:
        self.queue.enqueue(
            provider="github",
            delivery_id=delivery,
            event_name="pull_request",
            payload={
                "repository": {"id": repository_id, "full_name": FULL_NAME},
                "installation": {"id": INSTALLATION_ID},
            },
        )

    def claim_concurrently(self, claimers: int) -> list[object]:
        barrier = threading.Barrier(claimers)
        claimed: list[object] = []
        lock = threading.Lock()

        def claim() -> None:
            barrier.wait()
            job = self.queue.claim()
            if job is not None:
                with lock:
                    claimed.append(job)

        threads = [threading.Thread(target=claim) for _ in range(claimers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return claimed

    def test_concurrent_claims_take_one_job_per_repository(self) -> None:
        for index in range(5):
            self.enqueue(f"same-{index}")

        self.assertEqual(len(self.claim_concurrently(12)), 1)

    def test_different_repositories_run_in_parallel(self) -> None:
        self.enqueue("first", repository_id=1)
        self.enqueue("waiting", repository_id=1)
        self.enqueue("other", repository_id=2)

        self.assertEqual(self.queue.claim().delivery_id, "first")
        self.assertEqual(self.queue.claim().delivery_id, "other")

    def test_previous_owner_cannot_change_a_reclaimed_job(self) -> None:
        self.enqueue("job")
        stale = self.queue.claim()
        self.query(
            "UPDATE webhook_deliveries SET locked_at = now() - interval '1 hour' "
            "RETURNING id"
        )
        current = self.queue.claim()
        self.assertNotEqual(stale.lock_token, current.lock_token)

        self.assertFalse(self.queue.complete(stale))
        self.queue.fail(stale, RuntimeError("late failure"))
        self.queue.mark_failed(stale)
        self.assertFalse(self.queue.heartbeat(stale))
        with self.assertRaises(LostJobLock):
            self.queue.verify_lock(stale)
        with self.assertRaises(LostJobLock):
            self.queue.set_external_result_id(stale, "1")

        self.assertEqual(self.delivery_status("job"), "processing")
        self.assertTrue(self.queue.complete(current))
        self.assertEqual(self.delivery_status("job"), "completed")

    def test_schema_rejects_a_second_running_job(self) -> None:
        self.enqueue("first")
        self.enqueue("second")
        self.assertIsNotNone(self.queue.claim())

        with self.assertRaisesRegex(Exception, "one_running_per_key"):
            self.query(
                "UPDATE webhook_deliveries "
                "SET status = 'processing', locked_at = now(), "
                "lock_token = gen_random_uuid() "
                "WHERE delivery_id = %s RETURNING id",
                "second",
            )
