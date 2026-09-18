"""Tests for the search API.

Elasticsearch is replaced by a stub that records the request bodies, so these
check the two things that can silently go wrong without a cluster in the room:
the queries we send, and the shape of what we hand the browser.
"""

import json
from urllib.parse import unquote

from django.test import SimpleTestCase

from . import es
from .views import MAX_RESULTS


class FakeCluster:
    """Stands in for `es._call`, recording what was asked and replying to order."""

    def __init__(self, *searches):
        self.searches = list(searches)
        self.documents: dict[str, dict] = {}
        self.sent: list[dict] = []

    def __call__(self, method, path, body=None):
        if path == "/":
            return {"version": {"number": "8.14.0"}}

        if path.endswith("/_search"):
            self.sent.append(body)
            return self.searches.pop(0) if self.searches else hits()

        if "/_doc/" in path:
            found = self.documents.get(unquote(path.rsplit("/_doc/", 1)[1]))
            if found is None:
                raise es.SearchError("Elasticsearch refused the request (HTTP 404): {}")
            return {"_source": found}

        raise AssertionError("unexpected call: %s %s" % (method, path))


def hits(sources=(), total=0, aggregations=None, inner=None):
    return {
        "hits": {
            "total": {"value": total or len(sources)},
            "hits": [
                {"_source": source, "inner_hits": inner or {}}
                for source in sources
            ],
        },
        "aggregations": aggregations or {},
    }


def facet_buckets(**fields):
    return {
        field: {"values": {"buckets": [
            {"key": key, "doc_count": count} for key, count in values
        ]}}
        for field, values in fields.items()
    }


