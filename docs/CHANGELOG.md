# Changelog

Each entry names what changed for the person who runs an addon. The
releases, with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-addons/releases>.

## Unreleased

### Added

- `developer`: a package proxy in the manager, so a worker can install
  dependencies and run the tests of the project. The new `packages` block in
  `developer.yaml` holds the host names a worker may reach, such as
  `registry.npmjs.org`, `pypi.org`, and `files.pythonhosted.org`. With
  `enabled: true`, the manager listens on port 8003, allows a `CONNECT` to a
  host in the list, and refuses every other host with 403 and one log line.
  A worker gets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`. The proxy
  carries no credential, allows no git host, and gives a worker no new
  access to a repository. `enabled: false` is the default, so an instance
  that does not set the block keeps its behavior. To use it, add the block,
  add port 8003 to `service.extraPorts` (the chart values of the addon do),
  and keep `networkPolicy.workersInternetEgress: false`.

### Changed

- The chart value `networkPolicy.managerPorts` is now `[8001, 8002, 8003]`,
  so a worker pod can reach the package proxy of the `developer` manager.

- `tasks`: a task list of a project shows one row for each task, with the
  number, the title, the size, the due date, the assignee, the done state,
  and the blocked state. The note of a task now sits behind a "note"
  control, which starts closed on every task, also after a reload. A long
  note no longer fills the screen. The API and the MCP tools do not change.

## 0.2.0 - 2026-11-08

Three new addons: `tasks` (projects, phases, goals, and blocking links, with a
dashboard web UI), `chores` (chores and an XP ledger), and `developer` (a
manager that gives a coding task to a worker). `mcp` gains search by meaning,
`vinyl` answers questions about the collection, and the `tasks`, `chores`, and
`pantry` web UIs have the shared Joshua look.

### Upgrading from 0.1.3

- Set `targetRevision` (ArgoCD) or `JOSHUA_ADDONS_VERSION` (compose) to
  `0.2.0`, as [releasing.md](releasing.md#4-update-an-installation) says.
- `tasks`: `list_tasks` returns the tasks with no group as `backlog`, in
  place of `ungrouped`. The new columns and tables are added to an existing
  database on start; nothing is dropped.
- `developer`: the manager and the worker image must be of the same
  release. Set `env.WORKER_IMAGE` to the `0.2.0` worker if your own values
  name it.
- The chart now uses `strategy: Recreate` for a Deployment that mounts the
  claim of the chart, so the old pod stops before the new pod starts.

### Added

- `tasks`, an addon for household task lists: projects, an ordered task
  list, optional groups, notes, and done or not done. Joshua uses 14 MCP
  tools, and people use a web UI on the same port that updates live.
  SQLite by default, or Postgres with `DATABASE_URL`.
  `addons/tasks/README.md` has the tools and the settings.
- `tasks`: goals and phases. A project can have a goal, a target date, and a
  status (`active`, `paused`, `done`, `closed`). Groups are the phases of a
  project and can have a target date; the ungrouped tasks are the backlog
  and come after the phases. A task can have a size (`XS` to `XL`), a due
  date, and an assignee. `list_projects` returns the percent done, a health
  signal, and the next task. Five new tools: `set_project_goal`,
  `complete_project` (refuses while a task is open), `set_project_status`,
  `next_steps`, and `set_group_target_date`. `list_tasks` returns `backlog`
  in place of `ungrouped`. The new columns are added to an existing
  database on start; nothing is dropped.
- `tasks`: search. The new `search_tasks` tool, the twentieth, finds the
  projects and the tasks that hold a word, so Joshua can look for a task
  before it adds one. The match is a case-insensitive substring: a project
  on its name or its goal, a task on its title or its notes. The result
  keeps the projects and the tasks apart. Only open tasks and unfinished
  projects are searched, until `include_done` is true, and an archived
  project is never searched. `GET /api/search` gives the same result to the
  web UI.
- `tasks`: every task has a short number, for example `24`, so a person can
  say which task they mean. One count runs over all the projects. The addon
  gives the number when it makes the task; the number then never changes and
  is never given to another task, not even after a delete. Every tool result
  that holds a task gives its number, and every tool that takes a task takes
  the number or the task id. The tool count stays 20. The REST field `number`
  is new on a task, `GET /api/tasks/by-number/<number>` finds one task, and a
  task link in the web UI is `/p/<slug>#task-<number>`. An existing database
  gets the column on start, and its tasks are numbered oldest first.
