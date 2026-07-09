from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .config import RepositoryConfig, load_repository_config
from .jobs import WebhookJob, WebhookQueue
from .knowledge import RepositoryKnowledgeSynchronizer, RepositorySource
from .models import (
    ChangedFile,
    ChangeRequestKind,
    FindingImpact,
    GateState,
    Provider,
    ReviewRequest,
    TaskReference,
)
from .providers import normalize_github_event
from .repository import EmbeddingProvider
from .service import ReviewService


CHECK_NAME = "RepoMonster review gate"
SUPPORTED_PULL_REQUEST_ACTIONS = {
    "opened",
    "reopened",
    "synchronize",
    "edited",
    "ready_for_review",
}


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, detail: str) -> None:
        super().__init__(f"GitHub API {method} {path} returned {status}: {detail}")
        self.status = status


class RepositoryConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubSettings:
    app_id: str
    private_key_path: Path
    webhook_secret: str
    api_url: str = "https://api.github.com"
    api_version: str = "2026-03-10"
    request_timeout_seconds: int = 30
    max_changed_files: int = 1000
    max_knowledge_files: int = 100
    max_knowledge_file_bytes: int = 500_000

    @classmethod
    def from_environment(cls) -> "GitHubSettings":
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
            max_changed_files=int(os.getenv("GITHUB_MAX_CHANGED_FILES", "1000")),
            max_knowledge_files=int(os.getenv("GITHUB_MAX_KNOWLEDGE_FILES", "100")),
            max_knowledge_file_bytes=int(
                os.getenv("GITHUB_MAX_KNOWLEDGE_FILE_BYTES", "500000")
            ),
        )


