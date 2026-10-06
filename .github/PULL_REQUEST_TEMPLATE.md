Closes #

## What changed

<!-- What the code does now. Short sentences. Active voice. -->

## Why

<!-- What was wrong before, for the person who runs the addon. -->

## Checks

- [ ] `make lint` and `make test` pass.
- [ ] `uv run python scripts/check_test_policy.py` passes.
- [ ] The addon keeps the contract: port 8000, MCP at `/mcp`, `/healthz` is
      `{"ok": true}`, a wrong `ADDON_TOKEN` gets 401, and `serverInfo.version`
      is `ADDON_VERSION`.
- [ ] A new addon has an entry in `catalog.yaml`, a `README.md` with its
      `joshua.yaml` snippet, and tests that try the denied case.
- [ ] No addon is versioned on its own. A version change touches every file
      `scripts/check_chart_version.py` names, and nothing else changes a
      version.
- [ ] The docs that this change makes wrong are changed here, and
      `docs/CHANGELOG.md` has a line under `## Unreleased` when a person who
      runs an addon can see the change.
- [ ] This text, the commit messages, and the docs are in Simplified Technical
      English.
- [ ] The commits are from the repository identity in `CLAUDE.md`, "Git
      identity".
- [ ] No hostname, IP address, person, or secret from my own installation is in
      the diff.
- [ ] When a coding agent wrote the change: the commits carry the
      `Co-Authored-By:` trailer, and the agent pushed no tag.
