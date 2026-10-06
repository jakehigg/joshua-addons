# Releasing

One version covers the whole repository: the chart, the chart `appVersion`,
and every addon image. This page is the procedure for one release.

The version is a date, `YYYY.M.N`: the year, the month with no leading zero,
and the sequence of the release in that month, from 1. The first release in
October 2026 is `2026.10.1`, the second is `2026.10.2`, and the first in
January 2027 is `2027.1.1`. Helm requires SemVer, so no part has a leading
zero, and a version always has three parts. joshua-ai keeps its own sequence,
so name the repository beside a version. The maintainer picks every version
number and cuts every release. A coding agent proposes the next free number
and does nothing else: it never pushes a tag, never runs the release
workflow, and never commits to `main`.

## 1. Change the version in these files

Use `<old>` for the current version and `<new>` for the new version, with no
`v`, such as `2026.10.2`.

| File | What to change |
|---|---|
| `charts/joshua-addon/Chart.yaml` | `version: <new>` and `appVersion: "<new>"` |
| `addons/*/docker-compose.yml` | Each `${JOSHUA_ADDONS_VERSION:-<old>}`. `addons/developer/docker-compose.yml` has two: the manager `image` and the `WORKER_IMAGE` default. |
| `addons/developer/values.yaml` | The tag of `env.WORKER_IMAGE` |
| `charts/joshua-addon/ci/developer-values.yaml` | The tag of `env.WORKER_IMAGE` |
| `addons/*/.env.example` | `JOSHUA_ADDONS_VERSION=<new>` |
| `docs/CHANGELOG.md` | Change `## Unreleased` to `## <new> - <date>`, and add an empty `## Unreleased` above it |

No other doc names the current release. The `v2026.10.1` in `docs/install.md`
and in `charts/joshua-addon/README.md` is an example. Do not change it.

The Docker socket proxy in `addons/developer/docker-compose.yml` is pinned
by digest. When you move it to a new tag, get the new digest with the
command in the comment above its image line, and change the tag and the
digest together.

## 2. Check the change

Run these commands from the repository root:

```
make lint
make test
uv run python scripts/check_test_policy.py
uv run pytest tests -q
git grep -n "<old>" -- addons charts docs
```

`make lint` runs `scripts/check_chart_version.py`. The script fails when one
of the files in step 1 has another version, except `docs/CHANGELOG.md`, and
when the version is not of the date shape. The `git grep` must show only the
changelog.

## 3. Merge and tag

1. Open a pull request with the change. Merge it when CI is green.
2. Tag the merge commit on `main`, and push the tag:

```
git checkout main
git pull --ff-only
git tag v<new>
git push origin v<new>
```

The tag starts `.github/workflows/release.yml`. The workflow:

1. Reads the version from the tag, without the `v`.
2. Finds each addon with a `Dockerfile` under `addons/`. The worker of
   `developer` is one of them.
3. Builds each image for `linux/amd64` and `linux/arm64`, and publishes
   `ghcr.io/jakehigg/joshua-addons-<name>:<new>` as one multi-arch tag.
4. Packages the chart with `--version <new>` and `--app-version <new>`.
5. Makes a GitHub release for the tag, with the chart package and generated
   notes.

A manual run of the workflow (`workflow_dispatch`) builds the images and the
chart for a version, and makes no GitHub release.

## 4. Update an installation

### ArgoCD

1. Set `targetRevision` to `v<new>`.
2. Remove the `parameters` block that sets `image.tag`, if there is one. An
   empty `image.tag` takes the `appVersion` of the chart, `<new>`.
3. For `developer`, make sure that `env.WORKER_IMAGE` ends in `:<new>`.
   `addons/developer/values.yaml` at the tag has this pin. When your own
   values set `env.WORKER_IMAGE`, such as a branch build, set its tag to
   `<new>`.

### Docker Compose

1. Check out the tag: `git fetch --tags && git checkout v<new>`.
2. Set `JOSHUA_ADDONS_VERSION=<new>` in the `.env` of the addon.
3. For `developer`, remove `WORKER_IMAGE` from `.env`, or set it to
   `ghcr.io/jakehigg/joshua-addons-developer-worker:<new>`.
4. Run `make up ADDON=<name>`.

Never move or delete a published tag. To fix a release, make a new one.
