# Adding an addon

This page is the contract for a new addon. Follow the numbered steps in
order. `addons/hello/` is a worked example of every step.

1. **Lay out the directory.** Make `addons/<name>/` with:

   ```
   addons/<name>/pyproject.toml       a workspace member, name joshua-<name>
   addons/<name>/Dockerfile           the image; its presence puts the addon in CI
   addons/<name>/docker-compose.yml   how a person runs this addon alone
   addons/<name>/docker-compose.dev.yml   a build of the checkout, for a developer
   addons/<name>/.env.example         the settings and secrets a person copies to .env
   addons/<name>/values.yaml          the Helm values for this addon
   addons/<name>/joshua_<name>/       the package
   addons/<name>/tests/               the tests
   addons/<name>/README.md            what it does, its settings, its joshua.yaml snippet
   ```

   The Dockerfile build context is the repository root, because the addon
   depends on the workspace lockfile. Copy the `Dockerfile` and both compose
   files from `addons/hello/` and change only the addon name in them.

2. **Know what the Dockerfile triggers.** CI finds an addon by the presence
   of `addons/<name>/Dockerfile`, so no other file registers it. Once the
   Dockerfile lands:

   - The CI test job runs `pytest` for the addon, with `--cov-fail-under=80`.
   - The CI build job builds the image of the addon.
   - The release workflow builds and publishes the image of the addon, for
     both `linux/amd64` and `linux/arm64`, on every `v*` tag.

   No workflow file needs an edit. `scripts/list_addons.py` finds the addon
   from the tree at each run.

3. **Meet the server contract.** The addon serves MCP at `POST /mcp`, over
   streamable HTTP, on port 8000. It answers `GET /healthz` with
   `{"ok": true}`, open, with no token check. When the environment carries
   `ADDON_TOKEN`, every other route needs
   `Authorization: Bearer <ADDON_TOKEN>`, checked with `hmac.compare_digest`.
   A missing or wrong token gets 401. Logs are one JSON line per event.
   Never log a token, and never log a message body at INFO.

4. **Avoid these two mistakes.** Both cost the `hello` addon real time:

   - **The MCP server class moved.** The `mcp` package (2.1.x) ships
     `MCPServer` in `mcp.server.mcpserver`, in place of the classic `FastMCP`
     import many examples still show. The decorator API is the same:
     `mcp = MCPServer(name="...")`, then `@mcp.tool()`.
   - **Bind the streamable-HTTP app to `0.0.0.0`.** Call
     `mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")`.
     The loopback default turns on the DNS-rebinding guard of the SDK, and
     the guard answers 421 to a request that arrives by any hostname but
     `localhost`. A caller always reaches the addon by its compose or
     cluster hostname, so the default 421s every real request.

5. **Write the tests.** Coverage floor is 80 percent for the addon. Prove
   every claim the README makes, including the negative case: a request
   with no token, or the wrong token, gets 401. `addons/hello/tests/` has a
   worked case for each rule above.

6. **Add a chart `ci` values file only when you need one.** CI already
   renders the chart with `charts/joshua-addon/ci/hello-values.yaml` (the
   minimal case) and `charts/joshua-addon/ci/overrides-values.yaml` (every
   override path: ingress, persistence, an extra container, an existing
   Secret, and more). Add a new file under `charts/joshua-addon/ci/` only
   when the `values.yaml` of your addon uses a chart feature neither file
   exercises yet.

7. **Match the version.** One version string covers the chart, the chart
   `appVersion`, and every addon image tag, current release `0.0.1`. Set the
   default in your `docker-compose.yml` to match:
   `${JOSHUA_ADDONS_VERSION:-0.0.1}`. `scripts/check_chart_version.py`, run
   by `make lint`, fails the build when a `docker-compose.yml` disagrees
   with `charts/joshua-addon/Chart.yaml`.

8. **Document the addon.** Write `addons/<name>/README.md`: what the addon
   does, its tools, its settings table, and the `mcp:` snippet for
   `joshua.yaml`. Read `../joshua-ai/docs/config.md` for the full `mcp:`
   shape.

Run `make lint` and `make test` before you open a pull request.
`CONTRIBUTING.md` has the fork, branch, and review steps.