def verify_webhook_signature(body: bytes, secret: str, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubClient:
    def __init__(self, settings: GitHubSettings) -> None:
        self.settings = settings
        self._installation_tokens: dict[tuple[int, int], tuple[str, datetime]] = {}

    def installation_token(self, installation_id: int, repository_id: int) -> str:
        cache_key = (installation_id, repository_id)
        cached = self._installation_tokens.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and cached[1] > now + timedelta(minutes=2):
            return cached[0]
        response = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._app_jwt(),
            payload={"repository_ids": [repository_id]},
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
        response = self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/commits/{urlparse.quote(ref, safe='')}/check-runs?per_page=100",
            token=token,
        )
        return list(response.get("check_runs", []))

    def combined_status(self, full_name: str, ref: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/repos/{_repository_path(full_name)}/commits/{urlparse.quote(ref, safe='')}/status?per_page=100",
            token=token,
        )

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

    def _app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PyJWT with cryptography support is required for GitHub Apps"
            ) from exc
        now = datetime.now(timezone.utc)
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
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("GitHub API paths must be absolute")
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RepoMonster/0.1",
            "X-GitHub-Api-Version": self.settings.api_version,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request(
            self.settings.api_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlrequest.urlopen(  # noqa: S310
                request, timeout=self.settings.request_timeout_seconds
            ) as response:
                body = response.read()
        except urlerror.HTTPError as exc:
            detail = _api_error_detail(exc.read())
            raise GitHubAPIError(
                exc.code, method, path.split("?", 1)[0], detail
            ) from exc
        if not body:
            return None
        return json.loads(body)


class GitHubReviewProcessor:
    def __init__(
        self,
        *,
        database_url: str,
        queue: WebhookQueue,
        client: GitHubClient,
        embedding_provider: EmbeddingProvider,
        review_service: ReviewService,
    ) -> None:
        self.database_url = database_url
        self.queue = queue
        self.client = client
        self.embedding_provider = embedding_provider
        self.review_service = review_service

    def process(self, job: WebhookJob) -> bool:
        """Process one delivery. Returns False when the event is intentionally ignored."""
        if job.event_name != "pull_request":
            return False
        if job.payload.get("action") not in SUPPORTED_PULL_REQUEST_ACTIONS:
            return False

        normalized = normalize_github_event(job.event_name, job.payload)
        repository_id = int(normalized.repository_external_id)
        installation_id = int(normalized.installation_id or 0)
        pull_number = int(normalized.external_id)
        if not repository_id or not installation_id or not pull_number:
            raise ValueError(
                "GitHub pull request webhook is missing repository or installation identity"
            )

        token = self.client.installation_token(installation_id, repository_id)
        repository = self.client.get_repository(normalized.repository, token)
        if int(repository["id"]) != repository_id:
            raise ValueError(
                "GitHub repository identity changed during review preparation"
            )
        full_name = str(repository["full_name"])
        pull_request = self.client.get_pull_request(full_name, pull_number, token)
        head_sha = str(pull_request["head"]["sha"])

        check_run_id = int(job.external_result_id) if job.external_result_id else None
        if normalized.head_sha and head_sha != normalized.head_sha:
            if check_run_id is not None:
                self.client.update_check_run(
                    full_name,
                    check_run_id,
                    token,
                    conclusion="neutral",
                    title="Superseded by a newer commit",
                    summary="This delivery was not reviewed because the pull-request head changed.",
                )
            return False
        if pull_request.get("state") != "open":
            if check_run_id is not None:
                self.client.update_check_run(
                    full_name,
                    check_run_id,
                    token,
                    conclusion="neutral",
                    title="Pull request is no longer open",
                    summary="This delivery was not reviewed because the pull request was closed.",
                )
            return False
        if check_run_id is None:
            check = self.client.create_check_run(
                full_name,
                token,
                head_sha=head_sha,
                details_url=pull_request.get("html_url"),
                external_id=job.delivery_id,
            )
            check_run_id = int(check["id"])
            self.queue.set_external_result_id(job.id, str(check_run_id))
            job.external_result_id = str(check_run_id)

        if pull_request.get("draft"):
            self.client.update_check_run(
                full_name,
                check_run_id,
                token,
                conclusion="neutral",
                title="Draft pull request",
                summary="RepoMonster will review this pull request when it is marked ready.",
            )
            return True

        try:
            review_request, profile_id = self._prepare_review(
                repository=repository,
                pull_request=pull_request,
                token=token,
                installation_external_id=installation_id,
            )
        except RepositoryConfigurationError as exc:
            self.client.update_check_run(
                full_name,
                check_run_id,
                token,
                conclusion="action_required",
                title="Repository configuration required",
                summary=str(exc),
                text=(
                    "Add `.repomonster.yml` and its referenced knowledge files to the "
                    "repository's default branch, then redeliver the webhook or update the PR."
                ),
            )
            return True

        result = self.review_service.review(review_request, profile_id=profile_id)
        self._persist_result(review_request, result)
        self.client.update_check_run(
            full_name,
            check_run_id,
            token,
            conclusion=_check_conclusion(result.gate_state),
            title=_check_title(result.gate_state),
            summary=result.summary,
            text=_format_findings(result),
        )
        return True

    def mark_terminal_failure(self, job: WebhookJob, error: Exception) -> None:
        if not job.external_result_id or job.event_name != "pull_request":
            return
        try:
            normalized = normalize_github_event(job.event_name, job.payload)
            token = self.client.installation_token(
                int(normalized.installation_id or 0),
                int(normalized.repository_external_id),
            )
            self.client.update_check_run(
                normalized.repository,
                int(job.external_result_id),
                token,
                conclusion="neutral",
                title="RepoMonster could not complete the review",
                summary="The review failed after all retry attempts. Maintainer review is required.",
                text=f"{type(error).__name__}: {error}"[:4000],
            )
        except Exception:
            # The queue retains the original error; a provider outage can also prevent
            # publication of the terminal state.
            return

    def _prepare_review(
        self,
        *,
        repository: dict[str, Any],
        pull_request: dict[str, Any],
        token: str,
        installation_external_id: int,
    ) -> tuple[ReviewRequest, str]:
        full_name = str(repository["full_name"])
        default_branch = str(repository["default_branch"])
        config_blob = self._load_config_blob(full_name, default_branch, token)
        config_content = _decode_github_content(config_blob)
        try:
            config = load_repository_config(config_content)
        except (TypeError, ValueError) as exc:
            raise RepositoryConfigurationError(
                f"Invalid `.repomonster.yml`: {exc}"
            ) from exc

        sources = self._load_knowledge_sources(
            full_name=full_name,
            default_branch=default_branch,
            token=token,
            config=config,
        )
        repository_origin = _repository_origin(repository)
        repository_key = f"github:{repository_origin}:{repository['id']}"
        task_references, task_sources = self._load_task_context(
            full_name=full_name,
            repository_id=int(repository["id"]),
            title=str(pull_request.get("title") or ""),
            body=str(pull_request.get("body") or ""),
            token=token,
        )
        changed_files = self._changed_files(
            full_name, int(pull_request["number"]), token
        )
        language, framework, standard_packs = _select_stack(config, changed_files)
        head_sha = str(pull_request["head"]["sha"])
        ci_checks = self._ci_checks(full_name, head_sha, token)

        with self._connect() as connection:
            installation_db_id = self._upsert_installation(
                connection,
                external_id=installation_external_id,
                namespace=str(
                    repository.get("owner", {}).get("login")
                    or full_name.split("/", 1)[0]
                ),
            )
            synchronizer = RepositoryKnowledgeSynchronizer(
                connection, self.embedding_provider
            )
            synchronizer.sync(
                provider="github",
                provider_base_url=repository_origin,
                external_id=str(repository["id"]),
                full_name=full_name,
                repository_key=repository_key,
                default_branch=default_branch,
                config_sha=str(config_blob["sha"]),
                config=config,
                sources=sources,
                installation_id=installation_db_id,
            )
            for task_key, task_source in task_sources:
                synchronizer.sync_task_sources(repository_key, task_key, [task_source])
        return (
            ReviewRequest(
                provider=Provider.GITHUB,
                change_kind=ChangeRequestKind.PULL_REQUEST,
                repository=full_name,
                repository_key=repository_key,
                external_id=str(pull_request["number"]),
                title=str(pull_request.get("title") or ""),
                description=str(pull_request.get("body") or ""),
                target_branch=str(pull_request.get("base", {}).get("ref") or ""),
                source_branch=str(pull_request.get("head", {}).get("ref") or ""),
                language=language,
                framework=framework,
                standard_packs=standard_packs,
                task_references=task_references,
                changed_files=changed_files,
                metadata={
                    "head_sha": head_sha,
                    "provider_base_url": repository_origin,
                    "external_repository_id": str(repository["id"]),
                    "pull_request_url": pull_request.get("html_url"),
                    "ci_checks": ci_checks,
                },
            ),
            config.model_profile,
        )

    def _load_config_blob(
        self, full_name: str, branch: str, token: str
    ) -> dict[str, Any]:
        try:
            blob = self.client.get_content(full_name, ".repomonster.yml", branch, token)
        except GitHubAPIError as exc:
            if exc.status == 404:
                raise RepositoryConfigurationError(
                    "`.repomonster.yml` was not found on the default branch."
                ) from exc
            raise
        if isinstance(blob, list) or blob.get("type") != "file":
            raise RepositoryConfigurationError(
                "`.repomonster.yml` must be a regular file."
            )
        return blob

    def _load_knowledge_sources(
        self,
        *,
        full_name: str,
        default_branch: str,
        token: str,
        config: RepositoryConfig,
    ) -> list[RepositorySource]:
        tree = self.client.get_tree(full_name, default_branch, token)
        if tree.get("truncated"):
            raise RepositoryConfigurationError(
                "The default-branch Git tree is too large for recursive configuration discovery."
            )
        blobs = {
            str(item["path"]): item
            for item in tree.get("tree", [])
            if item.get("type") == "blob"
        }
        patterns_by_type = {
            "description": config.knowledge.description,
            "requirements": config.knowledge.requirements,
            "standards": config.knowledge.standards,
        }
        selected: dict[str, tuple[str, dict[str, Any]]] = {}
        missing_patterns: list[str] = []
        for source_type, patterns in patterns_by_type.items():
            for pattern in patterns:
                matches = [
                    (path, blob)
                    for path, blob in blobs.items()
                    if fnmatch(path, pattern)
                ]
                if not matches:
                    missing_patterns.append(pattern)
                for path, blob in matches:
                    selected.setdefault(path, (source_type, blob))
        if missing_patterns:
            raise RepositoryConfigurationError(
                "Repository knowledge paths matched no files: "
                + ", ".join(missing_patterns)
            )
        if len(selected) > self.client.settings.max_knowledge_files:
            raise RepositoryConfigurationError(
                f"Repository knowledge selects {len(selected)} files; the limit is "
                f"{self.client.settings.max_knowledge_files}."
            )

        sources: list[RepositorySource] = []
        for path, (source_type, item) in sorted(selected.items()):
            size = int(item.get("size") or 0)
            if size > self.client.settings.max_knowledge_file_bytes:
                raise RepositoryConfigurationError(
                    f"Repository knowledge file is too large: {path} ({size} bytes)."
                )
            blob = self.client.get_blob(full_name, str(item["sha"]), token)
            content = _decode_github_content(blob)
            stack = config.stacks_for_path(path)
            sources.append(
                RepositorySource(
                    path=path,
                    title=path,
                    content=content,
                    source_sha=str(item["sha"]),
                    source_type=source_type,
                    language=stack[0].language if len(stack) == 1 else None,
                    framework=stack[0].framework if len(stack) == 1 else None,
                )
            )
        return sources

    def _load_task_context(
        self,
        *,
        full_name: str,
        repository_id: int,
        title: str,
        body: str,
        token: str,
    ) -> tuple[list[TaskReference], list[tuple[str, RepositorySource]]]:
        text = f"{title}\n{body}"
        issue_numbers = _github_issue_numbers(text, full_name)
        references: list[TaskReference] = []
        sources: list[tuple[str, RepositorySource]] = []
        for number in issue_numbers[:20]:
            try:
                issue = self.client.get_issue(full_name, number, token)
            except GitHubAPIError as exc:
                if exc.status == 404:
                    continue
                raise
            if "pull_request" in issue:
                continue
            task_key = f"github:{repository_id}#{number}"
            references.append(TaskReference(kind="github_issue", value=task_key))
            content = f"# {issue.get('title') or f'Issue #{number}'}\n\n{issue.get('body') or ''}"
            sources.append(
                (
                    task_key,
                    RepositorySource(
                        path=f"issues/{number}.md",
                        title=f"GitHub issue #{number}: {issue.get('title') or ''}",
                        content=content,
                        source_sha=str(
                            issue.get("updated_at") or issue.get("id") or number
                        ),
                        source_type="task",
                    ),
                )
            )
        for ticket in _external_ticket_references(text):
            references.append(TaskReference(kind="ticket", value=ticket))
        return references, sources

    def _changed_files(
        self, full_name: str, number: int, token: str
    ) -> list[ChangedFile]:
        files = self.client.list_pull_request_files(full_name, number, token)
        if len(files) > self.client.settings.max_changed_files:
            raise RepositoryConfigurationError(
                f"Pull request changes {len(files)} files; the configured review limit is "
                f"{self.client.settings.max_changed_files}."
            )
        return [
            ChangedFile(
                path=str(item["filename"]),
                language=_language_for_path(str(item["filename"])),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                summary=(
                    f"{item.get('status', 'modified')}"
                    + (
                        f" (renamed from {item['previous_filename']})"
                        if item.get("previous_filename")
                        else ""
                    )
                ),
                diff_excerpt=str(item.get("patch") or "")[:12_000],
            )
            for item in files
        ]

    def _ci_checks(self, full_name: str, head_sha: str, token: str) -> dict[str, str]:
        checks: dict[str, str] = {}
        for check in self.client.list_check_runs(full_name, head_sha, token):
            name = str(check.get("name") or "")
            if not name or name == CHECK_NAME:
                continue
            checks[name] = str(
                check.get("conclusion") or check.get("status") or "unknown"
            )
        status = self.client.combined_status(full_name, head_sha, token)
        for item in status.get("statuses", []):
            context = str(item.get("context") or "")
            if context:
                checks[context] = str(item.get("state") or "unknown")
        return checks

    def _upsert_installation(
        self, connection, *, external_id: int, namespace: str
    ) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO provider_installations (
                    provider, provider_base_url, external_installation_id,
                    namespace, auth_reference, webhook_secret_reference
                ) VALUES ('github', %s, %s, %s, %s, %s)
                ON CONFLICT (provider, provider_base_url, external_installation_id)
                DO UPDATE SET namespace = EXCLUDED.namespace, enabled = true, updated_at = now()
                RETURNING id
                """,
                (
                    _provider_origin_from_api(self.client.settings.api_url),
                    str(external_id),
                    namespace,
                    "env:GITHUB_APP_ID+GITHUB_PRIVATE_KEY_PATH",
                    "env:GITHUB_WEBHOOK_SECRET",
                ),
            )
            return int(cursor.fetchone()[0])

    def _persist_result(self, request: ReviewRequest, result) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM repositories WHERE repository_key = %s",
                (request.resolved_repository_key(),),
            )
            repository_row = cursor.fetchone()
            cursor.execute(
                """
                INSERT INTO review_runs (
                    repository_id, provider, repository_key, external_id, head_sha,
                    profile_id, gate_state, request_payload, llm_brief,
                    insight_recommendation, insight_recommendation_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (provider, repository_key, external_id, head_sha)
                DO UPDATE SET profile_id = EXCLUDED.profile_id,
                              gate_state = EXCLUDED.gate_state,
                              request_payload = EXCLUDED.request_payload,
                              llm_brief = EXCLUDED.llm_brief,
                              insight_recommendation = EXCLUDED.insight_recommendation,
                              insight_recommendation_reason = EXCLUDED.insight_recommendation_reason
                RETURNING id
                """,
                (
                    int(repository_row[0]) if repository_row else None,
                    request.provider.value,
                    request.resolved_repository_key(),
                    request.external_id,
                    request.metadata.get("head_sha"),
                    result.applied_profile,
                    result.gate_state.value,
                    json.dumps(request.to_dict()),
                    result.llm_review_brief,
                    (
                        result.insight_recommendation.value
                        if result.insight_recommendation is not None
                        else None
                    ),
                    result.insight_recommendation_reason or None,
                ),
            )
            run_id = int(cursor.fetchone()[0])
            cursor.execute(
                "DELETE FROM review_findings WHERE review_run_id = %s", (run_id,)
            )
            for finding in result.findings:
                cursor.execute(
                    """
                    INSERT INTO review_findings (
                        review_run_id, severity, impact, rule_id, category, title, detail,
                        evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        run_id,
                        finding.severity.value,
                        finding.impact.value,
                        finding.rule_id,
                        finding.category,
                        finding.title,
                        finding.detail,
                        json.dumps(finding.evidence),
                    ),
                )

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install database dependencies with: pip install -e '.[db]'"
            ) from exc
        return psycopg.connect(self.database_url)


