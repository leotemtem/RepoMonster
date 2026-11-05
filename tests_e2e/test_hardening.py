from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import psycopg
from harness import (
    DEFAULT_REQUIRED_CI,
    INSTALLATION_ID,
    REPOSITORY_ID,
    ROOT,
    EndToEndTestCase,
    repository_config,
)

from review_gatekeeper.github import GitHubClient, GitHubSettings


def model_response(
    findings: list[dict] | None = None, finish_reason: str = "stop"
) -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": json.dumps(
                        {
                            "findings": findings or [],
                            "recommendation": "ready",
                            "recommendation_reason": "Review complete.",
                        }
                    )
                },
            }
        ]
    }


class HardeningTests(EndToEndTestCase):
    use_model = True
    settings = {"REPOMONSTER_API_KEY": "e2e-key"}

    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.model_response = model_response()

    def review(self, delivery: str = "open") -> dict:
        status, _ = self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery=delivery
        )
        self.assertEqual(status, 202)
        self.wait_for_delivery(delivery)
        return self.github.check_runs[-1]

    def command(
        self, *arguments: str, environment: dict | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "review_gatekeeper.management", *arguments],
            env=environment or self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_terminal_failure_publishes_a_stable_code(self) -> None:
        for _ in range(3):
            self.github.fail_next("GET", r"/pulls/7/files$", status=500)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open", "failed")

        [run] = self.github.check_runs
        output = json.dumps(run["output"])
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn("github_api_error", output)
        self.assertIn("worker logs", output)
        for internal in ("/pulls/7/files", "500", "injected failure", "Traceback"):
            self.assertNotIn(internal, output)
        [(error,)] = self.query("SELECT last_error FROM webhook_deliveries")
        self.assertIn("/pulls/7/files", error)
        self.assertIn(error, self.logs())

    def test_insight_failure_publishes_only_its_type(self) -> None:
        self.github.model_response = None
        run = self.review()
        self.assertEqual(run["conclusion"], "action_required")
        [(detail,)] = self.query(
            "SELECT detail FROM review_findings WHERE category = 'insight_provider'"
        )
        self.assertEqual(detail, "HTTPError")
        self.assertNotIn("500", json.dumps(run["output"]))
        self.assertIn("HTTP Error 500", self.logs())

    def test_findings_and_title_evidence_render_as_text(self) -> None:
        text = "[Reference](https://example.test/info) ![Image](https://example.test/image) <img src=x>"
        self.github.pulls[7]["title"] = text
        self.github.pulls[7]["body"] = "Please review."
        self.github.model_response = model_response(
            [
                {
                    "severity": "warning",
                    "impact": "significant",
                    "category": "clarity",
                    "title": text,
                    "detail": text,
                    "evidence": [text],
                    "rule_id": "clarity",
                }
            ]
        )
        self.github.model_response["choices"][0]["message"]["content"] = json.dumps(
            {
                "findings": json.loads(
                    self.github.model_response["choices"][0]["message"]["content"]
                )["findings"],
                "recommendation": "request_changes",
                "recommendation_reason": text,
            }
        )
        run = self.review()
        self.assertIn(r"\[Reference\]", run["output"]["text"])
        self.assertIn("&lt;img src=x&gt;", run["output"]["text"])
        self.assertNotIn("[Reference]", run["output"]["text"])
        self.assertNotIn("example.test", run["output"]["summary"])
        self.assertNotIn(text, run["output"]["summary"])

    def test_direct_review_omits_the_model_brief(self) -> None:
        self.review()
        request = urlrequest.Request(
            f"{self.api_url}/review",
            data=(ROOT / "examples/review_request.json").read_bytes(),
            headers={
                "Authorization": "Bearer e2e-key",
                "Content-Type": "application/json",
            },
        )
        with urlrequest.urlopen(request, timeout=20) as response:  # nosec B310
            result = json.loads(response.read())
        self.assertIn("gate_state", result)
        self.assertNotIn("llm_review_brief", result)

    def test_request_storage_omits_diff_excerpts(self) -> None:
        self.review()
        [(payload,)] = self.query("SELECT request_payload FROM review_runs")
        self.assertEqual(payload["title"], self.github.pulls[7]["title"])
        self.assertEqual(payload["metadata"]["ci_checks"], {"lint": "success"})
        for stored, original in zip(
            payload["changed_files"], self.github.pull_files[7], strict=True
        ):
            self.assertNotIn("diff_excerpt", stored)
            self.assertEqual(stored["path"], original["filename"])
            self.assertEqual(stored["additions"], original["additions"])

    def test_webhook_and_model_text_are_storable(self) -> None:
        text = "Before\x00\ud800After\udfff"
        payload = self.pull_request_payload()
        payload["extra\x00\ud800"] = {"nested": [text]}
        self.github.pulls[7]["title"] = "Before\x00After"
        self.github.model_response = model_response(
            [
                {
                    "severity": "warning",
                    "impact": "significant",
                    "category": text,
                    "title": text,
                    "detail": text,
                    "evidence": [text],
                    "rule_id": text,
                }
            ]
        )
        status, _ = self.send_webhook("pull_request", payload, delivery="open")
        self.assertEqual(status, 202)
        self.wait_for_delivery("open")
        [(stored,)] = self.query("SELECT payload FROM webhook_deliveries")
        self.assertEqual(stored["extra"], {"nested": ["BeforeAfter"]})
        [(category, title, detail, evidence, rule_id)] = self.query(
            "SELECT category, title, detail, evidence, rule_id FROM review_findings WHERE title = 'BeforeAfter'"
        )
        self.assertEqual(
            (category, title, detail, evidence, rule_id),
            (
                "BeforeAfter",
                "BeforeAfter",
                "BeforeAfter",
                ["BeforeAfter"],
                "BeforeAfter",
            ),
        )
        [(stored, brief)] = self.query(
            "SELECT request_payload, llm_brief FROM review_runs"
        )
        self.assertEqual(stored["title"], "BeforeAfter")
        self.assertNotIn("\x00", brief)
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])

    def test_only_complete_model_responses_are_used(self) -> None:
        for reason in ("length", "content_filter", "tool_calls", None):
            with self.subTest(reason=reason):
                self.github.model_response = model_response(finish_reason=reason)
                run = self.review(f"finish-{reason}")
                self.assertEqual(run["conclusion"], "action_required")
                self.assertEqual(
                    self.query("SELECT gate_state FROM review_runs"),
                    [("manual_escalation",)],
                )
                self.assertEqual(
                    self.query(
                        "SELECT detail FROM review_findings WHERE category = 'insight_provider'"
                    ),
                    [("InsightResponseError",)],
                )

    def test_non_ascii_credentials_return_unauthorized(self) -> None:
        status, _ = self.post_webhook("pull_request", b"{}", "sha256=\xe9")
        self.assertEqual(status, 401)
        request = urlrequest.Request(
            f"{self.api_url}/review",
            data=b"{}",
            headers={"Authorization": "Bearer \xe9"},
        )
        with self.assertRaises(urlerror.HTTPError) as failure:
            urlrequest.urlopen(request, timeout=10)  # nosec B310
        self.assertEqual(failure.exception.code, 401)

    def test_expired_tokens_are_removed_and_permissions_are_scoped(self) -> None:
        client = GitHubClient(
            GitHubSettings(
                app_id=self.environment["GITHUB_APP_ID"],
                private_key_path=Path(self.environment["GITHUB_PRIVATE_KEY_PATH"]),
                webhook_secret="unused",
                api_url=self.github.url,
            )
        )
        client._installation_tokens[(1, 2)] = (
            "old",
            datetime.now(UTC) - timedelta(minutes=1),
        )
        self.assertEqual(
            client.installation_token(INSTALLATION_ID, REPOSITORY_ID), "ghs_e2e"
        )
        self.assertNotIn((1, 2), client._installation_tokens)
        client.installation_token(INSTALLATION_ID, REPOSITORY_ID)
        self.assertEqual(len(self.github.token_requests), 1)
        self.assertEqual(
            self.github.token_requests[0]["permissions"]["contents"], "read"
        )

    def test_repository_and_installation_disablement_is_durable(self) -> None:
        self.review()
        self.stop_process("worker")
        for table in ("repositories", "provider_installations"):
            with self.subTest(table=table):
                self.run_sql(f"UPDATE {table} SET enabled = false")
                # Operator syncs refresh metadata without changing enablement.
                result = self.command(
                    "repository",
                    "sync-local",
                    str(self.local_checkout()),
                    "--provider",
                    "github",
                    "--provider-base-url",
                    "https://github.com",
                    "--external-id",
                    str(REPOSITORY_ID),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.query(f"SELECT enabled FROM {table}"), [(False,)])
                self.start_worker()
                for event, payload in (
                    ("pull_request", self.pull_request_payload()),
                    ("check_run", self.check_run_event("lint")),
                ):
                    delivery = f"disabled-{table}-{event}"
                    self.send_webhook(event, payload, delivery=delivery)
                    self.wait_for_delivery(delivery, "ignored")
                self.stop_process("worker")
                self.assertEqual(len(self.github.check_runs), 1)
                self.assertEqual(self.github.model_calls, 1)
                self.run_sql(f"UPDATE {table} SET enabled = true")

    def local_checkout(self) -> Path:
        checkout = self.workspace / "checkout"
        checkout.mkdir(exist_ok=True)
        (checkout / ".repomonster.yml").write_text(
            json.dumps(
                {
                    "version": 1,
                    "knowledge": {"description": ["*.md", "linked/*.md"]},
                    "stacks": json.loads(repository_config([]))["stacks"],
                }
            )
        )
        (checkout / "project.md").write_text("Local project guidance.")
        return checkout

    def test_installation_sync_preserves_disablement(self) -> None:
        self.review()
        self.github.after_next(
            "GET",
            r"/pulls/7/files$",
            lambda: self.run_sql("UPDATE provider_installations SET enabled = false"),
        )
        self.review("metadata")
        self.assertEqual(
            self.query("SELECT enabled FROM provider_installations"), [(False,)]
        )

    def test_disabled_repository_ignores_a_timer_without_a_repository_url(self) -> None:
        self.review()
        self.stop_process("worker")
        self.query(
            "UPDATE repositories SET provider_base_url = %s, enabled = false RETURNING id",
            self.github.url,
        )
        payload = self.pull_request_payload()
        payload["repository"].pop("html_url")
        payload["head_sha"] = payload["pull_request"]["head"]["sha"]
        payload["check_run_id"] = self.github.check_runs[0]["id"]
        self.query(
            "INSERT INTO webhook_deliveries (provider, delivery_id, event_name, payload) VALUES ('github', 'timer', 'repomonster_ci_wait', %s::jsonb) RETURNING id",
            json.dumps(payload),
        )
        requests = len(self.github.requests)
        self.start_worker()
        self.wait_for_delivery("timer", "ignored")
        self.assertEqual(len(self.github.requests), requests)
        self.assertEqual(self.github.model_calls, 1)

    def test_local_sync_keeps_sources_inside_the_checkout(self) -> None:
        checkout = self.local_checkout()
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "other.md").write_text("External guidance.")
        (checkout / "other.md").symlink_to(outside / "other.md")
        (checkout / "linked").symlink_to(outside, target_is_directory=True)
        result = self.command(
            "repository",
            "sync-local",
            str(checkout),
            "--provider",
            "github",
            "--provider-base-url",
            "https://github.com",
            "--external-id",
            str(REPOSITORY_ID),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.query(
                "SELECT source_path FROM standard_documents WHERE scope = 'repo'"
            ),
            [("project.md",)],
        )

    def test_retrieval_uses_the_active_embedding_model(self) -> None:
        self.run_sql(
            "UPDATE standard_chunks SET embedding_model = 'previous-model', content = 'Previous model guidance.'"
        )
        self.review()
        [brief] = self.github.model_briefs
        self.assertNotIn("Previous model guidance.", brief)
        self.assertIn("Keep handlers small", brief)

    def test_current_task_version_replaces_other_enabled_versions(self) -> None:
        self.review()
        self.run_sql("""
            UPDATE standard_documents SET enabled = false WHERE scope = 'task';
            INSERT INTO standard_documents (
                id, logical_key, version, scope, repository_key, task_key, stack_key,
                title, body_markdown, content_checksum
            ) SELECT id || '-alternate', logical_key, 'alternate', scope, repository_key,
                     task_key, stack_key, title, body_markdown, content_checksum
              FROM standard_documents WHERE scope = 'task'
        """)
        self.review("again")
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM standard_documents WHERE scope = 'task' AND enabled"
            ),
            [(1,)],
        )
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM standard_documents WHERE scope = 'task' AND enabled AND version = 'alternate'"
            ),
            [(0,)],
        )

    def test_document_constraints_allow_public_pack_versions(self) -> None:
        self.review()
        for scope in ("repo", "task", "public"):
            statement = """
                INSERT INTO standard_documents (
                    id, logical_key, version, scope, repository_key, task_key, stack_key,
                    title, body_markdown, content_checksum, pack_id, pack_version
                ) SELECT id || '-copy', logical_key, version || '-copy', scope, repository_key,
                         task_key, stack_key, title, body_markdown, content_checksum, pack_id, pack_version
                  FROM standard_documents WHERE scope = %s AND enabled LIMIT 1
            """
            with (
                self.subTest(scope=scope),
                psycopg.connect(self.database_url) as connection,
            ):
                if scope == "public":
                    connection.execute(statement, (scope,))
                else:
                    with self.assertRaises(psycopg.errors.UniqueViolation):
                        connection.execute(statement, (scope,))
                    connection.rollback()

    def test_migrations_explain_dimensions_and_missing_files(self) -> None:
        result = self.command(
            "db",
            "migrate",
            environment={**self.environment, "EMBEDDING_DIMENSIONS": "16"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs recreating or reindexing", result.stderr)
        self.assertNotIn("has been modified", result.stderr)
        migrations = self.workspace / "db/migrations"
        migrations.parent.mkdir()
        shutil.copytree(ROOT / "db/migrations", migrations)
        (migrations / "005_constraints.sql").unlink()
        result = self.command(
            "db",
            "migrate",
            environment={**self.environment, "REPOMONSTER_ROOT": str(self.workspace)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing from disk: 005", result.stderr)

    def test_prune_preserves_active_and_recent_records(self) -> None:
        self.review()
        self.stop_process("worker")
        self.run_sql("""
            INSERT INTO webhook_deliveries (provider, delivery_id, event_name, payload, status,
                                            updated_at, locked_at, lock_token)
            SELECT 'github', state, 'pull_request', '{}', state, now() - interval '100 days',
                   CASE WHEN state = 'processing' THEN now() END,
                   CASE WHEN state = 'processing' THEN '00000000-0000-0000-0000-000000000001'::uuid END
            FROM unnest(ARRAY['pending','processing','completed','failed','ignored']) AS state;
            UPDATE review_runs SET created_at = now() - interval '100 days';
        """)
        result = self.command("prune")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 deliveries, 1 reviews", result.stdout)
        self.assertEqual(
            self.query(
                "SELECT delivery_id FROM webhook_deliveries ORDER BY delivery_id"
            ),
            [("open",), ("pending",), ("processing",)],
        )
        self.assertEqual(self.query("SELECT count(*) FROM review_findings"), [(0,)])
        result = self.command("prune", "--days", "0")
        self.assertNotEqual(result.returncode, 0)
        self.run_sql(
            "UPDATE webhook_deliveries SET updated_at = now() - interval '5 days' WHERE delivery_id = 'open'"
        )
        result = self.command(
            "prune", environment={**self.environment, "RETENTION_DAYS": "3"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.delivery_status("open"))

    def test_shutdown_returns_work_before_embedding_or_model(self) -> None:
        for index, endpoint in enumerate((r"/pulls/7/files$", r"/embeddings$")):
            with self.subTest(endpoint=endpoint):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)

                def hold(entered=entered, release=release) -> None:
                    entered.set()
                    release.wait(20)

                method = "GET" if index == 0 else "POST"
                self.github.after_next(method, endpoint, hold)
                delivery = f"shutdown-{index}"
                self.send_webhook(
                    "pull_request", self.pull_request_payload(), delivery=delivery
                )
                self.wait_for(entered.is_set, description="the review step to start")
                self.processes["worker"].terminate()
                release.set()
                self.processes["worker"].wait(timeout=10)
                self.assertEqual(self.processes["worker"].returncode, 0)
                self.assertEqual(
                    self.query(
                        "SELECT status, attempts, locked_at, lock_token, available_at <= now() FROM webhook_deliveries WHERE delivery_id = %s",
                        delivery,
                    ),
                    [("pending", 0, None, None, True)],
                )
                self.assertEqual(self.github.model_calls, 0)
                self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])
                if index == 0:
                    self.assertEqual(self.github.embedding_inputs, [])
                # Remove this fixture's pending job so the next case starts independently.
                self.query(
                    "DELETE FROM webhook_deliveries WHERE delivery_id = %s RETURNING id",
                    delivery,
                )
                self.start_worker()

    def test_shutdown_finishes_an_active_model_call(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            entered.set()
            release.wait(20)

        self.github.after_next("POST", r"/chat/completions$", hold)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for(entered.is_set, description="the model call to start")
        self.processes["worker"].terminate()
        release.set()
        self.processes["worker"].wait(timeout=10)
        self.assertEqual(self.processes["worker"].returncode, 0)
        self.assertEqual(self.delivery_status("open"), "completed")
        self.assertEqual(self.github.model_calls, 1)
        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")

    def test_shutdown_before_the_model_returns_the_job(self) -> None:
        self.review()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            entered.set()
            release.wait(20)

        # Current knowledge is reused, leaving only the retrieval embedding call.
        self.github.after_next("POST", r"/embeddings$", hold)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="next")
        self.wait_for(entered.is_set, description="retrieval to start")
        self.processes["worker"].terminate()
        release.set()
        self.processes["worker"].wait(timeout=10)
        self.assertEqual(self.processes["worker"].returncode, 0)
        self.assertEqual(
            self.query(
                "SELECT status, attempts FROM webhook_deliveries WHERE delivery_id = 'next'"
            ),
            [("pending", 0)],
        )
        self.assertEqual(self.github.model_calls, 1)
        self.assertEqual(self.github.check_runs[-1]["status"], "in_progress")
        self.start_worker()
        self.wait_for_delivery("next")
        self.assertEqual(self.github.model_calls, 2)
        self.assertEqual(self.github.check_runs[-1]["conclusion"], "success")


class StoredEvidenceTests(EndToEndTestCase):
    use_model = True

    def test_stored_brief_omits_diffs_and_issue_text_is_storable(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.issues[42]["title"] = "Dropped\x00 requests"
        self.github.issues[42]["body"] = "Body with \x00 NUL."
        self.github.pull_files[7][0]["patch"] = "+stored_patch_marker = 1"

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertIn("stored_patch_marker", self.github.model_briefs[0])
        [(brief,)] = self.query("SELECT llm_brief FROM review_runs")
        self.assertNotIn("stored_patch_marker", brief)
        self.assertIn("[diff not stored]", brief)
        self.assertEqual(
            self.query("SELECT attempts FROM webhook_deliveries"),
            [(1,)],
        )
