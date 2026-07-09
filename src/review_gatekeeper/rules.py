from __future__ import annotations

from .models import Finding, ReviewProfile, ReviewRequest, Severity, StandardDocument


class RuleEngine:
    def evaluate(
        self,
        request: ReviewRequest,
        profile: ReviewProfile,
        standards: list[StandardDocument],
    ) -> list[Finding]:
        findings: list[Finding] = []
        findings.extend(self._check_description_sections(request, profile))
        findings.extend(self._check_task_reference(request, profile))
        findings.extend(self._check_tests(request, profile))
        findings.extend(self._check_required_ci(request, profile))
        findings.extend(self._check_large_change_without_explanation(request, profile))
        findings.extend(self._check_docs_for_contract_changes(request))
        findings.extend(self._surface_retrieved_guidance(standards))
        return findings

    def _check_required_ci(
        self, request: ReviewRequest, profile: ReviewProfile
    ) -> list[Finding]:
        checks = request.metadata.get("ci_checks", {})
        findings: list[Finding] = []
        for name in profile.required_ci:
            status = str(checks.get(name, "missing")).lower()
            if status in {"success", "successful", "passed"}:
                continue
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    category="ci",
                    title=f"Required CI check is not successful: {name}",
                    detail=f"The repository requires '{name}', but its status is '{status}'.",
                    evidence=[f"{name}={status}"],
                )
            )
        return findings

    def _check_description_sections(
        self, request: ReviewRequest, profile: ReviewProfile
    ) -> list[Finding]:
        missing = [
            section
            for section in profile.required_description_sections
            if section.lower() not in request.description.lower()
        ]
        if not missing:
            return []
        return [
            Finding(
                severity=Severity.ERROR,
                category="description",
                title="Required change request sections are missing",
                detail=(
                    "The change request description is missing required review sections: "
                    + ", ".join(missing)
                ),
                evidence=[request.title or request.external_id],
                rule_id="pr-required-description-sections",
            )
        ]

    def _check_task_reference(
        self, request: ReviewRequest, profile: ReviewProfile
    ) -> list[Finding]:
        if not profile.require_task_reference_for_code_changes:
            return []
        if not request.source_files:
            return []
        if request.task_references:
            return []
        return [
            Finding(
                severity=Severity.ERROR,
                category="traceability",
                title="No linked task or issue",
                detail=(
                    "Code changed, but the request is not linked to a ticket, issue, or design note."
                ),
                evidence=[item.path for item in request.source_files[:5]],
            )
        ]

    def _check_tests(
        self, request: ReviewRequest, profile: ReviewProfile
    ) -> list[Finding]:
        if not profile.require_test_evidence_when_code_changes:
            return []
        if not request.source_files:
            return []

        findings: list[Finding] = []

        if not request.test_files:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    category="testing",
                    title="Code changed without accompanying test file changes",
                    detail=(
                        "Source files changed but no obvious test file changes were provided in the request."
                    ),
                    evidence=[item.path for item in request.source_files[:5]],
                )
            )

        if "test evidence" not in request.description.lower():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    category="testing",
                    title="Test evidence is missing from the description",
                    detail=(
                        "The profile requires test evidence when source code changes, but none was documented."
                    ),
                    evidence=[request.title or request.external_id],
                    rule_id="pr-test-evidence",
                )
            )

        return findings

    def _check_large_change_without_explanation(
        self, request: ReviewRequest, profile: ReviewProfile
    ) -> list[Finding]:
        if request.total_churn < profile.large_change_line_threshold:
            return []
        if len(request.description.strip()) >= 250:
            return []
        return [
            Finding(
                severity=Severity.WARNING,
                category="clarity",
                title="Large change with weak written context",
                detail=(
                    "The code churn is large relative to the written explanation. Reviewers will likely need more context."
                ),
                evidence=[f"total_churn={request.total_churn}"],
                rule_id="pr-large-change-context",
            )
        ]

    def _check_docs_for_contract_changes(self, request: ReviewRequest) -> list[Finding]:
        changed_api_paths = [
            item.path
            for item in request.source_files
            if any(
                token in item.path.lower()
                for token in ("api", "route", "controller", "schema")
            )
        ]
        if not changed_api_paths:
            return []
        if request.documentation_files:
            return []
        return [
            Finding(
                severity=Severity.WARNING,
                category="documentation",
                title="Possible contract change without docs change",
                detail=(
                    "Files that look like API or schema paths changed, but no documentation file changes were provided."
                ),
                evidence=changed_api_paths[:5],
            )
        ]

    def _surface_retrieved_guidance(
        self, standards: list[StandardDocument]
    ) -> list[Finding]:
        findings: list[Finding] = []
        for document in standards[:2]:
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    category="retrieval",
                    title=f"Retrieved standards: {document.title}",
                    detail="This document should shape the LLM review pass for maintainability and fit.",
                    evidence=[rule.title for rule in document.rules[:3]],
                )
            )
        return findings
