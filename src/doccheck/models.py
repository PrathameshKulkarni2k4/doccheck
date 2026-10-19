"""The two small types everything else is built from."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Claim:
    """Something a document asserts that the repository can settle.

    ``kind`` is how we found it: ``link`` (a Markdown link), ``inline`` (a
    backticked span) or ``command`` (a shell command in a code block).
    """

    kind: str
    doc: str
    line: int
    text: str


@dataclass(frozen=True)
class Finding:
    """A claim the repository disagrees with."""

    kind: str  # path | link | anchor | command | env
    severity: str  # high | medium | low
    doc: str
    line: int
    target: str  # what the document said
    detail: str  # why it is wrong
    suggestion: str = ""
    resolved: str = ""  # repo-relative path the claim pointed at, if any

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "file": self.doc,
            "line": self.line,
            "target": self.target,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }
