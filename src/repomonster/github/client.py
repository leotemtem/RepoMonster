from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import parse as urlparse

import httpx

from ..http import HTTPClient, retry_after_seconds
from ..url_safety import require_http_url
from .publish import CHECK_NAME


class GitHubAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        method: str,
        path: str,
        detail: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(f"GitHub API {method} {path} returned {status}: {detail}")
        self.status = status
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class GitHubSettings:
    app_id: str
    private_key_path: Path
    webhook_secret: str
    api_url: str = "https://api.github.com"
    api_version: str = "2026-03-10"
    request_timeout_seconds: int = 30
    ci_wait_timeout_seconds: int = 1800
    max_changed_files: int = 1000
    max_knowledge_files: int = 100
    max_knowledge_file_bytes: int = 500_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "api_url",
            require_http_url(self.api_url, setting_name="GITHUB_API_URL"),
        )

    @classmethod
    def from_environment(cls) -> GitHubSettings:
        required = {
            "GITHUB_APP_ID": os.getenv("GITHUB_APP_ID", "").strip(),
            "GITHUB_PRIVATE_KEY_PATH": os.getenv("GITHUB_PRIVATE_KEY_PATH", "").strip(),
            "GITHUB_WEBHOOK_SECRET": os.getenv("GITHUB_WEBHOOK_SECRET", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                "Missing GitHub App configuration: " + ", ".join(missing)
            )
        return cls(
            app_id=required["GITHUB_APP_ID"],
            private_key_path=Path(required["GITHUB_PRIVATE_KEY_PATH"]),
            webhook_secret=required["GITHUB_WEBHOOK_SECRET"],
            api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            api_version=os.getenv("GITHUB_API_VERSION", "2026-03-10"),
            request_timeout_seconds=int(os.getenv("GITHUB_TIMEOUT_SECONDS", "30")),
            ci_wait_timeout_seconds=int(os.getenv("CI_WAIT_TIMEOUT_SECONDS", "1800")),
            max_changed_files=int(os.getenv("GITHUB_MAX_CHANGED_FILES", "1000")),
            max_knowledge_files=int(os.getenv("GITHUB_MAX_KNOWLEDGE_FILES", "100")),
            max_knowledge_file_bytes=int(
                os.getenv("GITHUB_MAX_KNOWLEDGE_FILE_BYTES", "500000")
            ),
        )


class GitHubClient:
    def __init__(self, settings: GitHubSettings) -> None:
        self.settings = settings
        self._http = HTTPClient()
        self._installation_tokens: dict[tuple[int, int], tuple[str, datetime]] = {}

    def installation_token(self, installation_id: int, repository_id: int) -> str:
        cache_key = (installation_id, repository_id)
        now = datetime.now(UTC)
        self._installation_tokens = {
            key: value
            for key, value in self._installation_tokens.items()
            if value[1] > now + timedelta(minutes=2)
        }
        cached = self._installation_tokens.get(cache_key)
        if cached:
            return cached[0]
        response = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._app_jwt(),
            payload={
                "repository_ids": [repository_id],
                "permissions": {
                    "checks": "write",
                    "contents": "read",
                    "issues": "read",
                    "metadata": "read",
                    "pull_requests": "read",
                    "statuses": "read",
                },
            },
            retry_post=True,
        )
        token = str(response["token"])
        expires_at = datetime.fromisoformat(
            str(response["expires_at"]).replace("Z", "+00:00")
        )
        self._installation_tokens[cache_key] = (token, expires_at)
        return token

    def get_repository(self, full_name: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/repos/{_repository_path(full_name)}", token=token
        )

    def get_pull_request(
        self, full_name: str, number: int, token: str
    ) -> dict[str, Any]:
        return self._request(
            "GET", f"/repos/{_repository_path(full_name)}/pulls/{number}", token=token
        )

    def list_open_pull_requests(
        self, full_name: str, token: str
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"/repos/{_repository_path(full_name)}/pulls?state=open", token=token
        )

    def list_pull_request_files(
        self, full_name: str, number: int, token: str
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"/repos/{_repository_path(full_name)}/pulls/{number}/files", token=token
        )

    def get_content(
        self, full_name: str, path: str, ref: str, token: str
    ) -> dict[str, Any]:
        encoded_path = "/".join(
            urlparse.quote(part, safe="") for part in path.split("/")
        )
        query = urlparse.urlencode({"ref": ref})
        return self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/contents/{encoded_path}?{query}",
            token=token,
        )

    def get_branch_tree_sha(self, full_name: str, branch: str, token: str) -> str:
        response = self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/branches/{urlparse.quote(branch, safe='')}",
            token=token,
        )
        return str(response["commit"]["commit"]["tree"]["sha"])

    def get_tree(self, full_name: str, ref: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/git/trees/{urlparse.quote(ref, safe='')}?recursive=1",
            token=token,
        )

    def get_blob(self, full_name: str, sha: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/git/blobs/{urlparse.quote(sha, safe='')}",
            token=token,
        )

    def get_issue(self, full_name: str, number: int, token: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/repos/{_repository_path(full_name)}/issues/{number}", token=token
        )

    def list_check_runs(
        self, full_name: str, ref: str, token: str
    ) -> list[dict[str, Any]]:
        checks = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{_repository_path(full_name)}/commits/{urlparse.quote(ref, safe='')}/check-runs?per_page=100&page={page}",
                token=token,
            )
            items = response.get("check_runs", [])
            checks.extend(items)
            if len(items) < 100:
                return checks
            page += 1

    def combined_status(self, full_name: str, ref: str, token: str) -> dict[str, Any]:
        statuses = []
        page = 1
        combined = {}
        while True:
            response = self._request(
                "GET",
                f"/repos/{_repository_path(full_name)}/commits/{urlparse.quote(ref, safe='')}/status?per_page=100&page={page}",
                token=token,
            )
            if page == 1:
                combined = response
            items = response.get("statuses", [])
            statuses.extend(items)
            if len(items) < 100:
                return {**combined, "statuses": statuses}
            page += 1

    def create_check_run(
        self,
        full_name: str,
        token: str,
        *,
        head_sha: str,
        details_url: str | None,
        external_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": CHECK_NAME,
            "head_sha": head_sha,
            "status": "in_progress",
            "started_at": _timestamp(),
            "external_id": external_id,
            "output": {
                "title": "RepoMonster is reviewing this change",
                "summary": "Fetching repository policy, change evidence, and CI state.",
            },
        }
        if details_url:
            payload["details_url"] = details_url
        return self._request(
            "POST",
            f"/repos/{_repository_path(full_name)}/check-runs",
            token=token,
            payload=payload,
        )

    def get_check_run(
        self, full_name: str, check_run_id: int, token: str
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/check-runs/{check_run_id}",
            token=token,
        )

    def update_check_run(
        self,
        full_name: str,
        check_run_id: int,
        token: str,
        *,
        conclusion: str,
        title: str,
        summary: str,
        text: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/repos/{_repository_path(full_name)}/check-runs/{check_run_id}",
            token=token,
            payload={
                "status": "completed",
                "conclusion": conclusion,
                "completed_at": _timestamp(),
                "output": {
                    "title": title[:255],
                    "summary": summary[:65_535],
                    "text": text[:65_535],
                },
            },
        )

    def update_check_run_progress(
        self,
        full_name: str,
        check_run_id: int,
        token: str,
        *,
        title: str,
        summary: str,
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/repos/{_repository_path(full_name)}/check-runs/{check_run_id}",
            token=token,
            payload={
                "status": "in_progress",
                "output": {"title": title[:255], "summary": summary[:65_535]},
            },
        )

    def _app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PyJWT with cryptography support is required for GitHub Apps"
            ) from exc
        now = datetime.now(UTC)
        private_key = self.settings.private_key_path.read_text()
        return jwt.encode(
            {
                "iat": int((now - timedelta(seconds=60)).timestamp()),
                "exp": int((now + timedelta(minutes=9)).timestamp()),
                "iss": self.settings.app_id,
            },
            private_key,
            algorithm="RS256",
        )

    def _paginate(self, path: str, *, token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self._request(
                "GET", f"{path}{separator}per_page=100&page={page}", token=token
            )
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        payload: dict[str, Any] | None = None,
        retry_post: bool = False,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("GitHub API paths must be absolute")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RepoMonster/0.1",
            "X-GitHub-Api-Version": self.settings.api_version,
        }
        try:
            response = self._http.request(
                method,
                self.settings.api_url + path,
                payload=payload,
                headers=headers,
                timeout=self.settings.request_timeout_seconds,
                retry_post=retry_post,
            )
        except httpx.HTTPStatusError as exc:
            raise GitHubAPIError(
                exc.response.status_code,
                method,
                path.split("?", 1)[0],
                _api_error_detail(exc.response.content),
                retry_after_seconds(exc.response),
            ) from exc
        if not response.content:
            return None
        return response.json()


def _repository_path(full_name: str) -> str:
    parts = full_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid GitHub repository name: {full_name}")
    return "/".join(urlparse.quote(part, safe="") for part in parts)


def _api_error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body)
        return str(payload.get("message") or "request failed")[:1000]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")[:1000] or "request failed"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
