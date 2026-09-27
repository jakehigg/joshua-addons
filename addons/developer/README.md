# joshua-developer

The `developer` addon lets Joshua give a coding task to a worker. Joshua
calls `develop` with a repository and a brief. The addon starts one worker for
the task. The worker changes the code on a branch and sends a report.

This addon is the manager. It serves MCP to the gateway, keeps the tasks in
SQLite, and applies the rules: the repository list, the personas, the worker
limit, and one task at a time on one branch.

This release has the `stub` runtime only. The stub starts no worker. It marks
the task `running` and then records a fake `success` report. Use it to test
the tools and the rules.

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
| `WORKER_RUNTIME` | `stub` | The runtime that starts a worker. This release has `stub` only. `docker` and `kubernetes` stop the addon at start. |
| `WORKER_IMAGE` | empty | The worker image. The `stub` runtime does not use it. |
| `LOG_LEVEL` | `INFO` | The log level. |

A bad value stops the addon at start. The message names the variable or the
key, and never a token.

### developer.yaml

```yaml
network: off                       # off or on
default_persona: opus
max_workers: 2
personas:                          # when set, replaces the three built-in personas
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

The addon refuses a value that looks like a token (`sk-ant-`, `glpat-`,
`ghp_`, `github_pat_`, `xoxb-`). Put the token in an environment variable,
and write the name of the variable in `token_env`.

A person can change `git_name`, `git_email`, `default_persona`, and `notify`
with `set_settings`. The addon keeps the change in `developer.db`. The change
applies before the value in `developer.yaml`.

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

Write `developer.yaml` next to `docker-compose.yml`, copy `.env.example` to
`.env`, and then start the addon:

```
make up ADDON=developer
```

## Development

```
uv run --package joshua-developer pytest addons/developer/tests
```
