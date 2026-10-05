#!/usr/bin/env bash
# Prove the Claude forwarder with a real Claude token. A person runs this by
# hand. CI and the tests never run it, because it sends one real request to
# the Claude API.
#
#   CLAUDE_CODE_OAUTH_TOKEN=<token> addons/developer/scripts/prove_forwarder.sh [model]
#
# The script:
#   1. starts the worker API of a manager on 127.0.0.1:18001, with the stub
#      runtime and a task that stays running;
#   2. sends POST /worker/claude/v1/messages with the task token as the bearer,
#      the fake Claude token a worker holds, and a one-line prompt;
#   3. expects 200. On a failure it prints the status and the reply body.
#
# The data of the throwaway manager goes in a temporary folder that the
# script removes. The script never prints the Claude token.
set -euo pipefail

: "${CLAUDE_CODE_OAUTH_TOKEN:?set CLAUDE_CODE_OAUTH_TOKEN to a Claude token}"
MODEL="${1:-claude-sonnet-5}"
PORT=18001
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
WORK="$(mktemp -d)"
PID=""
cleanup() {
  if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT"
uv run --package joshua-developer python - "$WORK" "$PORT" <<'PY' &
import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from joshua_developer.config import Settings, parse_config
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_worker_app

work, port = Path(sys.argv[1]), int(sys.argv[2])


async def main() -> None:
    config = parse_config({"repos": ["github.com/example-home/*"], "people": {"prove": {}}})
    settings = Settings(
        config=config,
        data_dir=work,
        claude_token=os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
    )
    manager = Manager(settings, TaskStore(settings.db_path), LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    result = await manager.develop("prove", "github.com/example-home/app", "Prove the forwarder.")
    task = manager.store.get_task_full(result["task_id"])
    (work / "token").write_text(task["worker_token"])
    app = build_worker_app(manager)
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()


asyncio.run(main())
PY
PID=$!

for _ in $(seq 1 100); do
  if [ -s "$WORK/token" ] && curl -s -o /dev/null "http://127.0.0.1:$PORT/healthz"; then break; fi
  sleep 0.2
done
TASK_TOKEN="$(cat "$WORK/token")"

# An OAuth token is accepted for the system prompt of Claude Code, so the
# request sends that prompt, as the Claude CLI does.
BODY=$(printf '{"model": "%s", "max_tokens": 32, "system": "You are Claude Code, Anthropic'"'"'s official CLI for Claude.", "messages": [{"role": "user", "content": "Reply with the word OK."}]}' "$MODEL")

STATUS=$(curl -sS -o "$WORK/reply" -w '%{http_code}' \
  -X POST "http://127.0.0.1:$PORT/worker/claude/v1/messages" \
  -H "Authorization: Bearer $TASK_TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "content-type: application/json" \
  -d "$BODY")

if [ "$STATUS" = "200" ]; then
  echo "PASS: the forwarder sent the request with the manager's token (HTTP 200)."
  exit 0
fi
echo "FAIL: HTTP $STATUS from the forwarder. The reply:"
cat "$WORK/reply"
echo
echo "If the Claude API refuses the subscription token here, record the fallback:"
echo "an API key in its own Console workspace, with a spend limit (plan section 2)."
exit 1
