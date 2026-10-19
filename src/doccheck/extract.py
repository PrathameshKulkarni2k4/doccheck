"""Pull checkable claims out of a Markdown document.

Deliberately regex-based and dependency-free: a documentation checker has to
run in everyone's CI, so it must install instantly and never disagree with
itself. We only extract things the repository can *settle* - a path, a link, a
command. Prose is left alone, because a checker that guesses is wrong often
enough that nobody trusts its findings.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .models import Claim

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# Commands we can resolve against a manifest. Kept narrow on purpose: a bare
# `npm install` or `yarn add` is not a project script, so flagging it would be
# a false positive on nearly every README.
COMMAND_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn)\s+run\s+([A-Za-z0-9:._-]+)"),
    re.compile(r"\bmake\s+([A-Za-z0-9:._-]+)"),
)

# Inside a code block a command has to *start* the line. Matching it anywhere
# turns prose like "...you can make the change..." into a `make the` claim,
# which is a false positive we saw on a real repository.
LINE_COMMAND = re.compile(
    r"^\s*(?:\$|>|#)?\s*(?:sudo\s+)?"
    r"((?:npm|pnpm|yarn)\s+run\s+[A-Za-z0-9:._-]+|make\s+[A-Za-z0-9:._-]+)\b"
)


def _flush_commands(path: str, block: List[Tuple[str, int]], out: List[Claim]) -> None:
    for text, lineno in block:
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LINE_COMMAND.match(text)
        if match:
            out.append(Claim("command", path, lineno, match.group(1)))


def extract(path: str, text: str) -> List[Claim]:
    """Return every checkable claim in one document."""
    claims: List[Claim] = []
    in_fence = False
    block: List[Tuple[str, int]] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _FENCE.match(raw):
            if in_fence:
                _flush_commands(path, block, claims)
                block = []
            in_fence = not in_fence
            continue

        if in_fence:
            block.append((raw, lineno))
            continue

        # Images are links too, but their target is an asset we do not resolve.
        line = _IMAGE.sub("", raw)
        for match in _LINK.finditer(line):
            claims.append(Claim("link", path, lineno, match.group(1)))
        for match in _INLINE.finditer(line):
            claims.append(Claim("inline", path, lineno, match.group(1).strip()))

    if in_fence:  # unterminated block
        _flush_commands(path, block, claims)

    return claims


def is_command(text: str) -> bool:
    """True when the whole span is a project command we know how to resolve."""
    stripped = text.strip()
    for pattern in COMMAND_PATTERNS:
        match = pattern.search(stripped)
        if match and match.group(0) == stripped:
            return True
    return False
