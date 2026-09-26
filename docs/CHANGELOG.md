# Changelog

Each entry names what changed for the person who runs an addon. The
releases, with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-addons/releases>.

## Unreleased

### Added

- A push to any branch, `main` included, builds the image of every addon for
  amd64 and publishes it tagged with the commit SHA and `branch-<name>`, so
  `branch-main` always names the newest commit on `main`. To test a branch
  before a release, point the ArgoCD Application at the branch and set
  `image.tag` to `$ARGOCD_APP_REVISION`. `docs/install.md` shows how. A
  release is still one `v*` tag, multi-arch, with a packaged chart. A weekly
  job removes the SHA-tagged branch builds older than two weeks, and the
  untagged layers, from the registry.

- `mcp` (joshua-mcp), an addon that gives a caller outside the agent access
  to Joshua's wiki, journal, and knowledge folder. Claude Code on a laptop is
  the first such caller. The tools are `search`, `read_page`,
  `list`, `write_page`, `write_journal_entry`, `read_journal`,
  `knowledge_search`, and `knowledge_read`. A write commits to the wiki
  repository, and records the caller in the frontmatter. Each caller has its
  own token (`MCP_TOKENS`), and a caller can be read-only
  (`MCP_READONLY`). The addon mounts the joshua-ai data volume.
- The chart gains `persistence.existingClaim`, to mount a claim that another
  release owns.
- `vinyl` gains the tools that answer a question about the collection, and
  the tools that change it. Seven read the static bundle, so an answer needs
  no network: `vinyl_search` matches the title, the artist or the label and
  tolerates a missing "The"; `vinyl_owned` answers the question asked in a
  shop; `vinyl_details`, `vinyl_stats`, `vinyl_recent` and `vinyl_lent_out`
  report what the house has; and `vinyl_pick` chooses one record to play,
  leaving a record it suggested in the last two weeks alone while anything
  else fits. Nobody reports a play for that to work.
- Four reach Discogs, and say what is missing without a token. `vinyl_lookup`
  searches in the order of the evidence, barcode first, then catalog number,
  then artist and title, and answers with at most three candidates, each with
  the full format line, the release notes, the country and the year.
  `vinyl_label_images` answers with the disc labels as pictures, to compare
  against a photograph of the record. `vinyl_add` and `vinyl_remove` change
  the collection, and each plans first and writes only on a second call with
  `confirm`. An add marks a pressing it cannot confirm in the collection note
  and refuses a release the collection already holds; a removal asks which
  copy when the house owns two.
- Two mark a record as out with somebody without touching Discogs:
  `vinyl_lend` and `vinyl_return`. A lent record keeps its note, its shelf
  section and the date it was added, is never suggested, and the page says
  who has it.
- Four correct the filing: `vinyl_set_sort_name`, `vinyl_set_section`,
  `vinyl_set_genre` and `vinyl_corrections`. The corrections move out of the
  shelf rules file and into the database, because a mounted file is read-only
  and a tool has to be able to write one. A file that still holds an
  `overrides` block is imported one time and then ignored. Every correction
  files the records it touches again at once and writes the page, so the
  shelf is right with no sync.
- A record added, removed, lent or corrected is on the browse page at once.

### Changed

- The `vinyl` database gains `master_id`, `sort_artist` and `traits` on the
  albums table, and an `overrides` table. A database from an earlier version
  gains the columns when it is opened. `master_id` is what lets the addon say
  the house owns the same album in another pressing, and it fills at the next
  sync.

## 0.1.3 - 2026-09-11

### Added

- `vinyl`, a shelf browser for a record collection kept on Discogs. A
  nightly sync reads the collection (read-only), caches the album art,
  resolves each artist's sort-name from MusicBrainz to derive the shelf
  section, reads each release's tracklist and lowest listed price, and
  writes a static bundle. A page at `/` shows the covers on a 3D shelf with
  a genre filter, search, sort orders, and a detail view that names the
  shelf section and lists the tracks. The facet list and the special sections after Z
  come from a JSON file the operator keeps. One MCP tool, `vinyl_status`.
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
