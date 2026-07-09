from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Provider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


class ChangeRequestKind(str, Enum):
    PULL_REQUEST = "pull_request"
    MERGE_REQUEST = "merge_request"
    PUSH = "push"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FindingImpact(str, Enum):
    ADVISORY = "advisory"
    SIGNIFICANT = "significant"
    BLOCKING = "blocking"


class InsightRecommendation(str, Enum):
    READY = "ready"
    REQUEST_CHANGES = "request_changes"
    BLOCK = "block"


class AutoBlockMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class GateState(str, Enum):
    BLOCKED = "blocked"
    NEEDS_AUTHOR_UPDATES = "needs_author_updates"
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    MANUAL_ESCALATION = "manual_escalation"


@dataclass(frozen=True, slots=True)
class AutoBlockPolicy:
    mode: AutoBlockMode = AutoBlockMode.OFF
    require_poor_documentation: bool = True
    minimum_model_impact: FindingImpact = FindingImpact.SIGNIFICANT

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "AutoBlockPolicy":
        payload = payload or {}
        minimum_model_impact = FindingImpact(
            str(payload.get("minimum_model_impact", FindingImpact.SIGNIFICANT.value))
        )
        if minimum_model_impact == FindingImpact.ADVISORY:
            raise ValueError("minimum_model_impact must be significant or blocking")
        return cls(
            mode=AutoBlockMode(str(payload.get("mode", AutoBlockMode.OFF.value))),
            require_poor_documentation=bool(
                payload.get("require_poor_documentation", True)
            ),
            minimum_model_impact=minimum_model_impact,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "require_poor_documentation": self.require_poor_documentation,
            "minimum_model_impact": self.minimum_model_impact.value,
        }


@dataclass(slots=True)
class ChangedFile:
    path: str
    language: str | None = None
    additions: int = 0
    deletions: int = 0
    summary: str = ""
    diff_excerpt: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChangedFile":
        return cls(
            path=payload["path"],
            language=payload.get("language"),
            additions=int(payload.get("additions", 0)),
            deletions=int(payload.get("deletions", 0)),
            summary=payload.get("summary", ""),
            diff_excerpt=payload.get("diff_excerpt", ""),
        )

    @property
    def churn(self) -> int:
        return self.additions + self.deletions

    def is_test_file(self) -> bool:
        lowered = self.path.lower()
        return (
            "/test" in lowered
            or "/tests/" in lowered
            or lowered.startswith("test")
            or lowered.startswith("tests/")
            or lowered.endswith("_test.py")
            or lowered.endswith(".spec.ts")
            or lowered.endswith(".spec.tsx")
            or lowered.endswith(".test.ts")
            or lowered.endswith(".test.tsx")
        )

    def is_documentation_file(self) -> bool:
        lowered = self.path.lower()
        return (
            lowered.endswith(".md") or lowered.endswith(".mdx") or "/docs/" in lowered
        )

    def is_source_file(self) -> bool:
        return not self.is_test_file() and not self.is_documentation_file()


@dataclass(slots=True)
class TaskReference:
    kind: str
    value: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskReference":
        return cls(kind=payload["kind"], value=payload["value"])


