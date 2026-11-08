#!/usr/bin/env bash
# Prove the Docker runtime of the developer addon from end to end, on a host
# with Docker. CI runs this script in the developer-compose-e2e job. A person
# can run it too:
#
#   addons/developer/scripts/e2e_compose.sh
#
# The script:
#   1. builds the manager and the worker images from this checkout (:dev);
#   2. starts a git server over HTTPS (e2e_gitserver.py) on the joshua network,
#      with a test CA, and one bare repository, scratch/repo, with a main branch;
#   3. writes a throwaway developer.yaml and .env in a temporary folder. The
#      .env sets WORKER_FAKE_SESSION, so each worker runs the scripted session
#      and never calls Claude;
#   4. starts the manager and the Docker socket proxy with compose;
#   5. sends one develop through MCP with the bearer of alex, and expects
#      success: a branch with one commit by Alex, and the worker removed;
#   6. sends one more develop with WORKER_FAKE_SESSION=ask. No person has a
#      notify target and no channels exist, so the question is not sent. The
#      worker gets the fallback reply at once, and the task ends blocked with
#      the open question.
#   7. removes all that it made, also after a failure.
#
# Each check prints a PASS or FAIL line. The exit code is 0 only when all
# checks pass. The script needs docker (with compose), uv, curl, and python3.
# It keeps the :dev images, as `make up-dev ADDON=developer` does.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HERE="$ROOT/addons/developer"
PROJECT=developer-e2e
JOSHUA_NET=joshua-ai_default
GIT_HOST=git.e2e.test
GIT_PORT=8443
GIT_CONTAINER=developer-e2e-git
GIT_IMAGE=developer-e2e-git:local
# The git server image: Python and git only. Pinned by digest (2026-10-04).
GIT_BASE=python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a
MANAGER_IMAGE=joshua-addons-developer:dev
WORKER_IMAGE=joshua-addons-developer-worker:dev
MCP_PORT="${E2E_MCP_PORT:-18000}"
REPO="$GIT_HOST:$GIT_PORT/scratch/repo"
BARE=/srv/git/scratch/repo.git
QUESTION="Which greeting goes in hello-from-worker.txt?"
TASK_TIMEOUT_S=300

WORK="$(mktemp -d)"
CREATED_NET=0
FAILED=0
TASK_IDS=()

pass() { echo "PASS: $*"; }
fail() {
  echo "FAIL: $*"
  FAILED=1
}
# same <actual> <expected> <check>: PASS when the two are equal.
same() {
  if [ "$1" = "$2" ]; then
    pass "$3"
  else
    fail "$3 (got: $1)"
  fi
}
# Stop at once. Use it when the next steps cannot run.
die() {
  echo "FAIL: $*"
  FAILED=1
  exit 1
}

compose() {
  docker compose -p "$PROJECT" --project-directory "$WORK" \
    -f "$HERE/docker-compose.yml" -f "$WORK/docker-compose.e2e.yml" "$@"
}

# The value of one key of a JSON object, or an empty string.
field() {
  python3 -c 'import json, sys; v = json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"
}

