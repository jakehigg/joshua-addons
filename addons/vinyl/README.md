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
answers `GET /healthz` with `{"ok": true}`. Fourteen tools let the agent answer a
question about the collection, and add a record to it.

Eight read the shelf. Each one reads the static bundle, so an answer needs no
network and no database query.

| Tool | What it answers |
|---|---|
| `vinyl_search(query, genre, decade, year, section, limit)` | "Do we have Rumours?" A query matches the title, the artist, or the label, and tolerates a missing "The". A filter with no query lists a part of the collection. |
| `vinyl_owned(artist, title, release_id, master_id)` | "Do we own this already?" The question asked in a shop. A match on the title alone is a maybe, never a yes. |
| `vinyl_details(record_id)` | "What is that pressing?" The shelf section, the label and catalog number, the format, the country, the tracklist, and the last price with the date it was checked. |
| `vinyl_stats()` | "How many records do we have?" The count, the genres, the decades, the sections, and the year of the oldest and the newest pressing. |
| `vinyl_recent(limit)` | "What is new?" The records added most recently, newest first. |
| `vinyl_pick(genre, decade, section, exclude_ids, avoid_days)` | "What do we put on?" One record the house owns, with a reason and the shelf section. A record suggested in the last two weeks waits its turn, and a record that is out with somebody is never suggested. |
| `vinyl_lent_out()` | "Who has what?" Every record that is off the shelf, and with whom. |
| `vinyl_status()` | The record count, the time of the last sync, and its result. `GET /api/status` is the same report over plain HTTP. |

Two mark a record as out with somebody, and write only to the local
database:

| Tool | What it does |
|---|---|
| `vinyl_lend(release_id, to, note)` | Marks a record as out with a person. The record stays in the collection. |
| `vinyl_return(release_id)` | Puts a record that was out back on the shelf. |

Four reach Discogs, so they need `DISCOGS_TOKEN` and `DISCOGS_USERNAME`.
Without them the addon still browses, answers, and lends; these four say
what is missing.

| Tool | What it does |
|---|---|
| `vinyl_lookup(artist, title, catalog_no, barcode, label, matrix, year, country, tracks)` | Finds at most three candidate releases from what a person read on the record, each with the full format line, the release notes, the country and the year. |
| `vinyl_label_images(release_id, limit)` | Answers with the disc labels Discogs holds for one release, as pictures, to compare against the photograph. |
| `vinyl_add(release_id, note, pressing_confirmed, confirm, allow_duplicate)` | Plans an add, and writes it on a second call with `confirm`. |
| `vinyl_remove(release_id, instance_id, confirm)` | Plans a removal, and takes the record out of the collection on a second call with `confirm`. |

`vinyl_add` and `vinyl_remove` are the only tools that write to Discogs, and
neither writes without `confirm`. `vinyl_lend` and `vinyl_return` write only
to the local database, so a loan needs no token.

## Add one record

The path for a record bought at a yard sale, or found in a shop. It is not
the path for a whole collection; that is a batch job and it belongs in a
program of its own.

1. **Ask for a photograph of the disc label.** The label carries the catalog
   number, the label name, the pressing details and the matrix. Cover art
   identifies nothing: a bowler-hat sleeve once read as the wrong album, and
   the label photograph settled it. A back cover is the second choice, and it
   is enough to name the album.
2. **Read the photograph and record what is printed**, not what you know.
   The catalog number goes in as printed, including a leading X. Write the
   track titles as well.
3. `vinyl_owned` first when the record is not bought yet. It answers from the
   shelf with no network.
4. `vinyl_lookup` with every field that was read.
5. `vinyl_label_images` on the candidates, and compare the pictures against
   the photograph. Layout first, then text, then colour. This is the check
   that catches most wrong picks, and no automatic test replaces it.
6. `vinyl_add` with no `confirm`, and show the plan to the person.
7. `vinyl_add` again with `confirm` once they agree.

What the answers are for, and what each one hides:

- **A catalog number or a barcode that finds nothing is a misread**, not a
  rare pressing. Read the photograph again.
- **A barcode that fails its own check digit is dropped** with a reason, and
  the search falls back to the catalog number. A US sleeve of the 1980s often
  prints eleven digits and no check digit; that one cannot be checked and is
  searched as it is.
- **The format line and the release notes decide more records than the
  match does.** Picture Disc, Test Pressing and Quadraphonic are usually the
  wrong pressing, and a promotional copy hides in the notes while the format
  line stays ordinary. Both are reported for every candidate.
- **A tracklist never names a release.** It is the one thing a wrong match
  cannot fake, so a track that is not on the candidate is a reason to reject
  it.
- **A pressing that is not confirmed says so in the collection note.**
  `vinyl_add` writes `pressing-unconfirmed.` at the front unless
  `pressing_confirmed` is set, which is how a later pass finds the records
  that still need one. Set it only for a barcode, a matrix, or a label
  picture that matches.
- **A note is cut to 255 characters.** Discogs refuses a longer one, and it
  refuses it after the record is already in the collection. Put the
  important half first.
- **A record already in the collection is refused** unless
  `allow_duplicate` is set. Two copies of one album can be two pressings, so
  the guard asks rather than decides.

After a write the release goes into the local database and the bundle is
written again, so the record is on the shelf page at once. The answer names
the section to file it under.

## Take one record off the shelf

A record leaves the shelf in two different ways, and the difference is the
point.

**A loan is not a sale.** `vinyl_lend` marks the record as out with a person
and changes nothing on Discogs. The record keeps its note, its shelf section
and the date it was first added, so nobody identifies the pressing again when
it comes back. Until then `vinyl_pick` never suggests it, the caption under
the shelf says who has it, and the gatefold says since when. `vinyl_return`
puts it back. `vinyl_lent_out` says who has what.

**A removal is for good.** `vinyl_remove` takes the record out of the Discogs
collection, which is where the collection lives, so the record is gone from
the shelf and from every future sync. Use it for a record that was sold,
given away or lost, and for a record that was matched to the wrong pressing.
It plans first and writes only on a second call with `confirm`.

Two rules the tool applies for you:

- **Two copies of one release need a choice.** The collection can hold the
  same release twice, and the copies differ only in the note and the date each
  was added. The plan lists both and asks which `instance_id` left the house.
- **A release with a copy left stays on the shelf.** Removing one of two
  copies takes that copy out of Discogs and leaves the record in the
  collection, because the house still owns one.

The page is written again after either one, so the shelf is right at once.

## Suggestions do not repeat

`vinyl_pick` writes down what it suggested. A record suggested inside the
last two weeks is left out while anything else fits, so a person who asks
twice in a week gets two different records. When every record that fits was
suggested recently, the answer says so and repeats one rather than refusing.
`avoid_days` changes the two weeks for one call, and `0` turns the memory
off.

Nobody has to report a play. The addon remembers only what it said itself.

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
arrives with no person. The collection is shared, and the one tool that
writes asks first, so this is the same posture as the browse interface.

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
| `DISCOGS_TOKEN` | not set | A Discogs personal access token. The sync, the lookup and the add need it. Without it the nightly sync stays off, the addon serves the last bundle, and the three Discogs tools say what is missing. A token that can add a record must be a personal token of the account that owns the collection. |
| `DISCOGS_USERNAME` | not set | The Discogs account whose collection to read and add to. When it is not set, the addon asks Discogs which account the token belongs to. |
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
