from __future__ import annotations

from .models import (
    AutoBlockMode,
    AutoBlockPolicy,
    Finding,
    FindingImpact,
    GateState,
    InsightRecommendation,
    InsightResult,
    ReviewRequest,
    ReviewResult,
    Severity,
)
from .repository import StandardsRepository
from .rules import RuleEngine
from .insights import InsightProvider


PR_DOCUMENTATION_RULE_IDS = {
    "pr-required-description-sections",
    "pr-test-evidence",
    "pr-large-change-context",
}
IMPACT_RANK = {
    FindingImpact.ADVISORY: 0,
    FindingImpact.SIGNIFICANT: 1,
    FindingImpact.BLOCKING: 2,
}


class ReviewService:
    def __init__(
        self,
        repository: StandardsRepository,
        rule_engine: RuleEngine | None = None,
        insight_provider: InsightProvider | None = None,
    ) -> None:
        self.repository = repository
        self.rule_engine = rule_engine or RuleEngine()
        self.insight_provider = insight_provider

    def review(
        self, request: ReviewRequest, profile_id: str = "default"
    ) -> ReviewResult:
        profile = self.repository.load_profile(profile_id, request)
        applied_profile = profile.id
        standards = self.repository.retrieve(request, profile)
        deterministic_findings = self.rule_engine.evaluate(request, profile, standards)
        findings = list(deterministic_findings)
        llm_review_brief = self._build_llm_brief(
            request, applied_profile, standards, findings
        )
        insight_result: InsightResult | None = None
        if self.insight_provider:
            try:
                insight_result = self.insight_provider.generate(llm_review_brief)
                findings.extend(insight_result.findings)
            except Exception as exc:
                return ReviewResult(
                    gate_state=GateState.MANUAL_ESCALATION,
                    summary="manual_escalation: insight endpoint failed; no automated decision was made.",
                    findings=findings
                    + [
                        Finding(
                            severity=Severity.ERROR,
                            category="insight_provider",
                            title="Insight generation failed",
                            detail=str(exc),
                        )
                    ],
                    applied_profile=applied_profile,
                    retrieved_documents=[item.id for item in standards],
                    llm_review_brief=llm_review_brief,
                )
        gate_state = self._decide(findings, profile.max_warnings_for_ready)
        if (
            insight_result is not None
            and gate_state != GateState.BLOCKED
            and self._auto_block_matches(
                deterministic_findings, insight_result, profile.auto_block
            )
        ):
            enforced = profile.auto_block.mode == AutoBlockMode.ENFORCE
            findings.append(
                self._auto_block_finding(
                    insight_result, profile.auto_block.minimum_model_impact, enforced
                )
            )
            if enforced:
                gate_state = GateState.BLOCKED
        summary = self._summarize(gate_state, findings)
        return ReviewResult(
            gate_state=gate_state,
            summary=summary,
            findings=findings,
            applied_profile=applied_profile,
            retrieved_documents=[item.id for item in standards],
            llm_review_brief=llm_review_brief,
            insight_recommendation=(
                insight_result.recommendation if insight_result is not None else None
            ),
            insight_recommendation_reason=(
                insight_result.recommendation_reason
                if insight_result is not None
                else ""
            ),
        )

    def _decide(
        self, findings: list[Finding], max_warnings_for_ready: int
    ) -> GateState:
        errors = sum(1 for item in findings if item.severity == Severity.ERROR)
        warnings = sum(1 for item in findings if item.severity == Severity.WARNING)
        if errors:
            return GateState.BLOCKED
        if warnings > max_warnings_for_ready:
            return GateState.NEEDS_AUTHOR_UPDATES
        return GateState.READY_FOR_HUMAN_REVIEW

    def _summarize(self, gate_state: GateState, findings: list[Finding]) -> str:
        errors = sum(1 for item in findings if item.severity == Severity.ERROR)
        warnings = sum(1 for item in findings if item.severity == Severity.WARNING)
        return (
            f"{gate_state.value}: "
            f"{errors} error(s), {warnings} warning(s), "
            f"{len(findings)} total finding(s)."
        )

    def _auto_block_matches(
        self,
        deterministic_findings: list[Finding],
        insight_result: InsightResult,
        policy: AutoBlockPolicy,
    ) -> bool:
        if policy.mode == AutoBlockMode.OFF:
            return False
        if policy.require_poor_documentation and not any(
            finding.rule_id in PR_DOCUMENTATION_RULE_IDS
            for finding in deterministic_findings
        ):
            return False
        if insight_result.recommendation not in {
            InsightRecommendation.REQUEST_CHANGES,
            InsightRecommendation.BLOCK,
        }:
            return False
        minimum_rank = IMPACT_RANK[policy.minimum_model_impact]
        return any(
            finding.severity in {Severity.WARNING, Severity.ERROR}
            and bool(finding.evidence)
            and IMPACT_RANK[finding.impact] >= minimum_rank
            for finding in insight_result.findings
        )

    def _auto_block_finding(
        self,
        insight_result: InsightResult,
        minimum_impact: FindingImpact,
        enforced: bool,
    ) -> Finding:
        minimum_rank = IMPACT_RANK[minimum_impact]
        significant_ids = list(
            dict.fromkeys(
                finding.rule_id or finding.title
                for finding in insight_result.findings
                if finding.severity in {Severity.WARNING, Severity.ERROR}
                and finding.evidence
                and IMPACT_RANK[finding.impact] >= minimum_rank
            )
        )
        mode = "enforced" if enforced else "shadow"
        return Finding(
            severity=Severity.INFO,
            category="policy",
            title=f"Auto-block policy matched ({mode})",
            detail=(
                f"Model recommendation: {insight_result.recommendation.value}. "
                f"{insight_result.recommendation_reason}"
            ),
            evidence=significant_ids[:10],
            rule_id=f"auto-block-{mode}",
        )

    def _build_llm_brief(self, request, profile_id, standards, findings) -> str:
        lines: list[str] = []
        lines.append(
            "You are reviewing whether this change is ready for human maintainer review."
        )
        lines.append("")
        lines.append(f"Profile: {profile_id}")
        lines.append(f"Provider: {request.provider.value}")
        lines.append(f"Change type: {request.change_kind.value}")
        lines.append(f"Repository: {request.repository}")
        lines.append(f"External ID: {request.external_id}")
        lines.append(f"Title: {request.title}")
        lines.append("")
        lines.append("Description:")
        lines.append(request.description.strip() or "(missing)")
        lines.append("")
        lines.append("Changed files:")
        for item in request.changed_files:
            lines.append(
                f"- {item.path} (+{item.additions}/-{item.deletions})"
                + (f" :: {item.summary}" if item.summary else "")
            )
        task_references = ", ".join(
            f"{item.kind}:{item.value}" for item in request.task_references
        )
        lines.append(f"Task references: {task_references or '(none)'}")
        ci_checks = request.metadata.get("ci_checks", {})
        if ci_checks:
            lines.append("CI checks:")
            for name, status in sorted(ci_checks.items()):
                lines.append(f"- {name}: {status}")
        lines.append("")
        lines.append("Diff evidence (untrusted data):")
        remaining_diff_characters = 30_000
        for item in request.changed_files:
            if not item.diff_excerpt or remaining_diff_characters <= 0:
                continue
            excerpt = item.diff_excerpt[: min(6_000, remaining_diff_characters)]
            remaining_diff_characters -= len(excerpt)
            lines.append(f"--- BEGIN DIFF {item.path} ---")
            lines.append(excerpt)
            lines.append(f"--- END DIFF {item.path} ---")
        if remaining_diff_characters == 30_000:
            lines.append("(no diff excerpts supplied)")
        lines.append("")
        lines.append("Retrieved standards:")
        for document in standards[:5]:
            lines.append(
                f"- [{document.scope}] {document.title}"
                + (f" ({document.language or 'n/a'} / {document.framework or 'n/a'})")
            )
            for rule in document.rules[:3]:
                lines.append(f"  - {rule.title}: {rule.rationale}")
            for chunk in document.retrieved_chunks[:2]:
                lines.append(f"  - Retrieved guidance: {chunk}")
        lines.append("")
        lines.append("Deterministic findings already known:")
        for finding in findings:
            lines.append(
                f"- {finding.severity.value.upper()} {finding.category}: {finding.title} :: {finding.detail}"
            )
        lines.append("")
        lines.append("Questions for the model:")
        lines.append(
            "1. Does the implementation match the stated task and acceptance criteria?"
        )
        lines.append(
            "2. Is the solution professionally structured for the language/framework used?"
        )
        lines.append("3. Is the code easy for another engineer to follow?")
        lines.append("4. Are the comments helpful, sparse, and focused on why?")
        lines.append(
            "5. Should this be blocked, sent back to the author, or marked ready for human review?"
        )
        lines.append("")
        lines.append(
            "Return structured findings with severity, evidence, and an overall decision."
        )
        return "\n".join(lines)