cleanup() {
  local rc=$?
  set +eu
  if [ "$rc" -ne 0 ] || [ "$FAILED" -ne 0 ]; then
    echo "--- manager log (last 100 lines)"
    compose logs --no-color --tail 100 developer 2>/dev/null
    echo "--- git server log (last 50 lines)"
    docker logs --tail 50 "$GIT_CONTAINER" 2>&1
  fi
  for id in "${TASK_IDS[@]}"; do
    for c in $(docker ps -aq --filter "label=task-id=$id"); do docker rm -f -v "$c" >/dev/null 2>&1; done
  done
  compose down -v --remove-orphans >/dev/null 2>&1
  docker rm -f -v "$GIT_CONTAINER" >/dev/null 2>&1
  docker image rm "$GIT_IMAGE" >/dev/null 2>&1
  if [ "$CREATED_NET" -eq 1 ]; then docker network rm "$JOSHUA_NET" >/dev/null 2>&1; fi
  rm -rf "$WORK"
  if [ "$rc" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
    echo "PASS: all checks"
  else
    echo "FAIL: one or more checks failed"
    exit 1
  fi
}
trap cleanup EXIT

docker info >/dev/null 2>&1 || die "docker does not answer"
for net in developer_workers developer_control; do
  if docker network inspect "$net" >/dev/null 2>&1; then
    die "the network $net exists; another developer install uses this Docker host"
  fi
done

# --- 1. The images -----------------------------------------------------------

echo "== build the manager and the worker images"
docker compose -p "$PROJECT" -f "$HERE/docker-compose.yml" -f "$HERE/docker-compose.dev.yml" \
  build developer developer-worker

# --- 2. The git server -------------------------------------------------------

echo "== start the git server"
if ! docker network inspect "$JOSHUA_NET" >/dev/null 2>&1; then
  docker network create "$JOSHUA_NET" >/dev/null
  CREATED_NET=1
fi
docker build -q -t "$GIT_IMAGE" - >/dev/null <<EOF
FROM $GIT_BASE
# git http-backend is in the git-daemon package of Alpine.
RUN apk add --no-cache git git-daemon openssl
EOF

# A test CA, and a server certificate for GIT_HOST that it signs. The
# manager gets the CA as GIT_CA_BUNDLE, and gives it to each worker.
mkdir -p "$WORK/certs"
docker run --rm -v "$WORK/certs:/certs" "$GIT_IMAGE" sh -euc "
  cd /certs
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=developer e2e CA' \
    -keyout ca.key -out ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj '/CN=$GIT_HOST' \
    -keyout server.key -out server.csr 2>/dev/null
  printf 'subjectAltName=DNS:$GIT_HOST\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > ext.cnf
  openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 1 \
    -extfile ext.cnf -out server.pem 2>/dev/null
  chmod 644 ca.pem server.pem server.key
"
cp "$WORK/certs/ca.pem" "$WORK/git-ca.pem"

docker run -d --name "$GIT_CONTAINER" --network "$JOSHUA_NET" --network-alias "$GIT_HOST" \
  -v "$WORK/certs:/certs:ro" -v "$HERE/scripts/e2e_gitserver.py:/srv/server.py:ro" \
  "$GIT_IMAGE" sh -euc "
    git init -q --bare -b main $BARE
    git -C $BARE config http.receivepack true
    git init -q -b main /tmp/seed
    cd /tmp/seed
    echo '# scratch' > README.md
    git add README.md
    git -c user.name=Seed -c user.email=seed@example.test commit -q -m initial
    git push -q $BARE main
    exec python3 /srv/server.py $GIT_PORT /certs/server.pem /certs/server.key
  " >/dev/null

ready=0
for _ in $(seq 1 30); do
  if docker exec "$GIT_CONTAINER" git -c http.sslCAInfo=/certs/ca.pem \
    ls-remote "https://$REPO.git" main 2>/dev/null | grep -q refs/heads/main; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "the git server does not serve https://$REPO.git"
pass "the git server serves https://$REPO.git over TLS"

# --- 3. The configuration ----------------------------------------------------

ALEX_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
ADDON_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

cat >"$WORK/developer.yaml" <<EOF
network: "off"
max_workers: 1
platforms:
  "$GIT_HOST:$GIT_PORT":
    kind: git
    token_env: E2E_GIT_TOKEN
people:
  alex:
    git:
      name: Alex Example
      email: alex@example.test
    repos:
      - $REPO
EOF

write_env() {
  cat >"$WORK/.env" <<EOF
DEVELOPER_TOKENS=alex=$ALEX_TOKEN
ADDON_TOKEN=$ADDON_TOKEN
CLAUDE_CODE_OAUTH_TOKEN=fake
WORKER_RUNTIME=docker
WORKER_IMAGE=$WORKER_IMAGE
WORKER_FAKE_SESSION=$1
WORKER_START_GRACE_S=120
E2E_GIT_TOKEN=dummy-token
GIT_CA_BUNDLE=/etc/joshua-addon/git-ca.pem
EOF
}

# The test override: the :dev manager, the CA, and the MCP port on loopback,
# so the client on this host can reach it.
cat >"$WORK/docker-compose.e2e.yml" <<EOF
services:
  developer:
    image: $MANAGER_IMAGE
    ports:
      - "127.0.0.1:$MCP_PORT:8000"
    volumes:
      - ./git-ca.pem:/etc/joshua-addon/git-ca.pem:ro
EOF

# --- 4. The manager ----------------------------------------------------------

start_manager() {
  write_env "$1"
  compose up -d --wait --wait-timeout 120 developer docker-socket-proxy >/dev/null ||
    die "the manager did not become healthy (WORKER_FAKE_SESSION=$1)"
  local health
  health="$(curl -fsS "http://127.0.0.1:$MCP_PORT/healthz")" || die "/healthz did not answer"
  if [ "$health" = '{"ok":true}' ]; then
    pass "the manager answers /healthz (WORKER_FAKE_SESSION=$1)"
  else
    fail "/healthz answered $health"
  fi
}

# --- 5 and 6. The tasks ------------------------------------------------------

# dispatch <brief>: send develop and wait for the end. Prints the last
# task_status as JSON.
dispatch() {
  (cd "$ROOT" && uv run --frozen --package joshua-developer python \
    "$HERE/scripts/e2e_client.py" "http://127.0.0.1:$MCP_PORT/mcp" "$ALEX_TOKEN" \
    "$REPO" "$1" "$TASK_TIMEOUT_S") | tail -n 1
}

# check_worker_gone <task id>: the supervisor removes the worker and its
# volumes after the report.
check_worker_gone() {
  local left=""
  for _ in $(seq 1 30); do
    left="$(docker ps -aq --filter "label=task-id=$1")"
    [ -z "$left" ] && break
    sleep 1
  done
  if [ -z "$left" ]; then
    pass "the worker container of task $1 is gone"
  else
    fail "a container with the label task-id=$1 is still there"
  fi
  left="$(docker ps -aq --filter label=joshua-developer-instance=developer)"
  same "$left" "" "no worker container is left"
  left="$(docker volume ls -q --filter label=joshua-developer-instance=developer)"
  same "$left" "" "no worker volume is left"
}

# in_repo <args>: run git in the bare repository of the git server.
in_repo() {
  docker exec "$GIT_CONTAINER" git -C "$BARE" "$@"
}

echo "== task 1: the scripted session"
start_manager 1
if [ "$(docker network inspect developer_workers -f '{{.Internal}}')" = "true" ]; then
  pass "the network developer_workers is internal"
else
  fail "the network developer_workers is not internal"
fi
OUT="$(dispatch "Add hello-from-worker.txt.")"
TASK="$(field "$OUT" task_id)"
[ -n "$TASK" ] || die "develop was not accepted: $OUT"
TASK_IDS+=("$TASK")
STATUS="$(field "$OUT" status)"
BRANCH="$(field "$OUT" branch_name)"
same "$STATUS" success "task 1 ended success"
[ "$STATUS" = success ] || echo "  error: $(field "$OUT" error)"
AUTHORS="$(in_repo log --format='%an <%ae>' "main..$BRANCH" 2>&1 || true)"
same "$AUTHORS" "Alex Example <alex@example.test>" \
  "the branch $BRANCH has one commit by Alex Example <alex@example.test>"
FILE="$(in_repo show "$BRANCH:hello-from-worker.txt" 2>&1 || true)"
same "$FILE" "task $TASK" "the branch holds hello-from-worker.txt with the task id"
check_worker_gone "$TASK"

echo "== task 2: the scripted ask session, with nobody to ask"
start_manager ask
OUT="$(dispatch "Ask a question, then add hello-from-worker.txt.")"
TASK="$(field "$OUT" task_id)"
[ -n "$TASK" ] || die "develop was not accepted: $OUT"
TASK_IDS+=("$TASK")
STATUS="$(field "$OUT" status)"
BRANCH="$(field "$OUT" branch_name)"
QUESTION_GOT="$(field "$OUT" open_question)"
same "$STATUS" blocked "task 2 ended blocked"
[ "$STATUS" = blocked ] || echo "  error: $(field "$OUT" error)"
same "$QUESTION_GOT" "$QUESTION" "task 2 has the open question"
FILE="$(in_repo show "$BRANCH:hello-from-worker.txt" 2>&1 || true)"
case "$FILE" in
  *"Nobody can be reached"*) pass "the worker got the fallback reply at once" ;;
  *) fail "hello-from-worker.txt holds: $FILE" ;;
esac
check_worker_gone "$TASK"
