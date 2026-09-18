# Midrash BnF viewer

A reading surface for 349 Hebrew manuscripts from the Bibliothèque nationale de
France: the Gallica image with its transcription laid over it line by line, a
slider to fade between the two, and a facing column of the text on its own.

Searching used to happen in the browser. The transcriptions are 5.1 million
lines, so the corpus shipped a pre-built index chopped into 14,570 little files,
and a search fetched the ones its words needed and intersected the page lists by
hand. That worked, and it could not be made to do much more: no relevance, no
counts, no highlighting that understood Hebrew, and no way to answer "which
line" rather than "which page".

Now Elasticsearch holds the corpus and answers the questions.

```
  browser ──► nginx ──► Django (/api) ──► Elasticsearch  index: manuscripts
   index.html            no database          5.1M nested line documents
```

Elasticsearch is never reachable from the browser. Anything allowed to run a
search is also allowed to run `_delete_by_query`, and the cluster cannot tell a
reader from a vandal, so Django is the only thing that talks to it and it
exposes five read-only routes.

## Layout

| path | what it is |
| --- | --- |
| `index.html` | the whole viewer: one file, no build step |
| `importer/import_manuscripts.py` | reads `data/`, fills the index (standard library only) |
| `importer/mapping.json` | index settings and mappings, including the Hebrew analyzer |
| `server/` | the Django project that answers `/api/` |
| `deploy/` | nginx and systemd examples |
| `data/` | the corpus on disk: the importer's input, not a web asset |

## The index

One index, `manuscripts`, holding two kinds of document told apart by
`doc_type`:

- **`manuscript`** — one per manuscript. The catalogue record: shelfmark, title,
  date, script, language, genre, subject, scribes. Everything worth searching is
  copied into `meta_all`, which is what a catalogue query actually hits.
- **`page`** — one per page. Its image box and IIIF address, plus `lines` as
  **nested** documents: text, bounding box in image pixels, OCR confidence.

Nested lines are the reason the whole thing is shaped this way. A search comes
back with the line that matched, where it sits on the page and which words to
mark, so a hit puts the reader in front of the words rather than the folio.

Hebrew is folded by a `hebrew_fold` analyzer built from character filters, so no
plugin is needed:

- vowel points and cantillation (`U+0591`–`U+05C7`) are stripped,
- geresh, gershayim and quotation marks become spaces,
- final letters are unified (ך→כ, ם→מ, ן→נ, ף→פ, ץ→צ).

That is the same folding the viewer used to do in JavaScript, moved to where the
text is indexed, so what a reader types matches what a scribe wrote.

> The analyzer cannot be added to an index that already exists. If the index was
> created by hand, the importer will say so and stop rather than fill an index
> that cannot answer; run it with `--recreate`.

## Importing

Needs nothing but Python 3.10 or newer.

```bash
cd importer
python3 import_manuscripts.py ../data --recreate
```

Roughly 113,000 pages and 5.1 million lines. Reading them takes about five
seconds on eight cores; indexing them is Elasticsearch's own pace.

```bash
python3 import_manuscripts.py ../data --dry-run          # read everything, index nothing
python3 import_manuscripts.py ../data --limit 5          # a smoke test
python3 import_manuscripts.py ../data --only 990000532270205171_IE58044885
python3 import_manuscripts.py ../data --workers 4 --bulk-mb 4
```

Connection settings come from flags or the environment: `ES_URL`, `ES_INDEX`,
`ES_USER`, `ES_PASSWORD`, `ES_API_KEY`, `ES_CA_CERT`; `--insecure` skips TLS
verification for a cluster with its own certificate.

Documents are given stable ids (`ms:<id>`, `page:<id>:<file>`), so re-running the
importer updates in place rather than duplicating. It never deletes: a page
removed from `data/` stays in the index until the next `--recreate`.

## Running the API

```bash
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
cp server/.env.example server/.env        # then edit it

cd server
set -a && . ./.env && set +a
../.venv/bin/python manage.py test api    # 17 tests, no cluster needed
../.venv/bin/python manage.py runserver 8001
```

`runserver` also serves `index.html`, so that alone is a working site at
<http://localhost:8001/>. In production nginx serves the file and proxies
`/api/` to gunicorn; see `deploy/`.

There is no database. `DATABASES` is empty on purpose — every record the site
serves lives in Elasticsearch.

### Routes

| route | answers |
| --- | --- |
| `GET /api/info` | corpus counts and the facet lists, asked once at startup |
| `GET /api/manuscripts?q=&script=&language=&genre=&subject=&from=&size=` | the catalogue, with facet counts |
| `GET /api/manuscripts/<ms_id>` | one catalogue record and its pages in reading order |
| `GET /api/manuscripts/<ms_id>/pages/<file>` | one page, in the compact shape the viewer expands |
| `GET /api/search?q=&ms=&from=&size=` | the transcriptions, answering with lines |

A text search is tried twice. First the words as a phrase with the last one
treated as a prefix, so a half-typed word still finds something; if that finds
nothing, the same words in any order within one line, and the answer says
`mode: "words"` so the viewer can tell the reader what it did.

Facet counts come back narrowed by every *other* facet, never by themselves, so
choosing a script still shows which languages remain reachable.

Highlighting travels as `[[text, is_match], ...]` rather than as HTML.
Elasticsearch does not escape what it wraps, and a manuscript is not a trusted
source of markup; the browser builds text nodes from the pieces.

## What changed in the viewer

`index.html` is still one file with no build step. What went:

- the shard fetching, the page table, the client-side intersection of word
  lists — around 120 lines, replaced by one request;
- loading the whole catalogue at startup. The viewer now asks what the corpus
  holds and fetches records as it needs them.

What arrived: facet counts that move with the filters, hits that name their
line, and `mark`ing that comes from the index rather than from a second folding
in the browser.

Picking a corpus folder still works with no server at all, which is what a
presentation wants. That path can browse the catalogue and read pages; it cannot
search the transcriptions, and says so.

## Checking it works

These were not run against a live cluster — verify in this order:

```bash
# 1. the index exists and carries the analyzer
curl -s localhost:9200/manuscripts/_settings | grep -o hebrew_fold

# 2. folding works: points and final letters must not matter
curl -s localhost:9200/manuscripts/_analyze -H 'Content-Type: application/json' \
  -d '{"analyzer":"hebrew_fold","text":"בָּרוּךְ אַתָּה"}'
#    expect tokens ברוכ and אתה

# 3. the corpus is all there
curl -s 'localhost:9200/manuscripts/_count?q=doc_type:manuscript'   # 349
curl -s 'localhost:9200/manuscripts/_count?q=doc_type:page'         # 113228

# 4. the API answers
curl -s localhost:8001/api/info
curl -s 'localhost:8001/api/search?q=ברוך אתה' | head -c 600
```

Then in the browser: type in the catalogue box and watch the facet counts move;
switch to text search and click a hit — it should open that manuscript at that
page with the line outlined and the words marked.

## Loose end

`data/search/` — the old browser-side index, 177 MB in 14,572 files — is no
longer read by anything. It is still in the repository because deleting that
many tracked files is your call, not mine.
