# vinyl

The `vinyl` addon is a shelf browser for a record collection kept on Discogs.
A nightly sync reads the collection, caches the album art, works out the shelf
section of each record, and writes a static bundle. A web page at `/` shows
the covers in a carousel, with a genre filter, a search box, a sort order,
and a detail view that names the shelf section. Guests browse from a phone
with no login.

The addon never writes to Discogs. The collection stays on Discogs, and the
data directory is fully reconstructible, so you can delete it at any time.

## The interface

Open `http://<host>:8000/` on a device that can reach the container.

- **Shelf.** The covers stand in a 3D row. The one in front is the record in
  hand, with its vinyl out of the sleeve, and the caption under the shelf
  names it. Swipe, drag, use the arrow keys, or tap a cover to bring it
  forward.
- **Genre.** One tap on a genre is a complete filter. Inside a genre, the
  styles present in it are optional refinements. A record with no styles
  still shows under its genre.
- **Search.** Matches the artist, the title, and the label as you type. A
  missing leading "The" still matches.
- **Sort.** Shelf order (section, then artist), genre runs, release year, or
  recently added. The caption shows the section, the genre, the decade, or
  the date added, to match.
- **Detail.** Tap the record in hand, or "Flip it over". The detail view
  shows the shelf section, the label and catalog number, the format, the
  country, the genres and styles, the tracklist, and the lowest listed price
  with the date it was checked. A missing value is absent, never zero.

## Shelf sections

The section letter is the first letter of the artist sort-name that
MusicBrainz publishes: `Dylan, Bob` files under D, and `Beatles, The` under
B. The addon writes no name parser of its own. A various-artists release
(credited to Various) and a soundtrack (a `Soundtrack` or `Score` style) go
to one section after Z. A sort-name that starts with a digit or a symbol
files under `#`, before A.

A compilation by one artist files under that artist. A greatest-hits record
belongs beside the rest of that artist, so `Best Of The Beach Boys` files
under B. It still counts as a compilation for search and display; only the
shelf position changes.

A classical release files under its performer. Discogs credits the composer
first (`Bach / Glenn Gould`), so a release in the Classical genre with more
than one credit files under the last one: `Gould, Glenn`, section G.

The sync asks MusicBrainz once for each artist and caches the answer. An
artist MusicBrainz cannot match keeps the Discogs name as its sort-name for
that run, and the sync tries again the next night. Set an override for any
artist that stays wrong (below).

## The bundle

The sync writes `bundle/index.json`, one file for the whole collection, and
`bundle/detail/<id>.json` for each record, under the data directory. The
page reads the index once and does every filter, search, and sort in the
browser. Art is cached under `art/` at two sizes, and the page never loads
an image from Discogs.

## The tools

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, and
answers `GET /healthz` with `{"ok": true}`. Six tools let the agent answer a
question about the collection. Each one reads the bundle, so an answer needs
no network and no database query.

| Tool | What it answers |
|---|---|
| `vinyl_search(query, genre, decade, year, section, limit)` | "Do we have Rumours?" A query matches the title, the artist, or the label, and tolerates a missing "The". A filter with no query lists a part of the collection. |
| `vinyl_details(record_id)` | "What is that pressing?" The shelf section, the label and catalog number, the format, the country, the tracklist, and the last price with the date it was checked. |
| `vinyl_stats()` | "How many records do we have?" The count, the genres, the decades, the sections, and the year of the oldest and the newest pressing. |
| `vinyl_recent(limit)` | "What is new?" The records added most recently, newest first. |
| `vinyl_pick(genre, decade, section, exclude_ids)` | "What do we put on?" One record the house owns, with a reason and the shelf section. |
| `vinyl_status()` | The record count, the time of the last sync, and its result. `GET /api/status` is the same report over plain HTTP. |

Every tool is read-only. No tool writes to Discogs, and no tool changes the
collection. Before the first sync, each one reports that the collection is
not synced yet.

To give the tools to Joshua, put the addon in the `mcp` section of
`joshua.yaml` as a `type: http` upstream:

```yaml
mcp:
  vinyl:
    type: http
    url: http://vinyl:8000/mcp
    allow: all
    headers:
      Authorization: "Bearer ${VINYL_ADDON_TOKEN:-}"
```

`allow: all` gives the tools to every person, and to a group turn, which
arrives with no person. The collection is shared and no tool changes it, so
this is the same posture as the browse interface.

