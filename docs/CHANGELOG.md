# Changelog

Each entry names what changed for the person who runs an addon. The
releases, with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-addons/releases>.

## 0.1.3 - 2026-09-11

### Added

- `vinyl`, a shelf browser for a record collection kept on Discogs. A
  nightly sync reads the collection (read-only), caches the album art,
  resolves each artist's sort-name from MusicBrainz to derive the shelf
  section, reads each release's tracklist and lowest listed price, and
  writes a static bundle. A page at `/` shows the covers on a 3D shelf with
  a genre filter, search, sort orders, and a detail view that names the
  shelf section and lists the tracks. The facet list and the special sections after Z
  come from a JSON file the operator keeps. Seven read-only MCP tools let
  the agent answer a question about the collection: `vinyl_search`,
  `vinyl_details`, `vinyl_stats`, `vinyl_recent`, `vinyl_pick`, and
  `vinyl_status`, and `vinyl_owned` for the question asked in a shop.
  Three more reach Discogs for one record at a time: `vinyl_lookup` finds
  at most three candidate releases from what a person reads off the label,
  with the format line, the release notes, the country and the year of each;
  `vinyl_label_images` answers with the disc labels as pictures, to compare
  against the photograph; and `vinyl_add` puts one record in the collection.
  `vinyl_add` plans first and writes only on a second call with `confirm`,
  marks a pressing it cannot confirm in the collection note, refuses a
  record the collection already holds, and writes the bundle again so the
  record is on the shelf page at once.
  `/`, `/bundle`, `/art`, and `/api/status` are open; `/mcp` takes the
  bearer token.

## 0.1.2

### Added

- `pantry` serves its old family web UI at `/` and the REST API it needs at
  `/api/*`, from the same container and port as `/mcp`. `/` and `/api` are
  open, no bearer token -- the same posture the old family UI had; a
  deployer gates access with the ingress or the docker network. `/mcp` and
  `/healthz` keep their existing behavior unchanged.

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
