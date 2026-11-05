from __future__ import annotations

import json

from harness import (
    DEFAULT_REQUIRED_CI,
    FULL_NAME,
    HEAD_SHA,
    READY_BODY,
    ROOT,
    EndToEndTestCase,
    repository_config,
)


def check_result(state: str, name: str = DEFAULT_REQUIRED_CI) -> dict[str, str | None]:
    return {
        "name": name,
        "status": "in_progress" if state == "pending" else "completed",
        "conclusion": None if state == "pending" else state,
    }


class GateIntegrityTests(EndToEndTestCase):
    def test_issue_references_follow_supported_number_format(self) -> None:
        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.github.issues[77] = {
            "id": 77,
            "title": "Additional context",
            "body": "Review.",
        }
        self.github.issues[9999999999] = {
            "id": 9999999999,
            "title": "Task context",
            "body": "Review.",
        }
        references = [
            "#42",
            "#9999999999",
            f"https://github.com/{FULL_NAME}/issues/77",
            f"https://github.com/{FULL_NAME}/issues/9999999999",
        ]
        for digits in ("8" * 11, "7" * 5000):
            references.extend(
                [f"#{digits}", f"https://github.com/{FULL_NAME}/issues/{digits}"]
            )
        self.github.pulls[7]["body"] = READY_BODY + "\n" + "\n".join(references)

        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="issues"
        )
        self.wait_for_delivery("issues")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")
        reads = self.github.requests_matching("GET", r"/issues/")
        self.assertEqual(
            sorted(path.rsplit("/", 1)[1] for path in reads),
            ["42", "77", "9999999999"],
        )
        self.assertEqual(
            self.query(
                "SELECT attempts FROM webhook_deliveries WHERE delivery_id = 'issues'"
            ),
            [(1,)],
        )

    def test_same_named_ci_results_use_the_most_cautious_evidence(self) -> None:
        cases = [
            (["failure"], ["success"], "blocked"),
            (["success"], ["failure"], "blocked"),
            (["failure", "success"], ["pending"], "blocked"),
            (["success", "failure"], ["success"], "blocked"),
            (["success"], ["failure", "success"], "blocked"),
            (["pending"], ["success", "failure"], "blocked"),
            (["success", "success"], ["success", "success"], "ready_for_human_review"),
        ]
        for index, (checks, statuses, gate) in enumerate(cases):
            with self.subTest(checks=checks, statuses=statuses):
                self.github.external_checks[HEAD_SHA] = [
                    check_result(state) for state in checks
                ]
                self.github.statuses[HEAD_SHA] = [
                    {"context": DEFAULT_REQUIRED_CI, "state": state}
                    for state in statuses
                ]
                delivery = f"evidence-{index}"
                status, _ = self.send_webhook(
                    "pull_request", self.pull_request_payload(), delivery=delivery
                )
                self.assertEqual(status, 202)
                self.wait_for_delivery(delivery)

                run = self.github.check_runs[-1]
                self.assertEqual(
                    run["conclusion"],
                    "success"
                    if gate == "ready_for_human_review"
                    else "action_required",
                )
                self.assertEqual(
                    self.query("SELECT gate_state FROM review_runs"), [(gate,)]
                )

    def test_same_named_ci_waits_until_all_results_succeed(self) -> None:
        self.github.external_checks[HEAD_SHA] = [
            check_result("success"),
            check_result("pending"),
        ]
        self.github.statuses[HEAD_SHA] = [
            {"context": DEFAULT_REQUIRED_CI, "state": "success"},
            {"context": DEFAULT_REQUIRED_CI, "state": "pending"},
        ]
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")
        self.assertEqual(self.github.check_runs[0]["status"], "in_progress")
        self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

        self.github.set_ci(DEFAULT_REQUIRED_CI)
        self.send_webhook(
            "check_run", self.check_run_event(DEFAULT_REQUIRED_CI), delivery="checks"
        )
        self.wait_for_delivery("checks")
        self.assertEqual(self.github.check_runs[0]["status"], "in_progress")
        self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

        self.github.statuses[HEAD_SHA][1]["state"] = "success"
        event = {
            **self.check_run_event(DEFAULT_REQUIRED_CI),
            "sha": HEAD_SHA,
            "context": DEFAULT_REQUIRED_CI,
            "state": "success",
        }
        self.send_webhook("status", event, delivery="statuses")
        self.wait_for_delivery("statuses")
        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "success")

    def test_ci_evidence_includes_all_pages(self) -> None:
        self.github.default_files[".repomonster.yml"] = repository_config(
            ["lint", "test"]
        )
        self.github.external_checks[HEAD_SHA] = [
            check_result("success", "lint"),
            *[check_result("success", f"check-{index}") for index in range(99)],
            check_result("failure", "lint"),
        ]
        self.github.statuses[HEAD_SHA] = [
            {"context": "test", "state": "success"},
            *[
                {"context": f"status-{index}", "state": "success"}
                for index in range(99)
            ],
            {"context": "test", "state": "failure"},
        ]
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="pages")
        self.wait_for_delivery("pages")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")
        [(payload,)] = self.query("SELECT request_payload FROM review_runs")
        self.assertEqual(payload["metadata"]["ci_checks"]["lint"], "failure")
        self.assertEqual(payload["metadata"]["ci_checks"]["test"], "failure")
        for route in ("check-runs", "status"):
            reads = self.github.requests_matching("GET", rf"/commits/[^/]+/{route}\?")
            self.assertTrue(any("page=2" in path for path in reads), reads)

    def test_configuration_problems_are_reported_on_the_first_attempt(self) -> None:
        base = json.loads(repository_config([]))
        cases = [("yaml", "version: [", "valid YAML")]
        for value in (None, [], "invalid"):
            cases.append(("root", json.dumps(value), "must be a mapping"))
        for value in (None, {}, "invalid"):
            config = {**base, "stacks": value}
            cases.append(("stacks", json.dumps(config), "stacks must be a list"))
        config = {**base, "stacks": [None]}
        cases.append(("stack", json.dumps(config), "Each stack must be a mapping"))
        for section in ("model", "knowledge", "checks"):
            for value in (None, [], "invalid"):
                config = {**base, section: value}
                cases.append(
                    (section, json.dumps(config), f"{section} must be a mapping")
                )
        for section, key in (("knowledge", "description"), ("checks", "required_ci")):
            for value in ([42], [None], [False], [{"name": "item"}]):
                config = {**base, section: {**base[section], key: value}}
                message = (
                    "must be a check name"
                    if key == "required_ci"
                    else "list of strings"
                )
                cases.append((key, json.dumps(config), message))
        for key in ("paths", "packs"):
            config = {**base, "stacks": [{**base["stacks"][0], key: [42]}]}
            cases.append((key, json.dumps(config), "list of strings"))
        for key in ("require_task_reference", "require_test_evidence"):
            config = {**base, "checks": {key: []}}
            cases.append((key, json.dumps(config), "must be a boolean"))
        config = {**base, "stacks": [{**base["stacks"][0], "packs": ["python@9.9.9"]}]}
        cases.append(("pack", json.dumps(config), "standard pack is not installed"))
        cases.append(("knowledge", json.dumps(base), "must not contain NUL"))
        config = {**base, "model": {"profile": "unknown"}}
        cases.append(("profile", json.dumps(config), "model profile is unavailable"))
        for profile in ("../default", "/default", "Default", "default.json", "", 42):
            config = {**base, "model": {"profile": profile}}
            cases.append(("profile", json.dumps(config), "model.profile must use"))

        original = self.github.default_files[".repomonster/project.md"]
        for index, (kind, content, message) in enumerate(cases):
            with self.subTest(kind=kind, content=content):
                self.github.default_files[".repomonster.yml"] = content
                self.github.default_files[".repomonster/project.md"] = (
                    original + "\x00" if message == "must not contain NUL" else original
                )
                delivery = f"configuration-{index}"
                status, _ = self.send_webhook(
                    "pull_request", self.pull_request_payload(), delivery=delivery
                )
                self.assertEqual(status, 202)
                self.wait_for_delivery(delivery)

                run = self.github.check_runs[-1]
                self.assertEqual(run["history"], ["in_progress", "action_required"])
                self.assertEqual(
                    run["output"]["title"], "Repository configuration required"
                )
                self.assertIn(message, run["output"]["summary"])
                published = json.dumps(run["output"])
                for detail in (
                    "Traceback",
                    "psycopg",
                    "INSERT INTO",
                    "ForeignKeyViolation",
                    "DataError",
                ):
                    self.assertNotIn(detail, published)
                self.assertEqual(
                    self.query(
                        "SELECT attempts, last_error FROM webhook_deliveries WHERE delivery_id = %s",
                        delivery,
                    ),
                    [(1, None)],
                )
                self.assertEqual(
                    self.query("SELECT count(*) FROM repositories"), [(0,)]
                )
                self.assertEqual(self.query("SELECT count(*) FROM review_runs"), [(0,)])

    def test_profile_files_remain_inside_the_profiles_directory(self) -> None:
        self.stop_process("worker")
        profiles = self.workspace / "profiles"
        profiles.mkdir()
        (profiles / "linked.json").symlink_to(ROOT / "profiles/default.json")
        self.environment["REPOMONSTER_ROOT"] = str(self.workspace)
        self.start_worker()
        config = json.loads(repository_config([]))
        config["model"] = {"profile": "linked"}
        self.github.default_files[".repomonster.yml"] = json.dumps(config)

        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="profile"
        )
        self.wait_for_delivery("profile")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn("model profile is unavailable", run["output"]["summary"])
        self.assertEqual(self.query("SELECT attempts FROM webhook_deliveries"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM repositories"), [(0,)])

        profile = json.loads((ROOT / "profiles/default.json").read_text())
        profile["id"] = "review-v2_1"
        (profiles / "review-v2_1.json").write_text(json.dumps(profile))
        config["model"] = {"profile": "review-v2_1"}
        self.github.default_files[".repomonster.yml"] = json.dumps(config)
        self.send_webhook(
            "pull_request", self.pull_request_payload(), delivery="selected"
        )
        self.wait_for_delivery("selected")
        self.assertEqual(self.github.check_runs[-1]["conclusion"], "success")
        self.assertEqual(
            self.query("SELECT profile_id FROM review_runs"), [("review-v2_1",)]
        )


class PinnedCheckTests(EndToEndTestCase):
    def use_required_check(self, entry: object) -> None:
        self.github.default_files[".repomonster.yml"] = repository_config([entry])

    def test_only_the_pinned_app_can_satisfy_a_check(self) -> None:
        self.use_required_check({"name": "lint", "app_id": 15368})
        self.github.set_ci("lint", app_id=999)
        self.github.statuses[HEAD_SHA] = [{"context": "lint", "state": "success"}]

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")
        self.assertEqual(self.github.check_runs[0]["status"], "in_progress")

        self.github.set_ci("lint", app_id=15368)
        self.send_webhook("check_run", self.check_run_event("lint"), delivery="done")
        self.wait_for_delivery("done")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")

    def test_pinned_app_failure_blocks_despite_other_successes(self) -> None:
        self.use_required_check({"name": "lint", "app_id": 15368})
        self.github.set_ci("lint", app_id=999)
        self.github.set_ci("lint", conclusion="failure", app_id=15368)

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")

    def test_invalid_pin_is_reported_as_configuration(self) -> None:
        self.use_required_check({"name": "lint", "app_id": "15368"})

        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

        [run] = self.github.check_runs
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn("app_id", run["output"]["summary"])


class TicketReferenceTests(EndToEndTestCase):
    def open_ticket_only_pull_request(self) -> None:
        self.github.add_pull_request(body=READY_BODY.replace("Fixes #42", "See AB-1"))
        self.send_webhook("pull_request", self.pull_request_payload(), delivery="open")
        self.wait_for_delivery("open")

    def test_unverified_ticket_does_not_satisfy_the_task_rule(self) -> None:
        self.github.default_files[".repomonster.yml"] = repository_config([])

        self.open_ticket_only_pull_request()

        self.assertEqual(self.github.check_runs[0]["conclusion"], "action_required")

    def test_repository_can_opt_in_to_unverified_tickets(self) -> None:
        config = json.loads(repository_config([]))
        config["checks"]["allow_unverified_tickets"] = True
        self.github.default_files[".repomonster.yml"] = json.dumps(config)

        self.open_ticket_only_pull_request()

        self.assertEqual(self.github.check_runs[0]["conclusion"], "success")