def _decode_github_content(payload: dict[str, Any]) -> str:
    if payload.get("encoding") != "base64" or not isinstance(
        payload.get("content"), str
    ):
        raise RepositoryConfigurationError(
            "GitHub returned unsupported repository content encoding."
        )
    try:
        encoded = "".join(payload["content"].split())
        decoded = base64.b64decode(encoded, validate=True)
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RepositoryConfigurationError(
            "Repository knowledge must be valid UTF-8 text."
        ) from exc


def _repository_path(full_name: str) -> str:
    parts = full_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid GitHub repository name: {full_name}")
    return "/".join(urlparse.quote(part, safe="") for part in parts)


def _repository_origin(repository: dict[str, Any]) -> str:
    parsed = urlparse.urlsplit(str(repository.get("html_url") or "https://github.com"))
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://github.com"
    )


def _provider_origin_from_api(api_url: str) -> str:
    parsed = urlparse.urlsplit(api_url)
    if parsed.netloc == "api.github.com":
        return "https://github.com"
    path = parsed.path.removesuffix("/api/v3")
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def _select_stack(
    config: RepositoryConfig, changed_files: list[ChangedFile]
) -> tuple[str | None, str | None, list[str]]:
    weights: dict[tuple[str, str | None], int] = {}
    packs: list[str] = []
    for changed_file in changed_files:
        for stack in config.stacks_for_path(changed_file.path):
            key = (stack.language, stack.framework)
            weights[key] = weights.get(key, 0) + changed_file.churn + 1
            packs.extend(stack.packs)
    if not weights:
        language_weights: dict[str, int] = {}
        for changed_file in changed_files:
            if changed_file.language:
                language_weights[changed_file.language] = (
                    language_weights.get(changed_file.language, 0)
                    + changed_file.churn
                    + 1
                )
        language = (
            max(language_weights, key=lambda language: language_weights[language])
            if language_weights
            else None
        )
        return language, None, []
    language, framework = max(weights, key=lambda stack: weights[stack])
    return language, framework, list(dict.fromkeys(packs))


