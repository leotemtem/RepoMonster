"""End-to-end harness: real API and worker processes, real PostgreSQL, fake GitHub.

Requires Docker. Each test gets its own database, cloned from a bootstrapped template.
Docker being unavailable is a failure, not a skip, so CI cannot pass without running.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import jwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "pgvector/pgvector:pg16"
DATABASE_USER = "e2e"
DATABASE_PASSWORD = "e2e"  # nosec B105 - throwaway container bound to localhost.
TEMPLATE_DATABASE = "e2e_template"
EMBEDDING_DIMENSIONS = 8
APP_ID = "4242"
INSTALLATION_ID = 77
REPOSITORY_ID = 1001
FULL_NAME = "acme/api"
HEAD_SHA = "a" * 40
WEBHOOK_SECRET = "e2e-webhook-secret"  # nosec B105 - test-only value.
DEFAULT_REQUIRED_CI = "lint"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY_PEM = _PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUBLIC_KEY_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)

READY_BODY = """Fixes #42

## Problem
The handler drops requests.

## Approach
Retry once.

## Acceptance Criteria
Requests are not dropped.

## Test Evidence
Covered by tests/test_main.py.
"""


def repository_config(required_ci: list[str]) -> str:
    return json.dumps(
        {
            "version": 1,
            "knowledge": {
                "description": [".repomonster/project.md"],
                "requirements": [".repomonster/requirements.md"],
                "standards": [".repomonster/standards/*.md"],
            },
            "stacks": [
                {
                    "paths": ["app/**", "tests/**"],
                    "language": "python",
                    "framework": "fastapi",
                    "packs": ["python@1.0.0", "fastapi@1.0.0"],
                }
            ],
            "checks": {"required_ci": required_ci},
        }
    )


class FakeGitHub:
    """GitHub REST API subset, plus the embeddings endpoint, on a local port.

    It enforces App JWTs and repository-scoped installation tokens, so an auth
    regression in the client fails here the way it would against GitHub.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.failures: list[tuple[str, re.Pattern[str], int, str | None]] = []
        self.hooks: list[tuple[str, re.Pattern[str], Callable[[], None]]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.default_files: dict[str, str] = {
            ".repomonster.yml": repository_config([DEFAULT_REQUIRED_CI]),
            ".repomonster/project.md": "# Project\n\nAn API service.\n",
            ".repomonster/requirements.md": "# Requirements\n\nDo not drop requests.\n",
            ".repomonster/standards/style.md": "# Style\n\nKeep handlers small.\n",
        }
        self.head_files: dict[str, str] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.pull_files: dict[int, list[dict[str, Any]]] = {}
        self.issues: dict[int, dict[str, Any]] = {
            42: {"id": 42, "number": 42, "title": "Dropped requests", "body": "Fix it."}
        }
        self.external_checks: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.check_runs: list[dict[str, Any]] = []
        self.model_calls = 0
        self.add_pull_request()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_GET(self) -> None:
                fake._dispatch(self)

            def do_POST(self) -> None:
                fake._dispatch(self)

            def do_PATCH(self) -> None:
                fake._dispatch(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_pull_request(self, number: int = 7, **overrides: Any) -> dict[str, Any]:
        pull = {
            "number": number,
            "state": "open",
            "draft": False,
            "title": "Stop dropping requests",
            "body": READY_BODY,
            "html_url": f"https://github.com/{FULL_NAME}/pull/{number}",
            "head": {"sha": HEAD_SHA, "ref": "fix-drops"},
            "base": {"ref": "main"},
            **overrides,
        }
        self.pulls[number] = pull
        self.pull_files[number] = [
            {
                "filename": "app/main.py",
                "status": "modified",
                "additions": 4,
                "deletions": 1,
                "patch": "@@ -1 +1,4 @@\n+retry()",
            },
            {
                "filename": "tests/test_main.py",
                "status": "modified",
                "additions": 6,
                "deletions": 0,
                "patch": "@@ -1 +1,6 @@\n+def test_retry(): ...",
            },
        ]
        return pull

    def set_ci(
        self,
        name: str = DEFAULT_REQUIRED_CI,
        *,
        status: str = "completed",
        conclusion: str | None = "success",
        sha: str = HEAD_SHA,
        app_id: int = 999,
    ) -> None:
        with self.lock:
            checks = self.external_checks.setdefault(sha, [])
            checks[:] = [
                check
                for check in checks
                if check["name"] != name
                or check.get("app", {}).get("id", app_id) != app_id
            ]
            checks.append(
                {
                    "name": name,
                    "status": status,
                    "conclusion": conclusion,
                    "app": {"id": app_id},
                }
            )

    def fail_next(
        self,
        method: str,
        path_pattern: str,
        status: int = 500,
        retry_after: str | None = None,
    ) -> None:
        with self.lock:
            self.failures.append(
                (method, re.compile(path_pattern), status, retry_after)
            )

    def after_next(
        self, method: str, path_pattern: str, callback: Callable[[], None]
    ) -> None:
        """Run a callback once the request is handled, before its response is sent."""
        with self.lock:
            self.hooks.append((method, re.compile(path_pattern), callback))

    def requests_matching(self, method: str, path_pattern: str) -> list[str]:
        with self.lock:
            return [
                path
                for seen_method, path in self.requests
                if seen_method == method and re.search(path_pattern, path)
            ]

    def _dispatch(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        payload = json.loads(raw) if raw else None
        parsed = urlparse.urlsplit(handler.path)
        method = handler.command
        retry_after = None
        try:
            if method == "POST" and parsed.path == "/embeddings":
                status, body = 200, self._embeddings(payload)
            elif method == "POST" and parsed.path == "/chat/completions":
                with self.lock:
                    self.model_calls += 1
                status, body = 500, {"message": "model unavailable"}
            else:
                with self.lock:
                    self.requests.append((method, handler.path))
                    injected = next(
                        (
                            item
                            for item in self.failures
                            if item[0] == method and item[1].search(parsed.path)
                        ),
                        None,
                    )
                    if injected:
                        self.failures.remove(injected)
                if injected:
                    status, body = injected[2], {"message": "injected failure"}
                    retry_after = injected[3]
                else:
                    status, body = self._github(
                        method,
                        parsed.path,
                        urlparse.parse_qs(parsed.query),
                        payload,
                        handler.headers.get("Authorization", ""),
                    )
        except Exception as exc:  # A harness bug must surface in the worker log.
            status, body = 599, {"message": f"fake GitHub error: {exc!r}"}
        with self.lock:
            hook = next(
                (
                    item
                    for item in self.hooks
                    if item[0] == method and item[1].search(parsed.path)
                ),
                None,
            )
            if hook:
                self.hooks.remove(hook)
        if hook:
            hook[2]()
        encoded = json.dumps(body).encode()
        handler.send_response(status)
        if retry_after:
            handler.send_header("Retry-After", retry_after)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    @staticmethod
    def _embeddings(payload: dict[str, Any]) -> dict[str, Any]:
        data = []
        for index, text in enumerate(payload["input"]):
            digest = hashlib.sha256(text.encode()).digest()
            vector = [1.0 + byte / 255 for byte in digest[:EMBEDDING_DIMENSIONS]]
            data.append({"index": index, "embedding": vector})
        return {"data": data}

    def _github(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        payload: dict[str, Any] | None,
        authorization: str,
    ) -> tuple[int, Any]:
        bearer = authorization.removeprefix("Bearer ")
        token_path = f"/app/installations/{INSTALLATION_ID}/access_tokens"
        if path == token_path and method == "POST":
            try:
                jwt.decode(bearer, PUBLIC_KEY_PEM, algorithms=["RS256"], issuer=APP_ID)
            except jwt.PyJWTError as exc:
                return 401, {"message": f"Bad App JWT: {exc}"}
            with self.lock:
                self.token_requests.append(payload or {})
            if (payload or {}).get("repository_ids") != [REPOSITORY_ID]:
                return 422, {"message": "Token must be scoped to the repository"}
            return 201, {"token": "ghs_e2e", "expires_at": "2099-01-01T00:00:00Z"}
        if bearer != "ghs_e2e":
            return 401, {"message": "Bad credentials"}
        prefix = f"/repos/{FULL_NAME}"
        if not path.startswith(prefix):
            return 404, {"message": "Not Found"}
        route = path[len(prefix) :]
        if route == "" and method == "GET":
            return 200, {
                "id": REPOSITORY_ID,
                "full_name": FULL_NAME,
                "default_branch": "main",
                "html_url": f"https://github.com/{FULL_NAME}",
                "owner": {"login": FULL_NAME.split("/")[0]},
            }
        if route == "/check-runs" and method == "POST":
            return 201, self._create_check_run(payload or {})
        if match := re.fullmatch(r"/check-runs/(\d+)", route):
            if method == "PATCH":
                return self._update_check_run(int(match[1]), payload or {})
            if method == "GET":
                with self.lock:
                    for run in self.check_runs:
                        if run["id"] == int(match[1]):
                            return 200, dict(run)
                return 404, {"message": "Not Found"}
        if method != "GET":
            return 404, {"message": "Not Found"}
        if route == "/pulls":
            page = int(query.get("page", ["1"])[0])
            return 200, list(self.pulls.values()) if page == 1 else []
        if match := re.fullmatch(r"/pulls/(\d+)", route):
            pull = self.pulls.get(int(match[1]))
            return (200, pull) if pull else (404, {"message": "Not Found"})
        if match := re.fullmatch(r"/pulls/(\d+)/files", route):
            page = int(query.get("page", ["1"])[0])
            return 200, self.pull_files.get(int(match[1]), []) if page == 1 else []
        if match := re.fullmatch(r"/issues/(\d+)", route):
            issue = self.issues.get(int(match[1]))
            return (200, issue) if issue else (404, {"message": "Not Found"})
        if match := re.fullmatch(r"/contents/(.+)", route):
            return self._content(urlparse.unquote(match[1]), query.get("ref", [""])[0])
        if match := re.fullmatch(r"/git/trees/([^/]+)", route):
            files = self._files_for_ref(urlparse.unquote(match[1]))
            return 200, {
                "truncated": False,
                "tree": [
                    {
                        "path": file_path,
                        "type": "blob",
                        "sha": _sha(content),
                        "size": len(content),
                    }
                    for file_path, content in files.items()
                ],
            }
        if match := re.fullmatch(r"/git/blobs/([0-9a-f]+)", route):
            for content in [*self.default_files.values(), *self.head_files.values()]:
                if _sha(content) == match[1]:
                    return 200, _encoded(content)
            return 404, {"message": "Not Found"}
        if match := re.fullmatch(r"/commits/([^/]+)/check-runs", route):
            sha = urlparse.unquote(match[1])
            with self.lock:
                own = [
                    {
                        "id": run["id"],
                        "app": {"id": int(APP_ID)},
                        "name": run["name"],
                        "status": run["status"],
                        "conclusion": run["conclusion"],
                    }
                    for run in self.check_runs
                    if run["head_sha"] == sha
                ]
                checks = [*self.external_checks.get(sha, []), *own]
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["100"])[0])
            start = (page - 1) * per_page
            return 200, {
                "total_count": len(checks),
                "check_runs": checks[start : start + per_page],
            }
        if match := re.fullmatch(r"/commits/([^/]+)/status", route):
            sha = urlparse.unquote(match[1])
            statuses = self.statuses.get(sha, [])
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["100"])[0])
            start = (page - 1) * per_page
            return 200, {
                "total_count": len(statuses),
                "statuses": statuses[start : start + per_page],
            }
        return 404, {"message": f"Unhandled route: {method} {path}"}

    def _files_for_ref(self, ref: str) -> dict[str, str]:
        return self.default_files if ref == "main" else self.head_files

    def _content(self, file_path: str, ref: str) -> tuple[int, Any]:
        content = self._files_for_ref(ref).get(file_path)
        if content is None:
            return 404, {"message": "Not Found"}
        return 200, {"type": "file", "sha": _sha(content), **_encoded(content)}

    def _create_check_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            run = {
                "id": len(self.check_runs) + 1,
                "name": payload["name"],
                "head_sha": payload["head_sha"],
                "external_id": payload.get("external_id"),
                "status": payload.get("status", "queued"),
                "conclusion": payload.get("conclusion"),
                "output": payload.get("output", {}),
                "history": [payload.get("status", "queued")],
            }
            self.check_runs.append(run)
            return dict(run)

    def _update_check_run(
        self, run_id: int, payload: dict[str, Any]
    ) -> tuple[int, Any]:
        with self.lock:
            run = next((r for r in self.check_runs if r["id"] == run_id), None)
            if run is None:
                return 404, {"message": "Not Found"}
            for key in ("status", "conclusion", "output"):
                if key in payload:
                    run[key] = payload[key]
            run["history"].append(payload.get("conclusion") or payload.get("status"))
            return 200, dict(run)


