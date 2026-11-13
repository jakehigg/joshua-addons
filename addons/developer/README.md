# joshua-developer

The `developer` addon lets Joshua give a coding task to a worker. It has two
parts. The **manager** is this addon: an MCP server that the gateway calls.
It holds every secret, keeps the tasks in SQLite, applies the rules, and
opens the pull request. The **worker** is the image
`ghcr.io/jakehigg/joshua-addons-developer-worker`, from
`addons/developer-worker/`. The manager starts one worker container for each
task. The worker clones the repository, runs one Claude Code session, commits,
pushes one branch, sends one report, and stops.

## What it can do

- Change code in a repository on GitHub, GitHub Enterprise, GitLab, or a
  plain git host, on a new branch or on the branch of an open pull request.
- Open the pull request (GitHub) or the merge request (GitLab) after a clean
  credential scan.
- Ask Joshua questions in the middle of a task, and wait for the answers,
  for up to `ask_wait_s` in total (see "The task clock").
- Install the dependencies of the project and run its tests, when the
  `packages` block names the registries the project needs (see "The package
  proxy").

## What it cannot do

- Merge a pull request, move a label, or read an issue. The addon has no
  tool for these. Joshua reads the issue and puts what the worker needs in
  the brief.
- Read the wiki, the journal, or any Joshua memory. The worker asks Joshua
  with `ask`.
- Start a task on a repository that is not in the lists in `developer.yaml`.

## Tools

| Tool | What it does |
|---|---|
| `develop(repo, brief, base_branch?, branch?, persona?, notify?, refs?)` | Starts a task. Returns `task_id`, `persona`, and `status` at once. `refs` is a list of up to 5 reference repositories that the worker can read (see [Reference repositories](#reference-repositories)). |
| `rework(repo, feedback, pr?, branch?, persona?, notify?, refs?)` | Starts a task on the source branch of a pull request. `pr` is a number or a URL. On a `git` host, give `branch` and not `pr`. `refs` is the same as for `develop`. |
| `answer(task_id, text)` | Gives a waiting worker the answer to its question. On a task that stopped `blocked` or `timed_out` after a push, starts a new worker that continues on the branch. |
| `task_status(task_id)` | The status, the branch, the pull request, the scan result, the cost, the summary, and the open question. |
| `task_output(task_id)` | The full report and the session log. |
| `list_tasks(limit?)` | The recent tasks of the person, newest first. `limit` is 1 to 100, default 10. |
| `get_settings()` | The git name, the git email, the default persona, and the chat for reports. |
| `set_settings(git_name?, git_email?, default_persona?, notify?)` | Changes these four settings. An empty string removes the change. |
| `list_personas()` | The personas, and the default persona of the person. |
| `run_project(repo, project, branch?, persona?, notify?)` | Starts a run: it works through the open tasks of a tasks project on one branch and one pull request (see [Development auto-run](#development-auto-run)). |
| `run_status(run_id?, project?)` | The status, the branch, the pull request, and the current task of a run. With no argument, it lists the recent runs of the person. |
| `pause_run(run_id?, project?)` | Pauses a run. The task that runs now goes on; no next task starts. |
| `resume_run(run_id?, project?)` | Resumes a paused run, and starts its next open task. |
| `stop_run(run_id?, project?)` | Stops a run for good. |

A worker that needs a fact calls `ask`. The manager sends the question to the
person's chat as an event, and Joshua answers with `answer`, from what it
knows or after it asks the person. When the task and the person have no
`notify` chat, or the event does not go through, the manager keeps the
question in `open_question` but nobody gets it. `ask` then returns at once
and tells the model to do the work that does not need the answer, and to
set `blocked` with the question.

The status of a task is `dispatched`, `running`, `success`, `failed`,
`blocked`, or `timed_out`. A task is `dispatched` until its worker reads its
brief, and `running` after that. `repo` is a URL or `host/owner/name`, such as
`github.com/owner/name`.

A call that breaks a rule gets `status: rejected`, a `reason`, and a
`message`. The reasons are `invalid_arguments`, `repo_not_allowed`,
`repo_not_configured`, `token_missing`, `pr_not_found`, `pr_not_open`,
`platform_error`, `unknown_persona`, `concurrency_limit`, `locked`,
`not_waiting`, `not_resumable`, and `branch_is_base`. The run tools add
`tasks_not_configured`, `tasks_unreachable`, `project_not_found`, `no_tasks`,
`run_exists`, `run_not_found`, `run_ambiguous`, `run_not_pausable`, and
`run_not_resumable`. The rules:

- One task at a time works on one branch of a repository. A `rework` works
  on the source branch of its pull request, so a `develop` and a `rework` on
  the same branch do not run together. A second call gets `locked` and the
  id of the first task.
- `max_workers` sets how many tasks are `dispatched` or `running` at the
  same time. A task whose worker still starts holds its slot.
- The persona is the `persona` argument, else the default of the person,
  else `default_persona`.
- A person sees only their own tasks.
- `answer` on a `running` task works only when the worker waits for the
  answer to its open question. When the worker stopped the wait, or the
  question was not sent, `answer` gets `not_waiting`. When the task then
  ends `blocked` or `timed_out` after a push, an `answer` resumes it.
- `answer` on a `blocked` or `timed_out` task resumes it, when a worker of
  the task pushed the branch. The task keeps its id, and a new worker gets
  the answer and the summary of the last worker. `max_workers` and the lock
  apply. When no worker pushed and the manager has no saved commits of the
  task, the result is `not_resumable`: call `develop` again.
- `answer` on a `failed` task resumes it when the manager has saved commits
  of the task (see [When a push fails](#when-a-push-fails)). `task_output` shows the reports of the earlier workers in
  `history`.
- A manager restart does not end a task whose worker still runs. See
  [What a restart does to a running task](#what-a-restart-does-to-a-running-task).

## Development auto-run

`run_project` replaces the project loop that a manager prompt (such as
`40-projects.md`) runs by hand. A **run** works through the open tasks of one
project of the tasks addon, in roadmap order. It owns one branch (default
`feat-<project slug>`) and one pull request in one repository. It never merges.

How a run goes:

1. `run_project` starts the first task as `develop`. The brief is the task
   title, the task notes, and the line "Commit and push your work." The first
   line is the title of the pull request. A project that has an `active` or
   `paused` run in the same repository gets `run_exists`.
2. The task ends. The manager records the report. When the task is a task of
   a run, the manager then takes one step. A step never raises into the
   report.
3. On `success`, the step marks the tasks task done, and adds "done in
   `<pull request>`" to its notes. It then reads the limits and the next task:
   - No open task is left: the run is `done`, and Joshua gets a model turn:
     "Project `<name>` finished".
   - Every open task waits for a blocker: the run pauses, and Joshua gets a
     model turn.
   - The next task is in another task group (a phase): the run pauses, and
     Joshua gets a model turn: "Phase `<name>` done". Say `resume` to go on.
   - A limits window is at or over its ceiling: the run pauses, and Joshua
     gets a model turn.
   - Else the next task starts as `rework` of the pull request (or `develop`
     again, when no pull request exists yet). The step adds "dispatched as
     developer task `<id>` on `<branch>`" to its notes, and sends a verbatim
     line to the chat: `✓ #3 Title — next: #4 Title`. A verbatim line costs
     no model turn.
4. On a failure (`failed`, `timed_out`, or `blocked` with no question), the
   step starts the same task one more time and sends a verbatim line. On the
   second failure the run pauses, and the normal report event gives Joshua a
   model turn.
5. On a question, the normal report event goes out, and the run stays
   `active`. The `answer` tool goes on as for any task, and the step runs
   when the task ends.

The normal report event of a task goes out only for a task with no run, and
for a failure that the retry did not fix, and for a question. A run does not
cost a model turn for each task. Each verbatim line and each model turn
carries the `workspace` of the run, when the run has one.

`pause_run` and `stop_run` let the task that runs now go on to its end. The
step then marks it done, and starts no next task. `resume_run` makes a paused
run `active` and starts its next task. It does not check the ceilings. It also
restarts an `active` run whose task a manager restart ended.

Set `TASKS_URL` for the tasks addon and `CORE_URL` for joshua-ai core (see
"Environment"). Without `TASKS_URL`, `run_project` returns `tasks_not_configured`.
Without `CORE_URL`, the limits are unknown, and a run goes on. A window that
core cannot give is unknown too. The ceilings are the `runs` block of
`developer.yaml`: `ceiling_five_hour_pct` (default 80) and
`ceiling_seven_day_pct` (default 90).

A call with a workspace stamp gets a `resource_link` to the pull request in
the result of each run tool, once the run has a pull request.

## Workspaces

A call from a Joshua workspace carries the workspace id in
`params._meta["joshua/workspace"]`. The gateway sets it. A call from any other
chat has no such key, and then nothing here applies.

- `develop` and `rework` store the id on the task, in the `workspace` column.
  An older database gets the column when the manager starts.
- Each report event and each question event of that task has a `workspace`
  field with the same id in its body to `/v1/events`. Channels then runs the
  event in the workspace. An event of a task without an id has no `workspace`
  field.
- `develop`, `rework`, and `task_status` add an MCP `resource_link` block to
  their result when the call has the id and the task has a pull request. The
  link is the URL of the pull request. A task has no report page, so no link
  names one. A call with no id gets the same result as before, with no link.

## Reference repositories

A task can read other repositories beside its own: for example, to move code
from one repository to another. Give them in `refs` on `develop` or `rework`,
in the same form as `repo`, at most 5.

- Each one passes the same checks as `repo`: it is in the list of the
  person, and its git host has a platform and a token. The task repository
  itself and a repeated entry are refused. `task_status` shows `refs`, and a
  resumed task keeps them.
- The worker clones each one, shallow, on its default branch, into
  `/work/refs/<host>/<owner>/<name>`. The clone has no push URL, and its
  files are read-only. The session can read the folder, and the prompt tells
  the model to read the source there and write the code into the checkout.
- The worker pushes only the task branch of `repo`. The manager scans and
  opens the pull request only there.
- The git host tunnel lets the task reach the host of each reference
  repository too, and the brief carries the token of each such host.

The read-only clone is a guard, not a boundary: the git token of a host can
still write where the host lets it. Give a reference repository on a host
whose token can only read when that matters.

## The task clock

A task has a time limit on its work: the persona's `timeout_s`. The clock
starts when the worker reads its brief, so the image pull and the container
start are not on it. The clock pauses while a question waits for an answer,
because a wait for a person is not work. `ask_wait_s`, 2 hours by default,
is the total time that a task can wait for answers, over all its questions.

- When the answer comes, the clock starts again, and the worker continues.
- When the task has used its `ask_wait_s` with no answer, the worker stops
  the wait, and the clock starts again. The model gets a reply that says no
  answer came, and the question stays open. A later question in the same
  task gets that reply at once and is not sent.
- The task ends `blocked` only when the model sets `blocked` in its result,
  or when the clock runs out while a question is open. A later `answer`
  resumes a `blocked` task when a worker of the task pushed the branch.
- `task_status` shows `started_at`, `waiting_since` (the start of the open
  wait), and `paused_s` (the seconds of the waits that ended).

On Docker and on Kubernetes, the manager stops a worker 120 seconds after
the persona timeout plus the paused time. The hard cap is
`timeout_s + ask_wait_s + 120` seconds after `started_at`, and no wait
moves it. A worker that has not read its brief `WORKER_START_GRACE_S`
seconds (600 by default) after its start is stopped, and its task fails
with an error that names the image pull. On Kubernetes, the Job's
`activeDeadlineSeconds` is `timeout_s + ask_wait_s + 120 +
WORKER_START_GRACE_S`, so Kubernetes stops a worker that the manager cannot.
The manager stops the container, or deletes the Job and waits for its pod
to go, before it records `timed_out`, so the worker cannot push after that.

A worker that waits still holds one of the `max_workers` slots, and the lock
of its branch. A long wait can thus stop a new task with
`concurrency_limit`. Two settings control this: a lower `ask_wait_s` frees a
slot sooner, and a higher `max_workers` lets other tasks run during a wait.

## Security

The worker runs a model with Bash over code that a stranger can write. Text
in that code can tell the model what to do. The addon treats the worker as
hostile, and these rules limit what it can read and where its data can go:

1. **The worker holds no Joshua secret.** It has the repository, a git token
   for its host, and a task token that works only on its own task's routes on
   the manager. It has no wiki, no journal, no gateway, and no MCP server but
   `ask`. The git token can do on the host what its scope allows, so give it
   the smallest scope. The model can push to every branch that the token can
   write, so protect the default branch on the host. The addon refuses a task
   on the base branch, `main`, or `master` (`branch_is_base`). That is a
   guard, not a boundary.
2. **The Claude token never goes into the worker.** The worker sends its
   Claude requests to a forwarder on the manager, with the task token as a
   fake Claude token. The manager puts the real token on each request. The
   forwarder sends on only the API paths that the Claude CLI uses
   (`v1/messages`, `v1/messages/count_tokens`, `v1/models`, and
   `v1/models/<model id>`), and refuses
   other paths with 403. It answers the CLI probe `api/hello` itself.
3. **By default, a worker reaches the manager and nothing else.** The
   manager forwards two destinations for it: the Claude API, and the git host
   of the task's repository (and the host of each of its reference
   repositories). With the `packages` block on, it also forwards
   the package hosts in that block, with no credential (see "The package
   proxy"). On Kubernetes, a worker can also use the cluster DNS resolver, so
   a DNS tunnel through the resolver stays possible.
4. **The manager gates the pull request.** It scans the diff of the worker's
   commits for credentials before it opens the pull request. A hit fails the
   task, and deletes the branch only when the task made it. A person reads
   every pull request.
5. **The worker's words are data, not instructions.** Its report and its
   questions go to Joshua as events that say so. Each text from the worker
   is in a fenced block that the worker cannot close. The branch and the
   pull request in the event come from the manager's own data, never from
   the report. The worker API refuses a body over 2 MiB, and the manager
   cuts each stored report field to its limit.
6. **The repository configures nothing.** The session loads no settings, no
   hooks, and no MCP servers from the checkout. It reads `CLAUDE.md` as
   plain text.
7. **Each task is alone.** A new container, a new clone, and a new config
   folder for each task, with limits on time, turns, memory, and CPU.
8. **A person reaches only listed repositories.** The manager refuses a
   repository outside the person's list before it calls the git host.

What a bad repository can still do: spend money on the Claude plan up to the
persona limits, put text in a diff that a person reads, and send the content
of the repository to Anthropic and to its own git host. With `network: on`, it
can also send that content to any address.

## The network setting

`network` in `developer.yaml` is `off` or `on`, and `off` is the default. With
`network: off`, a worker reaches the manager only. To let it install
dependencies, turn on the `packages` block, and keep `network: off`. With
`network: on`, a worker can connect to the internet, so dependency installs
work, and a bad repository can send its content anywhere.
On Docker Compose, `network: on` also puts each worker on the default
bridge, so it can reach the host LAN and every port that the host
publishes. On Kubernetes, the chart blocks private addresses (see "Run it on
Kubernetes").

## The package proxy

With the `packages` block on, a worker can install dependencies and run the
tests of the project, and it still reaches no other address. The manager
listens on port 8003 and speaks HTTP `CONNECT`. It allows a connection only
when the host in the `CONNECT` line is in `packages.hosts`, and refuses every
other host with 403 and one log line that names it. The worker gets
`HTTPS_PROXY` and `HTTP_PROXY` (`http://<MANAGER_HOST>:8003`) and `NO_PROXY`
(the manager host, the loopback, and the cluster domain), so `npm ci`,
`uv sync`, and the other package tools use the proxy, and the manager API,
the Claude forwarder, and the git tunnel do not.

```yaml
packages:
  enabled: true
  hosts:
    - registry.npmjs.org
    - pypi.org
    - files.pythonhosted.org
```

- `enabled: false` is the default. The manager then starts no proxy, nothing
  listens on 8003, and a worker gets no proxy variable.
- Each host is a name, such as `registry.npmjs.org`. A pattern, a port, an
  address, or a URL is refused at start. The check is on the name, because a
  registry sits behind a CDN whose addresses change, and a rule on addresses
  would break.
- The proxy carries no credential: a public registry needs none. The tunnel
  carries TLS from end to end, so the proxy reads nothing in it.
- The proxy gives a worker no new access to a repository. A git host belongs
  in `platforms`, not here: a worker reaches a git host only through the
  tunnel on port 8002, which injects the token of the task and refuses a
  push to another host. A `CONNECT` to port 22 gets 403.
- A worker still sends the content of the repository nowhere but Anthropic,
  its own git host, and the hosts in this list.
- The proxy variables are a convenience, not the boundary. A worker that
  ignores them reaches no registry, because the runtime holds it in: on
  Docker Compose the worker container is on `developer_workers`, an internal
  Docker network that reaches the manager only; on Kubernetes the
  NetworkPolicy of the chart lets the worker pod reach the manager ports and
  the cluster DNS only.

## Settings

### developer.yaml

```yaml
network: off                       # off or on. See "The network setting".
default_persona: opus              # the persona when neither the call nor the person names one
max_workers: 2                     # tasks that run at the same time
ask_wait_s: 7200                   # the total wait for answers in one task. See "The task clock".
worker:                            # the limits of one worker
  memory: 2g
  cpus: 2.0
  disk: 4Gi
  manager_grace: 600
  env: {}                          # extra variables for your own worker image. See "Using your own worker image".
work: { keep_failed: 7 }           # days a kept work directory stays. See "The work directories".
runs:                              # the limits ceilings of a run, in percent used. See "Development auto-run".
  ceiling_five_hour_pct: 80
  ceiling_seven_day_pct: 90
packages:                          # the package proxy. See "The package proxy".
  enabled: false                   # the default: no proxy, and no proxy variable in a worker
  hosts:                           # host names, never a pattern or an address
    - registry.npmjs.org
    - pypi.org
    - files.pythonhosted.org
personas:                          # a model, its effort, and its limits
  sonnet: { model: claude-sonnet-5,  effort: medium, max_turns: 60,  timeout_s: 1200 }
  opus:   { model: claude-opus-5,    effort: high,   max_turns: 80,  timeout_s: 2400 }
  fable:  { model: claude-fable-5-1, effort: xhigh,  max_turns: 120, timeout_s: 3600 }
platforms:                         # a git host, its kind, and the token for all people
  github.com:
    kind: github                   # github, gitlab, or git
    token_env: GITHUB_TOKEN        # the name of a variable, never the token
  gitlab.example.net:
    kind: gitlab
    token_env: GITLAB_TOKEN
repos:                             # for all people; fnmatch on host/owner/name
  - github.com/example-home/*
people:                            # one entry for each person in DEVELOPER_TOKENS
  alex:
    git: { name: Alex Example, email: alex@users.noreply.github.com }
    default_persona: opus
    notify: telegram:dm:alex       # the chat for reports and questions
    platforms:                     # this person's own token for a host
      github.com: { token_env: GITHUB_TOKEN_ALEX }
    repos:                         # added to the list for all people
      - github.com/alex-example/*
```

`personas` merges over the three built-in personas above. An entry with the
name of a built-in persona replaces it. The other built-in personas stay.
`effort` is `low`, `medium`, `high`, `xhigh`, or `max`. A persona can also
set its own `ask_wait_s`, which replaces the instance value for its tasks.
`ask_wait_s` is a positive whole number of seconds.

`worker.disk` is a Kubernetes quantity, such as `4Gi`. On Kubernetes, it is
the `sizeLimit` of each of the two emptyDirs and the `ephemeral-storage`
limit of the worker. Docker has no disk limit for a container. On Docker,
`worker.disk_docker_storage_opt: true` (off by default) sets the
`storage_opt` `size` of the container to `worker.disk`. The storage driver
must support it (overlay2 on xfs with `pquota`). The limit applies to the
writable layer of the container only, so with this setting `/work` and
`/tmp` stay in that layer, and the root file system is not read-only.

`worker.manager_grace` is the number of seconds a worker keeps trying to
reach a manager that does not answer, default 600. See "What a restart does
to a running task".

`worker.env` is a map of extra environment variables for each worker. Both
runtimes give them to the worker. A name must be upper-case letters, digits,
and underscores. A reserved name is not valid, and the manager stops at
start with the name in the message. See "Using your own worker image".

Give each person a `notify` chat. Without one, a report and a question go
nowhere, and a worker that asks is told to set `blocked`. `notify` is a
channels destination: a logical name or a reference such as
`telegram:dm:alex`.

The addon refuses a value that looks like a token (`sk-ant-`, `glpat-`,
`ghp_`, `github_pat_`, `xoxb-`, or `AKIA` and 16 upper-case letters or
digits). A person can change the git name, the
git email, `default_persona`, and `notify` with `set_settings`. The change
applies before the value in the file.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `DEVELOPER_TOKENS` | empty | One bearer for each person, `alex=<token>,mia=<token>`. Each person must be in `people`. |
| `ADDON_TOKEN` | empty | A bearer with no person. It can use `task_status`, `task_output`, `list_tasks`, `list_personas`, and `run_status` only. |
| `DEVELOPER_CONFIG` | `/etc/joshua-addon/developer.yaml` | The path of `developer.yaml`. No file means the defaults. |
| `DEVELOPER_DATA_DIR` | `/data` | The folder of `developer.db`, and of `bundles/`: the saved commits of failed pushes. |
| `CHANNELS_URL` | empty | The address of joshua-ai channels. Set it with `JOSHUA_TOKEN_DEVELOPER`, or not at all. |
| `JOSHUA_TOKEN_DEVELOPER` | empty | The fleet token that the manager sends to channels. |
| `TASKS_URL` | empty | The address of the tasks addon, such as `http://tasks:8000`. The manager adds `/api`, which needs no bearer. Without it, `run_project` is refused. |
| `CORE_URL` | empty | The address of joshua-ai core, such as `http://core:8000` (Compose) or `http://joshua-core:8000` (Kubernetes). A run reads `GET /v1/limits` from it, which needs no bearer. Without it, the limits are unknown. |
| `CLAUDE_CODE_OAUTH_TOKEN` | empty | The Claude token of the forwarder. Without it, the forwarder answers 503. |
| `WORKER_RUNTIME` | `stub` | `docker`, `kubernetes`, or `stub`. `stub` starts no worker and records a fake `success` report, to test the tools. |
| `WORKER_IMAGE` | empty | The worker image. `docker` and `kubernetes` need it. Use the worker of the same release, or your own image (see "Using your own worker image"). On Kubernetes, the chart value `worker.image` sets it. |
| `MANAGER_HOST` | `developer` | The name a worker uses to reach the manager. |
| `WORKER_NETWORK` | `developer_workers` | The Docker network of the workers. |
| `PUBLIC_NETWORK` | `bridge` | The Docker network a worker also joins with `network: on`. |
| `DOCKER_HOST` | empty | The Docker API address. Empty means the Docker SDK default. |
| `POD_NAMESPACE` | the namespace of the pod | The namespace of the worker Jobs. |
| `WORKER_IMAGE_PULL_SECRET` | empty | The image pull Secret of a worker pod, for a private registry. The chart sets it from `worker.imagePullSecret`. |
| `WORKER_IMAGE_PULL_POLICY` | `IfNotPresent` | Kubernetes: when a worker pod pulls its image. `Always`, `IfNotPresent`, or `Never`. The chart sets it from `worker.imagePullPolicy`. The Docker runtime does not read it: it pulls an image that is missing, and no other time. |
| `GIT_CA_BUNDLE` | empty | The path of a CA bundle for a git host with a private certificate authority. The manager trusts it for the host API, and sends its text to each worker, which trusts it for git. |
| `DEVELOPER_WORK_DIR` | empty | Where the manager has the work storage mounted, such as `/work`. Set, each task gets its own directory on it, which a restart keeps. Empty, each worker has a scratch `/work`. See "The work directories". |
| `WORKER_WORK_CLAIM` | empty | Kubernetes: the ReadWriteMany PersistentVolumeClaim of the work storage, which the manager also mounts at `DEVELOPER_WORK_DIR`. Required with `DEVELOPER_WORK_DIR` on `kubernetes`. The chart sets it from `worker.workClaim`. |
| `WORKER_WORK_VOLUME` | empty | Docker: the named volume of the work storage, which the manager also mounts at `DEVELOPER_WORK_DIR`. Required with `DEVELOPER_WORK_DIR` on `docker`. Docker Engine 26 or later. |
| `WORKER_START_GRACE_S` | `600` | The seconds a worker has from its start to its first brief call: the image pull and the container start. A worker that takes longer is stopped, and its task fails. |
| each `token_env` in `developer.yaml` | empty | The git tokens. |
| `LOG_LEVEL` | `INFO` | The log level. The access line of a `GET /healthz` shows at `DEBUG` only. |

A bad value stops the manager at start. The message names the variable or
the key, and never a token. A missing or wrong bearer gets HTTP 401.
`GET /healthz` needs no bearer and answers `{"ok": true}`. With no bearer
set, the addon asks for none, and no caller has a person, so no caller can
start a task.

### The Secret on Kubernetes

Put these keys in one Secret, and name it in `existingSecret`:
`DEVELOPER_TOKENS`, `ADDON_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
`JOSHUA_TOKEN_DEVELOPER`, and one key for each `token_env` in
`developer.yaml`, such as `GITHUB_TOKEN` and `GITHUB_TOKEN_ALEX`.

The manager reads the Secret when its pod starts. After you change the
Secret, restart the pod, or run a Reloader: put its annotation, such as
`reloader.stakater.com/auto: "true"`, in the chart value
`deploymentAnnotations`. A change to `configFile` (`developer.yaml`)
restarts the pod on its own, through the `checksum/config` annotation.

## Connect it to joshua-ai

1. Mint a token for each person and for the fleet identity `developer`. Put
   the person tokens in `DEVELOPER_TOKENS`.
2. Add the addon to `joshua.yaml`, with one `identities` entry for each
   person. A group chat has no person, so it cannot start a task.

```yaml
mcp:
  developer:
    type: http
    url: http://developer:8000/mcp
    allow:
      - alex
      - mia
    identities:
      alex:
        headers:
          Authorization: "Bearer ${DEVELOPER_TOKEN_ALEX:-}"
      mia:
        headers:
          Authorization: "Bearer ${DEVELOPER_TOKEN_MIA:-}"
channels:
  webhooks:
    allowed_callers: [laptop, ci, developer]
```

3. Give the gateway `DEVELOPER_TOKEN_ALEX` and `DEVELOPER_TOKEN_MIA`, with
   the same values as in `DEVELOPER_TOKENS`. On compose, add them to the
   `gateway` environment in the joshua-ai `docker-compose.yml`. On
   Kubernetes, add them under `secrets.gateway.keys`.
4. Give channels `JOSHUA_TOKEN_DEVELOPER`, with the same value as the
   manager. On compose, see step 3 of "Run it on Docker Compose". On
   Kubernetes, add `JOSHUA_TOKEN_DEVELOPER: {}` under
   `secrets.channels.keys`.
5. Apply the change as `docs/install.md` says: reload the gateway, and
   restart core and channels.

On Kubernetes, the `url` is
`http://<release>.<namespace>.svc.cluster.local:8000/mcp`.

## Run it on Docker Compose

1. Write `developer.yaml` next to `docker-compose.yml`.
2. Copy `.env.example` to `.env`, and set the tokens. The manager gets every
   variable in `.env`, so the git tokens need no other step.
3. Add `JOSHUA_TOKEN_DEVELOPER` to the `x-fleet-tokens` block of the
   joshua-ai `docker-compose.yml`, with the same value as in `.env`. The
   joshua-ai compose file gives channels a fixed list of fleet tokens, so
   channels refuses the events of the manager until you add it. Apply the
   change as step 5 of "Connect it to joshua-ai" says.
4. Start the addon:

```
make up ADDON=developer
```

After a change to `.env`, run `make up ADDON=developer` again: compose
recreates the manager with the new values.

The compose file starts two services, the manager and
`tecnativa/docker-socket-proxy`. The manager never mounts the Docker socket.
The proxy has the socket, read-only, and passes on the container, image, and
network calls only. It refuses `exec`. Because the proxy has the socket, the
compose file pins its image to the tag `0.3.0`, the version whose
`haproxy.cfg` the comments in the compose file describe. A release also pins
its digest, so a moved tag cannot change the code that has the socket.

The compose file makes two internal networks:

- `developer_workers`: the workers and the manager. A worker reaches the
  manager on port 8001 (the worker API), port 8002 (the git host tunnel),
  and port 8003 (the package proxy, with the `packages` block on).
- `developer_control`: the manager and the proxy.

The manager is also on the joshua-ai network, where the gateway reaches it
at `http://developer:8000/mcp`. The addon publishes no port. With
`network: on`, each worker also joins `PUBLIC_NETWORK`, by default the
default bridge (see "The network setting").

A worker gets the `worker` memory and CPU limits, at most 512 processes, no
Linux capabilities, `no-new-privileges`, and a read-only root file system.
It writes to `/tmp`, an anonymous volume that the manager removes with the
container, and to `/work`, its own directory of the `developer-work` volume
(see "The work directories"). Docker has no disk limit for them: see
`worker.disk`. Each worker has the label `joshua-developer-instance` with
the value of `MANAGER_HOST`. The manager removes old workers, and counts
workers, with that label only, so two installs on one Docker daemon need
two values of `MANAGER_HOST`. The manager pulls the worker image when it
starts, and a task pulls it again only when it is missing.

`.env` also takes two compose settings. `JOSHUA_ADDONS_VERSION` picks the
release of both images. `JOSHUA_NETWORK` names the joshua-ai network when
its compose project is not `joshua-ai`. To mount a CA bundle for
`GIT_CA_BUNDLE`, add a volume to the `developer` service.

### Proved in CI

The CI job `developer-compose-e2e` runs `scripts/e2e_compose.sh` on a real
Docker daemon when a file under `addons/developer/` or
`addons/developer-worker/` changes. The script builds both images, and
starts this compose file with a throwaway `developer.yaml`, a git server
over HTTPS with a test CA, and `WORKER_FAKE_SESSION`. It sends one
`develop` through MCP and expects `success`: one commit by the person on
the branch, and no worker container or volume left. Then it sends one
`develop` in the `ask` mode with no chat, and expects `blocked` with the
open question. The job calls no Claude API and needs no secret. You can
run the script on any host with Docker, `uv`, `curl`, and `python3`.

### Test aids

Not for production. These settings replace the work of a real worker.

| Variable | Default | What it does |
|---|---|---|
| `WORKER_FAKE_SESSION` | empty | `1` or `ask`, with `WORKER_RUNTIME=docker` only. The Docker runtime gives the value to each worker as `JOSHUA_WORKER_FAKE_SESSION`, and the worker runs a scripted session, not Claude Code. See the worker README. The manager logs a warning at start when it is set. |

## Run it on Kubernetes

```
helm install developer charts/joshua-addon -f addons/developer/values.yaml
```

`values.yaml` turns on persistence at `/data`, mounts `developer.yaml` from
`configFile`, and sets `WORKER_RUNTIME: kubernetes`. Also:

- Set `existingSecret` to the Secret in "The Secret on Kubernetes".
- Set `MANAGER_HOST` to the release name. The release name is the Service
  name, and a worker finds the manager by it.
- Set `CHANNELS_URL` to the address of the joshua-ai channels Service.
- For your own worker image, set `worker.image`. It wins over
  `env.WORKER_IMAGE`. See "Using your own worker image".
- For a private registry, set `worker.imagePullSecret` to the name of an
  image pull Secret in the namespace.
- To keep the work of a task through a restart, make a ReadWriteMany
  PersistentVolumeClaim in the namespace, and set `worker.workClaim` to its
  name. The chart does not make the claim. See "The work directories".

The two chart blocks are on:

- `rbac` makes a ServiceAccount, a Role, and a RoleBinding. The Role lets the
  manager create, read, and delete Jobs, and read pods and pod logs, in its
  own namespace only.
- `networkPolicy` lets a worker connect to the manager on ports 8001, 8002,
  and 8003, and to DNS on the cluster resolver, and to no other address. Set
  `networkPolicy.dns` in the chart values when your cluster DNS pods are not
  `k8s-app: kube-dns` in `kube-system`. The resolver sends queries for other
  names on, so a DNS tunnel through it stays possible. The policy lets the
  manager accept ports 8001, 8002, and 8003 from the workers only. The CNI of
  the cluster must enforce NetworkPolicy. If it does not, a worker can connect
  to all addresses.

For `network: on`, set `networkPolicy.workersInternetEgress: true`, and set
`networkPolicy.clusterCidrs` to the pod and Service CIDRs of the cluster. A
worker can then connect to public addresses, but not to private addresses,
link-local addresses (cloud metadata), carrier-grade NAT addresses, or the
cluster.

The manager starts one Job for each task, named
`dev-worker-<first 8 characters of the task id>`, with `-r<n>` added for
the worker of a resumed task. The worker pod runs as uid
1000 with no Linux capabilities, the `RuntimeDefault` seccomp profile, and a
read-only root file system. It writes to `/tmp`, an emptyDir with the
`sizeLimit` `worker.disk`, and to `/work`: an emptyDir of the same size, or,
with `worker.workClaim`, its own directory of the work claim (see "The work
directories"). It gets no ServiceAccount token. The
`worker` limits, with `ephemeral-storage` set to `worker.disk`, are its
requests and its limits.

## Git hosts

| `kind` | API | Pull requests | Credential user name |
|---|---|---|---|
| `github` | `https://api.github.com` for `github.com`. `https://<host>/api/v3` for GitHub Enterprise. | yes | `x-access-token` |
| `gitlab` | `https://<host>/api/v4`, for GitLab.com and a self-hosted GitLab | yes, merge requests | `oauth2` |
| `git` | none | no. The task ends at the pushed branch. | `git` |

The key of a platform is the host of the repository, with the port when the
repository address has one. For a task, the manager uses the person's entry
for the host, else the entry for all people. With neither, it refuses the
task. A person's entry needs a `kind` only when the host has no entry for
all people.

Give each token access to the listed repositories and no more. The worker
gets the token of its task in the brief. With a shared token, the commits
carry the person's name and email, but the host shows the push and the pull
request as the owner of the token. Give a person their own token to show
them on the host.

For a git host with a private certificate authority, mount the CA bundle and
set `GIT_CA_BUNDLE` to its path. The manager trusts it for the host API. The
brief gives its text to the worker, which writes it to its home folder and
sets `http.sslCAInfo`, so git trusts it too.

### Branches

`develop` without `branch` makes `joshua/<slug>-<id6>`: the first line of the
brief in kebab case, at most 40 characters, and the start of the task id.
`base_branch` is `main` when the call gives none. When the branch is already
on the remote, the worker continues on it. `rework` gets the source branch
from the pull request.

A task never works on its base branch, `main`, or `master`. `develop` and
`rework` refuse such a branch with `branch_is_base`, and a `rework` on a
`git` host refuses `main` and `master`.

On GitLab, the manager finds the merge request of a branch only when it
comes from the same project. A merge request from a fork with a branch of
the same name is not taken.

### The pull request gate

After the worker pushes, the manager:

1. Reads the diff from `clone_head` to the pushed branch over the API.
   `clone_head` is the commit of the branch before the session, so the
   diff holds only the worker's commits. Without a `clone_head`, the diff
   starts at the base branch. The worker sends `clone_head`, so the scan is
   a guard against a mistake, not against a hostile worker.
2. Scans the added lines for credentials: `sk-ant-`, `glpat-`, `ghp_`,
   `github_pat_`, `gho_`, `xoxb-`, `xoxp-`, an AWS access key, a private key
   block, and a quoted value of 16 or more characters set to a name such as
   `api_key`, `secret`, `token`, or `password`. That value must mix at least
   three of lowercase, uppercase, digits, and symbols.
3. On a hit, fails the task. The error names the pattern, the file, and the
   line, and never the text. The manager deletes the branch only when this
   task made it: a `develop` whose worker found no branch on the remote. A
   branch that was there before the task stays, and the error says so. A
   `rework` never deletes the branch.
4. On a clean `develop` with status `success`, finds or opens the pull
   request. A `rework` push changes the pull request that is there.

`scan` in `task_status` is `clean`, `partial` (the host sent no text for a
file, or did not list every file), `hit`, or `unavailable` (a `git` host
has no diff API). GitHub lists at most 300 files in one compare reply. The
manager reads the reply page by page, and when the list stops at that cap,
the scan is `partial`. A task that
ends `blocked` or `timed_out` keeps its pushed branch and gets no pull
request. One `answer` call resumes it. A new `develop` with the same
`branch` also continues the work, as a new task.

## When a push fails

A push can fail after a complete session: for example, the git token has no
write access to the repository, or the token expired. The worker then does
not lose the commits:

1. The worker makes a git bundle of its commits after `clone_head`, and
   sends it to the manager (`PUT /worker/bundle`) before the report.
2. The manager keeps the bundle at `<DEVELOPER_DATA_DIR>/bundles/<task id>.bundle`.
   The limit is 100 MiB.
3. The task ends `failed`. The report has `bundle_saved: true`, and
   `unpushed_status`: the status of the session before the push failed. The
   error says that the commits are saved.
4. Correct the cause, then call `answer` on the task. A new worker clones the
   repository and restores the commits from the bundle on the task branch.
   - When `unpushed_status` is `success`, the session was complete. The
     worker pushes the commits and reports with the summary of the earlier
     worker. It runs no session, so it uses no Claude tokens. Any answer
     text does this, for example `push it`.
   - Otherwise, the new worker runs a session on top of the restored
     commits, with the answer.
5. After a worker of the task pushes, the manager deletes the bundle. The
   manager also deletes each bundle older than 14 days when it starts.

When the bundle cannot be made or sent, the error says that the commits are
lost, and `answer` returns `not_resumable`.

## What a restart does to a running task

A deploy of the addon, a changed Secret with a Reloader, a node drain, and a
crash all restart the manager. The worker is a separate Job or container, and
it keeps running.

1. **The worker waits.** Every call of the worker to the manager (the brief,
   the log, a question, the report, the git tunnel of a push) is tried again
   on a connection error, with a longer wait each time, for
   `worker.manager_grace` seconds (default 600). The Claude CLI reaches the
   model through the forwarder of the manager, so the worker also sets
   `CLAUDE_CODE_MAX_RETRIES` high enough for the CLI to wait the same time.
   The session and the commits stay in the worker.
2. **The manager finds the worker again.** At start, the manager looks for
   the worker of each task that is `dispatched` or `running`: the Job
   (Kubernetes) or the container (Docker) with the name of the current worker
   and the task label. When it is there, the task keeps its status, its lock,
   and its task token, and a new supervisor watches the worker. The deadline
   of the task does not change. Then the manager removes the workers that no
   active task owns.
3. **A task fails only when its worker is gone.** The error is `the worker is
   gone: the manager restarted and found no worker for the task`, and the
   report goes to the chat. The work directory of the task stays (see "The
   work directories"), so `answer` can resume the task from its commits.
4. **No report is lost.** A worker whose report cannot reach the manager in
   `worker.manager_grace` seconds writes the report to its log as the last
   line (`unsent report`), and exits. When the manager then finds a worker
   that ended, it takes the report from the log. A worker that ended with no
   such line gets a `failed` report from the state of its Job or container.
   The Job of a worker stays for one hour after it ends
   (`ttlSecondsAfterFinished`), so the manager must be back in that time to
   read the log.

A restart that takes longer than `worker.manager_grace` seconds stops the
worker. The commits stay in its work directory.

**The strategy of the chart.** The chart uses `Recreate` for the manager,
because the claim of `developer.db` is ReadWriteOnce: the new pod can only
mount it after the old pod stops. The workers wait out the gap, so a deploy
does not need to drain the tasks first. A drain would hold a deploy for as
long as the longest task runs. Keep `worker.manager_grace` longer than the
time a new manager pod takes to start, the image pull included.

A Reloader on the Secret now restarts the manager without a lost task. It
still breaks the open Claude request of each worker once, and the CLI sends
it again.

## The work directories

Each worker clones the repository into `/work` and commits there. Where
`/work` lives decides what a restart, a failed push, or a crash loses.

| Runtime | Storage of `/work` | Kept after the worker ends |
|---|---|---|
| `docker` (the compose file) | `<task id>` on the named volume `developer-work` (`WORKER_WORK_VOLUME`) | yes |
| `kubernetes` with `worker.workClaim` | `<task id>` on that claim (`WORKER_WORK_CLAIM`, `subPath`) | yes |
| `kubernetes` without `worker.workClaim` | an emptyDir | no |
| `docker` without `WORKER_WORK_VOLUME` | an anonymous volume | no |

With kept storage, the manager has the storage mounted at
`DEVELOPER_WORK_DIR`, and:

1. Before a worker starts, the manager makes the directory of the task,
   `<DEVELOPER_WORK_DIR>/<task id>`. The runtime mounts that directory only
   at `/work` in the worker, so a worker cannot read the work of another
   task.
2. **Nothing is deleted while a task is not successful.** The manager deletes
   the directory of a task only when the task ends `success`. A task that
   ends `failed`, `blocked`, or `timed_out`, or that a manager restart
   failed, keeps its directory and its commits.
3. A resumed task keeps its directory. A new task of the same person, for
   the same repository and branch as a kept task, takes the directory of the
   newest such task. The worker then finds the checkout in `/work`, fetches,
   and continues on the branch from the commits that are there, instead of
   cloning. When the remote branch moved, the worker rebases the kept
   commits on it; when that rebase fails, the commits stay as they are.
4. `answer` also resumes a `failed` task whose directory holds a checkout,
   for example a task that a manager restart failed.
5. A sweeper, at start and every hour, deletes a kept directory when its
   task ended more than `work.keep_failed` days ago (default 7), and a
   directory with no task in the store that is older than that. It logs each
   deletion. It never deletes the directory of an active task.

The git token is not on the kept storage: with `DEVELOPER_WORK_DIR` set, a
worker gets `HOME=/tmp/home`, which goes with the worker. The manager runs
as uid 1000 and must be able to write the root of the storage. When it
cannot, a task fails with `the work directory was not made`.

## Using your own worker image

A task needs the toolchain of its project. `WORKER_IMAGE` can be your own
image, with the compilers and the tools that your repositories need. Build it
`FROM` the worker image of your release, so it keeps the worker program:

```dockerfile
FROM ghcr.io/jakehigg/joshua-addons-developer-worker:0.2.0
USER root
RUN apt-get update && apt-get install -y --no-install-recommends <your tools> \
    && rm -rf /var/lib/apt/lists/*
USER 1000
```

### The run-time contract

The manager starts your image the same way on both runtimes, and the code
enforces these rules. An image that does not keep them fails its tasks.

| Rule | What it means for your image |
|---|---|
| The process runs as uid 1000 | Install your tools as root at build time. The worker is not root and cannot install at run time. |
| The root file system is read-only | A path such as `/opt/<name>` or `/usr/local` is read-only at run time. A tool that writes beside itself fails. |
| Only `/work` and `/tmp` are writable | Nothing else takes a write. Each is a volume with the size `worker.disk` on Kubernetes. |
| `/work` is the directory of the task | With work storage, the runtime mounts one directory of it, the task id (`subPath` on Kubernetes, `subpath` on Docker), at `/work`. The checkout is there, and it stays after the task. |
| `HOME` is `/tmp/home` | With work storage. `CLAUDE_CONFIG_DIR` is `/tmp/home/.claude`. Both go with the worker, so no credential stays on the work storage. |
| The entrypoint starts the worker | Keep `ENTRYPOINT ["python", "-m", "joshua_developer_worker"]`. The manager gives the container no command. |
| `WORKDIR` is `/work` | Keep it. The worker clones into the current directory. |
| `/app/.venv/bin` is first on `PATH` | Keep that order. Your own directory goes after it. |
| The image labels stay | `FROM` keeps `org.opencontainers.image.version` and `org.opencontainers.image.revision` of the upstream image. Do not set them again: they name the release and the commit your worker came from. |
| `imagePullPolicy` is `IfNotPresent` | Kubernetes pulls a tag it already has again only with `WORKER_IMAGE_PULL_POLICY: Always`. Give each build its own tag, or set the policy. The Docker runtime pulls the image at start, and a task pulls it only when it is missing. |

The compose file and the chart values of this repository give a worker the
work storage, so the rows above apply as written. `worker.disk` limits each
volume on Kubernetes only. On Docker, the root file system is writable only
with `worker.disk_docker_storage_opt: true` (see "developer.yaml"), and an
image must not need that.

The read-only root file system has one consequence for every toolchain: each
variable that names a writable directory must point into `/tmp`. Rust shows
it. `CARGO_HOME` and `RUSTUP_HOME` are in `$HOME` by default, and a
`cargo build` writes to them. npm, pip, Go, and Gradle have the same need.
Set each of these variables in an `ENV` line of your image.

### worker.env

`worker.env` in `developer.yaml` holds the values that change with the
deployment: the URL of an internal registry, the path of a CA bundle, a
package mirror. Both runtimes give them to each worker, so the Compose user
and the Kubernetes user write the same block:

```yaml
worker:
  memory: 4g
  cpus: 4.0
  disk: 8Gi
  env:
    PIP_INDEX_URL: https://nexus.example.com/repository/pypi/simple
    NODE_EXTRA_CA_CERTS: /etc/ssl/certs/example-ca.pem
```

A variable of the toolchain in the image, such as `CARGO_HOME`,
`RUSTUP_HOME`, or `PATH`, belongs in an `ENV` line of that image. The person
who builds the image sets it there, and it travels with the image, so it
cannot get out of step with the toolchain that it points at.

These names are reserved, because the addon sets them itself:

`TASK_ID`, `MANAGER_URL`, `GIT_PROXY_URL`, `TASK_TOKEN`, `MANAGER_GRACE_S`,
`HOME`, `CLAUDE_CONFIG_DIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and
`JOSHUA_WORKER_FAKE_SESSION`.

One of these names in `worker.env` stops the manager at start, with an error
that names it. The addon does not drop the value and does not take it.

Put no credential in `worker.env`. The addon treats a worker as hostile (see
"Security"), and it refuses a value in `developer.yaml` that looks like a
token.

### Point the manager at the image

On Docker Compose, set `WORKER_IMAGE` in `.env`:

```
WORKER_IMAGE=ghcr.io/<you>/my-worker:2026-11-13
```

On Kubernetes, set the chart values. `worker.image` wins over
`env.WORKER_IMAGE`:

```yaml
worker:
  image: ghcr.io/<you>/my-worker:2026-11-13
  imagePullSecret: ghcr-pull   # for a private registry
  imagePullPolicy: IfNotPresent
```

A worker still reaches the manager only. To install dependencies at run
time, turn on the `packages` block (see "The package proxy"), or put the
dependencies in the image.

## The first run

1. Prove your Claude token through the forwarder, from a checkout:
   `CLAUDE_CODE_OAUTH_TOKEN=<token> addons/developer/scripts/prove_forwarder.sh`.
   It starts a worker API on your machine, sends one real request with a
   task token, as a worker does, and prints `PASS` or `FAIL`. It proves
   that the Claude API takes your token through the forwarder. It does not
   start a worker or touch a git host.
2. Start one small `develop` on a scratch repository.
3. Check `task_status`. The task is `dispatched` until its worker reads its
   brief. The first task can wait for the pull of the worker image. A worker
   that has not read its brief after `WORKER_START_GRACE_S` seconds (600 by
   default) is stopped, and the error names the image pull.
4. Read `task_output` while the task runs. It shows the session log.
5. Expect the report near the end of the persona timeout, at the latest.
   Time that a question waits for an answer is added. The worker stops the
   session 90 seconds before the timeout, then pushes and reports. The
   manager stops a worker that is still there 120 seconds after the
   timeout.

## What is verified, and what is not

A live run on Kubernetes (2026-10-04) verified these:

- The Claude forwarder with a subscription token.
- The `answer` path: Joshua's answer to a question event arrives on the
  person's connection, and the worker gets it.

These are not verified:

- **A real Claude session in a worker that the Docker runtime starts.** The
  unit tests use a fake Docker client. The compose end-to-end job uses a
  real Docker daemon and a scripted session.
- **The arm64 worker image.** The release workflow builds it, but no task
  ran on it.
- **`network: on`.** The worker sets `DISABLE_TELEMETRY`,
  `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. With `network: off`, a call of
  the CLI to another host fails. With `network: on`, these switches are the
  only guard, and it is not verified that the CLI makes no other call.

## Development

```
uv run --package joshua-developer pytest addons/developer/tests
make up-dev ADDON=developer
```

`make up-dev` builds the manager and the worker from the checkout, tagged
`:dev`, and points `WORKER_IMAGE` at the local worker build.
