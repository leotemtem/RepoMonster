from __future__ import annotations

import json
from urllib import error as urlerror
from urllib import request as urlrequest

from harness import (
    DEFAULT_REQUIRED_CI,
    HEAD_SHA,
    INSTALLATION_ID,
    REPOSITORY_ID,
    EndToEndTestCase,
    repository_config,
)


class ReviewFlowTests(EndToEndTestCase):
    def test_gitlab_webhook_route_is_closed(self) -> None:
        request = urlrequest.Request(
            f"{self.api_url}/webhooks/gitlab",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        with self.assertRaises(urlerror.HTTPError) as raised:
            with urlrequest.urlopen(request, timeout=10):  # nosec B310 - local API.
                pass

        self.assertEqual(raised.exception.code, 404)

    def test_ready_pull_request_publishes_success(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)

        status, body = self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="ready"
        )
        self.assertEqual(status, 202)
        self.assertTrue(body["queued"])
        self.wait_for_delivery("ready")

        [run] = self.github.check_runs
        self.assertEqual(run["head_sha"], HEAD_SHA)
        self.assertEqual(run["history"], ["in_progress", "success"])
        self.assertEqual(
            self.github.token_requests[0]["repository_ids"], [REPOSITORY_ID]
        )
        self.assertEqual(
            self.query("SELECT head_sha, gate_state FROM review_runs"),
            [(HEAD_SHA, "ready_for_human_review")],
        )
        self.assertEqual(
            self.query(
                "SELECT external_id FROM repositories WHERE external_id = %s",
                str(REPOSITORY_ID),
            ),
            [(str(REPOSITORY_ID),)],
        )
        self.assertEqual(
            self.query("SELECT external_installation_id FROM provider_installations"),
            [(str(INSTALLATION_ID),)],
        )

    def test_failed_required_ci_requires_author_action(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI, conclusion="failure")

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="red")
        self.wait_for_delivery("red")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")
        self.assertEqual(
            self.query("SELECT gate_state FROM review_runs"), [("blocked",)]
        )

    def test_policy_comes_from_default_branch_not_pull_request_head(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI, conclusion="failure")
        self.github.head_files[".repomonster.yml"] = repository_config([])

        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="policy"
        )
        self.wait_for_delivery("policy")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")
        policy_reads = self.github.requests_matching("GET", r"/contents/")
        self.assertTrue(policy_reads)
        self.assertTrue(all("ref=main" in path for path in policy_reads), policy_reads)

    def test_invalid_signature_is_rejected_before_anything_is_queued(self) -> None:
        body = json.dumps(self.pull_request_payload()).encode()

        status, _ = self.post_webhook("pull_request", body, "sha256=" + "0" * 64)
        self.assertEqual(status, 401)

        status, _ = self.send_webhook(
            "pull_request", self.pull_request_payload(), secret="wrong-secret"
        )
        self.assertEqual(status, 401)

        self.assertEqual(self.query("SELECT count(*) FROM webhook_deliveries"), [(0,)])
        self.assertEqual(self.github.requests, [])

    def test_redelivery_is_processed_once(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        payload = self.pull_request_payload()

        _, first = self.send_webhook("pull_request", payload, delivery="same")
        _, second = self.send_webhook("pull_request", payload, delivery="same")
        self.assertTrue(first["queued"])
        self.assertTrue(second["duplicate"])
        self.wait_for_delivery("same")

        self.assertEqual(len(self.github.check_runs), 1)

    def test_transient_github_failure_retries_on_the_same_check_run(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.fail_next("PATCH", r"/check-runs/\d+$", status=502)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="flaky")
        self.wait_for_delivery("flaky")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "success")
        self.assertEqual(
            self.query(
                "SELECT attempts FROM webhook_deliveries WHERE delivery_id = %s",
                "flaky",
            ),
            [(2,)],
        )
