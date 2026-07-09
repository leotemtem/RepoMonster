from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

import yaml

from .models import AutoBlockPolicy


@dataclass(frozen=True, slots=True)
class StackSelection:
    paths: list[str]
    language: str
    framework: str | None
    packs: list[str]

    def matches(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.paths)


@dataclass(frozen=True, slots=True)
class RepositoryKnowledgeConfig:
    description: list[str]
    requirements: list[str]
    standards: list[str]


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    version: int
    knowledge: RepositoryKnowledgeConfig
    stacks: list[StackSelection]
    required_ci: list[str] = field(default_factory=list)
    require_task_reference: bool = True
    require_test_evidence: bool = True
    model_profile: str = "default"
    auto_block: AutoBlockPolicy | None = None

    def stacks_for_path(self, path: str) -> list[StackSelection]:
        return [stack for stack in self.stacks if stack.matches(path)]

    @property
    def selected_packs(self) -> list[str]:
        return list(
            dict.fromkeys(pack for stack in self.stacks for pack in stack.packs)
        )

    @property
    def review_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "required_ci": self.required_ci,
            "require_task_reference": self.require_task_reference,
            "require_test_evidence": self.require_test_evidence,
        }
        if self.auto_block is not None:
            settings["auto_block"] = self.auto_block.to_dict()
        return settings


def load_repository_config(content: str) -> RepositoryConfig:
    payload = yaml.safe_load(content) or {}
    if payload.get("version") != 1:
        raise ValueError(".repomonster.yml must declare version: 1")
    model = payload.get("model", {})
    forbidden = {"api_key", "base_url", "endpoint"} & model.keys()
    if forbidden:
        raise ValueError(
            "Repository configuration cannot define model credentials or endpoints: "
            + ", ".join(sorted(forbidden))
        )

    context = payload.get("knowledge", {})
    knowledge = RepositoryKnowledgeConfig(
        description=_validated_paths(context.get("description", [])),
        requirements=_validated_paths(context.get("requirements", [])),
        standards=_validated_paths(context.get("standards", [])),
    )
    stacks = []
    for item in payload.get("stacks", []):
        paths = _validated_paths(item.get("paths", []))
        packs = [str(value) for value in item.get("packs", [])]
        if not paths or not packs or not item.get("language"):
            raise ValueError("Each stack requires paths, language, and versioned packs")
        if any("@" not in pack for pack in packs):
            raise ValueError("Standard packs must be pinned as pack@version")
        stacks.append(
            StackSelection(
                paths=paths,
                language=str(item["language"]).lower(),
                framework=(
                    str(item["framework"]).lower() if item.get("framework") else None
                ),
                packs=packs,
            )
        )
    if not stacks:
        raise ValueError("At least one stack must be configured")

    checks = payload.get("checks", {})
    auto_block_payload = checks.get("auto_block")
    if auto_block_payload is not None and not isinstance(auto_block_payload, dict):
        raise ValueError("checks.auto_block must be a mapping")
    try:
        auto_block = (
            AutoBlockPolicy.from_dict(auto_block_payload)
            if auto_block_payload is not None
            else None
        )
    except ValueError as exc:
        raise ValueError(f"Invalid checks.auto_block policy: {exc}") from exc
    return RepositoryConfig(
        version=1,
        knowledge=knowledge,
        stacks=stacks,
        required_ci=list(checks.get("required_ci", [])),
        require_task_reference=bool(checks.get("require_task_reference", True)),
        require_test_evidence=bool(checks.get("require_test_evidence", True)),
        model_profile=str(model.get("profile", "default")),
        auto_block=auto_block,
    )


def _validated_paths(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    for item in values:
        path = PurePosixPath(str(item))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"Repository knowledge path must stay inside the repository: {item}"
            )
    return [str(item) for item in values]
