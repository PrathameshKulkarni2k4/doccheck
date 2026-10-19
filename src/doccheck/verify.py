"""Resolve claims against the repository, and report the ones it disagrees with.

The whole design goal is a *low false-positive rate*. A documentation checker
that cries wolf gets muted in a week, so every check here is deliberately
conservative: when we are not sure a token is a claim at all, we say nothing.

Three rules keep it quiet in real repositories, each learned by running it
against big ones:

* **A bare filename means "somewhere in the repo".** A skill file that says
  ``CHANGELOG.md`` means the one at the root, not one next to itself.
* **If the path resolves from the repository root, accept it.** Authors are
  inconsistent about which base they had in mind.
* **Code files need a directory.** ``res.json`` and ``app.env`` are
  attributes; ``src/config.py`` is a file. A bare name is only treated as a
  path when its extension is a documentation or config one.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set

from .extract import is_command
from .models import Claim, Finding

# Directories we never walk into: dependencies, build output, caches.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", ".tox", ".idea", ".vscode",
}

# Paths that are build output and so are not expected to be committed.
BUILD_OUTPUT = (
    "dist/", "build/", "out/", "target/", "_build/", "coverage/",
    ".next/", ".nuxt/", ".output/", "htmlcov/", "site/",
)

# Directories that exist at runtime but are never committed.
GENERATED_PREFIXES = (
    "node_modules/", ".cache/", "tmp/", ".git/", "__pycache__/",
    ".venv/", "venv/", ".pytest_cache/",
)

# A *bare* filename (no directory) is only treated as a path when its
# extension is one a document would actually name. Code extensions are
# excluded on purpose: `res.json`, `flask.json` and `app.env` are attributes
# and modules far more often than they are files.
BARE_FILENAME_EXTENSIONS = {
    ".md", ".mdx", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml",
    ".lock", ".sh", ".bash", ".ps1", ".env.example", ".gitignore",
}

# Files that describe the past. A changelog naming a file that no longer
# exists is correct behaviour, not drift.
HISTORICAL_FILE = re.compile(
    r"^(change[-_]?log|changes|history|news|release[-_]?notes?|migration[-_]?notes?)",
    re.IGNORECASE,
)

_MIME_PREFIXES = (
    "application/", "text/", "image/", "audio/", "video/", "font/",
    "multipart/", "message/", "model/", "chemical/",
)

_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "//", "ftp://")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]*$")  # must contain an underscore
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# GitHub also honours explicit anchors written in raw HTML, which is how a
# project links to a heading whose text it does not want to depend on:
#     ## <a name="question"></a> Got a Question?
_HTML_ANCHOR = re.compile(
    r"<a\s+[^>]*?(?:name|id)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)

# Environment variables that are honoured by the platform or a standard
# library, so "not read in this repo" is not drift.
STANDARD_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "PATH", "HOME",
    "LANG", "LC_ALL", "TZ", "EDITOR", "PAGER", "TERM", "SHELL", "USER",
    "TMPDIR", "TEMP", "TMP", "PYTHONPATH", "NODE_ENV", "GITHUB_TOKEN",
    "GITHUB_ACTIONS", "CI", "SOURCE_DATE_EPOCH",
}

_TEXT_SUFFIXES = BARE_FILENAME_EXTENSIONS | {
    "", ".py", ".js", ".ts", ".go", ".rs", ".rb", ".php", ".java", ".kt",
    ".cs", ".c", ".h", ".cpp", ".sh", ".json", ".xml", ".html", ".css",
    ".sql", ".proto", ".tf", ".tfvars", ".gradle", ".swift", ".dart",
}
_MAX_SCAN_BYTES = 400_000


def slugify(heading: str) -> str:
    """GitHub's heading-anchor rules, near enough.

    Letters and digits survive (including non-Latin ones), everything else
    that is not a space or hyphen is dropped, and spaces become hyphens. Note
    that an emoji is dropped but the space after it still becomes a hyphen,
    which is why ``### \U0001f9e9 Automatic serialization`` anchors to
    ``#-automatic-serialization``.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    # One hyphen per whitespace character, *not* per run: GitHub turns
    # "Docs & Community" into "docs--community" (the ampersand is dropped but
    # both spaces still become hyphens).
    return re.sub(r"\s", "-", text)


