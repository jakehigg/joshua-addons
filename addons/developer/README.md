# joshua-developer

The `developer` addon lets Joshua give a coding task to a worker. Joshua
calls `develop` with a repository and a brief. The addon starts one worker for
the task. The worker changes the code on a branch and sends a report.

This addon is the manager. It serves MCP to the gateway, keeps the tasks in
SQLite, and applies the rules: the repository list, the personas, the worker
limit, and one task at a time on one branch.

Two runtimes start a worker:

- `docker` starts one container for each task, through a Docker socket
  proxy. See "Workers".
- `stub` starts no worker. It marks the task `running` and then records a
  fake `success` report. Use it to test the tools and the rules.

The worker image and the git host adapters come in later releases. Until
then, a `docker` worker has no image to run.

## Tools

| Tool | What it does |
|---|---|
| `develop(repo, brief, base_branch?, branch?, persona?, notify?)` | Starts a task. Returns `task_id`, `persona`, and `status` at once. |
| `rework(repo, pr, feedback, persona?, notify?)` | Starts a task on a pull request. `pr` is a number or a URL. |
| `answer(task_id, text)` | Gives a waiting task the answer to its question. |
| `task_status(task_id)` | The status, the branch, the pull request, the cost, the summary, and the open question. |
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
  `unknown_persona`, `concurrency_limit`, `locked`, and `not_waiting`.
- A person can use only a repository that matches `repos` in
  `developer.yaml`, or the person's own `repos`.
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
| `WORKER_RUNTIME` | `stub` | The runtime that starts a worker: `stub` or `docker`. `kubernetes` stops the addon at start. |
| `WORKER_IMAGE` | empty | The worker image. The `docker` runtime needs it. The compose file sets the worker of the same release. |
| `MANAGER_HOST` | `developer` | The name a worker uses to reach the manager. |
| `WORKER_NETWORK` | `developer_workers` | The Docker network of the workers. |
| `PUBLIC_NETWORK` | `bridge` | The Docker network a worker also joins when `network: on`. |
| `DOCKER_HOST` | empty | The Docker API address, for the `docker` runtime. Empty means the Docker SDK default. |
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

Install `charts/joshua-addon` with `values.yaml` from this folder. The chart
makes a claim for `/data` and mounts `developer.yaml` from `configFile`. Put
the tokens in a Secret, and set `existingSecret` to its name.

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
