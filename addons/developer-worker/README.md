# joshua-developer-worker

The image `ghcr.io/jakehigg/joshua-addons-developer-worker` does one task of
the `developer` addon. The developer manager (`addons/developer/`) starts one
container of this image for each task. No person runs it by hand, so this
directory has no compose file and no `values.yaml`.

## What the worker does

1. It gets the brief of its task from the manager.
2. It writes the git token to `$HOME/.git-credentials` (mode 0600), clones
   the repository through the git tunnel of the manager, and checks out the
   branch of the task. A `develop` task makes the branch from the base
   branch when the remote does not have it. A `rework` task uses the branch
   that is there.
3. It reads `CLAUDE.md` of the checkout as text, if there is one, and adds
   it to the prompt. It loads no other file from the checkout: no settings,
   no hooks, and no MCP config.
4. It runs one Claude Code session with the tools Read, Edit, Write, Bash,
   Glob, and Grep, and one in-process MCP tool, `ask`. `ask` sends a
   question to the person's chat through the manager and waits for the
   answer. The session never gets WebSearch or WebFetch.
5. The session stops at the persona timeout minus 90 seconds. Then the
   worker commits all work that is not committed (`wip: developer stopped
   (<reason>)`), pushes the branch when it has new commits, and sends the
   report. The worker never commits `.env`, `*.pem`, `*.key`, or `id_rsa*`
   files, and it runs no git hook of the checkout.

The worker exits 0 when the manager accepted the report, or when the task
had already ended. It exits 1 only when it could not send the report.

## Environment

The manager sets these four variables. The worker takes no other setting.

| Variable | What it is |
|---|---|
| `TASK_ID` | The task id. |
| `MANAGER_URL` | The worker API of the manager, `http://<manager>:8001`. |
| `GIT_PROXY_URL` | The git tunnel of the manager, `http://task:<TASK_TOKEN>@<manager>:8002`. |
| `TASK_TOKEN` | The task token. It works only on the routes of this task. |

The image sets `HOME=/work/home` and `CLAUDE_CONFIG_DIR=/work/home/.claude`.
The checkout is `/work/repo`.

The worker puts `GIT_PROXY_URL` in git's own config (`http.proxy`), and not
in `HTTPS_PROXY`. Only git uses the tunnel.

The worker gives the Claude CLI this environment:

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `$MANAGER_URL/worker/claude`, the Claude forwarder of the manager |
| `CLAUDE_CODE_OAUTH_TOKEN` | `$TASK_TOKEN`. The manager replaces it with the real Claude token. |
| `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |

## What was verified

- The four `DISABLE_*` names occur in the CLI binary that the SDK bundles
  (claude-agent-sdk 0.2.144, CLI 2.1.239). The tests did not run the CLI,
  so it is not verified that the CLI makes no other call with them set.
- With `network: off`, a call of the CLI to a host that is not
  `ANTHROPIC_BASE_URL` fails. The first live run must show that the session
  works with this limit.
- The SDK starts the CLI that it bundles before it looks on `PATH`. The
  image also installs `@anthropic-ai/claude-code@2.1.201`, the pin of
  joshua-ai core, and the SDK uses it only when the bundled CLI is missing.

## Tests

```
uv run pytest addons/developer-worker/tests
```

The tests are offline. A fake manager, a local bare repository, and a fake
SDK client take the place of the network, the git host, and the CLI.