def anchor_key(text: str) -> str:
    """Compare anchors leniently: ignore zero-width joiners and edge hyphens.

    Persian and Malayalam headings carry a zero-width non-joiner, which GitHub
    keeps in the anchor. Stripping it from both sides is what makes those
    links resolve.
    """
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    return text.strip("-").lower()


class Repo:
    """A lazily-built view of the repository."""

    def __init__(self, root: "os.PathLike[str] | str"):
        self.root = Path(root).resolve()
        self.files: Set[str] = set()
        self.dirs: Set[str] = set()
        self._walk()
        self._scripts: Optional[Dict[str, str]] = None
        self._targets: Optional[Set[str]] = None
        self._code_text: Optional[str] = None

    # -- filesystem ------------------------------------------------------
    def _walk(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            if rel_dir != ".":
                self.dirs.add(rel_dir)
            for name in filenames:
                self.files.add(name if rel_dir == "." else f"{rel_dir}/{name}")

    def has(self, rel: str) -> bool:
        rel = rel.rstrip("/")
        return rel in self.files or rel in self.dirs

    def read(self, rel: str) -> Optional[str]:
        try:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def by_basename(self, name: str) -> Optional[str]:
        """Where a bare filename actually lives, if anywhere."""
        for candidate in sorted(self.files):
            if PurePosixPath(candidate).name == name:
                return candidate
        for candidate in sorted(self.dirs):
            if PurePosixPath(candidate).name == name:
                return candidate
        return None

    # -- manifests -------------------------------------------------------
    def package_scripts(self) -> Optional[Dict[str, str]]:
        if self._scripts is None:
            self._scripts = {}
            raw = self.read("package.json")
            if raw:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {}
                scripts = data.get("scripts")
                if isinstance(scripts, dict):
                    self._scripts = {str(k): str(v) for k, v in scripts.items()}
        return self._scripts or None

    def make_targets(self) -> Optional[Set[str]]:
        if self._targets is None:
            self._targets = set()
            for name in ("Makefile", "makefile", "GNUmakefile"):
                raw = self.read(name)
                if raw is None:
                    continue
                for line in raw.splitlines():
                    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9:._-]*)\s*:(?!=)", line)
                    if match:
                        self._targets.add(match.group(1))
        return self._targets or None

    def code_text(self) -> str:
        """Everything outside the docs, concatenated once, for symbol lookups."""
        if self._code_text is None:
            chunks: List[str] = []
            for rel in sorted(self.files):
                if rel.endswith((".md", ".mdx", ".rst")):
                    continue
                if os.path.splitext(rel)[1].lower() not in _TEXT_SUFFIXES:
                    continue
                path = self.root / rel
                try:
                    if path.stat().st_size > _MAX_SCAN_BYTES:
                        continue
                    chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
            self._code_text = "\n".join(chunks)
        return self._code_text


# -- path helpers ---------------------------------------------------------


def resolve(doc: str, target: str) -> Optional[str]:
    """Resolve a link/path as written in ``doc`` to a repo-relative posix path."""
    if not target:
        return None
    joined = (
        PurePosixPath(target.lstrip("/"))
        if target.startswith("/")
        else PurePosixPath(doc).parent / target
    )
    parts: List[str] = []
    for part in joined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            else:
                return None
            continue
        parts.append(part)
    return PurePosixPath(*parts).as_posix() if parts else ""


