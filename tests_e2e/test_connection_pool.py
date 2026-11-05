from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch
from urllib import error as urlerror
from urllib import request as urlrequest

import psycopg
from harness import DEFAULT_REQUIRED_CI, EndToEndTestCase
from psycopg_pool import PoolTimeout

from review_gatekeeper.jobs import WebhookQueue
from review_gatekeeper.management import _connect, _retry_on_database_error
from review_gatekeeper.pool import build_connection_pool


class ConnectionPoolTests(EndToEndTestCase):
    settings = {
        "DATABASE_POOL_MIN_SIZE": "1",
        "DATABASE_POOL_MAX_SIZE": "2",
        "DATABASE_POOL_TIMEOUT_SECONDS": "2",
    }

    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)

    def connection_counts(self) -> dict[str, int]:
        counts = dict(
            self.query(
                "SELECT application_name, count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND application_name IN ('repomonster-api', 'repomonster-worker') "
                "GROUP BY application_name"
            )
        )
        for name, count in counts.items():
            self.assertLessEqual(count, 2, f"{name} exceeded its connection limit")
        return counts

    def test_burst_of_deliveries_stays_within_each_process_pool_maximum(self) -> None:
        numbers = list(range(10, 22))
        for number in numbers:
            self.github.add_pull_request(number)

        def send(number: int) -> tuple[int, dict]:
            return self.send_webhook(
                "pull_request", self.pull_request_payload(number), delivery=str(number)
            )

        # Hold inserts so the burst fills the API pool and queues for checkout.
        with ThreadPoolExecutor(max_workers=len(numbers)) as executor:
            with psycopg.connect(self.database_url) as connection:
                connection.execute("LOCK TABLE webhook_deliveries IN SHARE MODE")
                futures = [executor.submit(send, number) for number in numbers]
                self.wait_for(
                    lambda: self.connection_counts().get("repomonster-api") == 2,
                    description="the burst to fill the API pool",
                )
            for future in futures:
                status, body = future.result(timeout=10)
                self.assertEqual(status, 202)
                self.assertTrue(body["queued"])

        def all_completed() -> bool:
            self.connection_counts()
            return self.query(
                "SELECT count(*) FROM webhook_deliveries WHERE status = 'completed'"
            ) == [(len(numbers),)]

        self.wait_for(all_completed, description="all burst deliveries to complete")
        self.assertEqual(len(self.github.check_runs), len(numbers))
        self.assertTrue(
            all(run["conclusion"] == "success" for run in self.github.check_runs)
        )

    def test_worker_replaces_terminated_connections_before_checkout(self) -> None:
        holding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            holding.set()
            release.wait(20)

        self.github.after_next("GET", r"/pulls/7/files$", hold)
        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="restart"
        )
        self.wait_for(holding.is_set, description="the worker's provider call")
        rows = self.query(
            "SELECT pid, state FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = 'repomonster-worker'"
        )
        self.assertTrue(rows)
        self.assertTrue(all(state == "idle" for _, state in rows))
        pids = [pid for pid, _ in rows]
        self.assertTrue(
            all(
                terminated
                for (terminated,) in self.query(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid = ANY(%s)",
                    pids,
                )
            )
        )
        release.set()
        self.wait_for_delivery("restart")

        self.assertIsNone(self.processes["worker"].poll())
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])
        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(%s)", pids
            ),
            [(0,)],
        )

    def test_shutdown_closes_api_and_worker_connections(self) -> None:
        self.wait_for(
            lambda: len(self.connection_counts()) == 2,
            description="both pools to open connections",
        )
        self.stop_process("worker")
        self.stop_process("api")
        self.assertEqual(self.connection_counts(), {})

    def test_worker_rejects_a_pool_that_cannot_serve_its_heartbeat(self) -> None:
        self.stop_process("worker")
        self.environment["DATABASE_POOL_MAX_SIZE"] = "1"
        self.start_worker("invalid-worker")
        self.assertEqual(self.processes["invalid-worker"].wait(timeout=10), 1)
        self.assertIn("DATABASE_POOL_MAX_SIZE must be at least 2", self.logs())

    def test_api_starts_during_database_outage_and_readiness_recovers(self) -> None:
        self.stop_process("worker")
        self.stop_process("api")
        self.addCleanup(self.set_database_available, True)
        self.set_database_available(False)
        self.environment["DATABASE_POOL_TIMEOUT_SECONDS"] = "0.2"
        self.start_process("api", self.processes["api"].args)
        self.wait_for(self._api_is_up, description="API to start without the database")
        with self.assertRaises(urlerror.HTTPError) as failure:
            urlrequest.urlopen(f"{self.api_url}/readyz", timeout=5)
        self.assertEqual(failure.exception.code, 500)
        self.assertTrue(self._api_is_up())

        self.set_database_available(True)

        def ready() -> bool:
            try:
                with urlrequest.urlopen(
                    f"{self.api_url}/readyz", timeout=5
                ) as response:
                    body = json.loads(response.read())
                    return body["status"] == "ready" and body["standard_packs"] > 0
            except urlerror.HTTPError:
                return False

        self.wait_for(ready, description="readiness to recover")