- `tasks`: the web UI opens on a dashboard. The home page is one card for
  each project, with the percent done, the counts, the health, the target
  date, and the next task; the project that needs a person comes first.
  "Show finished" adds the done and the closed projects. A search box finds
  a project by its name or its goal and a task by its title or its notes,
  and keeps the two kinds apart; "Include done" adds the done tasks and the
  finished projects. A project is at `/p/<slug>`, and a found task opens its
  project page at the task.
- `tasks`: a task can wait for other tasks. `block_task` makes one link
  ("27 is blocked by 24") and `unblock_task` removes it; each takes the task
  number or the task id, and the blocker can be in another project. A task
  is `blocked` while one of its blockers is open. A blocked task is never
  the next task and never comes back from `next_steps`. A link to the task
  itself or a link that makes a loop is refused. `set_task_done` reports
  the tasks it unblocked, and warns when the task still waits for an open
  blocker. Every task in a tool result and in the REST API carries
  `blocked`, `blockers`, and `blocking`.
  `POST /api/tasks/<task_id>/blockers` and
  `DELETE /api/tasks/<task_id>/blockers/<blocker_id>` make and remove a
  link. The web UI marks a blocked task and links each blocker. The tool
  count is now 22. An existing database gets the new `task_blockers` table
  on start.
- The chart sets `strategy: Recreate` for a Deployment that mounts the claim
  the chart makes. That claim is ReadWriteOnce, and a rolling update waited
  forever on a Multi-Attach error. The chart also takes
  `deploymentAnnotations` and `podAnnotations`, and adds a `checksum/config`
  annotation when `configFile` is on, so a changed config file restarts the
  pod.
- `developer`, an addon that gives a coding task to a worker.
  `addons/developer/README.md` has the full detail and the install steps.

  **What it does.** Joshua calls `develop` or `rework` with a repository and
  a brief. The manager, image `joshua-addons-developer`, starts one worker
  for each task, image `joshua-addons-developer-worker`: a container through
  a Docker socket proxy on compose, or a Job on Kubernetes. The worker
  clones the repository, runs one Claude Code session, pushes one branch,
  and reports. The manager scans the diff of the worker's commits for
  credentials, and then opens the pull request on GitHub or GitLab. A plain
  git host ends at the branch. On a GitHub diff of more than 300 files, the
  scan is `partial`.

  **Security.** The worker holds no Joshua secret and no Claude token. The
  Claude forwarder on the manager sends on only the API paths that the
  Claude CLI uses. With `network: off`, the default, a worker connects to
  the manager only, so it cannot install dependencies. With `network: on`
  on compose, a worker joins the default bridge, so it can also reach the
  host LAN and the ports that the host publishes. A scan hit fails the task,
  and deletes the branch only when the task made it. A task never works on
  its base branch, `main`, or `master`. The branch and the pull request come
  from the manager's data, never from the worker's report. The worker's text
  in a chat event is in a fenced block. The worker API refuses a body over
  2 MiB. A worker has a read-only root file system and a process limit on
  Docker, and a disk limit (`worker.disk`) on Kubernetes. The worker gets
  the `GIT_CA_BUNDLE` of the manager for git. The compose file pins the
  socket proxy to `tecnativa/docker-socket-proxy:0.3.0`. A CI job runs the
  compose file on a real Docker daemon with a scripted worker session
  (`WORKER_FAKE_SESSION`, a test aid), and calls no Claude API.

  **Questions and the clock.** A worker asks with `ask`. The question
  arrives in the person's chat as an event, and Joshua answers with
  `answer`. A worker can ask more than one question. A task is `dispatched`
  until its worker reads its brief, and its clock starts then, so an image
  pull is not on it. A worker that does not read its brief in
  `WORKER_START_GRACE_S` seconds (600 by default) fails its task. The clock
  pauses while a question waits, for up to `ask_wait_s` seconds in total
  for the task (2 hours by default, and a persona can set its own). An
  `answer` that comes after the worker stopped waiting gets `not_waiting`.
  A task ends `blocked` only when the model sets `blocked`, or when the
  clock runs out while a question is open. An `answer` to a task that
  stopped `blocked` or `timed_out` after a push starts a new worker on the
  same branch. On Docker and on Kubernetes, the hard cap on a worker is
  `timeout_s + ask_wait_s + 120` seconds after it reads its brief.

  **Saved commits.** When the push of a worker fails, the worker sends a
  git bundle of its commits to the manager before its report. The manager
  keeps it in `<DEVELOPER_DATA_DIR>/bundles/` (up to 100 MiB), and deletes
  it after a push or after 14 days. The report has `bundle_saved` and
  `unpushed_status`. `answer` on such a `failed` task starts a new worker
  that restores the commits on the branch. When the session of the earlier
  worker completed, the new worker only pushes, with no session. The
  manager and the worker image must be of the same release.

  **Capacity.** Each `dispatched` or `running` task holds a `max_workers`
  slot, a waiting one too. A `develop` and a `rework` on the same branch
  never run together.

  **Logs.** The access line of a `GET /healthz` shows at `DEBUG` only. A log
  record above `DEBUG` names a chat destination by a hash prefix
  (`destination_ref`), never by its text.

  **To run it.** Mint `JOSHUA_TOKEN_DEVELOPER`, and give it to the manager
  and to channels. On compose, add it to the `x-fleet-tokens` block of the
  joshua-ai compose file. Add `developer` to
  `channels.webhooks.allowed_callers`, and add the `developer` entry, with
  one `identities` entry for each person, to `joshua.yaml`. On Kubernetes,
  restart the manager pod after a change to its Secret, or run a Reloader
  with the chart value `deploymentAnnotations`.

  **Not verified yet.** The Docker runtime against a real Docker daemon, the
  arm64 worker image, and `network: on`.
