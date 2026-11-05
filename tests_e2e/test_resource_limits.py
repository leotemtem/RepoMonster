from __future__ import annotations

import http.client
import json
import threading
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from harness import (
    DEFAULT_REQUIRED_CI,
    ROOT,
    EndToEndTestCase,
    repository_config,
)


class RequestSizeTests(EndToEndTestCase):
    settings = {"MAX_WEBHOOK_BYTES": "1000", "REPOMONSTER_API_KEY": "e2e-key"}

    def post(
        self, path: str, body: bytes, authorization: str = ""
    ) -> tuple[int, bytes]:
        request = urlrequest.Request(
            f"{self.api_url}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": authorization,
            },
        )
        try:
            with urlrequest.urlopen(request, timeout=10) as response:  # nosec B310
                return response.status, response.read()
        except urlerror.HTTPError as exc:
            return exc.code, exc.read()

    def test_declared_oversized_bodies_are_rejected(self) -> None:
        body = b"x" * 2000

        self.assertEqual(self.post("/webhooks/github", body)[0], 413)
        self.assertEqual(self.post("/review", body)[0], 413)
        self.assertEqual(self.query("SELECT count(*) FROM webhook_deliveries"), [(0,)])

    def test_streamed_oversized_body_is_rejected_before_it_is_fully_read(self) -> None:
        sent = 0

        def chunks():
            nonlocal sent
            for _ in range(200):
                sent += 1
                yield b"x" * 500

        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=10)
        self.addCleanup(connection.close)
        try:
            connection.request(
                "POST",
                "/webhooks/github",
                body=chunks(),
                headers={"Content-Type": "application/json"},
                encode_chunked=True,
            )
            status = connection.getresponse().status
        except (BrokenPipeError, ConnectionResetError):
            status = 413

        self.assertEqual(status, 413)
        self.assertEqual(self.query("SELECT count(*) FROM webhook_deliveries"), [(0,)])

    def test_review_checks_credentials_before_reading_the_body(self) -> None:
        self.assertEqual(self.post("/review", b"not json", "Bearer wrong")[0], 401)
        self.assertEqual(self.post("/review", b"not json", "Bearer e2e-key")[0], 400)


class DirectReviewTests(EndToEndTestCase):
    settings = {"REPOMONSTER_API_KEY": "e2e-key"}

    def test_direct_review_returns_a_decision(self) -> None:
        payload = (ROOT / "examples" / "review_request.json").read_bytes()
        request = urlrequest.Request(
            f"{self.api_url}/review",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer e2e-key",
            },
        )
        with urlrequest.urlopen(request, timeout=20) as response:  # nosec B310
            self.assertIn("gate_state", json.loads(response.read()))


class SlowDependencyTests(EndToEndTestCase):
    settings = {"REPOMONSTER_API_KEY": "e2e-key"}
    use_model = True

    def test_slow_model_does_not_stall_the_api(self) -> None:
        holding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            holding.set()
            release.wait(30)

        self.github.after_next("POST", r"/chat/completions$", hold)
        payload = (ROOT / "examples" / "review_request.json").read_bytes()

        def direct_review() -> None:
            request = urlrequest.Request(
                f"{self.api_url}/review",
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer e2e-key",
                },
            )
            with urlrequest.urlopen(request, timeout=40):  # nosec B310
                pass

        thread = threading.Thread(target=direct_review)
        thread.start()
        self.addCleanup(thread.join)
        self.wait_for(holding.is_set, description="the model call to start")

        started = time.monotonic()
        self.assertTrue(self._api_is_up())
        status, _ = self.send_webhook("pull_request", self.pull_request_payload())
        elapsed = time.monotonic() - started

        self.assertEqual(status, 202)
        self.assertLess(elapsed, 2)
        release.set()


class ConfigLimitTests(EndToEndTestCase):
    def assert_configuration_rejected(self, message: str) -> None:
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn(message, run["output"]["summary"])
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])

    def test_yaml_aliases_are_rejected(self) -> None:
        levels = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
        levels += [
            f"a{index}: &a{index} [" + ", ".join([f"*a{index - 1}"] * 9) + "]"
            for index in range(1, 9)
        ]
        self.github.default_files[".repomonster.yml"] = "\n".join(levels)

        self.assert_configuration_rejected("must not use YAML aliases")

    def test_oversized_config_is_rejected(self) -> None:
        self.github.default_files[".repomonster.yml"] = "# padding\n" * 10_000

        self.assert_configuration_rejected("must be under")

    def test_knowledge_file_with_too_many_sections_is_rejected(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.default_files[".repomonster/project.md"] = "\n".join(
            f"# Section {index}\nText {index}." for index in range(300)
        )

        self.assert_configuration_rejected("too many sections")


class EmbeddingLimitTests(EndToEndTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.default_files[".repomonster.yml"] = repository_config([])

    def test_long_issue_is_capped_and_old_versions_are_removed(self) -> None:
        issue = self.github.issues[42]
        issue["body"] = "\n".join(f"# H{index}\nT{index}." for index in range(3000))
        issue["updated_at"] = "2026-01-01T00:00:00Z"
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="one")
        self.wait_for_delivery("one")

        issue["body"] += "\n# Later\nEdited."
        issue["updated_at"] = "2026-01-02T00:00:00Z"
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="two")
        self.wait_for_delivery("two")

        self.assertEqual(
            self.query(
                "SELECT count(*) FROM standard_documents AS document "
                "JOIN standard_chunks AS chunk ON chunk.document_id = document.id "
                "WHERE document.scope = 'task'"
            ),
            [(200,)],
        )
        self.assertEqual(
            self.query("SELECT count(*) FROM standard_documents WHERE scope = 'task'"),
            [(1,)],
        )

    def test_only_a_few_linked_issues_are_embedded(self) -> None:
        references = []
        for number in range(100, 112):
            self.github.issues[number] = {
                "id": number,
                "number": number,
                "title": f"Issue {number}",
                "body": "Text.",
            }
            references.append(f"#{number}")
        self.github.pulls[7]["body"] += "\n" + " ".join(references)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertEqual(
            self.query("SELECT count(*) FROM standard_documents WHERE scope = 'task'"),
            [(5,)],
        )


class CoalescingTests(EndToEndTestCase):
    use_model = True

    def test_burst_of_edits_costs_one_extra_review(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        holding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold() -> None:
            holding.set()
            release.wait(30)

        self.github.after_next("GET", r"/pulls/7/files$", hold)
        payload = self.pull_request_payload(action="edited")
        self.send_webhook("pull_request", payload, delivery="first")
        self.wait_for(holding.is_set, description="the first review to start")
        for index in range(5):
            self.send_webhook("pull_request", payload, delivery=f"edit-{index}")
        release.set()

        self.wait_for_delivery("first")
        self.wait_for_delivery("edit-4")

        self.assertEqual(self.github.model_calls, 2)
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM webhook_deliveries WHERE status = 'ignored'"
            ),
            [(4,)],
        )
