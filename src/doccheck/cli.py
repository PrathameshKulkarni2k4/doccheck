"""Command line entry point.

    doccheck                      # check every Markdown file in the repo
    doccheck README.md docs/      # check specific files or directories
    doccheck --since origin/main  # only drift THIS change is answerable for
    doccheck --format json        # machine-readable
    doccheck --fail-on high       # CI gate
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Set

from . import __version__
from .extract import extract
from .models import Finding
from .report import render_github, render_json, render_text, sort_findings
from .verify import SKIP_DIRS, Repo, verify

DOC_SUFFIXES = (".md", ".mdx", ".rst")
SEVERITIES = ("high", "medium", "low")

RENDERERS = {
    "text": render_text,
    "json": render_json,
    "github": render_github,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doccheck",
        description=(
            "Check that your documentation still tells the truth about your code: "
            "paths, links, headings, commands and environment variables."
        ),
    )
    parser.add_argument("--version", action="version", version=f"doccheck {__version__}")
    parser.add_argument(
        "paths", nargs="*", default=None,
        help="documents or directories to check (default: the whole repository)",
    )
    parser.add_argument(
        "--root", default=".", metavar="DIR",
        help="repository root to resolve references against (default: current directory)",
    )
    parser.add_argument(
        "--format", choices=sorted(RENDERERS), default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--fail-on", choices=("high", "medium", "low", "none"), default="high",
        help="exit non-zero at or above this severity (default: high)",
    )
    parser.add_argument(
        "--since", metavar="REF",
        help=(
            "only report drift this change is answerable for: findings in a changed "
            "document, or pointing at a changed file"
        ),
    )
    parser.add_argument(
        "--include", action="append", default=[], metavar="GLOB",
        help="only check documents matching this glob (repeatable)",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="skip documents matching this glob (repeatable)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="print nothing when clean",
    )
    parser.add_argument(
        "--check-env", action="store_true",
        help=(
            "also flag environment variables that are not read anywhere in the "
            "code. Off by default: many are consumed by an SDK or the platform"
        ),
    )
    parser.add_argument(
        "--include-changelogs", action="store_true",
        help=(
            "also check changelogs and history files. Off by default: a changelog "
            "naming a file that no longer exists is describing the past, not drifting"
        ),
    )
    return parser


def _matches(rel: str, patterns: Sequence[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(rel, p) or fnmatch(os.path.basename(rel), p) for p in patterns)


def _discover(root: Path, paths: Sequence[str], include: Sequence[str], exclude: Sequence[str]) -> List[Path]:
    found: List[Path] = []
    for entry in paths:
        target = (root / entry).resolve() if not Path(entry).is_absolute() else Path(entry)
        if target.is_file():
            found.append(target)
            continue
        if not target.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.lower().endswith(DOC_SUFFIXES):
                    found.append(Path(dirpath) / name)

    unique = sorted({p.resolve() for p in found})
    selected: List[Path] = []
    for path in unique:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if include and not _matches(rel, include):
            continue
        if exclude and _matches(rel, exclude):
            continue
        selected.append(path)
    return selected


def _changed_files(root: Path, ref: str) -> Optional[Set[str]]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", ref],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _answerable(finding: Finding, changed: Set[str]) -> bool:
    if finding.doc in changed:
        return True
    return bool(finding.resolved) and finding.resolved in changed


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"doccheck: no such directory: {root}", file=sys.stderr)
        return 2

    repo = Repo(root)
    documents = _discover(root, args.paths or ["."], args.include, args.exclude)
    if not documents:
        print("doccheck: no Markdown documents found.", file=sys.stderr)
        return 2

    claims = []
    for path in documents:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        claims.extend(extract(rel, text))

    findings = verify(
        repo,
        claims,
        include_changelogs=args.include_changelogs,
        check_env=args.check_env,
    )

    if args.since:
        changed = _changed_files(root, args.since)
        if changed is None:
            print(
                f"doccheck: could not diff against '{args.since}' "
                "(is it a git repo, and is the ref fetched?)",
                file=sys.stderr,
            )
            return 2
        findings = [f for f in findings if _answerable(f, changed)]

    findings = sort_findings(findings)
    rendered = RENDERERS[args.format](findings, len(documents))
    if rendered.strip():
        print(rendered)
    elif not args.quiet:
        print(f"doccheck: clean - {len(documents)} document(s), no stale references.")

    if args.fail_on == "none":
        return 0
    limit = SEVERITIES.index(args.fail_on)
    blocking = [f for f in findings if SEVERITIES.index(f.severity) <= limit]
    return 1 if blocking else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