- The chart gains `extraEnv` (raw `env` entries, such as a `fieldRef`),
  `serviceAccount`, `rbac` (a ServiceAccount, a Role, and a RoleBinding in
  the release namespace), and `networkPolicy` (egress limits for worker pods
  and ingress limits for the addon). All are off by default. A worker pod
  can use DNS only on the cluster resolver (`networkPolicy.dns`). With
  `workersInternetEgress`, it cannot connect to link-local (cloud metadata)
  or carrier-grade NAT addresses.

- A push to any branch, `main` included, builds the image of every addon for
  amd64 and publishes it tagged with the commit SHA and `branch-<name>`, so
  `branch-main` always names the newest commit on `main`. To test a branch
  before a release, point the ArgoCD Application at the branch and set
  `image.tag` to `$ARGOCD_APP_REVISION`. `docs/install.md` shows how. A
  release is still one `v*` tag, multi-arch, with a packaged chart. A weekly
  job removes the SHA-tagged branch builds older than two weeks, and the
  untagged layers, from the registry.

- `mcp` gains `semantic_search(query, source?, limit?)`. It finds passages by
  meaning, with Joshua's own index: it calls joshua-ai core's
  `POST /v1/memory/search`. It needs `CORE_URL`, and `CORE_TOKEN` set to
  joshua-ai's `JOSHUA_TOKEN_MCP`. It needs a joshua-ai release that has the
  route. Without the settings, the other tools work as before.

- `mcp` (joshua-mcp), an addon that gives a caller outside the agent access
  to Joshua's wiki, journal, and knowledge folder. Claude Code on a laptop is
  the first such caller. The tools are `search`, `read_page`,
  `list`, `write_page`, `write_journal_entry`, `read_journal`,
  `knowledge_search`, and `knowledge_read`. A write commits to the wiki
  repository, and records the caller in the frontmatter. Each caller has its
  own token (`MCP_TOKENS`), and a caller can be read-only
  (`MCP_READONLY`). The addon mounts the joshua-ai data volume.
