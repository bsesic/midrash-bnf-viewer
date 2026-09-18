"""The search API the viewer talks to.

Every route answers JSON and reads from one Elasticsearch index. There is no
database and no write path: the importer fills the index, this only asks it
questions.
"""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse

from . import es

FACET_FIELDS = ("script", "language", "genre", "subject")

# What a catalogue row needs to be listed. `_f`-style blobs stay in the index.
MANUSCRIPT_SOURCE = [
    "ms_id", "alma", "ark", "shelfmark", "title", "date", "year",
    "language", "script", "genre", "subject", "persons", "extent",
    "n_pages", "n_lines",
]

HIT_SOURCE = ["ms_id", "shelfmark", "title", "year", "folio", "page_no", "file"]

MAX_PAGE_LIST = 5000
MAX_RESULTS = 500


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def selected_facets(request) -> dict[str, list[str]]:
    """Read facet selections off the query string.

    The viewer writes them into the address bar joined by a pipe, so a link
    carries the reader's filters; both spellings are accepted.
    """

    chosen: dict[str, list[str]] = {}

    for field in FACET_FIELDS:
        values: list[str] = []

        for raw in request.GET.getlist(field):
            values.extend(part for part in raw.split("|") if part)

        if values:
            chosen[field] = values

    return chosen


def facet_aggregations(chosen: dict[str, list[str]]) -> dict[str, Any]:
    """One aggregation per facet, filtered by the OTHER facets only.

    A facet that filtered itself would always come back with the single value
    already chosen, and the reader could never see what else is there.
    """

    aggregations: dict[str, Any] = {}

    for field in FACET_FIELDS:
        others = [
            {"terms": {other: values}}
            for other, values in chosen.items()
            if other != field
        ]

        aggregations[field] = {
            "filter": {"bool": {"filter": others}} if others else {"match_all": {}},
            "aggs": {
                "values": {"terms": {"field": field, "size": 40}},
            },
        }

    return aggregations


def read_facets(response: dict[str, Any]) -> dict[str, list]:
    out: dict[str, list] = {}

    for field in FACET_FIELDS:
        buckets = (
            response.get("aggregations", {})
            .get(field, {})
            .get("values", {})
            .get("buckets", [])
        )

        out[field] = [[b["key"], b["doc_count"]] for b in buckets]

    return out


def total_of(response: dict[str, Any]) -> int:
    total = response.get("hits", {}).get("total", 0)
    return total.get("value", 0) if isinstance(total, dict) else total


def failed(error: es.SearchError):
    return JsonResponse({"error": str(error)}, status=503)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
def info(request):
    """What the corpus holds. The viewer asks for this once, at startup."""

    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {"term": {"doc_type": "manuscript"}},
        "aggs": dict(
            facet_aggregations({}),
            pages={"sum": {"field": "n_pages"}},
            lines={"sum": {"field": "n_lines"}},
        ),
    }

    try:
        response = es.search(body)
        version = es.cluster_version()
    except es.SearchError as error:
        return failed(error)

    aggregations = response.get("aggregations", {})

    return JsonResponse({
        "index": settings.ES_INDEX,
        "elasticsearch": version,
        "manuscripts": total_of(response),
        "pages": int(aggregations.get("pages", {}).get("value") or 0),
        "lines": int(aggregations.get("lines", {}).get("value") or 0),
        "facets": read_facets(response),
    })


def manuscripts(request):
    """The catalogue: metadata search across the manuscripts, with facets."""

    query = request.GET.get("q", "").strip()
    chosen = selected_facets(request)
    size = clamp(request.GET.get("size"), 1, MAX_RESULTS, 60)
    start = clamp(request.GET.get("from"), 0, 9000, 0)

    must: list[dict[str, Any]] = [{"term": {"doc_type": "manuscript"}}]

    if query:
        # The reader is typing: every word must match, and the last one counts
        # as a prefix, so results narrow with each keystroke instead of
        # vanishing until a word happens to be finished.
        must.append({
            "match_bool_prefix": {
                "meta_all": {"query": query, "operator": "and"},
            },
        })

    body: dict[str, Any] = {
        "size": size,
        "from": start,
        "track_total_hits": True,
        "_source": MANUSCRIPT_SOURCE,
        "query": {"bool": {"must": must}},
        "aggs": facet_aggregations(chosen),
        "sort": (
            [{"_score": "desc"}, {"ms_order": "asc"}] if query
            else [{"ms_order": "asc"}]
        ),
    }

    if chosen:
        # A post filter narrows the hits without narrowing the aggregations,
        # which is what lets the facet counts stay useful while filtering.
        body["post_filter"] = {
            "bool": {"filter": [
                {"terms": {field: values}} for field, values in chosen.items()
            ]},
        }

    try:
        response = es.search(body)
    except es.SearchError as error:
        return failed(error)

    return JsonResponse({
        "total": total_of(response),
        "from": start,
        "facets": read_facets(response),
        "results": [hit["_source"] for hit in response["hits"]["hits"]],
    })


