from __future__ import annotations

import json
from typing import Any

import psycopg
from harness import (
    DEFAULT_REQUIRED_CI,
    FULL_NAME,
    INSTALLATION_ID,
    REPOSITORY_ID,
    EndToEndTestCase,
    repository_config,
)


class KnowledgeCacheTests(EndToEndTestCase):
    settings = {"WEBHOOK_LOCK_TIMEOUT_SECONDS": "1"}

    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)

    def review(self, delivery: str) -> None:
        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery=delivery
        )
        self.wait_for_delivery(delivery)

    def sync_key(self) -> list[Any]:
        [(key,)] = self.query("SELECT knowledge_sync_key FROM repositories")
        return json.loads(key)

    def knowledge_reads(self) -> list[str]:
        return self.github.requests_matching("GET", r"/git/(trees|blobs)/")

    def push_payload(self, branch: str = "main", **overrides: Any) -> dict[str, Any]:
        return {
            "ref": f"refs/heads/{branch}",
            "deleted": False,
            "repository": {
                "id": REPOSITORY_ID,
                "full_name": FULL_NAME,
                "html_url": f"https://github.com/{FULL_NAME}",
                "default_branch": "main",
            },
            "installation": {"id": INSTALLATION_ID},
            **overrides,
        }

    def test_second_review_skips_tree_blobs_and_repository_sync(self) -> None:
        self.review("first")
        reads = self.knowledge_reads()
        self.assertTrue(reads)
        sync_times = self.query(
            "SELECT indexed_at FROM repository_standard_sources ORDER BY source_path"
        )
        self.review("second")

        self.assertEqual(self.knowledge_reads(), reads)
        self.assertEqual(
            self.query(
                "SELECT indexed_at FROM repository_standard_sources ORDER BY source_path"
            ),
            sync_times,
        )
        self.assertEqual(
            len(self.github.requests_matching("GET", r"/contents/\.repomonster.yml")),
            2,
        )
        self.assertEqual(len(self.github.check_runs), 2)

    def test_new_tree_resyncs_knowledge_into_the_brief(self) -> None:
        self.review("first")
        self.github.default_files[".repomonster/standards/style.md"] = (
            "# Style\n\nEvery handler must record the retry counter.\n"
        )
        self.github.tree_sha = "c" * 40
        self.review("second")

        [(brief,)] = self.query("SELECT llm_brief FROM review_runs")
        self.assertIn("Every handler must record the retry counter", brief)
        self.assertNotIn("Keep handlers small", brief)
        self.assertEqual(self.sync_key()[0], self.github.tree_sha)
        self.assertEqual(len(self.github.requests_matching("GET", r"/git/trees/")), 2)

    def test_required_ci_policy_is_read_on_a_cache_hit(self) -> None:
        self.review("first")
        reads = self.knowledge_reads()

        def move_policy() -> None:
            self.github.default_files[".repomonster.yml"] = repository_config(
                ["security"]
            )
            self.github.tree_sha = "c" * 40

        # Config can move after the branch endpoint has already returned.
        self.github.after_next("GET", r"/branches/main$", move_policy)
        self.github.set_ci("security", conclusion="failure")
        self.review("second")

        self.assertEqual(self.knowledge_reads(), reads)
        self.assertEqual(self.github.check_runs[-1]["conclusion"], "action_required")
        [(payload, gate)] = self.query(
            "SELECT request_payload, gate_state FROM review_runs"
        )
        self.assertEqual(payload["metadata"]["ci_checks"]["security"], "failure")
        self.assertEqual(gate, "blocked")

    def test_required_ci_change_on_a_new_tree_applies_to_the_next_review(self) -> None:
        self.review("first")
        self.github.default_files[".repomonster.yml"] = repository_config(["security"])
        self.github.tree_sha = "c" * 40
        self.github.set_ci("security", conclusion="failure")
        self.review("second")

        self.assertEqual(self.github.check_runs[-1]["conclusion"], "action_required")
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"), [("blocked",)]
        )
        self.assertEqual(self.sync_key()[0], self.github.tree_sha)

    def test_issue_edit_syncs_on_a_cache_hit(self) -> None:
        self.review("first")
        reads = self.knowledge_reads()
        self.github.issues[42]["body"] = "The retry must preserve the request ID."
        self.review("second")

        self.assertEqual(self.knowledge_reads(), reads)
        [(body,)] = self.query(
            "SELECT body_markdown FROM standard_documents WHERE scope = 'task' AND enabled"
        )
        self.assertIn("The retry must preserve the request ID", body)

    def test_default_branch_push_refreshes_without_check_runs(self) -> None:
        self.review("first")
        self.github.default_files[".repomonster/project.md"] = (
            "# Project\n\nUpdated API."
        )
        self.github.tree_sha = "c" * 40
        checks = list(self.github.requests_matching("POST", r"/check-runs"))
        updates = list(self.github.requests_matching("PATCH", r"/check-runs"))
        _, response = self.send_webhook("push", self.push_payload(), delivery="push")
        self.assertTrue(response["queued"])
        self.wait_for_delivery("push")

        [(body,)] = self.query(
            "SELECT body_markdown FROM standard_documents "
            "WHERE scope = 'repo' AND source_path = '.repomonster/project.md' AND enabled"
        )
        self.assertIn("Updated API", body)
        self.assertEqual(self.sync_key()[0], self.github.tree_sha)
        self.assertEqual(self.github.requests_matching("POST", r"/check-runs"), checks)
        self.assertEqual(
            self.github.requests_matching("PATCH", r"/check-runs"), updates
        )

        reads = list(self.github.requests)
        self.send_webhook("push", self.push_payload(), delivery="cached-push")
        self.wait_for_delivery("cached-push")
        self.assertEqual(
            self.knowledge_reads(),
            [path for method, path in reads if method == "GET" and "/git/" in path],
        )

    def test_other_branch_and_deleted_pushes_are_not_queued(self) -> None:
        for payload in (self.push_payload("feature"), self.push_payload(deleted=True)):
            status, response = self.send_webhook("push", payload)
            self.assertEqual(status, 202)
            self.assertFalse(response["queued"])
        self.assertEqual(self.query("SELECT count(*) FROM webhook_deliveries"), [(0,)])
        self.assertEqual(self.github.requests, [])

    def test_push_failure_never_publishes_a_check(self) -> None:
        self.review("first")
        requests = list(self.github.requests_matching("GET", r"/check-runs"))
        self.github.tree_sha = "c" * 40
        for _ in range(3):
            self.github.fail_next("GET", r"/git/trees/", status=500)
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push", status="failed")

        self.assertEqual(len(self.github.check_runs), 1)
        self.assertEqual(self.github.requests_matching("GET", r"/check-runs"), requests)
        self.assertEqual(len(self.github.requests_matching("POST", r"/check-runs")), 1)
        self.assertEqual(len(self.github.requests_matching("PATCH", r"/check-runs")), 1)

    def test_push_onboards_a_repository_with_policy(self) -> None:
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push")

        self.assertEqual(self.sync_key()[0], self.github.tree_sha)
        self.assertEqual(self.github.check_runs, [])
        self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

    def test_unconfigured_repository_push_is_ignored(self) -> None:
        del self.github.default_files[".repomonster.yml"]
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push", status="ignored")

        self.assertEqual(self.query("SELECT count(*) FROM repositories"), [(0,)])
        self.assertEqual(self.github.check_runs, [])

    def test_invalid_push_policy_preserves_the_cache_and_is_ignored(self) -> None:
        self.review("first")
        old_key = self.sync_key()
        self.github.default_files[".repomonster.yml"] = "checks: [invalid]"
        self.github.tree_sha = "c" * 40
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push", status="ignored")

        self.assertEqual(self.sync_key(), old_key)
        self.assertEqual(len(self.github.check_runs), 1)

    def test_disabled_repository_push_is_ignored(self) -> None:
        self.review("first")
        self.run_sql("UPDATE repositories SET enabled = false")
        requests = list(self.github.requests)
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push", status="ignored")
        self.assertEqual(self.github.requests, requests)
        self.assertEqual(self.query("SELECT enabled FROM repositories"), [(False,)])

    def test_disabled_installation_push_is_ignored(self) -> None:
        self.review("first")
        self.run_sql("UPDATE provider_installations SET enabled = false")
        requests = list(self.github.requests)
        self.send_webhook("push", self.push_payload(), delivery="push")
        self.wait_for_delivery("push", status="ignored")
        self.assertEqual(self.github.requests, requests)
        self.assertEqual(
            self.query("SELECT enabled FROM provider_installations"), [(False,)]
        )

    def test_embedding_model_change_forces_resync(self) -> None:
        self.review("first")
        self.stop_process("worker")
        self.environment["EMBEDDING_MODEL"] = "new-embedding-model"
        self.start_worker("worker2")
        self.review("second")

        self.assertEqual(self.sync_key()[1], "new-embedding-model")
        self.assertEqual(len(self.github.requests_matching("GET", r"/git/trees/")), 2)
        self.assertEqual(
            self.query(
                "SELECT DISTINCT chunk.embedding_model FROM standard_chunks chunk "
                "JOIN standard_documents doc ON doc.id = chunk.document_id "
                "WHERE doc.scope = 'repo' AND doc.enabled"
            ),
            [("new-embedding-model",)],
        )

    def test_chunk_limit_change_forces_resync(self) -> None:
        self.review("first")
        self.stop_process("worker")
        self.environment["KNOWLEDGE_MAX_CHUNKS_PER_SOURCE"] = "100"
        self.start_worker("worker2")
        self.review("second")

        self.assertEqual(self.sync_key()[3], 100)
        self.assertEqual(len(self.github.requests_matching("GET", r"/git/trees/")), 2)

    def test_branch_move_leaves_the_older_key_and_forces_another_sync(self) -> None:
        old_sha = self.github.tree_sha

        def move_branch() -> None:
            self.github.tree_sha = "c" * 40
            self.github.default_files[".repomonster/project.md"] = "# New project"

        self.github.after_next("GET", r"/branches/main$", move_branch)
        self.review("first")
        self.assertEqual(self.sync_key()[0], old_sha)
        self.review("second")
        self.assertEqual(self.sync_key()[0], self.github.tree_sha)
        self.assertEqual(len(self.github.requests_matching("GET", r"/git/trees/")), 2)

    def test_crash_during_sync_rolls_back_the_key_and_knowledge(self) -> None:
        self.review("first")
        old_key = self.sync_key()
        self.github.tree_sha = "c" * 40
        self.github.default_files[".repomonster/project.md"] = "# Uncommitted project"
        self.run_sql(
            """
            CREATE FUNCTION pause_knowledge_sync() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock(8042);
                RETURN NEW;
            END $$;
            CREATE TRIGGER pause_knowledge_sync AFTER UPDATE OF knowledge_sync_key
            ON repositories FOR EACH ROW EXECUTE FUNCTION pause_knowledge_sync();
            """
        )
        with psycopg.connect(self.database_url, autocommit=True) as blocker:
            blocker.execute("SELECT pg_advisory_lock(8042)")
            self.send_webhook(
                "pull_request", self.pull_request_payload(), delivery="crash"
            )
            self.wait_for(
                lambda: self.query(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                ),
                description="the sync transaction to pause after writing its key",
            )
            self.kill_worker()
            blocker.execute("SELECT pg_advisory_unlock(8042)")
        self.wait_for(
            lambda: (
                not self.query(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objid = 8042"
                )
            ),
            description="the crashed sync transaction to roll back",
        )
        self.assertEqual(self.sync_key(), old_key)
        self.assertEqual(
            self.query(
                "SELECT body_markdown FROM standard_documents "
                "WHERE source_path = '.repomonster/project.md' AND enabled"
            ),
            [("# Project\n\nAn API service.\n",)],
        )
        self.run_sql("DROP TRIGGER pause_knowledge_sync ON repositories")
        self.start_worker("worker2")
        self.wait_for_delivery("crash")
        self.assertEqual(self.sync_key()[0], self.github.tree_sha)