def locate(repo: Repo, doc: str, target: str) -> "tuple[str, bool]":
    """Find where a reference points, forgiving about which base was meant.

    Returns ``(resolved_path, found)``.
    """
    if target.startswith("/"):
        resolved = target.lstrip("/")
        return resolved, repo.has(resolved)

    resolved = resolve(doc, target)
    if resolved is not None and repo.has(resolved):
        return resolved, True

    # Maybe the author meant it from the repository root. (Note the explicit
    # prefix test: ``lstrip('./')`` would also eat the dot in `.agents/`.)
    root_relative = target[2:] if target.startswith("./") else target
    if repo.has(root_relative):
        return root_relative, True

    # A tutorial that says `flaskr/blog.py` means the file that exists, read
    # from where the reader is standing. If the name is unique in the repo,
    # accept it - flagging it would be wrong, not helpful.
    found = repo.by_basename(PurePosixPath(target.rstrip("/")).name)
    if found is not None:
        return found, True

    return (resolved if resolved is not None else target), False


def is_build_output(path: str) -> bool:
    return path[2:].startswith(BUILD_OUTPUT) if path.startswith("./") else path.startswith(BUILD_OUTPUT)


def looks_like_path(text: str) -> bool:
    if not text or any(c in text for c in " \t"):
        return False
    if text.startswith(_EXTERNAL) or text.startswith("#") or text.startswith("-"):
        return False
    if text.startswith("@") or "://" in text:
        return False
    if any(c in text for c in "*?[]<>|()"):
        return False
    if text.startswith(_MIME_PREFIXES):
        return False
    # Home-directory references describe the reader's machine, not the repo.
    if text.startswith("~"):
        return False
    # A leading slash is a route on a website (`/users/`, `/lifecycle`), which
    # is what it means in almost every code sample and doc cross-reference.
    if text.startswith("/"):
        return False
    if text.startswith(GENERATED_PREFIXES):
        return False
    # Assignments, shell variables and Windows paths are not file references.
    if any(c in text for c in "=\\:$`"):
        return False
    if text.endswith("/"):
        # A directory reference only counts when it has a parent: `src/api/`
        # is a path, `dist/` is generated output.
        return "/" in text.rstrip("/")
    if "/" in text:
        # `deploying/asgi` and `tools/list` are doc references and method
        # names; a real file reference carries an extension.
        return bool(os.path.splitext(text)[1])
    return os.path.splitext(text)[1].lower() in BARE_FILENAME_EXTENSIONS


def _nearest(repo: Repo, resolved: Optional[str], original: str) -> str:
    """A gentle 'did you mean' - usually the file was renamed, not removed."""
    everything = sorted(repo.files | repo.dirs)
    name = PurePosixPath(original.rstrip("/")).name
    if name:
        same_name = [f for f in everything if PurePosixPath(f).name == name]
        if len(same_name) == 1:
            return f"did you mean `{same_name[0]}`?"
        if same_name:
            shown = ", ".join(f"`{c}`" for c in same_name[:3])
            return f"did you mean one of: {shown}?"
    close = difflib.get_close_matches(resolved or original, everything, n=1, cutoff=0.6)
    if close:
        return f"did you mean `{close[0]}`?"
    return "remove the reference, or create the file"


def _closest(name: str, options: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(options), n=1, cutoff=0.6)
    if close:
        return f"did you mean `{close[0]}`?"
    return "remove it, or define it"


# -- checks ---------------------------------------------------------------


def _check_anchor(repo: Repo, claim: Claim, target_file: str, anchor: str) -> List[Finding]:
    raw = repo.read(target_file)
    if raw is None:
        return []
    headings = {
        anchor_key(slugify(m.group(2)))
        for line in raw.splitlines()
        if (m := _HEADING.match(line))
    }
    headings |= {anchor_key(m.group(1)) for m in _HTML_ANCHOR.finditer(raw)}
    if anchor_key(anchor) in headings:
        return []
    return [
        Finding(
            "anchor", "high", claim.doc, claim.line, f"{target_file}#{anchor}",
            f"heading '#{anchor}' does not exist in {target_file}",
            "renaming a heading breaks every link into it - point at a heading that exists",
            target_file,
        )
    ]


