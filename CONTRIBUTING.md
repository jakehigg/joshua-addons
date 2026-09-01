# Contributing

Thank you for your interest in Joshua addons. This page says how to send a
change.

## Set up

You work in a fork. The main repository is `upstream`. Your fork is `origin`.

1. Fork the repository on GitHub.
2. Clone your fork and add the main repository:

```
git clone git@github.com:<you>/joshua-addons.git
cd joshua-addons
git remote add upstream https://github.com/jakehigg/joshua-addons.git
git fetch upstream
```

3. Install the workspace:

```
uv sync
```

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Docker is needed only
to run an addon.

## Make a change

1. Open an issue first, or find the issue the change belongs to. An issue says:
   Goal, Why, Spec, Acceptance criteria, Tests, Out of scope.
2. Branch from an up-to-date `main`. Name the branch `<issue>-<slug>`:

```
git checkout main
git fetch upstream
git merge --ff-only upstream/main
git checkout -b 42-weather-addon
```

3. Make the change. Add or change the tests. Change the docs that the change
   makes wrong.
4. Run the checks:

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

5. Commit. The subject is one short line. The body says why. Write both in
   Simplified Technical English (below).
6. Push the branch to your fork and open a pull request against
   `upstream/main`:

```
git push -u origin 42-weather-addon
```

7. In the pull request, say what changed and why, and end with `Closes #42`. CI
   must be green before review.

One pull request closes one issue. The maintainer squash-merges it. After the
merge, update your `main` from `upstream` and delete the branch:

```
git checkout main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
git branch -d 42-weather-addon
```

## What an addon must do

An addon lives in `addons/<name>/` and holds everything it needs: a
`pyproject.toml`, a `Dockerfile`, a `docker-compose.yml`, a `values.yaml`, the
package, `tests/`, and a `README.md`. It serves MCP over HTTP on port 8000, it
answers `GET /healthz` with `{"ok": true}`, and it accepts an optional
`ADDON_TOKEN` bearer. CI finds the addon by its `Dockerfile`, so a directory
without one is not built.

One version covers the whole repository. Never version one addon on its own.

## Files that never reach a commit

Your `.env` holds your secrets. Git ignores it. Before you push, run
`git status` and make sure it is not in the list.

No hostname, IP address, person, or secret from your own installation belongs in
a diff.

## The conventions

`CLAUDE.md` holds the conventions: the repo layout, the addon contract, the
version rule, the coding rules, the test rules, and the git identity rule. Read
it before a change. It is written for people and for coding agents alike.

## The writing standard

Every document, docstring, comment, commit message, pull request, and error
message is in Simplified Technical English (ASD-STE100). Short sentences. Active
voice. One name for one thing. No marketing words.

The rules and a linter are in the repository, in `.claude/skills/ste-writing/`.
A coding agent that supports skills picks it up on its own. Score a page by hand
with:

```
python3 .claude/skills/ste-writing/scripts/ste-lint.py README.md
```

The linter prints violations per 100 words. Aim for less than 2.5 on prose and
near 0 on a procedure. The score measures form, not truth. Check the facts
yourself.

## License of a contribution

Joshua addons are under the GNU Affero General Public License, version 3 or
later (`LICENSE`). A pull request contributes the change under the same license.
There is no separate contributor agreement.

## What a reviewer looks for

- The addon keeps to the contract: port 8000, `/healthz`, the optional bearer.
- The tests cover the code, and they pass offline.
- A security claim has a test that tries the denied case.
- The docs that the change makes wrong are changed in the same pull request.
- No hostname, IP address, person, or secret from your own installation is in
  the diff.