def manuscript(request, ms_id: str):
    """One manuscript, with its pages in reading order."""

    record = es.get_document("ms:" + ms_id)

    if record is None:
        raise Http404("no such manuscript")

    body = {
        "size": MAX_PAGE_LIST,
        "_source": ["file", "folio", "page_no"],
        "query": {"bool": {"filter": [
            {"term": {"doc_type": "page"}},
            {"term": {"ms_id": ms_id}},
        ]}},
        "sort": [{"page_no": "asc"}, {"file": "asc"}],
    }

    try:
        response = es.search(body)
    except es.SearchError as error:
        return failed(error)

    pages = [hit["_source"] for hit in response["hits"]["hits"]]

    return JsonResponse({
        "manuscript": {k: v for k, v in record.items() if k in MANUSCRIPT_SOURCE},
        "pages": pages,
        "truncated": total_of(response) > len(pages),
    })


def page(request, ms_id: str, name: str):
    """One page, in the compact shape the viewer already knows how to expand.

    Keeping the wire format is not nostalgia: the overlay renderer reads image
    pixels, and reshaping them here would mean touching the one part of the
    viewer that is known to be correct.
    """

    stem = name[:-5] if name.lower().endswith(".json") else name
    record = es.get_document(f"page:{ms_id}:{stem}")

    if record is None:
        raise Http404("no such page")

    return JsonResponse({
        "i": stem,
        "p": record.get("page_no"),
        "f": record.get("folio"),
        "u": record.get("iiif"),
        "c": record.get("image"),
        "w": record.get("width"),
        "h": record.get("height"),
        "l": [
            (line.get("box") or [0, 0, 0, 0])[:4] + [
                1 if line.get("main", True) else 0,
                line.get("conf"),
                line.get("text", ""),
            ]
            for line in record.get("lines", [])
        ],
    })


def _nested_lines(query: dict[str, Any]) -> dict[str, Any]:
    return {
        "nested": {
            "path": "lines",
            "score_mode": "max",
            "query": query,
            "inner_hits": {
                "size": 3,
                "_source": ["n", "text", "box", "conf", "main"],
                "highlight": {
                    "pre_tags": [es.HL_OPEN],
                    "post_tags": [es.HL_CLOSE],
                    "fields": {"lines.text": {"number_of_fragments": 0}},
                },
            },
        },
    }


def search_text(request):
    """Full text search across the transcriptions, answering with lines.

    Tried in two passes. First the words as a phrase, the last one as a prefix,
    so a half-typed word still finds something. If that finds nothing, the same
    words anywhere in one line, which catches a phrase remembered in the wrong
    order or read across a correction.
    """

    query = request.GET.get("q", "").strip()
    size = clamp(request.GET.get("size"), 1, MAX_RESULTS, 60)
    start = clamp(request.GET.get("from"), 0, 9000, 0)
    only = request.GET.get("ms", "").strip()

    if len(query) < 2:
        return JsonResponse({"total": 0, "mode": "empty", "results": []})

    scope: list[dict[str, Any]] = [{"term": {"doc_type": "page"}}]

    if only:
        scope.append({"term": {"ms_id": only}})

    passes = [
        ("phrase", {
            "match_phrase_prefix": {
                "lines.text": {"query": query, "max_expansions": 200},
            },
        }),
        ("words", {
            "match": {
                "lines.text": {"query": query, "operator": "and"},
            },
        }),
    ]

    response: dict[str, Any] = {}
    mode = "none"

    for name, inner in passes:
        body = {
            "size": size,
            "from": start,
            "track_total_hits": True,
            "_source": HIT_SOURCE,
            "query": {"bool": {"filter": scope, "must": [_nested_lines(inner)]}},
            "sort": [{"_score": "desc"}, {"ms_order": "asc"}, {"page_no": "asc"}],
        }

        try:
            response = es.search(body)
        except es.SearchError as error:
            return failed(error)

        if total_of(response):
            mode = name
            break

        # A phrase that is only one word cannot be reordered, so the second
        # pass would ask exactly the same question.
        if len(query.split()) < 2:
            break

    results = []

    for hit in response.get("hits", {}).get("hits", []):
        source = hit["_source"]
        inner_hits = (
            hit.get("inner_hits", {})
            .get("lines", {})
            .get("hits", {})
            .get("hits", [])
        )

        lines = []

        for inner in inner_hits:
            line = inner["_source"]
            marked = inner.get("highlight", {}).get("lines.text", [None])[0]

            lines.append({
                # Where the line sits in the page document's array, which is
                # the order /pages/ serves them in, so the viewer can go
                # straight to it. `n` is its number in the original OCR output,
                # kept because it is what the transcription files call it.
                "i": inner.get("_nested", {}).get("offset"),
                "n": line.get("n"),
                "text": line.get("text", ""),
                "box": line.get("box"),
                "conf": line.get("conf"),
                "parts": es.highlight_parts(marked, line.get("text", "")),
            })

        results.append({
            "ms_id": source.get("ms_id"),
            "shelfmark": source.get("shelfmark"),
            "title": source.get("title"),
            "year": source.get("year"),
            "folio": source.get("folio"),
            "page_no": source.get("page_no"),
            "file": source.get("file"),
            "lines": lines,
        })

    return JsonResponse({
        "total": total_of(response) if mode != "none" else 0,
        "from": start,
        "mode": mode,
        "results": results,
    })


def viewer(request):
    """Serve index.html, so `runserver` alone is a working site."""

    path = Path(settings.VIEWER_FILE)

    if not settings.SERVE_VIEWER or not path.is_file():
        raise Http404("the viewer is served by the web server, not by Django")

    return FileResponse(path.open("rb"), content_type="text/html; charset=utf-8")
