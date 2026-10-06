# CLAUDE.md

Conventions for people and agents who change this repository. `docs/` explains
the addons. This file explains how to work on them.

## What joshua-addons is

Joshua is a personal AI agent. Its core ships from `joshua-ai`: `channels`,
`core`, and `gateway`. This repository holds the optional addons. An addon is
one container that serves an MCP server over HTTP. The person who runs Joshua
puts the addon in the `mcp` section of `joshua.yaml` as a `type: http` upstream,
and the `gateway` calls it. The core is complete without any addon. Each addon
is a separate choice, and a person runs only the addons they want.

## Repo layout

```
pyproject.toml           uv workspace root: members addons/*
uv.lock                  committed; a pin change is a reviewed change
addons/<name>/           one addon, self-contained (see below)
charts/joshua-addon/     one generic Helm chart for every addon
docs/                    the addon documentation
scripts/                 the CI matrix script and the local checks
.claude/skills/          the ste-writing skill (the documentation standard)
.github/workflows/       CI
```

Each addon holds everything it needs:

```
addons/<name>/pyproject.toml       a workspace member
addons/<name>/Dockerfile           the image; its presence puts the addon in CI
addons/<name>/docker-compose.yml   how a person runs this addon alone
addons/<name>/values.yaml          the Helm values for this addon
addons/<name>/joshua_<name>/     the package
addons/<name>/tests/               the tests
addons/<name>/README.md            what it does, and how to configure it
```

Python 3.13. `uv` for everything (`uv sync --frozen` in the Dockerfiles).
`ruff` for format and lint. `pytest`. Pydantic v2 for config and API models.

## The addon contract

An addon serves MCP over HTTP on port 8000, answers `GET /healthz` with
`{"ok": true}`, and reads an optional `ADDON_TOKEN`. When `ADDON_TOKEN` is set,
every MCP route needs `Authorization: Bearer <token>`, compared with
`hmac.compare_digest`; a missing or wrong token gets 401. `/healthz` stays open
and never names a person or a secret. A `Dockerfile` in `addons/<name>/` is what
CI discovers, so a directory without one is not built and not published. Each
addon ships its own tests and its own `values.yaml`, and it takes every setting
from an environment variable that its `README.md` documents.

## Versioning and releases

One version for the whole repository. The chart version, the chart `appVersion`,
and every release image tag are the same string. A `v*` tag releases all of them
together, and an addon that did not change still gets the new tag. Never version
one addon on its own.

The version is a date: `YYYY.M.N`, the year, the month with no leading zero,
and the sequence of the release in that month, from 1. The first release in
October 2026 is `2026.10.1`, the second is `2026.10.2`, the first in January
2027 is `2027.1.1`. Two constraints give the shape. Helm requires SemVer, so
no part has a leading zero. A version always has three parts, because an
unquoted `2026.10` is a number in YAML. joshua-ai keeps its own sequence, so
print the repository beside a version.

`scripts/check_chart_version.py` refuses a tree where the pins differ or where
the shape is wrong. It runs in `make lint` and in CI. `docs/releasing.md`
lists every file that holds the version and the release procedure.

**The maintainer sets every version number and cuts every release.** An agent
proposes `YYYY.M.<next free N>` and changes nothing else. An agent never
pushes a tag, never runs the release workflow, never publishes a release, and
never commits to `main`. Its work ends at a branch with green CI and an open
pull request.

`docs/CHANGELOG.md` collects each change under `## Unreleased`. The release
moves that section under `## <version> - <date>`, with an "Upgrading from"
part when a person must act.

## Deployment

Two paths ship here. `addons/<name>/docker-compose.yml` runs one addon on one
Docker host. On Kubernetes, `charts/joshua-addon` is one generic chart: a person
installs it once for each addon they want, with that addon's `values.yaml`. One
Helm release, and one ArgoCD Application, for each addon. Neither path builds an
image; both take an image from the GitHub container registry. A release tag is
the default. `.github/workflows/dev-images.yml` also publishes every addon
image on each push to a branch, tagged with the commit SHA and
`branch-<name>`, so a branch can run on a test cluster before a release.