def _sha(content: str) -> str:
    return hashlib.sha1(content.encode(), usedforsecurity=False).hexdigest()


def _encoded(content: str) -> dict[str, str]:
    return {
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
    }


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Postgres:
    """One throwaway pgvector container per test run, bootstrapped once."""

    def __init__(self) -> None:
        self.container = f"repomonster-e2e-{os.getpid()}"
        self.port: int | None = None

    def url(self, database: str) -> str:
        return (
            f"postgresql://{DATABASE_USER}:{DATABASE_PASSWORD}"
            f"@127.0.0.1:{self.port}/{database}"
        )

    def start(self) -> None:
        if self.port is not None:
            return
        if shutil.which("docker") is None:
            raise RuntimeError("The end-to-end tests require Docker")
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container,
                "--env",
                f"POSTGRES_USER={DATABASE_USER}",
                "--env",
                f"POSTGRES_PASSWORD={DATABASE_PASSWORD}",
                "--env",
                "POSTGRES_DB=postgres",
                "--publish",
                "127.0.0.1::5432",
                "--tmpfs",
                "/var/lib/postgresql/data",
                POSTGRES_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        atexit.register(self.stop)
        published = subprocess.run(
            ["docker", "port", self.container, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.port = int(published.split()[0].rsplit(":", 1)[1])
        self._wait_until_ready()
        self._bootstrap_template()

    def stop(self) -> None:
        subprocess.run(
            ["docker", "rm", "--force", self.container],
            check=False,
            capture_output=True,
        )

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(self.url("postgres"), connect_timeout=2):
                    return
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)

    def _bootstrap_template(self) -> None:
        self._admin(f'CREATE DATABASE "{TEMPLATE_DATABASE}"')
        fake = FakeGitHub()
        try:
            subprocess.run(
                [sys.executable, "-m", "review_gatekeeper.management", "bootstrap"],
                check=True,
                capture_output=True,
                text=True,
                env=service_environment(self.url(TEMPLATE_DATABASE), fake.url, None),
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Bootstrap failed:\n{exc.stderr}") from exc
        finally:
            fake.close()

    def _admin(self, statement: str) -> None:
        with psycopg.connect(self.url("postgres"), autocommit=True) as connection:
            connection.execute(statement)

    def create_database(self) -> str:
        name = f"t_{uuid.uuid4().hex[:12]}"
        self._admin(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DATABASE}"')
        return name

    def set_connections_allowed(self, name: str, allowed: bool) -> None:
        self._admin(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS {allowed}')
        if not allowed:
            with psycopg.connect(self.url("postgres"), autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s",
                    (name,),
                )

    def drop_database(self, name: str) -> None:
        self._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


POSTGRES = _Postgres()


def service_environment(
    database_url: str, fake_url: str, key_path: Path | None
) -> dict[str, str]:
    """A minimal environment, so no developer settings or secrets leak in."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "REPOMONSTER_ROOT": str(ROOT),
        "DATABASE_URL": database_url,
        "EMBEDDING_BASE_URL": fake_url,
        "EMBEDDING_MODEL": "e2e-embedding",
        "EMBEDDING_DIMENSIONS": str(EMBEDDING_DIMENSIONS),
        "GITHUB_APP_ID": APP_ID,
        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "GITHUB_API_URL": fake_url,
        "GITHUB_TIMEOUT_SECONDS": "10",
        "WORKER_POLL_SECONDS": "0.05",
        "WEBHOOK_MAX_ATTEMPTS": "3",
        "WEBHOOK_RETRY_BASE_SECONDS": "0.1",
        "WEBHOOK_RETRY_MAX_SECONDS": "0.5",
    }
    if key_path is not None:
        environment["GITHUB_PRIVATE_KEY_PATH"] = str(key_path)
    return environment


class EndToEndTestCase(unittest.TestCase):
    """Runs the API and one worker against a fresh database and a fake GitHub."""

    settings: dict[str, str] = {}
    use_model = False

    @classmethod
    def setUpClass(cls) -> None:
        POSTGRES.start()

    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.addCleanup(self.github.close)
        self.database = POSTGRES.create_database()
        self.addCleanup(POSTGRES.drop_database, self.database)
        self.database_url = POSTGRES.url(self.database)

        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        key_path = self.workspace / "github-app.pem"
        key_path.write_bytes(PRIVATE_KEY_PEM)
        self.environment = service_environment(
            self.database_url, self.github.url, key_path
        )
        self.environment.update(self.settings)
        if self.use_model:
            self.environment["INSIGHT_BASE_URL"] = self.github.url
            self.environment["INSIGHT_MODEL"] = "e2e-model"
        self.processes: dict[str, subprocess.Popen[bytes]] = {}

        self.api_port = _free_port()
        self.api_url = f"http://127.0.0.1:{self.api_port}"
        self.start_process(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "review_gatekeeper.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.api_port),
            ],
        )
        self.wait_for(self._api_is_up, description="API to start")
        self.start_worker()

    def start_worker(self, name: str = "worker") -> None:
        self.start_process(
            name, [sys.executable, "-m", "review_gatekeeper.management", "worker"]
        )

    def start_process(self, name: str, command: list[str]) -> None:
        log = (self.workspace / f"{name}.log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            command, env=self.environment, stdout=log, stderr=subprocess.STDOUT
        )
        self.processes[name] = process
        self.addCleanup(self.stop_process, name)

    def stop_process(self, name: str) -> None:
        process = self.processes[name]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def kill_worker(self, name: str = "worker") -> None:
        self.processes[name].kill()
        self.processes[name].wait()

    def set_database_available(self, available: bool) -> None:
        POSTGRES.set_connections_allowed(self.database, available)

    def logs(self) -> str:
        sections = []
        for path in sorted(self.workspace.glob("*.log")):
            sections.append(f"--- {path.name}\n{path.read_text()[-4000:]}")
        return "\n".join(sections)

    def wait_for(
        self,
        predicate: Callable[[], Any],
        *,
        description: str,
        timeout: float = 30.0,
    ) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            for name, process in self.processes.items():
                if process.poll() is not None and name == "api":
                    self.fail(f"{name} exited early\n{self.logs()}")
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {description}\n{self.logs()}")

    def _api_is_up(self) -> bool:
        try:
            with urlrequest.urlopen(  # nosec B310 - local test server.
                f"{self.api_url}/healthz", timeout=1
            ) as response:
                return response.status == 200
        except (urlerror.URLError, ConnectionError):
            return False

    def pull_request_payload(
        self, number: int = 7, action: str = "opened"
    ) -> dict[str, Any]:
        return {
            "action": action,
            "number": number,
            "pull_request": self.github.pulls[number],
            "repository": {
                "id": REPOSITORY_ID,
                "full_name": FULL_NAME,
                "html_url": f"https://github.com/{FULL_NAME}",
            },
            "installation": {"id": INSTALLATION_ID},
        }

    def check_run_event(
        self,
        name: str,
        *,
        pull_numbers: tuple[int, ...] = (),
        app_id: int = 999,
    ) -> dict[str, Any]:
        return {
            "action": "completed",
            "check_run": {
                "name": name,
                "head_sha": HEAD_SHA,
                "status": "completed",
                "app": {"id": app_id},
                "pull_requests": [{"number": number} for number in pull_numbers],
            },
            "repository": {
                "id": REPOSITORY_ID,
                "full_name": FULL_NAME,
                "html_url": f"https://github.com/{FULL_NAME}",
            },
            "installation": {"id": INSTALLATION_ID},
        }

    def send_webhook(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        delivery: str | None = None,
        secret: str = WEBHOOK_SECRET,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.post_webhook(event, body, f"sha256={signature}", delivery)

    def post_webhook(
        self, event: str, body: bytes, signature: str, delivery: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        request = urlrequest.Request(
            f"{self.api_url}/webhooks/github",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
                "X-Hub-Signature-256": signature,
            },
        )
        try:
            with urlrequest.urlopen(request, timeout=10) as response:  # nosec B310
                return response.status, json.loads(response.read())
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def query(self, statement: str, *parameters: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.database_url, autocommit=True) as connection:
            return connection.execute(statement, parameters).fetchall()

    def delivery_status(self, delivery: str) -> str | None:
        rows = self.query(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = %s", delivery
        )
        return rows[0][0] if rows else None

    def wait_for_delivery(self, delivery: str, status: str = "completed") -> None:
        self.wait_for(
            lambda: self.delivery_status(delivery) == status,
            description=f"delivery {delivery} to reach {status}",
        )
