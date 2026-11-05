from __future__ import annotations

import html
import re

from ..database import sanitize_storage
from ..models import FindingImpact, GateState

CHECK_NAME = "RepoMonster review gate"


def _check_conclusion(state: GateState) -> str:
    # GitHub treats `neutral` as passing for required checks, so anything short of
    # ready, including an escalation, has to fail closed.
    if state == GateState.READY_FOR_HUMAN_REVIEW:
        return "success"
    return "action_required"


def _check_title(state: GateState) -> str:
    return {
        GateState.READY_FOR_HUMAN_REVIEW: "Ready for human review",
        GateState.BLOCKED: "Author updates required",
        GateState.NEEDS_AUTHOR_UPDATES: "Author updates recommended",
        GateState.MANUAL_ESCALATION: "Manual review required",
    }[state]


def _terminal_error_code(error: str) -> str:
    error_type = error.split(":", 1)[0]
    if error_type == "GitHubAPIError":
        return "github_api_error"
    if error_type == "InsightResponseError":
        return "model_error"
    if error_type == "HTTPResponseError":
        return "embedding_error"
    # Queue errors retain their type name, while diagnostics stay in worker logs.
    try:
        import psycopg
        import psycopg_pool

        candidate = getattr(psycopg.errors, error_type, None) or getattr(
            psycopg_pool, error_type, None
        )
        if isinstance(candidate, type) and issubclass(candidate, psycopg.Error):
            return "database_error"
    except ImportError:  # pragma: no cover
        pass
    return "internal_error"


def _escape_markdown(value: str) -> str:
    # Escape markup punctuation and flatten line breaks in untrusted fields.
    value = html.escape(sanitize_storage(value), quote=False)
    return (
        re.sub(r"([\\`*_{}\[\]()#+\-.!|>~:/@])", r"\\\1", value)
        .replace("\r", " ")
        .replace("\n", " ")
    )


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
        lines.append(
            f"### {finding.severity.value.upper()}{impact}: {_escape_markdown(finding.title)}"
        )
        lines.append(_escape_markdown(finding.detail))
        if finding.evidence:
            lines.append(
                "Evidence: "
                + ", ".join(_escape_markdown(item) for item in finding.evidence[:10])
            )
        lines.append("")
    return "\n".join(lines)
