#!/usr/bin/env bash
# Run the CI lint and test jobs locally with the same commands as
# .github/workflows/ci.yml. The image build is not run here.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> sync"
uv sync --frozen --all-packages

echo "==> lint: uv lock --check"
uv lock --check
echo "==> lint: ruff check"
uv run ruff check .
echo "==> lint: ruff format --check"
uv run ruff format --check .
echo "==> lint: chart version"
uv run --with pyyaml python scripts/check_chart_version.py
echo "==> lint: test policy"
uv run python scripts/check_test_policy.py
if [ -d tests ]; then
  echo "==> lint: root tests"
  # check_chart_version.py needs pyyaml, and export_legacy_pantry.py needs
  # asyncpg; neither is a root project dependency.
  uv run --with pyyaml --with asyncpg python -m pytest tests
fi

# The addon list comes from list_addons.py, the same script the CI discover
# job runs, so a new addon needs no edit here.
addons="$(python3 scripts/list_addons.py | python3 -c 'import json, sys; print("\n".join(json.load(sys.stdin)))')"

for member in $addons; do
  echo "==> test: $member"
  uv run --package "joshua-$member" pytest "addons/$member/tests" \
    --cov="joshua_$member" --cov-report=term --cov-fail-under=80
done

echo "==> ci-local: all stages passed"