- `chores`, an addon that keeps the chores and an XP ledger for the members
  of one household. It has eleven MCP tools. `list_members`, `list_chores`,
  `get_balance`, and `complete_chore` are for each person. `award_xp`,
  `deduct_xp`, `add_chore`, `update_chore`, `retire_chore`, `add_member`,
  and `set_member` are for a manager. The addon does not know who calls, so
  the README shows two `joshua.yaml` entries on one upstream, each with its
  own `allow` and `tools` filter. The web UI at `/` is a
  manager view that needs `MANAGER_PIN`, and `/<slug>` is the kiosk screen
  of one member. `/` and `/api` are open, as in `pantry`. `/mcp` takes the
  bearer token. The data is in SQLite on a volume by default. Set
  `DATABASE_URL` to use Postgres. `CHORES_TZ` sets the date of the
  household, `COOLDOWN_SECONDS` stops a second completion that comes too
  soon, and `XP_PER_DOLLAR` shows a balance in dollars.
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

- `pantry` gains `merge_items(source, target)`. It combines two tracked
  items that are the same food, for example a duplicate from a receipt. The
  purchases, consumption events, products, and aliases of the source move to
  the target. The source name becomes an alias of the target.
- `add_alias` now tells the agent to use `merge_items` when the alias is
  already a tracked item.
- `pantry`: `record_purchase` returns `possible_matches` for each item that
  it creates. These are up to three tracked items that share a significant
  word with the new name. Filler words such as "organic" and "fresh", and
  sizes, do not count. The automatic match does not change, so "almond
  milk" stays a new item and does not merge into "milk". The tool text tells
  the agent to merge each duplicate with `merge_items`, or to keep the new
  item and tell the person.
- `pantry` gains `check_items(names)`. It answers "do we have X?" For each
  name, it lists every tracked item that matches: the same name, an alias,
  or a name that holds every word of the query. Each match has its status
  and its last purchase date. One verdict for each name is `in_stock` when
  one match is in stock. Otherwise the verdict is the best status of the
  matches, or `no_match`. The agent uses it in place of `get_inventory` for
  a question about one food.

### Changed

- `tasks`, `chores`, `pantry`: each web UI has its own icon in the browser tab and
  on a phone home screen, instead of the Joshua elephant. Each is one icon in
  its own color on the Joshua navy tile: a violet checklist for tasks, green
  repeat arrows for chores, and an orange box for pantry. The app bar keeps
  the Joshua logo.
- `scripts/check_test_policy.py` and the CI test job change a dash in an
  addon directory name to an underscore in the package name, so an addon
  such as `developer-worker` gets its test policy and its coverage.
  `scripts/check_chart_version.py` also checks that a pinned `WORKER_IMAGE`
  tag in an addon's `values.yaml` or `docker-compose.yml`, or in a chart
  `ci` values file, is the chart version, and that each `.env.example`
  names the chart version. `docs/releasing.md` lists every file that a
  version change touches, and the release steps.
- The `vinyl` database gains `master_id`, `sort_artist` and `traits` on the
  albums table, and an `overrides` table. A database from an earlier version
  gains the columns when it is opened. `master_id` is what lets the addon say
  the house owns the same album in another pressing, and it fills at the next
  sync.
- `pantry`: the web UI uses the shared Joshua stylesheet (`joshua-ui.css`
  1.0.0): the app bar, the tabs, the list rows, the chips, the tables, and
  the fields. The field that records a purchase stays at the bottom of the
  inventory tab. The API and the behavior do not change.
- `chores`: the web UI uses the shared Joshua stylesheet (`joshua-ui.css`
  1.0.0), as `pantry` and `tasks` do. The manager view and the kiosk screen
  are dark only; the manager view has no light theme now. The manager view
  gets a sign-out button. The API and the behavior do not change.

### Fixed

- `pantry`: a purchase on the same day as a consumption is in stock. A
  receipt with a bare date records the purchase at 00:00 UTC, and
  `consume_items` records the current time. The status now compares the
  dates for such a purchase, so a purchase on the date of the consumption,
  or later, wins. A consumption on a later date still makes the item out of
  stock. `get_item_history` also reads the latest consumption, so its status
  agrees with `get_inventory` and the web page.
- `pantry`: a merge keeps the data of two purchases on the same date. The
  purchase of the target takes each blank field (cost, store, SKU, UPC,
  quantity) from the purchase of the source. When the two purchases hold
  different values for one field, the target keeps its value. `merge_items`
  reports the other value in `purchase_conflicts`.
- `pantry`: a merge moves the products of the source to the target. Before,
  the merge deleted them. These two fixes apply to the web page too.

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
