# joshua-developer

The `developer` addon lets Joshua give a coding task to a worker. Joshua
calls `develop` with a repository and a brief. The addon starts one worker for
the task. The worker changes the code on a branch and sends a report.

This addon is the manager. It serves MCP to the gateway, keeps the tasks in
SQLite, and applies the rules: the repository list, the git host tokens, the
personas, the worker limit, and one task at a time on one branch. After a
worker pushes, the manager scans the diff and opens the pull request.

Three runtimes start a worker:

- `kubernetes` starts one Job for each task, in the namespace of the
  manager. See "Kubernetes".
- `docker` starts one container for each task, through a Docker socket
  proxy. See "Workers".
- `stub` starts no worker. It marks the task `running` and then records a
  fake `success` report. Use it to test the tools and the rules.

The `kubernetes` and `docker` runtimes run the worker image
`ghcr.io/jakehigg/joshua-addons-developer-worker`. `addons/developer-worker/`
builds it, and its README tells what a worker does.

## Tools

| Tool | What it does |
|---|---|
| `develop(repo, brief, base_branch?, branch?, persona?, notify?)` | Starts a task. Returns `task_id`, `persona`, and `status` at once. |
| `rework(repo, feedback, pr?, branch?, persona?, notify?)` | Starts a task on a pull request. `pr` is a number or a URL. On a `git` host, give `branch` and not `pr`. |
| `answer(task_id, text)` | Gives a waiting task the answer to its question. |
| `task_status(task_id)` | The status, the branch, the pull request, the scan result, the cost, the summary, and the open question. |
| `task_output(task_id)` | The full report and the session log. |
| `list_tasks(limit?)` | The recent tasks of the person. |
| `get_settings()` | The git name, the git email, the default persona, and the chat for reports. |
| `set_settings(git_name?, git_email?, default_persona?, notify?)` | Changes these four settings. An empty string removes the change. |
| `list_personas()` | The personas, and the default persona of the person. |

The status of a task is `dispatched`, `running`, `success`, `failed`,
`blocked`, or `timed_out`.

`repo` is a URL or `host/owner/name`, such as `github.com/owner/name`. The
addon changes a URL to `host/owner/name` in lowercase.

### Rules

- A call that breaks a rule gets `status: rejected`, a `reason`, and a
  `message`. The reasons are `invalid_arguments`, `repo_not_allowed`,
  `repo_not_configured`, `token_missing`, `pr_not_found`, `pr_not_open`,
  `platform_error`, `unknown_persona`, `concurrency_limit`, `locked`, and
  `not_waiting`.
- A person can use only a repository that matches `repos` in
  `developer.yaml`, or the person's own `repos`. The addon checks this
  before it calls the git host.
- The host of the repository must have a platform entry, and the variable
  that the entry names must hold a token. If not, the call gets
  `repo_not_configured` or `token_missing`. The message names the host or the
  variable, and never a token.
- One task at a time works on one branch of a repository, or on one pull
  request. A second call gets `locked` and the id of the first task.
- `max_workers` sets how many tasks run at the same time.
- The persona of a task is the `persona` argument. If there is no argument,
  it is the default of the person, and then the default of the addon.
- A person sees only their own tasks. The task of another person gets
  "404 not found".
- `answer` works only when the task has an open question and no answer yet.
- When the addon starts, each task that is `dispatched` or `running`
  changes to `failed` with the error `manager restarted`.

## Callers

The gateway connects one time for each person, with that person's token.

- `DEVELOPER_TOKENS` gives one token to each person:
  `alex=<token>,mia=<token>`. Each person must be in `people` in
  `developer.yaml`.
- `ADDON_TOKEN` is a token with no person. It can use `task_status`,
  `task_output`, `list_tasks`, and `list_personas` for all tasks. The other
  tools give "403 forbidden: this tool needs a person".

When no token is set, the addon asks for no token. The network is then the
only boundary. The caller has no person, so it cannot start a task.

