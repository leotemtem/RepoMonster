"""Real demo Compose stack and Caddy; providers stay on an isolated test network.

Docker is required and its absence is a failure, not a skip. No tunnel is started.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

try:
    from harness import PRIVATE_KEY_PEM, ROOT, WEBHOOK_SECRET
except ModuleNotFoundError as exc:
    if exc.name != "harness":
        raise
    from tests_e2e.harness import PRIVATE_KEY_PEM, ROOT, WEBHOOK_SECRET

PROVIDER_STUB = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
calls = []
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(200, calls) if self.path == '/calls' else self.respond(404, {})
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append({'method': 'POST', 'path': self.path, 'payload': payload})
        if self.path == '/embeddings':
            self.respond(200, {'data': [
                {'index': i, 'embedding': [1.0] * 8}
                for i, _ in enumerate(payload['input'])]})
        else:
            self.respond(503, {'message': 'GitHub test stub unavailable'})
    def respond(self, status, result):
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""


class DemoStackTest(unittest.TestCase):
    def run_command(self, *arguments: str, timeout: int = 180) -> str:
        result = subprocess.run(
            list(arguments),
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            self.fail(f"Command failed: {arguments}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def compose(self, *arguments: str, timeout: int = 180) -> str:
        return self.run_command(*self.compose_command, *arguments, timeout=timeout)

    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.fail("The demo Compose end-to-end test requires Docker")
        workspace = tempfile.TemporaryDirectory(prefix="repomonster-demo-")
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.project = f"repomonster-demo-{uuid.uuid4().hex[:10]}"
        self.image = f"{self.project}:test"
        # Allow Docker connection settings, never developer application settings.
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_CONFIG",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
                "XDG_RUNTIME_DIR",
            }
        }
        empty_config = self.workspace / "empty.conf"
        empty_config.write_text("")
        key_file = self.workspace / "github-app.pem"
        key_file.write_bytes(PRIVATE_KEY_PEM)
        # This disposable key must be readable by UID 65532 in the real image.
        key_file.chmod(0o644)
        self.environment.update(
            POSTGRES_PASSWORD="demo-test-password",
            GITHUB_KEY_FILE=str(key_file),
            REPOMONSTER_ENV_FILE=str(empty_config),
        )
        self.compose_command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(empty_config),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(ROOT / "deploy/compose.demo.yml"),
        ]
        self.run_command("docker", "info")
        self.check_demo_config(empty_config)
        override = self.workspace / "test.yml"
        application_environment = {
            "EMBEDDING_BASE_URL": "http://stub:8080",
            "EMBEDDING_MODEL": "demo-test-embedding",
            "EMBEDDING_DIMENSIONS": "8",
            "GITHUB_API_URL": "http://stub:8080",
            "GITHUB_APP_ID": "4242",
            "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "HTTP_MAX_RETRIES": "0",
            "GITHUB_TIMEOUT_SECONDS": "2",
            "WORKER_POLL_SECONDS": "0.1",
            # Leave time to inspect the first failure before any retry.
            "WEBHOOK_RETRY_BASE_SECONDS": "300",
        }
        services = "services:\n"
        for service in ("bootstrap", "api", "worker"):
            services += (
                f"  {service}:\n"
                f"    image: {self.image}\n"
                "    build: !reset null\n"
                f"    env_file: !override [{json.dumps(str(empty_config))}]\n"
                f"    environment: {json.dumps(application_environment)}\n"
            )
        override.write_text(
            services + "  stub:\n"
            f"    image: {self.image}\n"
            f"    command: {json.dumps(['python', '-c', PROVIDER_STUB])}\n"
            "    healthcheck:\n"
            '      test: [CMD, python, -c, "import urllib.request; '
            "urllib.request.urlopen('http://127.0.0.1:8080/calls', timeout=2)\"]\n"
            "      interval: 1s\n"
            "      timeout: 3s\n"
            "      retries: 30\n"
            # Containers can only reach one another, never real providers.
            "networks: !override\n"
            "  default:\n"
            "    internal: true\n"
        )
        self.compose_command.extend(["-f", str(override)])
        self.addCleanup(self.remove_image)
        self.addCleanup(self.clean_stack)
        self.run_command("docker", "build", "--tag", self.image, ".", timeout=600)
        self.compose("up", "-d", "--wait", "stub", "postgres")
        self.compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "120",
            "--no-build",
            timeout=300,
        )

    def check_demo_config(self, empty_config: Path) -> None:
        # Inspect the actual overlay before adding the provider test overrides.
        config = json.loads(
            self.compose("--profile", "tunnel", "config", "--format", "json")
        )
        for name, service in config["services"].items():
            with self.subTest(service=name):
                self.assertFalse(service.get("ports"))
                self.assertNotIn("shared", service.get("networks", {}))
        self.assertEqual(set(config["networks"]), {"default"})
        tunnel = config["services"]["cloudflared"]
        self.assertFalse(tunnel.get("env_file"))
        self.assertFalse(tunnel.get("environment"))
        unresolved = json.loads(
            self.compose(
                "--profile",
                "tunnel",
                "config",
                "--no-env-resolution",
                "--format",
                "json",
            )
        )
        self.assertFalse(unresolved["services"]["cloudflared"].get("env_file"))
        for name in ("bootstrap", "api", "worker"):
            self.assertEqual(
                [entry["path"] for entry in unresolved["services"][name]["env_file"]],
                [str(empty_config)],
            )

    def remove_image(self) -> None:
        # A failed build may never have created the tag.
        if self.run_command("docker", "image", "ls", "--quiet", self.image).strip():
            self.run_command("docker", "image", "rm", self.image)

    def clean_stack(self) -> None:
        logs = ""
        try:
            try:
                logs = self.compose("logs", "--no-color", "--tail=40", timeout=15)
            except (AssertionError, subprocess.TimeoutExpired) as exc:
                logs = f"Log collection failed: {exc}"
        finally:
            try:
                self.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
            except (AssertionError, subprocess.TimeoutExpired) as exc:
                self.fail(f"{exc}\nStack logs:\n{logs}")

    def http(
        self, path: str, *, body: bytes | None = None, headers=None
    ) -> tuple[int, bytes]:
        # Probe real Caddy over the internal network without publishing host ports.
        probe = (
            "import json\n"
            "from urllib import error, request\n"
            f"req = request.Request({'http://caddy:8080' + path!r}, "
            f"data={body!r}, headers={headers or {}!r})\n"
            "try:\n"
            " with request.urlopen(req, timeout=10) as response:\n"
            "  status, body = response.status, response.read()\n"
            "except error.HTTPError as exc:\n"
            " status, body = exc.code, exc.read()\n"
            "print(json.dumps([status, body.decode()]))\n"
        )
        status, response = json.loads(
            self.compose("exec", "-T", "api", "python", "-c", probe)
        )
        return status, response.encode()

    def sql(self, query: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "repomonster",
            "-d",
            "repomonster",
            "-Atc",
            query,
        ).strip()

    def stub_calls(self) -> list[dict]:
        return json.loads(
            self.compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request; "
                "print(urllib.request.urlopen('http://stub:8080/calls', timeout=2).read().decode())",
            )
        )

    def test_webhook_queue_and_worker(self) -> None:
        self.assertNotIn(
            "cloudflared",
            self.compose("ps", "--services", "--status", "running").splitlines(),
        )
        for path, body in [
            ("/webhooks/github", None),
            ("/review", b"{}"),
            ("/readyz", None),
            ("/healthz", None),
            ("/webhooks/github/x", b"{}"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.http(path, body=body)[0], 404)
        status, response = self.http(
            "/webhooks/github",
            body=b"{}",
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
        self.assertEqual(status, 401, response)
        self.assertEqual(
            json.loads(response), {"detail": "Invalid GitHub webhook signature"}
        )
        self.assertEqual(self.sql("SELECT count(*) FROM webhook_deliveries"), "0")

        self.compose("stop", "worker")
        delivery = str(uuid.uuid4())
        body = json.dumps(
            {
                "action": "opened",
                "number": 7,
                "pull_request": {"number": 7},
                "repository": {"id": 1001, "full_name": "example/test"},
                "installation": {"id": 77},
            }
        ).encode()
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        status, response = self.http(
            "/webhooks/github",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": delivery,
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )
        self.assertEqual(status, 202, response)
        self.assertEqual(
            json.loads(response),
            {
                "received": True,
                "queued": True,
                "duplicate": False,
                "delivery_id": delivery,
            },
        )
        query = (
            "SELECT status, attempts, coalesce(last_error, '') FROM webhook_deliveries "
            f"WHERE delivery_id = '{delivery}'"
        )
        self.assertEqual(self.sql(query), "pending|0|")
        self.assertTrue(
            any(call["path"] == "/embeddings" for call in self.stub_calls())
        )
        self.compose("start", "worker")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = self.sql(query)
            if row.startswith("pending|1|") and "503" in row:
                break
            time.sleep(0.25)
        else:
            self.fail(
                f"Worker did not claim and record the stub's failure: {row}\n"
                + self.compose("logs", "worker")
            )
        # Auth requests happen first and fail here; the queue returns to pending.
        # Do not expect 'processing' after the worker has recorded a retry.
        token_calls = [
            call
            for call in self.stub_calls()
            if call["path"] == "/app/installations/77/access_tokens"
        ]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(token_calls[0]["payload"]["repository_ids"], [1001])
