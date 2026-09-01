# hello

The `hello` addon is a placeholder. It proves the addon pipeline: one
container, one MCP tool, over HTTP. A later addon replaces it. Use this addon
as the pattern for a new addon.

## The tool

`hello(name)` returns a greeting for `name`.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, and answers
`GET /healthz` with `{"ok": true}`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ADDON_TOKEN` | not set | When set, every request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz` stays open. When `ADDON_TOKEN` is not set, the addon checks no token; the docker network is the boundary. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## Add it to joshua.yaml

Add this block to the `mcp:` section of your `joshua.yaml`, and set
`ADDON_TOKEN` for the `hello` container and the matching header for the
gateway:

```yaml
mcp:
  hello:
    type: http
    url: http://hello:8000/mcp
    allow: all
    headers:
      Authorization: "Bearer ${HELLO_ADDON_TOKEN:-}"
```

Read `../../docs/config.md` in `joshua-ai` for the full `mcp:` shape, including
`allow`, `tools`, and per-person identities.