A missing or wrong token gets HTTP 401. `GET /healthz` needs no token and
answers only `{"ok": true}`.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `DEVELOPER_TOKENS` | empty | One bearer token for each person, as `person=token` pairs, separated by commas. |
| `ADDON_TOKEN` | empty | A bearer token with no person. It can only read. |
| `DEVELOPER_CONFIG` | `/etc/joshua-addon/developer.yaml` | The path of `developer.yaml`. If there is no file, the addon uses the defaults. |
| `DEVELOPER_DATA_DIR` | `/data` | The folder for `developer.db`. |
| `CHANNELS_URL` | empty | The address of joshua-ai channels, for the task reports. Set it with `JOSHUA_TOKEN_DEVELOPER`, or not at all. |
| `JOSHUA_TOKEN_DEVELOPER` | empty | The fleet token that the addon sends to channels. |
| `CLAUDE_CODE_OAUTH_TOKEN` | empty | The Claude token. The Claude forwarder puts it on each worker request. Without it, the forwarder answers 503. |
| `WORKER_RUNTIME` | `stub` | The runtime that starts a worker: `stub`, `docker`, or `kubernetes`. |
| `WORKER_IMAGE` | empty | The worker image. The `docker` and `kubernetes` runtimes need it. Use the worker of the same release. |
| `MANAGER_HOST` | `developer` | The name a worker uses to reach the manager. On Kubernetes, it is the Service name, which is the release name. |
| `POD_NAMESPACE` | the namespace of the pod | The namespace of the worker Jobs. `values.yaml` sets it from the downward API. |
| `WORKER_IMAGE_PULL_SECRET` | empty | The image pull Secret of a worker pod, for a private registry. |
| `WORKER_NETWORK` | `developer_workers` | The Docker network of the workers. |
| `PUBLIC_NETWORK` | `bridge` | The Docker network a worker also joins when `network: on`. |
| `DOCKER_HOST` | empty | The Docker API address, for the `docker` runtime. Empty means the Docker SDK default. |
| `GIT_CA_BUNDLE` | empty | The path of a CA bundle for a git host with a private certificate authority. The manager trusts it for the git host API. |
| one variable for each `token_env` | empty | The git tokens. See "Git hosts". |
| `LOG_LEVEL` | `INFO` | The log level. |

A bad value stops the addon at start. The message names the variable or the
key, and never a token.

### developer.yaml

```yaml
network: off                       # off or on
default_persona: opus
max_workers: 2
worker: { memory: 2g, cpus: 2.0 }  # the limits of one worker container
personas:                          # each entry replaces the built-in persona of that name
  sonnet: { model: claude-sonnet-5,  effort: medium, max_turns: 60,  timeout_s: 1200 }
  opus:   { model: claude-opus-5,    effort: high,   max_turns: 80,  timeout_s: 2400 }
  fable:  { model: claude-fable-5-1, effort: xhigh,  max_turns: 120, timeout_s: 3600 }
platforms:
  github.com:
    kind: github                   # github, gitlab, or git
    token_env: GITHUB_TOKEN        # the name of a variable, never the token
  gitlab.example.net:
    kind: gitlab
    token_env: GITLAB_TOKEN
repos:                             # for all people; fnmatch on host/owner/name
  - github.com/example-home/*
people:
  alex:
    git: { name: Alex Example, email: alex@users.noreply.github.com }
    default_persona: opus
    notify: telegram:dm:alex
    platforms:
      github.com: { token_env: GITHUB_TOKEN_ALEX }
    repos:
      - github.com/alex-example/*
```

`personas` merges over the three built-in personas. An entry with the name
of a built-in persona replaces that persona. The other built-in personas
stay.

The addon refuses a value that looks like a token (`sk-ant-`, `glpat-`,
`ghp_`, `github_pat_`, `xoxb-`). Put the token in an environment variable,
and write the name of the variable in `token_env`.

A person can change `git_name`, `git_email`, `default_persona`, and `notify`
with `set_settings`. The addon keeps the change in `developer.db`. The change
applies before the value in `developer.yaml`.

