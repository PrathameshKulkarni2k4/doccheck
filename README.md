# doccheck

**Check that your documentation still tells the truth about your code.**

Documentation has no compiler. Rename a file, delete a script, move a heading —
and your docs keep describing the old world with total confidence. Nothing
fails. Nobody finds out until a reader follows a page that is wrong, and then
asks you why.

doccheck reads every *checkable* claim in your documentation and resolves it
against the repository, so the lie shows up as a failing check instead of a
support ticket.

## What it checks

| A claim in your docs | Settled against |
| --- | --- |
| a file or directory path | the working tree |
| a link, or a link into a heading | the target file, and its headings |
| a project command | your package scripts, or Makefile targets |
| an environment variable | whether the code reads it at all |

A *checkable* claim is one the repository can settle on its own. Prose is left
alone — a checker that guesses is wrong often enough that nobody trusts it.

## Install

```sh
pip install doccheck
```

## Use

```sh
doccheck                      # every Markdown file in the repository
doccheck README.md docs/      # just these
doccheck --format json        # machine-readable
doccheck --fail-on high       # CI gate
```

Output looks like this:

```
doccheck report
================================================================
documents checked : 12
stale references  : 2   high 2

README.md
  high   line 12   'src/config.py' does not exist in the repository
                   -> did you mean `src/settings.py`?
```

## Adopt it without fixing your history

This is the part that matters. Most teams cannot switch on a strict docs check,
because years of accumulated drift fails it on day one — and a check that is
red on day one gets turned off by Friday.

The `--since` flag reports **only drift the change is answerable for**: a
finding in a document this change touched, or pointing at a file this change
renamed or deleted.

```sh
doccheck --since origin/main --fail-on high
```

So a pull request that renames a settings module and forgets to update the
README fails. A pull request that touches neither still passes, however much
old drift is sitting elsewhere in the tree.

## In CI

```yaml
name: Docs
on: pull_request
jobs:
  doccheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0        # --since needs the merge base
      - run: pip install doccheck
      - run: doccheck --since origin/${{ github.base_ref }} --format github
```

The `github` format emits workflow annotations, so each finding lands on the
exact line in the pull request diff.

## Measured on real repositories

The only number that matters for a checker is its false-positive rate, so
here is what it does on eight real, popular repositories. *before* is the
first version; *now* is with the precision rules below.

| repository | documents | before | now |
| --- | ---: | ---: | ---: |
| django/django | 12 | 2 | 0 |
| psf/requests | 29 | 19 | 2 |
| pallets/flask | 85 | 277 | 1 |
| expressjs/express | 4 | 21 | 0 |
| axios/axios | 17 | 38 | 0 |
| realestate-com-au/shush | 3 | 3 | 0 |
| gothinkster/realworld | 31 | 80 | 2 |
| morluto/rea | 205 | 805 | 78 |

The two left in requests are **real**: its test-certificate README tells you
to run `make clean` and `make all`, and the Makefile defines neither.

rea is the interesting one. It reverse-engineers things, so its docs are full
of paths from the systems it analyses - macOS app bundles, JavaScript
bundles, Ghidra installs. No reference checker can know those are not
repository paths, and that is the honest limit of this approach.

## What it deliberately ignores

Every rule here exists because a real repository produced a false positive:

- **changelogs and history files** - a changelog naming a file that no longer
  exists is describing the past, not drifting. Use `--include-changelogs`.
- **bare filenames** are looked up anywhere in the repository, not just next
  to the document that mentions them.
- **paths that resolve from the repository root**, for documents that live in
  a subdirectory.
- **build output** and generated directories.
- **website routes** such as `/users/` or `/lifecycle`, which are not files.
- **MIME types**, home-directory paths, assignments, and Windows paths.
- **environment variables**, unless you ask for them with `--check-env`.
- **commands**, when the manifest they would be checked against is absent.

`--fail-on` decides what breaks a build. Makefile targets default to `low`
severity, because a target can legitimately be generated.

## Limits

doccheck checks that references **resolve**. It cannot tell you whether a
sentence is true, well written, or still the right advice. A clean run means
the references it could resolve did resolve — not that your documentation is
correct.

It also cannot tell a repository path from a path belonging to something
else the document describes. If your docs quote file layouts from other
systems, some of those will be reported.

## Layout

```
src/doccheck/extract.py   turn a document into checkable claims
src/doccheck/verify.py    resolve claims against the repository
src/doccheck/report.py    text, JSON and GitHub annotation output
src/doccheck/cli.py       the command line
```

## License

MIT
