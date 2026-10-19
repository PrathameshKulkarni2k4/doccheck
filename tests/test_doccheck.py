"""Tests for doccheck."""

import json
import subprocess
from pathlib import Path

import pytest

from doccheck.cli import main
from doccheck.extract import extract, is_command
from doccheck.verify import Repo, looks_like_path, resolve, slugify, verify

DEMO = Path(__file__).resolve().parent / "fixtures" / "demo"


def write_repo(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def findings_for(tmp_path, doc="README.md", text="", check_env=False):
    write_repo(tmp_path, {doc: text})
    repo = Repo(tmp_path)
    return verify(repo, extract(doc, text), check_env=check_env)


# -- extraction -----------------------------------------------------------


def test_extracts_links_inline_spans_and_commands():
    text = "See [guide](docs/guide.md).\n\nRun `npm run build` now.\n\n```sh\nmake release\n```\n"
    kinds = [c.kind for c in extract("README.md", text)]
    assert kinds.count("link") == 1
    assert kinds.count("inline") == 1  # `npm run build`
    assert kinds.count("command") == 1  # make release, inside the fence


def test_is_command_is_strict():
    assert is_command("npm run build")
    assert is_command("make test")
    assert not is_command("npm install express")
    assert not is_command("just some prose")


# -- resolution -----------------------------------------------------------


def test_resolve_handles_parent_and_root():
    assert resolve("README.md", "src/app.py") == "src/app.py"
    assert resolve("docs/guide.md", "../src/app.py") == "src/app.py"
    assert resolve("docs/guide.md", "/src/app.py") == "src/app.py"
    assert resolve("README.md", "./src/./app.py") == "src/app.py"


def test_slugify_matches_github_headings():
    assert slugify("Getting started") == "getting-started"
    assert slugify("API: the basics!") == "api-the-basics"
    # one hyphen per whitespace character, not per run - found on axios
    assert slugify("Docs & Community") == "docs--community"
    # an emoji is dropped but the space after it still becomes a hyphen
    assert slugify("\U0001f9e9 Automatic serialization") == "-automatic-serialization"


def test_explicit_html_anchor_resolves(tmp_path):
    # `## <a name="question"></a> Got a Question?` is a valid GitHub anchor
    write_repo(tmp_path, {"CONTRIBUTING.md": '## <a name="question"></a> Got a Q?\n'})
    assert findings_for(tmp_path, text="See [q](CONTRIBUTING.md#question).\n") == []


def test_zero_width_joiner_in_an_anchor_resolves(tmp_path):
    write_repo(tmp_path, {"docs/fa.md": "# \u0645\u06cc\u200c\u06a9\u0646\u062f\n"})
    assert findings_for(tmp_path, text="[x](docs/fa.md#\u0645\u06cc\u200c\u06a9\u0646\u062f)\n") == []


def test_bare_filename_found_elsewhere_is_clean(tmp_path):
    # A skill file saying `CHANGELOG.md` means the one at the root, not one
    # next to itself - the bug that made rea report 805 findings.
    write_repo(tmp_path, {"docs/readme/CHANGELOG.md": "# log\n",
                          ".agents/skills/x/SKILL.md": ""})
    findings = findings_for(
        tmp_path, doc=".agents/skills/x/SKILL.md", text="See `CHANGELOG.md`.\n"
    )
    assert findings == []


def test_doc_relative_path_resolves_from_the_repo_root(tmp_path):
    write_repo(tmp_path, {"docs/releases/notes.json": "{}\n",
                          ".agents/skills/x/SKILL.md": ""})
    findings = findings_for(
        tmp_path, doc=".agents/skills/x/SKILL.md",
        text="Writes `docs/releases/notes.json`.\n",
    )
    assert findings == []


def test_build_output_is_not_expected_to_be_committed(tmp_path):
    assert findings_for(tmp_path, text="Built to `dist/cli.js`.\n") == []
    assert findings_for(tmp_path, text="See `./dist/`.\n") == []


def test_home_directory_and_mime_and_routes_are_ignored(tmp_path):
    assert findings_for(tmp_path, text="Config at `~/.claude.json`.\n") == []
    assert findings_for(tmp_path, text="Returns `application/json`.\n") == []
    assert findings_for(tmp_path, text="A `GET /users/` route.\n") == []
    assert findings_for(tmp_path, text="`deploying/asgi` and `tools/list`.\n") == []


def test_changelogs_are_skipped_unless_asked_for(tmp_path):
    write_repo(tmp_path, {"CHANGELOG.md": "Removed `src/old.py`.\n"})
    repo = Repo(tmp_path)
    claims = extract("CHANGELOG.md", (tmp_path / "CHANGELOG.md").read_text())
    assert verify(repo, claims) == []
    assert verify(repo, claims, include_changelogs=True) != []


def test_looks_like_path_is_conservative():
    assert looks_like_path("src/app.py")
    assert looks_like_path("README.md")
    assert not looks_like_path("pip install requests")
    assert not looks_like_path("--verbose")
    assert not looks_like_path("https://example.com/a/b")
    assert not looks_like_path("@scope/pkg")
    assert not looks_like_path("foo(*args)")


# -- checks ---------------------------------------------------------------


def test_missing_path_is_reported_with_a_suggestion(tmp_path):
    write_repo(tmp_path, {"src/new_name.py": "x = 1\n"})
    findings = findings_for(tmp_path, text="Config is in `src/old_name.py`.\n")
    assert len(findings) == 1
    assert findings[0].kind == "path"
    assert findings[0].severity == "high"
    assert "src/new_name.py" in findings[0].suggestion


def test_existing_path_is_clean(tmp_path):
    write_repo(tmp_path, {"src/app.py": "x = 1\n"})
    assert findings_for(tmp_path, text="See `src/app.py`.\n") == []


def test_broken_link_is_reported(tmp_path):
    findings = findings_for(tmp_path, text="Read [the notes](docs/api.md).\n")
    assert [f.kind for f in findings] == ["link"]


def test_valid_link_is_clean(tmp_path):
    write_repo(tmp_path, {"docs/api.md": "# API\n"})
    assert findings_for(tmp_path, text="Read [the notes](docs/api.md).\n") == []


def test_renamed_heading_breaks_its_anchor(tmp_path):
    write_repo(tmp_path, {"docs/guide.md": "# Guide\n\n## Getting started\n"})
    findings = findings_for(tmp_path, text="See [setup](docs/guide.md#setup).\n")
    assert [f.kind for f in findings] == ["anchor"]


def test_matching_anchor_is_clean(tmp_path):
    write_repo(tmp_path, {"docs/guide.md": "# Guide\n\n## Getting started\n"})
    assert findings_for(tmp_path, text="See [setup](docs/guide.md#getting-started).\n") == []


def test_missing_npm_script_is_reported(tmp_path):
    write_repo(tmp_path, {"package.json": json.dumps({"scripts": {"build": "tsc"}})})
    findings = findings_for(tmp_path, text="Run `npm run bulid`.\n")  # typo
    assert [f.kind for f in findings] == ["command"]
    assert "`build`" in findings[0].suggestion


def test_present_npm_script_is_clean(tmp_path):
    write_repo(tmp_path, {"package.json": json.dumps({"scripts": {"build": "tsc"}})})
    assert findings_for(tmp_path, text="Run `npm run build`.\n") == []


def test_env_check_is_off_by_default(tmp_path):
    # An env var is often consumed by an SDK or the platform, so "not read
    # here" is too weak to fail a build on unless you ask for it.
    write_repo(tmp_path, {"src/app.py": "import os\nX = os.environ.get('OTHER')\n"})
    assert findings_for(tmp_path, text="Set `DEMO_TOKEN` before running.\n") == []


def test_env_var_not_read_anywhere_is_reported(tmp_path):
    write_repo(tmp_path, {"src/app.py": "import os\nX = os.environ.get('OTHER')\n"})
    findings = findings_for(
        tmp_path, text="Set `DEMO_TOKEN` before running.\n", check_env=True
    )
    assert [f.kind for f in findings] == ["env"]


def test_env_var_used_in_code_is_clean(tmp_path):
    write_repo(tmp_path, {"src/app.py": "import os\nX = os.environ.get('DEMO_TOKEN')\n"})
    assert findings_for(
        tmp_path, text="Set `DEMO_TOKEN` before running.\n", check_env=True
    ) == []


# -- false-positive guards ------------------------------------------------


def test_prose_and_urls_are_never_flagged(tmp_path):
    text = (
        "Install it with `pip install requests` and read the "
        "[docs](https://example.com/guide) or pass `--verbose`.\n"
    )
    assert findings_for(tmp_path, text=text) == []


# -- CLI ------------------------------------------------------------------


def test_cli_reports_the_demo_fixture(capsys):
    code = main(["--root", str(DEMO), "--check-env"])
    out = capsys.readouterr().out
    assert code == 1  # high findings, and --fail-on defaults to high
    assert "src/config.py" in out
    assert "docs/api.md" in out
    assert "npm run build" in out or "build" in out
    assert "DEMO_API_TOKEN" in out


def test_cli_json_is_machine_readable(capsys):
    code = main(["--root", str(DEMO), "--format", "json", "--check-env"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["summary"]["stale_references"] == len(payload["findings"])
    kinds = {f["kind"] for f in payload["findings"]}
    assert {"path", "link", "anchor", "command", "env"} <= kinds


def test_cli_is_clean_on_its_own_source(capsys):
    root = Path(__file__).resolve().parents[1]
    code = main(["--root", str(root), "--exclude", "tests/fixtures/**", "--exclude", "tests/**"])
    capsys.readouterr()
    assert code == 0


def test_cli_fail_on_none_never_fails(capsys):
    code = main(["--root", str(DEMO), "--fail-on", "none"])
    capsys.readouterr()
    assert code == 0


def test_cli_fail_on_high_ignores_low_findings(tmp_path, capsys):
    write_repo(tmp_path, {
        "Makefile": "build:\n\techo hi\n",
        "README.md": "Run `make deploy`.\n",
    })
    code = main(["--root", str(tmp_path), "--fail-on", "high"])
    capsys.readouterr()
    assert code == 0  # make targets are low severity


def test_cli_since_only_reports_answerable_drift(tmp_path, capsys):
    write_repo(tmp_path, {
        "src/old_name.py": "x = 1\n",
        "README.md": "Config is in `src/old_name.py`.\n",
    })
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    # A change that has nothing to do with the docs: must report nothing.
    (tmp_path / "src" / "unrelated.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "other"],
        cwd=tmp_path, check=True,
    )
    code = main(["--root", str(tmp_path), "--since", "HEAD~1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "old_name" not in out