## Git hosts

`platforms` in `developer.yaml` gives each git host a `kind` and a
`token_env`. The key is the host of the repository, with the port if the
repository address has one.

| `kind` | API | Pull requests | Credential user name |
|---|---|---|---|
| `github` | `https://api.github.com` for `github.com`. `https://<host>/api/v3` for any other host (GitHub Enterprise). | yes | `x-access-token` |
| `gitlab` | `https://<host>/api/v4`, for GitLab.com and a self-hosted GitLab | yes (merge requests) | `oauth2` |
| `git` | none | no; the task ends at the pushed branch | `git` |

`token_env` is the name of the environment variable that holds the token,
never the token. A person can have an entry of their own under
`people.<id>.platforms`. For a task, the addon uses the person's entry for
the host first, then the instance entry. If there is neither, the addon
refuses the task. A person's entry needs a `kind` only when the instance has
no entry for that host.

The worker gets the token and the credential user name in its brief, over
the internal network, with its task token. The token must have access to
the repositories in the lists, and no more.

With a shared token, the commits carry the person's name and email, but the
host shows the push and the pull request as the owner of the token. Give a
person their own token to show them as the author on the host.

### Branches

`develop` without `branch` makes the branch `joshua/<slug>-<id6>`. The slug
is the first line of the brief in kebab case, in ASCII, at most 40
characters. `id6` is the start of the task id. `develop` with `branch` uses
that branch. `base_branch` is `main` when the call gives none.

`rework` asks the git host for the pull request. The pull request gives the
source branch and the base branch. An unknown pull request gets
`pr_not_found`, and a closed or merged one gets `pr_not_open`. A `git` host
has no pull requests, so `rework` there needs `branch`.

### The pull request gate

A worker says in its report that it pushed its branch. The manager then:

1. Reads the diff of the branch against the base branch over the API.
2. Scans the added lines for text that looks like a credential: `sk-ant-`,
   `glpat-`, `ghp_`, `github_pat_`, `gho_`, `xoxb-`, `xoxp-`, an AWS access
   key, a private key block, and a long quoted value set to a name such as
   `api_key`, `secret`, `token`, or `password`.
3. If the scan finds one, the manager deletes the branch and fails the task.
   The error names the pattern, the file, and the line, and never the text.
4. If the scan is clean and the task is a `develop` that ended with
   `success`, the manager finds the open pull request of the branch, or
   opens one. The title is the first line of the brief. The body has the
   summary, the changed files, the tests, the person, and the task id.
5. For a `rework`, the push changed the pull request. The manager checks
   that the pull request is still there.

`scan` in `task_status` is `clean`, `partial` (the host sent no text for
some files, such as a binary file), `hit`, or `unavailable` (a `git` host
has no diff API). An error from the host fails the task and keeps the
branch. The error has the HTTP status and the message of the host, without
the token. The manager tries a call one more time after a 5xx reply.

## Workers

The manager serves three ports:

| Port | What it serves | Who calls it |
|---|---|---|
| 8000 | MCP at `/mcp`, and `/healthz` | the gateway |
| 8001 | the worker API, and `/healthz` | the workers, with a task token |
| 8002 | the git host tunnel, an HTTP `CONNECT` proxy | the workers, with a task token |

The manager makes one task token for each task. The token works only for the
routes of that task, and only while the task runs. A worker gets its task
id, the manager's address, and its task token, and nothing else.

On port 8001, a worker reads its brief, sends log lines, asks a question,
waits for the answer, and sends its report. The Claude forwarder at
`/worker/claude/` sends the worker's Claude requests on to the Claude API
with the manager's `CLAUDE_CODE_OAUTH_TOKEN`. The Claude token never goes
into a worker.

On port 8002, a task token opens a tunnel to one destination: the git host
of the task's repository, on port 443. The tunnel refuses every other host,
and port 22.

A question from a worker goes to the person's chat as an event:
"Developer task <id> has a question. ..." Joshua answers with `answer`.

