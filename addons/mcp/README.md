# joshua-mcp

The `mcp` addon, joshua-mcp, gives a caller outside the agent access to
Joshua's wiki, journal, and knowledge folder. Claude Code on a laptop, Claude
Desktop, or a script can use it. It serves one MCP server over HTTP for three
sources, all in the joshua-ai data volume:

| Source | Folder | What it holds |
|---|---|---|
| `wiki` | `/data/wiki/**.md` | The wiki pages, the profiles in `people/`, and the skills in `skills/` |
| `journal` | `/data/wiki/journal/YYYY/MM/DD/*.md` | Joshua's journal: one day page and the entries for each day |
| `knowledge` | `/data/wiki/knowledge/` | The knowledge folder. It can be missing. |

The addon is not a gateway consumer, and it does not start a Joshua turn. To
talk to Joshua, use the terminal channel.

## Tools

| Tool | What it does |
|---|---|
| `search(query, source?, limit?)` | Finds pages by words (BM25). Each result has the path, the title, a snippet, the source, and the date of a journal page. |
| `read_page(path)` | Reads one page: the frontmatter and the body. |
| `list(path?, source?)` | Lists one folder: its folders, its pages, and its other files. |
| `write_page(path, markdown, message?)` | Creates or replaces one page, and commits it. |
| `write_journal_entry(slug, markdown, people?, date?)` | Writes one entry at `journal/YYYY/MM/DD/<slug>.md`, and commits it. |
| `read_journal(date?, days?, people?)` | Reads the day pages and the entries for a range of up to 31 days. |
| `knowledge_search(query, limit?)` | `search` in the knowledge folder only. |
| `knowledge_read(path)` | `read_page` in the knowledge folder only. |

A path is relative to the wiki, for example `people/alex.md`. The search is
lexical: it finds words, not meanings. Joshua's own vector index stays in
core, and core indexes a page that this addon writes within 60 seconds.

When there is no knowledge folder, the knowledge tools, and `search` or `list`
with `source: knowledge`, answer "no knowledge folder".

### Rules for a write

- `write_page` refuses a path in `journal/`, `people/`, `joshua-docs/`, or
  `attachments/`. It refuses a path in a folder whose name starts with a dot
  (`.git`, `.trash`), a path out of the wiki, and a file name that does not
  end in `.md`. The journal has its own tool. The profiles and the
  documentation are Joshua's.
- `write_journal_entry` never writes the day page `YYYY-MM-DD.md`. The
  nightly run writes it. The slug is lowercase letters, digits, and hyphens.
  An entry with the same day and slug is replaced.
- Each page and entry that the addon writes has `source: joshua-mcp` and
  `author: <caller>` in its frontmatter. Thus a reader can see which pages a
  tool wrote and which pages Joshua wrote.
- A page or an entry is 256 KB or less.
- When the wiki is a git repository, the addon commits the file it wrote,
  and only that file, with the message `joshua-mcp: <message>`. The nightly
  run commits all other changes.
- A refused write changes nothing on the disk.

## Callers

Each caller has its own bearer token. The addon uses the token to find the
name of the caller, and records that name as the `author` of a write.

- `ADDON_TOKEN` is the token for the caller `addon`.
- `MCP_TOKENS` gives one token to each caller:
  `laptop=<token>,laptop-ci=<token>`.
- `MCP_READONLY` names the callers that can only read. A write tool gives
  "403 forbidden" to such a caller.

When no token is set, the addon asks for no token. The network is then the
only boundary, and the caller is `anonymous`. Always set a token when a caller
outside the cluster can reach the addon.

A missing or wrong token gets HTTP 401. `GET /healthz` needs no token and
answers only `{"ok": true}`.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `ADDON_TOKEN` | empty | The bearer token for the caller `addon`. |
| `MCP_TOKENS` | empty | One bearer token for each caller, as `name=token` pairs, separated by commas. A name is lowercase letters, digits, `-`, and `_`. |
| `MCP_READONLY` | empty | The callers that can only read, separated by commas. `anonymous` is the caller when no token is set. |
| `MCP_DATA_DIR` | `/data` | The joshua-ai data volume. The wiki is `<MCP_DATA_DIR>/wiki`. |
| `MCP_TIMEZONE` | `UTC` | The timezone that sets "today" for the journal. Use the timezone in `joshua.yaml`. |
| `LOG_LEVEL` | `INFO` | The log level. |

A bad value stops the addon at start, with a message that names the variable
and never the token.

## Run it

The addon runs as uid 1000, the same user as joshua-ai core and gateway. The
image holds `git`, because each write makes a commit.

### Kubernetes

Install `charts/joshua-addon` with `values.yaml` from this folder. The chart
mounts the joshua-ai data claim (`persistence.existingClaim: joshua-ai-data`)
at `/data`, and makes no claim of its own. The claim must be ReadWriteMany,
because core and gateway mount it too. Put `ADDON_TOKEN` and `MCP_TOKENS`
in a Secret, and set `existingSecret` to its name. To reach the addon from
a laptop, turn on the ingress with TLS.

### Docker Compose

```
make up ADDON=mcp
```

`docker-compose.yml` mounts the joshua-ai data volume (`JOSHUA_DATA_VOLUME`,
default `joshua-ai_data`) and publishes port 8000 (`MCP_PORT`). Set a token
in `.env` before you start it.

## Connect Claude Code

```
claude mcp add --transport http joshua https://joshua-mcp.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

## Development

```
uv run --package joshua-mcp pytest addons/mcp/tests
uv run --package joshua-mcp pytest addons/mcp/tests -m integration
```

The unit tests use a wiki in a temporary folder. The integration tests also
need the `git` binary.