class ApiTests(SimpleTestCase):
    databases = set()

    def cluster(self, *searches):
        fake = FakeCluster(*searches)
        original = es._call
        es._call = fake
        self.addCleanup(lambda: setattr(es, "_call", original))
        return fake

    # -- info ---------------------------------------------------------------
    def test_info_reports_the_shape_of_the_corpus(self):
        self.cluster(hits(
            total=349,
            aggregations=dict(
                facet_buckets(script=[("ashkenazi", 67)]),
                pages={"value": 113228.0},
                lines={"value": 5134553.0},
            ),
        ))

        body = json.loads(self.client.get("/api/info").content)

        self.assertEqual(body["manuscripts"], 349)
        self.assertEqual(body["pages"], 113228)
        self.assertEqual(body["lines"], 5134553)
        self.assertEqual(body["facets"]["script"], [["ashkenazi", 67]])
        self.assertEqual(body["facets"]["genre"], [])

    # -- catalogue ----------------------------------------------------------
    def test_catalogue_query_is_a_prefix_match_over_the_metadata(self):
        fake = self.cluster(hits([{"ms_id": "a", "shelfmark": "Ms. hebr. 1"}]))

        body = json.loads(self.client.get("/api/manuscripts?q=mahzor rom").content)
        sent = fake.sent[0]

        self.assertEqual(sent["query"]["bool"]["must"][0], {"term": {"doc_type": "manuscript"}})
        self.assertEqual(
            sent["query"]["bool"]["must"][1],
            {"match_bool_prefix": {"meta_all": {"query": "mahzor rom", "operator": "and"}}},
        )
        self.assertNotIn("post_filter", sent)
        self.assertEqual(body["results"][0]["shelfmark"], "Ms. hebr. 1")

    def test_an_empty_query_keeps_the_catalogue_in_its_own_order(self):
        fake = self.cluster(hits())
        self.client.get("/api/manuscripts")

        self.assertEqual(fake.sent[0]["sort"], [{"ms_order": "asc"}])
        self.assertEqual(len(fake.sent[0]["query"]["bool"]["must"]), 1)

    def test_facets_filter_the_hits_but_not_their_own_counts(self):
        fake = self.cluster(hits())
        self.client.get("/api/manuscripts?script=italian&language=heb|ara")
        sent = fake.sent[0]

        # the hits see every chosen facet
        chosen = sent["post_filter"]["bool"]["filter"]
        self.assertIn({"terms": {"script": ["italian"]}}, chosen)
        self.assertIn({"terms": {"language": ["heb", "ara"]}}, chosen)

        # the script counts are narrowed by language, but not by script
        script_filter = sent["aggs"]["script"]["filter"]["bool"]["filter"]
        self.assertEqual(script_filter, [{"terms": {"language": ["heb", "ara"]}}])

        language_filter = sent["aggs"]["language"]["filter"]["bool"]["filter"]
        self.assertEqual(language_filter, [{"terms": {"script": ["italian"]}}])

    def test_a_size_beyond_the_cap_is_clamped(self):
        fake = self.cluster(hits())
        self.client.get("/api/manuscripts?size=99999")

        self.assertEqual(fake.sent[0]["size"], MAX_RESULTS)

    # -- one manuscript -----------------------------------------------------
    def test_manuscript_returns_its_pages_in_reading_order(self):
        fake = self.cluster(hits([
            {"file": "IE1_P000002.json", "folio": "1r", "page_no": 2},
            {"file": "IE1_P000003.json", "folio": "1v", "page_no": 3},
        ]))
        fake.documents["ms:abc"] = {
            "ms_id": "abc", "shelfmark": "Ms. hebr. 1", "doc_type": "manuscript",
        }

        body = json.loads(self.client.get("/api/manuscripts/abc").content)

        self.assertEqual(fake.sent[0]["sort"], [{"page_no": "asc"}, {"file": "asc"}])
        self.assertEqual(body["manuscript"]["shelfmark"], "Ms. hebr. 1")
        self.assertNotIn("doc_type", body["manuscript"])
        self.assertEqual([p["folio"] for p in body["pages"]], ["1r", "1v"])

    def test_an_unknown_manuscript_is_a_404(self):
        self.cluster()
        self.assertEqual(self.client.get("/api/manuscripts/nope").status_code, 404)

    # -- one page -----------------------------------------------------------
    def test_a_page_comes_back_in_the_compact_viewer_format(self):
        fake = self.cluster()
        fake.documents["page:abc:IE1_P000002"] = {
            "page_no": 2, "folio": "1r", "width": 5901, "height": 4024,
            "iiif": "https://gallica.bnf.fr/iiif/ark:/12148/x/f2",
            "lines": [
                {"n": 0, "text": "one", "box": [1, 2, 3, 4], "conf": 0.93, "main": True},
                {"n": 1, "text": "two", "box": [5, 6, 7, 8], "conf": None, "main": False},
            ],
        }

        body = json.loads(
            self.client.get("/api/manuscripts/abc/pages/IE1_P000002.json").content
        )

        self.assertEqual(body["w"], 5901)
        self.assertEqual(body["l"][0], [1, 2, 3, 4, 1, 0.93, "one"])
        self.assertEqual(body["l"][1], [5, 6, 7, 8, 0, None, "two"])

    # -- full text ----------------------------------------------------------
    def inner_line(self, text, marked, offset=0):
        return {"lines": {"hits": {"hits": [{
            "_nested": {"field": "lines", "offset": offset},
            "_source": {"n": offset, "text": text, "box": [1, 2, 3, 4], "conf": 0.9},
            "highlight": {"lines.text": [marked]},
        }]}}}

    def test_a_phrase_is_searched_as_a_phrase_first(self):
        fake = self.cluster(hits(
            [{"ms_id": "abc", "shelfmark": "Ms. 1", "folio": "3r", "file": "p.json"}],
            inner=self.inner_line(
                "the words here",
                "the " + es.HL_OPEN + "words" + es.HL_CLOSE + " here",
            ),
        ))

        body = json.loads(self.client.get("/api/search?q=words here").content)
        nested = fake.sent[0]["query"]["bool"]["must"][0]["nested"]

        self.assertEqual(len(fake.sent), 1, "no fallback pass when the phrase is found")
        self.assertEqual(body["mode"], "phrase")
        self.assertIn("match_phrase_prefix", nested["query"])
        self.assertEqual(nested["query"]["match_phrase_prefix"]["lines.text"]["query"],
                         "words here")
        self.assertEqual(nested["inner_hits"]["highlight"]["pre_tags"], [es.HL_OPEN])

        line = body["results"][0]["lines"][0]
        self.assertEqual(line["i"], 0)
        self.assertEqual(line["parts"], [["the ", False], ["words", True], [" here", False]])

    def test_a_phrase_that_is_not_there_falls_back_to_the_same_words(self):
        fake = self.cluster(
            hits(),
            hits([{"ms_id": "abc", "file": "p.json"}],
                 inner=self.inner_line("here are words", "here are words")),
        )

        body = json.loads(self.client.get("/api/search?q=words here").content)

        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(body["mode"], "words")
        self.assertEqual(
            fake.sent[1]["query"]["bool"]["must"][0]["nested"]["query"],
            {"match": {"lines.text": {"query": "words here", "operator": "and"}}},
        )

    def test_one_word_is_never_searched_twice(self):
        fake = self.cluster(hits(), hits())

        body = json.loads(self.client.get("/api/search?q=word").content)

        self.assertEqual(len(fake.sent), 1, "reordering one word asks the same question")
        self.assertEqual(body["mode"], "none")
        self.assertEqual(body["total"], 0)

    def test_a_search_narrowed_to_one_manuscript(self):
        fake = self.cluster(hits())
        self.client.get("/api/search?q=word&ms=abc")

        self.assertIn({"term": {"ms_id": "abc"}},
                      fake.sent[0]["query"]["bool"]["filter"])

    def test_a_single_letter_is_not_worth_a_round_trip(self):
        fake = self.cluster()
        body = json.loads(self.client.get("/api/search?q=a").content)

        self.assertEqual(fake.sent, [])
        self.assertEqual(body["mode"], "empty")

    # -- failure ------------------------------------------------------------
    def test_a_cluster_that_is_down_is_reported_not_hidden(self):
        def dead(method, path, body=None):
            raise es.SearchError("Elasticsearch is not answering (refused)")

        original = es._call
        es._call = dead
        self.addCleanup(lambda: setattr(es, "_call", original))

        response = self.client.get("/api/manuscripts")

        self.assertEqual(response.status_code, 503)
        self.assertIn("not answering", json.loads(response.content)["error"])


class HighlightTests(SimpleTestCase):
    def test_marks_become_parts(self):
        marked = "before " + es.HL_OPEN + "hit" + es.HL_CLOSE + " after"

        self.assertEqual(
            es.highlight_parts(marked, "before hit after"),
            [["before ", False], ["hit", True], [" after", False]],
        )

    def test_a_line_with_nothing_marked_is_one_plain_part(self):
        self.assertEqual(es.highlight_parts(None, "plain"), [["plain", False]])

    def test_a_mark_at_the_very_start(self):
        marked = es.HL_OPEN + "hit" + es.HL_CLOSE + " rest"

        self.assertEqual(
            es.highlight_parts(marked, "hit rest"),
            [["hit", True], [" rest", False]],
        )
