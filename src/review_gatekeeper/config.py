from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

import yaml


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

    def stacks_for_path(self, path: str) -> list[StackSelection]:
        return [stack for stack in self.stacks if stack.matches(path)]

    @property
    def selected_packs(self) -> list[str]:
        return list(dict.fromkeys(pack for stack in self.stacks for pack in stack.packs))


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
                framework=(str(item["framework"]).lower() if item.get("framework") else None),
                packs=packs,
            )
        )
    if not stacks:
        raise ValueError("At least one stack must be configured")

    checks = payload.get("checks", {})
    return RepositoryConfig(
        version=1,
        knowledge=knowledge,
        stacks=stacks,
        required_ci=list(checks.get("required_ci", [])),
        require_task_reference=bool(checks.get("require_task_reference", True)),
        require_test_evidence=bool(checks.get("require_test_evidence", True)),
        model_profile=str(model.get("profile", "default")),
    )


def _validated_paths(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    for item in values:
        path = PurePosixPath(str(item))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Repository knowledge path must stay inside the repository: {item}")
    return [str(item) for item in values]
