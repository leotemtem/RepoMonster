from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from harness import EMBEDDING_DIMENSIONS, FULL_NAME, EndToEndTestCase

from review_gatekeeper.repository import OpenAICompatibleEmbeddingProvider


class HTTPClientTests(EndToEndTestCase):
    settings = {"HTTP_MAX_RETRIES": "2"}

    def test_transient_get_retries_within_one_queue_attempt(self) -> None:
        self.github.set_ci()
        self.github.fail_next("GET", r"/pulls/7$", status=502)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="get")
        self.wait_for_delivery("get")
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])
        self.assertEqual(len(self.github.requests_matching("GET", r"/pulls/7$")), 2)
        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")

    def test_check_creation_retries_only_through_queue(self) -> None:
        self.github.set_ci()
        # Hold queue retry long enough to inspect the first attempt independently.
        self.stop_process("worker")
        self.environment["WEBHOOK_RETRY_BASE_SECONDS"] = "2"
        self.start_worker("retry-worker")
        self.github.fail_next("POST", r"/check-runs$", status=502)
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="post")
        self.wait_for(
            lambda: self.query(
                "SELECT 1 FROM webhook_deliveries WHERE delivery_id = 'post' "
                "AND attempts = 1 AND status = 'pending' AND last_error IS NOT NULL"
            ),
            description="queue retry after Check Run creation failure",
        )
        self.assertEqual(len(self.github.requests_matching("POST", r"/check-runs$")), 1)
        self.assertEqual(self.github.check_runs, [])
        self.wait_for_delivery("post")
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(2,)])
        self.assertEqual(len(self.github.requests_matching("POST", r"/check-runs$")), 2)
        self.assertEqual(len(self.github.check_runs), 1)

    def test_redirect_is_rejected_without_sending_credentials_to_target(self) -> None:
        received: list[str | None] = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        self.github.redirects[f"/repos/{FULL_NAME}/pulls/7"] = (
            f"http://127.0.0.1:{target.server_address[1]}/stolen"
        )
        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="redirect"
        )
        self.wait_for_delivery("redirect", status="failed")
        self.assertEqual(received, [])
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(3,)])
        self.assertEqual(len(self.github.requests_matching("GET", r"/pulls/7$")), 3)
        self.assertIn(
            "returned 302",
            self.query("SELECT last_error FROM webhook_deliveries")[0][0],
        )

    def test_embedding_requests_are_batched_in_input_order(self) -> None:
        with patch.dict(self.environment, {"EMBEDDING_BATCH_SIZE": "2"}):
            with patch.dict("os.environ", self.environment, clear=True):
                provider = OpenAICompatibleEmbeddingProvider.from_environment()
        texts = [f"text {index}" for index in range(5)]
        original = self.github._embeddings

        def out_of_order(payload):
            body = original(payload)
            body["data"].reverse()
            return body

        with patch.object(self.github, "_embeddings", side_effect=out_of_order):
            vectors = provider.embed(texts)
        self.assertEqual(
            self.github.embedding_inputs, [texts[:2], texts[2:4], texts[4:]]
        )
        expected = self.github._embeddings({"input": texts})["data"]
        self.assertEqual(vectors, [item["embedding"] for item in expected])
        self.assertTrue(all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors))

    def test_embedding_dimensions_checked_in_every_batch(self) -> None:
        provider = OpenAICompatibleEmbeddingProvider(
            self.github.url,
            "e2e-embedding",
            dimensions=EMBEDDING_DIMENSIONS,
            batch_size=2,
        )
        original = self.github._embeddings
        calls = 0

        def wrong_dimension_in_second_batch(payload):
            nonlocal calls
            calls += 1
            body = original(payload)
            if calls == 2:
                body["data"][0]["embedding"].pop()
            return body

        with patch.object(
            self.github, "_embeddings", side_effect=wrong_dimension_in_second_batch
        ):
            with self.assertRaisesRegex(ValueError, "dimensions; expected"):
                provider.embed(["one", "two", "three"])
        self.assertEqual(calls, 2)