The `docker` runtime gives each worker the `worker` memory and CPU limits,
no Linux capabilities, and `no-new-privileges`. It stops a worker at the
persona timeout plus 120 seconds. A worker that stops without a report
gets a `failed` report, or `timed_out` at the timeout.

### Docker socket proxy

The manager never mounts the Docker socket. On compose it calls
`tecnativa/docker-socket-proxy`, which passes on the container, image, and
network calls only. The proxy has the socket, read-only, and shares the
internal network `developer_control` with the manager only.

### The network setting

With `network: off`, a worker is on `developer_workers`, an internal network
where the manager is the only other member. It reaches the Claude API and
its git host through the manager, and nothing else. With `network: off`, a
worker cannot install dependencies, so it cannot run most test suites.
`network: on` also connects each worker to `PUBLIC_NETWORK`, which gives it
the internet. Then a repository can send its content anywhere.

## joshua.yaml

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
          Authorization: "Bearer ${DEVELOPER_TOKEN_ALEX}"
      mia:
        headers:
          Authorization: "Bearer ${DEVELOPER_TOKEN_MIA}"
```

## Run it

### Kubernetes

Install `charts/joshua-addon` with `values.yaml` from this folder:

```
helm install developer charts/joshua-addon -f addons/developer/values.yaml
```

The chart makes a claim for `/data` and mounts `developer.yaml` from
`configFile`. Put the tokens in a Secret, and set `existingSecret` to its
name. The comment in `values.yaml` gives the keys.

Set `MANAGER_HOST` to the release name. The release name is also the name
of the Service, and a worker finds the manager with it. The Service has
port 8000 for the gateway, and ports 8001 and 8002 for the workers.

`rbac.enabled: true` makes a ServiceAccount, a Role, and a RoleBinding. The
Role lets the manager create, read, and delete Jobs, and read pods and pod
logs, in its own namespace only.

The manager starts one Job for each task, with the name
`dev-worker-<first 8 characters of the task id>`. The worker pod:

- Runs as uid 1000, with no Linux capabilities, no privilege escalation,
  the `RuntimeDefault` seccomp profile, and a read-only root file system.
- Has an emptyDir at `/work` and an emptyDir at `/tmp`. The worker writes
  its checkout and its home folder there.
- Gets no ServiceAccount token, and no variables other than the four in
  "Workers".
- Has the `worker` memory and CPU of `developer.yaml` as its requests and
  its limits.

The Job never starts a second pod. The manager deletes the Job when the
task ends, and at the persona timeout plus 120 seconds. The Job also has
this time as its deadline, and Kubernetes deletes a finished Job after one
hour.

`networkPolicy.enabled: true` makes two NetworkPolicies. A worker can
connect to the manager on ports 8001 and 8002, and to DNS. It cannot connect
to other addresses. The manager accepts port 8000 from the namespaces in
`networkPolicy.gatewayNamespaces`, or from every namespace when the list is
empty. It accepts ports 8001 and 8002 from the workers only. The CNI of the
cluster must enforce NetworkPolicy. If it does not, a worker can connect to
all addresses.

For `network: on` in `developer.yaml`, set
`networkPolicy.workersInternetEgress: true`. Also set
`networkPolicy.clusterCidrs` to the pod and Service CIDRs of your cluster.
A worker can then connect to public addresses, but not to private addresses
or to the cluster.

### Docker Compose

The compose file starts the manager and the Docker socket proxy, and makes
the two internal networks. Write `developer.yaml` next to
`docker-compose.yml`, copy `.env.example` to `.env`, and then start the
addon:

```
make up ADDON=developer
```

## Development

```
uv run --package joshua-developer pytest addons/developer/tests
```

`scripts/prove_forwarder.sh` sends one real request through the Claude
forwarder. It needs a real Claude token, so only a person runs it:

```
CLAUDE_CODE_OAUTH_TOKEN=<token> addons/developer/scripts/prove_forwarder.sh
```