@dataclass(slots=True)
class ReviewRequest:
    provider: Provider
    change_kind: ChangeRequestKind
    repository: str
    external_id: str
    title: str
    description: str
    repository_key: str | None = None
    target_branch: str | None = None
    source_branch: str | None = None
    language: str | None = None
    framework: str | None = None
    standard_packs: list[str] = field(default_factory=list)
    task_references: list[TaskReference] = field(default_factory=list)
    changed_files: list[ChangedFile] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewRequest":
        return cls(
            provider=Provider(payload["provider"]),
            change_kind=ChangeRequestKind(payload["change_kind"]),
            repository=payload["repository"],
            external_id=str(payload["external_id"]),
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            repository_key=payload.get("repository_key"),
            target_branch=payload.get("target_branch"),
            source_branch=payload.get("source_branch"),
            language=payload.get("language"),
            framework=payload.get("framework"),
            standard_packs=list(payload.get("standard_packs", [])),
            task_references=[
                TaskReference.from_dict(item)
                for item in payload.get("task_references", [])
            ],
            changed_files=[
                ChangedFile.from_dict(item) for item in payload.get("changed_files", [])
            ],
            metadata=payload.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_churn(self) -> int:
        return sum(item.churn for item in self.changed_files)

    @property
    def source_files(self) -> list[ChangedFile]:
        return [item for item in self.changed_files if item.is_source_file()]

    @property
    def test_files(self) -> list[ChangedFile]:
        return [item for item in self.changed_files if item.is_test_file()]

    @property
    def documentation_files(self) -> list[ChangedFile]:
        return [item for item in self.changed_files if item.is_documentation_file()]

    def inferred_language(self) -> str | None:
        if self.language:
            return self.language
        seen: dict[str, int] = {}
        for item in self.changed_files:
            if not item.language:
                continue
            seen[item.language] = seen.get(item.language, 0) + item.churn + 1
        if not seen:
            return None
        return max(seen, key=lambda language: seen[language])

    def resolved_repository_key(self) -> str:
        if self.repository_key:
            return self.repository_key
        provider_base_url = self.metadata.get("provider_base_url", "")
        external_id = self.metadata.get("external_repository_id")
        if provider_base_url and external_id:
            return f"{self.provider.value}:{provider_base_url}:{external_id}"
        return f"{self.provider.value}:{self.repository}"


@dataclass(slots=True)
class StandardRule:
    id: str
    severity: Severity
    gate: bool
    title: str
    rationale: str
    check_statements: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StandardRule":
        return cls(
            id=payload["id"],
            severity=Severity(payload["severity"]),
            gate=bool(payload.get("gate", False)),
            title=payload["title"],
            rationale=payload.get("rationale", ""),
            check_statements=list(payload.get("check_statements", [])),
        )


@dataclass(slots=True)
class StandardDocument:
    id: str
    scope: str
    language: str | None
    framework: str | None
    title: str
    tags: list[str]
    source_links: list[str]
    rules: list[StandardRule]
    retrieved_chunks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StandardDocument":
        return cls(
            id=payload["id"],
            scope=payload["scope"],
            language=payload.get("language"),
            framework=payload.get("framework"),
            title=payload["title"],
            tags=list(payload.get("tags", [])),
            source_links=list(payload.get("source_links", [])),
            rules=[StandardRule.from_dict(item) for item in payload.get("rules", [])],
            retrieved_chunks=list(payload.get("retrieved_chunks", [])),
        )


@dataclass(slots=True)
class ReviewProfile:
    id: str
    name: str
    required_description_sections: list[str]
    require_task_reference_for_code_changes: bool
    require_test_evidence_when_code_changes: bool
    max_warnings_for_ready: int
    large_change_line_threshold: int
    labels: dict[str, str]
    retrieval_order: list[str]
    required_ci: list[str] = field(default_factory=list)
    auto_block: AutoBlockPolicy = field(default_factory=AutoBlockPolicy)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewProfile":
        return cls(
            id=payload["id"],
            name=payload["name"],
            required_description_sections=list(
                payload.get("required_description_sections", [])
            ),
            require_task_reference_for_code_changes=bool(
                payload.get("require_task_reference_for_code_changes", True)
            ),
            require_test_evidence_when_code_changes=bool(
                payload.get("require_test_evidence_when_code_changes", True)
            ),
            max_warnings_for_ready=int(payload.get("max_warnings_for_ready", 1)),
            large_change_line_threshold=int(
                payload.get("large_change_line_threshold", 400)
            ),
            labels=dict(payload.get("labels", {})),
            retrieval_order=list(payload.get("retrieval_order", [])),
            required_ci=list(payload.get("required_ci", [])),
            auto_block=AutoBlockPolicy.from_dict(payload.get("auto_block")),
        )


@dataclass(slots=True)
class Finding:
    severity: Severity
    category: str
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    rule_id: str | None = None
    impact: FindingImpact = FindingImpact.ADVISORY

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "rule_id": self.rule_id,
            "impact": self.impact.value,
        }


@dataclass(slots=True)
class InsightResult:
    findings: list[Finding]
    recommendation: InsightRecommendation
    recommendation_reason: str


@dataclass(slots=True)
class ReviewResult:
    gate_state: GateState
    summary: str
    findings: list[Finding]
    applied_profile: str
    retrieved_documents: list[str]
    llm_review_brief: str
    insight_recommendation: InsightRecommendation | None = None
    insight_recommendation_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_state": self.gate_state.value,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
            "applied_profile": self.applied_profile,
            "retrieved_documents": self.retrieved_documents,
            "llm_review_brief": self.llm_review_brief,
            "insight_recommendation": (
                self.insight_recommendation.value
                if self.insight_recommendation is not None
                else None
            ),
            "insight_recommendation_reason": self.insight_recommendation_reason,
        }
