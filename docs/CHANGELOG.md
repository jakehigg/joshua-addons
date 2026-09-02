# Changelog

Each entry names what changed for the person who runs an addon. The
releases, with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-addons/releases>.

## 0.1.1

### Fixed

- The chart sets `fsGroup: 1000` on the pod, so an addon with persistence can
  write its volume. Without it, a PersistentVolumeClaim mounts root-owned and
  an addon that stores data (pantry) cannot open its database.

## 0.1.0 - 2026-09-01

### Added

- `pantry`, a receipt-first grocery pantry addon. `record_purchase` turns a
  receipt into pantry state; 16 tools cover inventory status, purchase
  history, price stats, preferred products, aliases, and a bulk `import_data`
  path for a first-time load from another system. SQLite by default, on a
  named volume; set `DATABASE_URL` to use Postgres instead.
- CI: a `postgres:16` service in the test job, so an addon whose tests need
  Postgres (marked `@pytest.mark.integration`) can run them there. An addon
  with none, such as `hello`, is unaffected.

## 0.0.1 - 2026-09-01

The first release.

### Added

- `hello`, the placeholder addon. One container, one MCP tool, over
  streamable HTTP. Use it as the pattern for a new addon.
- A `docker-compose.yml` and `.env.example` for each addon, plus a root
  `Makefile` (`make up ADDON=<name>`, `up-dev`, `down`, `logs`, `ps`) that
  runs any addon from its own compose files.
- `charts/joshua-addon`, one generic Helm chart for every addon. A person
  installs it once for each addon they run, with the values file of that
  addon. One Helm release and one ArgoCD Application per addon.
- CI: a lint job, a test job per addon with `--cov-fail-under=80`, and a
  chart job that renders and validates the chart against every file in
  `charts/joshua-addon/ci/`. A new addon with a `Dockerfile` joins the test
  and build matrix on its own.
- A release workflow. A `v*` tag builds every addon image, for `linux/amd64`
  and `linux/arm64`, packages the chart, and attaches both to a release. The
  chart version, the chart `appVersion`, and every addon image tag are the
  same string.