Never add the detail of one installation to this repository: no hostname, IP
address, roster, credential, or manifest for a particular deployment. Every such
value belongs in a values file or an environment file that the person who runs
the addon keeps.

## Coding rules

- Type hints everywhere. `ruff` clean. Tests offline by default: no network and
  no Docker. Mark a test that needs a service `@pytest.mark.integration`.
- Structured JSON logs. Never log a token, and never log a message body at INFO.
- No behavior behind an undocumented environment variable. Every knob is in the
  addon's `README.md` and in its `values.yaml`.
- Security-relevant behavior gets a test that proves the negative: a 401 without
  a token, a denied path. If a doc says "cannot", a test tries.

## Tests

- Every pull request adds or changes tests for the code it touches.
- Coverage floor: 80 percent for each addon (`--cov-fail-under=80`).
- No `@pytest.mark.skip` or `xfail` without a reason that names an issue.

## Documentation rules

- **Simplified Technical English.** Docs, READMEs, docstrings, comments, pull
  request text, commit messages, error messages, and release notes follow
  ASD-STE100. The `ste-writing` skill in `.claude/skills/ste-writing/` holds the
  rules and a linter. Use STE-flavored mode for prose and strict mode for
  procedures and error messages. Score a page with:

```
python3 .claude/skills/ste-writing/scripts/ste-lint.py docs/<page>.md
```

  Aim for less than 2.5 violations per 100 words on prose and near 0 on a
  procedure. The rule does not apply to code, identifiers, or command syntax.
- **Write the minimum the reader needs, and only the current behavior.** Never
  narrate history in code ("this used to", "changed from"). That belongs in the
  commit message.

## Git identity

Every commit in this repository is from `Jake
<15249846+jakehigg@users.noreply.github.com>`. Set it with a repo-local
`git config` in a fresh clone, before the first commit:

```
git config user.name "Jake"
git config user.email "15249846+jakehigg@users.noreply.github.com"
```

A real email address must never reach a commit, an author field, or a committer
field. A global `git config` can hold one, so the repo-local setting is what
protects the repository. After you commit, check with:

```
git log --format='%an <%ae> | %cn <%ce>'
```

## Workflow

- One branch per issue, named `<issue>-<slug>`. One pull request per issue.
  Squash merge. CI must be green.
- The pull request template in `.github/` is the checklist. The description
  says what changed and why, in STE, and ends with `Closes #<issue>`.
- A commit message has a short subject, a body that says why, and the
  `Co-Authored-By:` trailer when an agent wrote the change.
- An issue starts from a template in `.github/ISSUE_TEMPLATE/`: a change, a
  bug, or a new addon. A change has: Goal · Why · Spec · Acceptance criteria ·
  Tests · Out of scope · Depends on. Tickets live as GitHub issues in this
  repository.
- A vulnerability is not an issue. `SECURITY.md` says where it goes.

`CONTRIBUTING.md` has the fork and upstream setup.

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every pull
request:

- `discover`: `scripts/list_addons.py` lists every `addons/<name>/` with a
  `Dockerfile`, so no job names an addon.
- `lint`: `uv sync --frozen --all-packages`, `uv lock --check`,
  `ruff check .`, `ruff format --check .`, `scripts/check_test_policy.py`, and
  the root test suite with `pyyaml` and `asyncpg` added.
- `test`: one job per addon, with a Postgres service, `--cov-fail-under=80`
  over the unit and the integration suites.
- `chart`: `scripts/check_chart_version.py`, then `helm lint`, `helm template`,
  and `kubeconform` for every file in `charts/joshua-addon/ci/`.

`.github/workflows/dev-images.yml` builds the image of every addon on each
push to a branch, amd64, tagged with the commit SHA and `branch-<name>`.
`release.yml` builds every image for both architectures on a `v*` tag,
packages the chart, and publishes the release. `registry-cleanup.yml` removes
SHA-tagged branch builds older than two weeks each Monday.
