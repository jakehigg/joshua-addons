# Releasing

One version covers the whole repository: the chart, the chart `appVersion`,
and every addon image. This page is the procedure for one release. The
maintainer picks the version number. Before 1.0, a release moves the patch
number by one, unless the maintainer says otherwise.

## 1. Change the version in these files

Use `<old>` for the current version and `<new>` for the new version, with no
`v`, such as `0.1.4`.

| File | What to change |
|---|---|
| `charts/joshua-addon/Chart.yaml` | `version: <new>` and `appVersion: "<new>"` |
| `addons/*/docker-compose.yml` | Each `${JOSHUA_ADDONS_VERSION:-<old>}`. `addons/developer/docker-compose.yml` has two: the manager `image` and the `WORKER_IMAGE` default. |
| `addons/developer/values.yaml` | The tag of `env.WORKER_IMAGE` |
| `charts/joshua-addon/ci/developer-values.yaml` | The tag of `env.WORKER_IMAGE` |
| `addons/*/.env.example` | `JOSHUA_ADDONS_VERSION=<new>` |
| `docs/CHANGELOG.md` | Change `## Unreleased` to `## <new> - <date>`, and add an empty `## Unreleased` above it |

No other doc names the current release. The `v0.0.1` in `docs/install.md`
and in `charts/joshua-addon/README.md` is an example. Do not change it.

Before the first release that has the `developer` addon, also fill the
digest of the Docker socket proxy in `addons/developer/docker-compose.yml`.
The `TODO` comment above the `docker-socket-proxy` image line has the
command.

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
of the files in step 1 has another version, except `docs/CHANGELOG.md`. The
`git grep` must show only the changelog.

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
   notes. A `0.x` version is a pre-release.

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