class StatementTimeoutTests(EndToEndTestCase):
    settings = {
        "DATABASE_POOL_MAX_SIZE": "2",
        "DATABASE_STATEMENT_TIMEOUT_SECONDS": "1",
        "WEBHOOK_LOCK_TIMEOUT_SECONDS": "1",
        "WEBHOOK_RETRY_BASE_SECONDS": "2",
        "WEBHOOK_RETRY_MAX_SECONDS": "4",
    }

    def test_timed_out_statement_rolls_back_and_the_queue_retries(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        with psycopg.connect(self.database_url) as connection:
            connection.execute("LOCK TABLE provider_installations IN SHARE MODE")
            self.send_webhook(
                "pull_request", self.pull_request_payload(), delivery="lock"
            )
            self.wait_for(
                lambda: self.query(
                    "SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = 'repomonster-worker' AND wait_event_type = 'Lock'"
                ),
                description="the worker to block on the test's lock",
            )
            [(locked_at,)] = self.query(
                "SELECT locked_at FROM webhook_deliveries WHERE delivery_id = 'lock'"
            )
            self.wait_for(
                lambda: self.query(
                    "SELECT 1 FROM webhook_deliveries WHERE delivery_id = 'lock' "
                    "AND status = 'processing' AND locked_at > %s",
                    locked_at,
                ),
                description="the heartbeat to renew while the worker holds a connection",
            )
            rows = self.wait_for(
                lambda: self.query(
                    "SELECT attempts, last_error FROM webhook_deliveries "
                    "WHERE delivery_id = 'lock' AND status = 'pending'"
                ),
                description="the timeout to schedule a retry",
            )
            self.assertEqual(rows[0][0], 1)
            self.assertIn("QueryCanceled", rows[0][1])
            self.assertIn("statement timeout", rows[0][1])
            self.assertEqual(
                self.query("SELECT count(*) FROM provider_installations"), [(0,)]
            )
            self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

        self.wait_for_delivery("lock")
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(2,)])
        self.assertEqual(
            self.github.check_runs[0]["history"], ["in_progress", "success"]
        )


class PoolCheckoutTests(EndToEndTestCase):
    start_default_worker = False

    def test_pool_checkout_timeout_uses_database_outage_retry(self) -> None:
        with patch.dict(
            "os.environ",
            {
                **self.environment,
                "DATABASE_POOL_MAX_SIZE": "2",
                "DATABASE_POOL_TIMEOUT_SECONDS": "0.1",
            },
            clear=True,
        ):
            pool = build_connection_pool(self.database_url, worker=True)
        self.addCleanup(pool.close)
        pool.open(wait=True)
        queue = WebhookQueue(pool)
        queue.enqueue(
            provider="github",
            delivery_id="checkout",
            event_name="pull_request",
            payload=self.pull_request_payload(),
        )
        timed_out = threading.Event()
        stop = threading.Event()
        self.addCleanup(stop.set)

        def claim():
            try:
                return queue.claim()
            except PoolTimeout:
                timed_out.set()
                raise

        with (
            self.assertLogs(__name__, level="ERROR") as logged,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            with ExitStack() as held:
                held.enter_context(pool.connection())
                held.enter_context(pool.connection())
                future = executor.submit(
                    _retry_on_database_error, claim, stop, logging.getLogger(__name__)
                )
                self.assertTrue(timed_out.wait(5), "checkout did not time out")
            job = future.result(timeout=5)

        self.assertIn("database unavailable; retrying", logged.output[0])
        self.assertEqual(job.delivery_id, "checkout")
        self.assertEqual(job.attempts, 1)
        self.assertTrue(queue.complete(job))
        self.assertEqual(self.delivery_status("checkout"), "completed")

    def test_dedicated_management_connection_does_not_apply_statement_timeout(
        self,
    ) -> None:
        with patch.dict(
            "os.environ",
            {
                **self.environment,
                "DATABASE_CONNECT_TIMEOUT_SECONDS": "3",
                "DATABASE_STATEMENT_TIMEOUT_SECONDS": "0.05",
            },
            clear=True,
        ):
            pool = build_connection_pool(self.database_url)
            self.addCleanup(pool.close)
            pool.open(wait=True)
            with pool.connection() as pooled:
                self.assertEqual(pooled.info.get_parameters()["connect_timeout"], "3")
                self.assertEqual(
                    pooled.execute("SHOW statement_timeout").fetchone(), ("50ms",)
                )
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    pooled.execute("SELECT pg_sleep(0.1)")
                pooled.rollback()
            with _connect() as dedicated:
                self.assertEqual(
                    dedicated.info.get_parameters()["connect_timeout"], "3"
                )
                self.assertEqual(
                    dedicated.execute("SHOW statement_timeout").fetchone(), ("0",)
                )
                dedicated.execute("SELECT pg_sleep(0.1)")
