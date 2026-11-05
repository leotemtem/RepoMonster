from __future__ import annotations

import re
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
    # Check names that only count when reported by this GitHub App.
    required_ci_apps: dict[str, int] = field(default_factory=dict)
    allow_unverified_tickets: bool = False
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
            "allow_unverified_tickets": self.allow_unverified_tickets,
        }
        if self.auto_block is not None:
            settings["auto_block"] = self.auto_block.to_dict()
        return settings


MAX_CONFIG_BYTES = 65_536


class _PolicyLoader(yaml.SafeLoader):
    """Safe YAML without aliases, which can expand a small file enormously."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ValueError(".repomonster.yml must not use YAML aliases")
        return super().compose_node(parent, index)


def load_repository_config(content: str) -> RepositoryConfig:
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError(f".repomonster.yml must be under {MAX_CONFIG_BYTES} bytes")
    try:
        payload = _mapping(
            yaml.load(content, Loader=_PolicyLoader),  # nosec B506
            ".repomonster.yml",
        )
    except yaml.YAMLError as exc:
        raise ValueError(".repomonster.yml must contain valid YAML") from exc
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError(".repomonster.yml must declare version: 1")
    model = _mapping(payload.get("model", {}), "model")
    forbidden = {"api_key", "base_url", "endpoint"} & model.keys()
    if forbidden:
        raise ValueError(
            "Repository configuration cannot define model credentials or endpoints: "
            + ", ".join(sorted(forbidden))
        )

    profile = model.get("profile", "default")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9_-]+", profile):
        raise ValueError("model.profile must use lowercase letters, digits, '_' or '-'")

    context = _mapping(payload.get("knowledge", {}), "knowledge")
    knowledge = RepositoryKnowledgeConfig(
        description=_validated_paths(context.get("description", [])),
        requirements=_validated_paths(context.get("requirements", [])),
        standards=_validated_paths(context.get("standards", [])),
    )
    stacks = []
    stack_items = payload.get("stacks", [])
    if not isinstance(stack_items, list):
        raise ValueError("stacks must be a list of mappings")
    for item in stack_items:
        item = _mapping(item, "Each stack")
        paths = _validated_paths(item.get("paths", []))
        packs = _string_list(item.get("packs", []), "Stack packs")
        if not paths or not packs or not item.get("language"):
            raise ValueError("Each stack requires paths, language, and versioned packs")
        if not isinstance(item["language"], str):
            raise ValueError("Stack language must be a string")
        if item.get("framework") is not None and not isinstance(item["framework"], str):
            raise ValueError("Stack framework must be a string")
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

    checks = _mapping(payload.get("checks", {}), "checks")
    auto_block_payload = checks.get("auto_block")
    if auto_block_payload is not None:
        auto_block_payload = _mapping(auto_block_payload, "checks.auto_block")
        _boolean(
            auto_block_payload.get("require_poor_documentation", True),
            "checks.auto_block.require_poor_documentation",
        )
        for key in ("mode", "minimum_model_impact"):
            if key in auto_block_payload and not isinstance(
                auto_block_payload[key], str
            ):
                raise ValueError(f"checks.auto_block.{key} must be a string")
    try:
        auto_block = (
            AutoBlockPolicy.from_dict(auto_block_payload)
            if auto_block_payload is not None
            else None
        )
    except ValueError as exc:
        raise ValueError(f"Invalid checks.auto_block policy: {exc}") from exc
    required_ci, required_ci_apps = _required_checks(checks.get("required_ci", []))
    return RepositoryConfig(
        version=1,
        knowledge=knowledge,
        stacks=stacks,
        required_ci=required_ci,
        required_ci_apps=required_ci_apps,
        allow_unverified_tickets=_boolean(
            checks.get("allow_unverified_tickets", False),
            "checks.allow_unverified_tickets",
        ),
        require_task_reference=_boolean(
            checks.get("require_task_reference", True), "checks.require_task_reference"
        ),
        require_test_evidence=_boolean(
            checks.get("require_test_evidence", True), "checks.require_test_evidence"
        ),
        model_profile=profile,
        auto_block=auto_block,
    )


def _validated_paths(value: Any) -> list[str]:
    values = _string_list([value] if isinstance(value, str) else value, "Paths")
    for item in values:
        path = PurePosixPath(str(item))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"Repository knowledge path must stay inside the repository: {item}"
            )
    return [str(item) for item in values]


def _required_checks(value: Any) -> tuple[list[str], dict[str, int]]:
    if not isinstance(value, list):
        raise ValueError("checks.required_ci must be a list")
    names: list[str] = []
    apps: dict[str, int] = {}
    for item in value:
        if isinstance(item, str):
            names.append(item)
            continue
        entry = item if isinstance(item, dict) else {}
        name, app_id = entry.get("name"), entry.get("app_id")
        if (
            set(entry) != {"name", "app_id"}
            or not isinstance(name, str)
            or type(app_id) is not int
            or app_id <= 0
        ):
            raise ValueError(
                "Each checks.required_ci entry must be a check name, or a mapping "
                "with a name and a positive integer app_id"
            )
        names.append(name)
        apps[name] = app_id
    return names, apps


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value