## Run a sync

The container runs a sync every day at `VINYL_SYNC_TIME`. To run one now:

```
docker compose -f addons/vinyl/docker-compose.yml exec vinyl python -m joshua_vinyl sync
```

The sync makes one Discogs request for each page of the collection, two
image downloads for each new record, one MusicBrainz request for each new
artist, and one Discogs request for each record to read its tracklist, its
country, and its lowest listed price. It waits between requests to stay
inside both rate limits, so a sync of a few hundred records takes some
minutes. A record whose release request fails keeps what it had.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ADDON_TOKEN` | not set | When set, every request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz`, `/api/status`, the bundle, the art, and the page at `/` stay open. When `ADDON_TOKEN` is not set, the addon checks no token; the docker network is the boundary. |
| `DISCOGS_TOKEN` | not set | A Discogs personal access token. The sync needs it. Without it, the nightly sync stays off and the addon serves the last bundle. |
| `DISCOGS_USERNAME` | not set | The Discogs account whose collection to read. When it is not set, the sync asks Discogs which account the token belongs to. |
| `VINYL_SYNC_TIME` | `03:00` | The local time of the nightly sync, as `HH:MM`. An empty value turns the schedule off. |
| `VINYL_CURRENCY` | `USD` | The ISO currency code the market price is quoted in. |
| `VINYL_DATA_DIR` | `/data` | Where the SQLite file, the art cache, the bundle, and `config.json` live. |
| `VINYL_CONFIG` | `<VINYL_DATA_DIR>/config.json` | The shelf rules file (below). A missing file means the defaults. |
| `VINYL_STATIC_DIR` | the package's `static/` directory | Where the page is served from. |
| `VINYL_USER_AGENT` | `joshua-vinyl/0.1 (+https://github.com/jakehigg/joshua-addons)` | The `User-Agent` sent to Discogs and MusicBrainz. Both refuse a request without a descriptive one. |
| `TZ` | `UTC` | The timezone `VINYL_SYNC_TIME` is read in. A container has no timezone of its own, so without this a sync time of `03:00` runs at 03:00 UTC. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## The shelf rules file

The facet list and the special sections are household taste, so they live in
a JSON file at `VINYL_CONFIG`, not in code. This is the default, written out:

```json
{
  "facets": {
    "promote": {"Country": "Folk, World, & Country"},
    "relabel": {"Folk, World, & Country": "Folk & World"}
  },
  "sections": [
    {"name": "Compilations & Soundtracks", "traits": ["various", "soundtrack"]}
  ],
  "overrides": {
    "artist_sort": {},
    "primary_facet": {},
    "section": {}
  }
}
```

- `facets.promote` lifts a Discogs style to the top level. The value is the
  genre the style leaves: a record with that style shows under the style,
  not under that genre.
- `facets.relabel` renames a genre for display.
- `sections` lists the dividers after Z, in shelf order. Each matches one or
  more traits: `various`, `soundtrack`, or `compilation`. Split them into two
  sections, or add one, without a code change. `compilation` covers a
  greatest-hits record by one artist as well, so a section that lists it
  takes those off the artist shelf.
- `overrides.artist_sort` maps an artist name to the sort-name to use.
  `overrides.section` and `overrides.primary_facet` map a Discogs release id,
  as a string, to a fixed value.

Run a sync after you change the file.


### How each deployment path supplies it

With compose, put the file beside `docker-compose.yml` and uncomment the
bind mount and `VINYL_CONFIG` there. A read-only mount keeps the file out of
the data volume, so it survives a wipe of the cache.

With the chart, `addons/vinyl/values.yaml` writes the file to a ConfigMap and
mounts it read-only, and sets `VINYL_CONFIG` to the mount path. Edit
`configFile.content` to keep your own rules, and the file ships with the
release rather than being copied into a volume by hand.

Set `configFile.enabled` to `false`, or leave `VINYL_CONFIG` unset, and the
addon reads `config.json` from the data directory instead. A missing file
means the defaults.

## Add it to joshua.yaml

Add this block to the `mcp:` section of your `joshua.yaml`:

```yaml
mcp:
  vinyl:
    type: http
    url: http://vinyl:8000/mcp
    allow: all
```

Read `../../docs/config.md` in `joshua-ai` for the full `mcp:` shape, including
`allow`, `tools`, and per-person identities. See `../../docs/install.md` for
the steps that apply this change, and for how to add a token.