def _github_issue_numbers(text: str, full_name: str) -> list[int]:
    numbers = [int(value) for value in re.findall(r"(?<![\w])#(\d+)\b", text)]
    owner, repository = (re.escape(part) for part in full_name.split("/", 1))
    url_pattern = rf"https?://github\.com/{owner}/{repository}/issues/(\d+)\b"
    numbers.extend(
        int(value) for value in re.findall(url_pattern, text, flags=re.IGNORECASE)
    )
    return list(dict.fromkeys(numbers))


def _external_ticket_references(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9]{1,15}-\d+\b", text)))


def _language_for_path(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".pyi": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".java": "java",
        ".kt": "kotlin",
        ".go": "go",
        ".rs": "rust",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".sql": "sql",
    }.get(suffix)


def _check_conclusion(state: GateState) -> str:
    if state == GateState.READY_FOR_HUMAN_REVIEW:
        return "success"
    if state in {GateState.BLOCKED, GateState.NEEDS_AUTHOR_UPDATES}:
        return "action_required"
    return "neutral"


def _check_title(state: GateState) -> str:
    return {
        GateState.READY_FOR_HUMAN_REVIEW: "Ready for human review",
        GateState.BLOCKED: "Author updates required",
        GateState.NEEDS_AUTHOR_UPDATES: "Author updates recommended",
        GateState.MANUAL_ESCALATION: "Manual review required",
    }[state]


def _format_findings(result) -> str:
    if not result.findings:
        return "No findings."
    lines = []
    for finding in result.findings:
        impact = (
            f" / {finding.impact.value.upper()}"
            if finding.impact != FindingImpact.ADVISORY
            else ""
        )
        lines.append(f"### {finding.severity.value.upper()}{impact}: {finding.title}")
        lines.append(finding.detail)
        if finding.evidence:
            lines.append("Evidence: " + ", ".join(finding.evidence[:10]))
        lines.append("")
    return "\n".join(lines)


def _api_error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body)
        return str(payload.get("message") or "request failed")[:1000]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")[:1000] or "request failed"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
