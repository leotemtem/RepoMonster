from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .models import ChangeRequestKind, Provider


@dataclass(slots=True)
class NormalizedWebhookEvent:
    provider: Provider
    change_kind: ChangeRequestKind
    repository: str
    repository_key: str
    repository_external_id: str
    external_id: str
    head_sha: str | None
    installation_id: str | None
    title: str
    description: str
    source_branch: str | None
    target_branch: str | None
    url: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "change_kind": self.change_kind.value,
            "repository": self.repository,
            "repository_key": self.repository_key,
            "repository_external_id": self.repository_external_id,
            "external_id": self.external_id,
            "head_sha": self.head_sha,
            "installation_id": self.installation_id,
            "title": self.title,
            "description": self.description,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "url": self.url,
        }


def normalize_github_event(
    event_name: str, payload: dict[str, Any]
) -> NormalizedWebhookEvent:
    repository = payload.get("repository", {})
    repository_id = str(repository.get("id", ""))
    provider_origin = _origin(repository.get("html_url")) or "https://github.com"
    repository_key = f"github:{provider_origin}:{repository_id}"
    installation_id = str(payload.get("installation", {}).get("id") or "") or None
    if event_name == "pull_request":
        pr = payload.get("pull_request", {})
        return NormalizedWebhookEvent(
            provider=Provider.GITHUB,
            change_kind=ChangeRequestKind.PULL_REQUEST,
            repository=repository.get("full_name", ""),
            repository_key=repository_key,
            repository_external_id=repository_id,
            external_id=str(pr.get("number") or payload.get("number") or ""),
            head_sha=pr.get("head", {}).get("sha"),
            installation_id=installation_id,
            title=pr.get("title", ""),
            description=pr.get("body", "") or "",
            source_branch=pr.get("head", {}).get("ref"),
            target_branch=pr.get("base", {}).get("ref"),
            url=pr.get("html_url"),
        )

    if event_name == "push":
        return NormalizedWebhookEvent(
            provider=Provider.GITHUB,
            change_kind=ChangeRequestKind.PUSH,
            repository=repository.get("full_name", ""),
            repository_key=repository_key,
            repository_external_id=repository_id,
            external_id=str(payload.get("after", "")),
            head_sha=payload.get("after"),
            installation_id=installation_id,
            title=payload.get("head_commit", {}).get("message", "") or "push",
            description="",
            source_branch=(payload.get("ref", "") or "").removeprefix("refs/heads/"),
            target_branch=None,
            url=payload.get("compare"),
        )

    raise ValueError(f"Unsupported GitHub event: {event_name}")


def normalize_gitlab_event(
    event_name: str, payload: dict[str, Any]
) -> NormalizedWebhookEvent:
    lowered = event_name.lower().strip()
    project = payload.get("project", {})
    project_id = str(project.get("id") or payload.get("project_id") or "")
    provider_origin = _origin(project.get("web_url")) or "https://gitlab.com"
    repository_key = f"gitlab:{provider_origin}:{project_id}"

    if lowered == "merge request hook":
        attrs = payload.get("object_attributes", {})
        return NormalizedWebhookEvent(
            provider=Provider.GITLAB,
            change_kind=ChangeRequestKind.MERGE_REQUEST,
            repository=project.get("path_with_namespace", ""),
            repository_key=repository_key,
            repository_external_id=project_id,
            external_id=str(attrs.get("iid") or attrs.get("id") or ""),
            head_sha=(attrs.get("last_commit") or {}).get("id"),
            installation_id=None,
            title=attrs.get("title", ""),
            description=attrs.get("description", "") or "",
            source_branch=attrs.get("source_branch"),
            target_branch=attrs.get("target_branch"),
            url=attrs.get("url"),
        )

    if lowered == "push hook":
        return NormalizedWebhookEvent(
            provider=Provider.GITLAB,
            change_kind=ChangeRequestKind.PUSH,
            repository=project.get("path_with_namespace", ""),
            repository_key=repository_key,
            repository_external_id=project_id,
            external_id=str(payload.get("after", "")),
            head_sha=payload.get("after"),
            installation_id=None,
            title=(payload.get("commits") or [{}])[-1].get("message", "") or "push",
            description="",
            source_branch=(payload.get("ref", "") or "").removeprefix("refs/heads/"),
            target_branch=None,
            url=project.get("web_url"),
        )

    raise ValueError(f"Unsupported GitLab event: {event_name}")


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"
