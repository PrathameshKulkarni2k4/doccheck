"""Render findings as text, JSON, or GitHub annotations."""

from __future__ import annotations

import json
from typing import List, Sequence

from .models import Finding

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITIES = ("high", "medium", "low")


def sort_findings(findings: Sequence[Finding]) -> List[Finding]:
    return sorted(
        findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.doc, f.line)
    )


def _tally(findings: Sequence[Finding]) -> str:
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return "   ".join(f"{s} {counts[s]}" for s in SEVERITIES if counts[s])


def render_text(findings: Sequence[Finding], doc_count: int) -> str:
    lines: List[str] = [""]
    if not findings:
        lines.append(f"doccheck: clean - {doc_count} document(s), no stale references.")
        lines.append("")
        return "\n".join(lines)

    lines.append("doccheck report")
    lines.append("=" * 64)
    lines.append(f"documents checked : {doc_count}")
    lines.append(f"stale references  : {len(findings)}   {_tally(findings)}")
    lines.append("")

    current = None
    for finding in sort_findings(findings):
        if finding.doc != current:
            current = finding.doc
            lines.append(f"{current}")
        lines.append(f"  {finding.severity:<6} line {finding.line:<4} {finding.detail}")
        if finding.suggestion:
            lines.append(f"         {'':<11}-> {finding.suggestion}")
    lines.append("")
    return "\n".join(lines)


def render_json(findings: Sequence[Finding], doc_count: int) -> str:
    ordered = sort_findings(findings)
    payload = {
        "schema_version": 1,
        "summary": {
            "documents_checked": doc_count,
            "stale_references": len(ordered),
            "by_severity": {
                s: sum(1 for f in ordered if f.severity == s) for s in SEVERITIES
            },
        },
        "findings": [f.as_dict() for f in ordered],
    }
    return json.dumps(payload, indent=2)


def render_github(findings: Sequence[Finding], doc_count: int) -> str:
    """Workflow annotations, so findings land on the line in the PR diff."""
    level = {"high": "error", "medium": "warning", "low": "notice"}
    lines: List[str] = []
    for f in sort_findings(findings):
        message = f.detail + (f" ({f.suggestion})" if f.suggestion else "")
        message = message.replace("\n", " ")
        lines.append(f"::{level.get(f.severity, 'notice')} file={f.doc},line={f.line}::{message}")
    return "\n".join(lines)
