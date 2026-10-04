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
- Run the tests of the project, when the tests need no download (see "The
  network setting").

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
| `develop(repo, brief, base_branch?, branch?, persona?, notify?)` | Starts a task. Returns `task_id`, `persona`, and `status` at once. |
| `rework(repo, feedback, pr?, branch?, persona?, notify?)` | Starts a task on the source branch of a pull request. `pr` is a number or a URL. On a `git` host, give `branch` and not `pr`. |
| `answer(task_id, text)` | Gives a waiting worker the answer to its question. On a task that stopped `blocked` or `timed_out` after a push, starts a new worker that continues on the branch. |
| `task_status(task_id)` | The status, the branch, the pull request, the scan result, the cost, the summary, and the open question. |
| `task_output(task_id)` | The full report and the session log. |
| `list_tasks(limit?)` | The recent tasks of the person, newest first. `limit` is 1 to 100, default 10. |
| `get_settings()` | The git name, the git email, the default persona, and the chat for reports. |
| `set_settings(git_name?, git_email?, default_persona?, notify?)` | Changes these four settings. An empty string removes the change. |
| `list_personas()` | The personas, and the default persona of the person. |

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
`not_waiting`, `not_resumable`, and `branch_is_base`. The rules:

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
  apply. When no worker pushed, the result is `not_resumable`: call
  `develop` again. `task_output` shows the reports of the earlier workers in
  `history`.
- When the manager starts, each task that is `dispatched` or `running`
  changes to `failed` with the error `manager restarted`, and its report
  goes to the chat.

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
   of the task's repository. On Kubernetes, a worker can also use the cluster
   DNS resolver, so a DNS tunnel through the resolver stays possible.
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
`network: off`, a worker cannot install dependencies, so it cannot run most
test suites. With `network: on`, a worker can connect to the internet, so
dependency installs work, and a bad repository can send its content anywhere.
On Docker Compose, `network: on` also puts each worker on the default
bridge, so it can reach the host LAN and every port that the host
publishes. On Kubernetes, the chart blocks private addresses (see "Run it on
Kubernetes").

## Settings

### developer.yaml

```yaml
network: off                       # off or on. See "The network setting".
default_persona: opus              # the persona when neither the call nor the person names one
max_workers: 2                     # tasks that run at the same time
ask_wait_s: 7200                   # the total wait for answers in one task. See "The task clock".
worker: { memory: 2g, cpus: 2.0, disk: 4Gi }  # the limits of one worker
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
| `ADDON_TOKEN` | empty | A bearer with no person. It can use `task_status`, `task_output`, `list_tasks`, and `list_personas` only. |
| `DEVELOPER_CONFIG` | `/etc/joshua-addon/developer.yaml` | The path of `developer.yaml`. No file means the defaults. |
| `DEVELOPER_DATA_DIR` | `/data` | The folder of `developer.db`. |
| `CHANNELS_URL` | empty | The address of joshua-ai channels. Set it with `JOSHUA_TOKEN_DEVELOPER`, or not at all. |
| `JOSHUA_TOKEN_DEVELOPER` | empty | The fleet token that the manager sends to channels. |
| `CLAUDE_CODE_OAUTH_TOKEN` | empty | The Claude token of the forwarder. Without it, the forwarder answers 503. |
| `WORKER_RUNTIME` | `stub` | `docker`, `kubernetes`, or `stub`. `stub` starts no worker and records a fake `success` report, to test the tools. |
| `WORKER_IMAGE` | empty | The worker image. `docker` and `kubernetes` need it. Use the worker of the same release. |
| `MANAGER_HOST` | `developer` | The name a worker uses to reach the manager. |
| `WORKER_NETWORK` | `developer_workers` | The Docker network of the workers. |
| `PUBLIC_NETWORK` | `bridge` | The Docker network a worker also joins with `network: on`. |
| `DOCKER_HOST` | empty | The Docker API address. Empty means the Docker SDK default. |
| `POD_NAMESPACE` | the namespace of the pod | The namespace of the worker Jobs. |
| `WORKER_IMAGE_PULL_SECRET` | empty | The image pull Secret of a worker pod, for a private registry. |
| `GIT_CA_BUNDLE` | empty | The path of a CA bundle for a git host with a private certificate authority. The manager trusts it for the host API, and sends its text to each worker, which trusts it for git. |
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
  manager on port 8001 (the worker API) and port 8002 (the git host tunnel).
- `developer_control`: the manager and the proxy.

The manager is also on the joshua-ai network, where the gateway reaches it
at `http://developer:8000/mcp`. The addon publishes no port. With
`network: on`, each worker also joins `PUBLIC_NETWORK`, by default the
default bridge (see "The network setting").

A worker gets the `worker` memory and CPU limits, at most 512 processes, no
Linux capabilities, `no-new-privileges`, and a read-only root file system.
It writes to `/work` and `/tmp`, two anonymous volumes that the manager
removes with the container. Docker has no disk limit for them: see
`worker.disk`. Each worker has the label `joshua-developer-instance` with
the value of `MANAGER_HOST`. The manager removes old workers, and counts
workers, with that label only, so two installs on one Docker daemon need
two values of `MANAGER_HOST`. The manager pulls the worker image when it
starts, and a task pulls it again only when it is missing.

`.env` also takes two compose settings. `JOSHUA_ADDONS_VERSION` picks the
release of both images. `JOSHUA_NETWORK` names the joshua-ai network when
its compose project is not `joshua-ai`. To mount a CA bundle for
`GIT_CA_BUNDLE`, add a volume to the `developer` service.

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
- For a private registry, set `WORKER_IMAGE_PULL_SECRET` to the name of an
  image pull Secret in the namespace.

The two chart blocks are on:

- `rbac` makes a ServiceAccount, a Role, and a RoleBinding. The Role lets the
  manager create, read, and delete Jobs, and read pods and pod logs, in its
  own namespace only.
- `networkPolicy` lets a worker connect to the manager on ports 8001 and
  8002, and to DNS on the cluster resolver, and to no other address. Set
  `networkPolicy.dns` in the chart values when your cluster DNS pods are not
  `k8s-app: kube-dns` in `kube-system`. The resolver sends queries for other
  names on, so a DNS tunnel through it stays possible. The policy lets the
  manager accept ports 8001 and 8002 from the workers only. The CNI of the cluster must
  enforce NetworkPolicy. If it does not, a worker can connect to all
  addresses.

For `network: on`, set `networkPolicy.workersInternetEgress: true`, and set
`networkPolicy.clusterCidrs` to the pod and Service CIDRs of the cluster. A
worker can then connect to public addresses, but not to private addresses,
link-local addresses (cloud metadata), carrier-grade NAT addresses, or the
cluster.

The manager starts one Job for each task, named
`dev-worker-<first 8 characters of the task id>`, with `-r<n>` added for
the worker of a resumed task. The worker pod runs as uid
1000 with no Linux capabilities, the `RuntimeDefault` seccomp profile, and a
read-only root file system. It writes to two emptyDirs, `/work` and `/tmp`,
each with the `sizeLimit` `worker.disk`. It gets no ServiceAccount token. The
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

- **The Docker runtime against a real Docker daemon.** The tests use a fake
  Docker client.
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
