# joshua-developer-worker

The image `ghcr.io/jakehigg/joshua-addons-developer-worker` does one task of
the `developer` addon. The developer manager (`addons/developer/`) starts one
container of this image for each task. No person runs it by hand, so this
directory has no compose file and no `values.yaml`.

## What the worker does

1. It gets the brief of its task from the manager. The manager starts the
   task clock at this call.
2. It writes the git token to `$HOME/.git-credentials` (mode 0600). When the
   brief has a CA bundle (`git_ca_pem`, from `GIT_CA_BUNDLE` on the
   manager), it writes it to `$HOME/git-ca.pem` and sets git's
   `http.sslCAInfo` to that file. It clones
   the repository through the git tunnel of the manager, and checks out the
   branch of the task. A `develop` task makes the branch from the base
   branch when the remote does not have it. A `rework` task uses the branch
   that is there. So does a resumed task: a brief with `resumed: true`
   comes from an `answer` to a task whose earlier worker pushed the branch.
   Its prompt starts the task with the note of that worker and the answer.
3. It reads `CLAUDE.md` of the checkout as text, if there is one, and adds
   it to the prompt. It loads no other file from the checkout: no settings,
   no hooks, and no MCP config.
4. It runs one Claude Code session with the tools Read, Edit, Write, Bash,
   Glob, and Grep, and one in-process MCP tool, `ask`. `ask` sends a
   question to the person's chat through the manager and waits for the
   answer. When the manager says nobody gets the question, `ask` returns at
   once and tells the model to set `blocked`. The session never gets WebSearch or WebFetch.
5. The session has the persona timeout minus 90 seconds of work. The clock
   pauses while `ask` waits for an answer. The model can ask more than one
   question. `ask_wait_s`, a value from the brief, is the total waiting time
   for the task: each wait stops when the waits of the task reach it. The
   worker then tells the manager that it stopped the wait, and `ask` tells
   the model that no answer came. When no wait time is left, `ask` sends no
   question and returns a reply that says so at once. When the clock runs
   out, the worker interrupts the session. Then the worker
   pushes the branch when the model made new commits, and sends the
   report. The worker never makes a commit of its own: a file the model left
   uncommitted is named in the report and is not pushed, because a commit the
   worker made could carry a file the model never meant to publish. The push
   runs no git hook of the checkout.

The report status is `success` when the session ends with no `blocked` in
its result, and `blocked` when the model sets `blocked`. When the clock runs
out, the status is `blocked` if a question is still open, else
`timed_out`. A session error or a failed push makes it `failed`.

The worker exits 0 when the manager accepted the report, or when the task
had already ended. It exits 1 only when it could not send the report.

## The report

The report has these fields: `status`, `summary`, `files_changed`,
`tests_run`, `open_question`, `pushed`, `head` (the branch it pushed),
`clone_head` (the commit of the branch after the checkout), `created_branch`
(true when the remote did not have the branch), `commit_hash`, `error`, the
token counts, the cost, and `log`. The manager scans the diff from
`clone_head` to the branch, and deletes the branch after a scan hit only
when `created_branch` is true on a `develop`. The report has no field for
the branch name or the pull request. The manager takes them from its own
data, and it refuses a report with an unknown field.

## Environment

The manager sets these four variables. The worker takes no other setting,
except the test aid `JOSHUA_WORKER_FAKE_SESSION` below.

| Variable | What it is |
|---|---|
| `TASK_ID` | The task id. |
| `MANAGER_URL` | The worker API of the manager, `http://<manager>:8001`. |
| `GIT_PROXY_URL` | The git tunnel of the manager, `http://task:<TASK_TOKEN>@<manager>:8002`. |
| `TASK_TOKEN` | The task token. It works only on the routes of this task. |

The image sets `HOME=/work/home` and `CLAUDE_CONFIG_DIR=/work/home/.claude`.
The checkout is `/work/repo`. The worker keeps its files in `/work` and `/tmp`.
The root file system is read-only. On Kubernetes, these two folders are
emptyDirs with a size limit. On Docker, they are anonymous volumes, except
with `worker.disk_docker_storage_opt: true`, which keeps them in a writable
root file system with a size limit.

The worker puts `GIT_PROXY_URL` in git's own config (`http.proxy`), and not
in `HTTPS_PROXY`. Only git uses the tunnel.

The worker gives the Claude CLI this environment:

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `$MANAGER_URL/worker/claude`, the Claude forwarder of the manager |
| `CLAUDE_CODE_OAUTH_TOKEN` | `$TASK_TOKEN`. The manager replaces it with the real Claude token. |
| `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |

## Which CLI runs

The SDK (claude-agent-sdk 0.2.144) starts the CLI that it bundles, 2.1.239,
before it looks on `PATH`. The image also installs
`@anthropic-ai/claude-code@2.1.201`, the pin of joshua-ai core. The SDK uses
that CLI only when the bundled CLI is missing. Node stays in the image for
that CLI, and because the model runs the formatter and the tests of a
JavaScript project with Bash.

## Test aid: the scripted session

Not for production. When `JOSHUA_WORKER_FAKE_SESSION` is `1`, the worker
does not start the Claude Agent SDK. It writes `hello-from-worker.txt`
with the task id, commits it with the person's git identity from the
brief, and returns a structured result with `blocked` empty. When the
value is `ask`, it first calls `ask` one time with a fixed question and
writes the reply into the file. If nobody gets the question, the result
has the question in `blocked`, and the task ends `blocked`. All other
steps, the clone, the push, and the report, are the same as for a real
session. The scripted session needs no Claude token. Any other value, or
no value, runs the real session. The manager sets this variable only when
its own `WORKER_FAKE_SESSION` is set, on the Docker runtime.

## What is verified, and what is not

A live run on Kubernetes (2026-10-04) verified the Claude forwarder with a
subscription token, and an `ask` that Joshua answered.

These are not verified:

- The arm64 image. The release workflow builds it, but no task ran on it.
- A real Claude session in a worker that the Docker runtime starts. The
  compose end-to-end job of the manager starts workers on a real Docker
  daemon with the scripted session.
- `network: on`. The four `DISABLE_*` names occur in the bundled CLI
  binary. With `network: off`, a call of the CLI to a host that is not
  `ANTHROPIC_BASE_URL` fails. With `network: on`, these switches are the
  only guard, and it is not verified that the CLI makes no other call.

## Tests

```
uv run pytest addons/developer-worker/tests
```

The tests are offline. A fake manager, a local bare repository, and a fake
SDK client take the place of the network, the git host, and the CLI.