def _check_link(repo: Repo, claim: Claim) -> List[Finding]:
    target = claim.text
    if target.startswith(_EXTERNAL):
        return []
    if target.startswith("#"):
        return _check_anchor(repo, claim, claim.doc, target[1:])

    path, _, anchor = target.partition("#")
    path = urllib.parse.unquote(path)
    if not path:
        return _check_anchor(repo, claim, claim.doc, anchor)

    # An absolute link is a route on a documentation site (`/guides/intro`,
    # `/rea/product-catalog.json`), not a file in the repository.
    if path.startswith("/"):
        return []

    resolved, found = locate(repo, claim.doc, path)
    if not found:
        if is_build_output(resolved) or is_build_output(path):
            return []
        return [
            Finding(
                "link", "high", claim.doc, claim.line, target,
                f"link target '{target}' does not exist",
                _nearest(repo, resolved, path), resolved,
            )
        ]
    if anchor:
        return _check_anchor(repo, claim, resolved, anchor)
    return []


def _check_path(repo: Repo, claim: Claim, text: str) -> List[Finding]:
    resolved, found = locate(repo, claim.doc, text.rstrip("/"))
    if found or is_build_output(resolved) or is_build_output(text):
        return []
    return [
        Finding(
            "path", "high", claim.doc, claim.line, text,
            f"'{text}' does not exist in the repository",
            _nearest(repo, resolved, text), resolved,
        )
    ]


def _check_command(repo: Repo, claim: Claim) -> List[Finding]:
    match = re.search(r"\b(?:npm|pnpm|yarn)\s+run\s+([A-Za-z0-9:._-]+)", claim.text)
    if match:
        name = match.group(1)
        scripts = repo.package_scripts()
        if not scripts or name in scripts:
            return []
        return [
            Finding(
                "command", "high", claim.doc, claim.line, name,
                f"script '{name}' is not defined in package.json",
                _closest(name, scripts),
            )
        ]
    match = re.search(r"\bmake\s+([A-Za-z0-9:._-]+)", claim.text)
    if match:
        name = match.group(1)
        targets = repo.make_targets()
        if not targets or name in targets:
            return []
        return [
            Finding(
                "command", "low", claim.doc, claim.line, name,
                f"make target '{name}' is not defined in the Makefile",
                _closest(name, targets),
            )
        ]
    return []


def _check_env(repo: Repo, claim: Claim) -> List[Finding]:
    name = claim.text
    if name in STANDARD_ENV:
        return []
    if re.search(rf"\b{re.escape(name)}\b", repo.code_text()):
        return []
    return [
        Finding(
            "env", "medium", claim.doc, claim.line, name,
            f"'{name}' is not read anywhere in the code",
            "remove it, or check the name is still correct",
        )
    ]


def _check_inline(repo: Repo, claim: Claim) -> List[Finding]:
    text = claim.text
    if not text:
        return []
    if is_command(text):
        return _check_command(repo, claim)
    if any(c in text for c in " \t"):
        return []
    if _ENV_RE.match(text):
        return _check_env(repo, claim)
    if looks_like_path(text):
        return _check_path(repo, claim, text)
    return []


def verify(
    repo: Repo,
    claims: Iterable[Claim],
    include_changelogs: bool = False,
    check_env: bool = False,
) -> List[Finding]:
    """Resolve every claim; return the ones the repository disagrees with.

    The environment-variable check is **off by default**. An environment
    variable is often consumed by an SDK, a CLI or the platform rather than by
    this repository, so "not read here" is weak evidence - too weak to fail a
    build on. Turn it on with ``check_env`` when you want the hint.
    """
    findings: List[Finding] = []
    for claim in claims:
        if not include_changelogs and HISTORICAL_FILE.match(
            PurePosixPath(claim.doc).name
        ):
            continue
        if claim.kind == "link":
            findings.extend(_check_link(repo, claim))
        elif claim.kind == "command":
            findings.extend(_check_command(repo, claim))
        elif claim.kind == "inline":
            if not check_env and _ENV_RE.match(claim.text):
                continue
            findings.extend(_check_inline(repo, claim))
    return findings
