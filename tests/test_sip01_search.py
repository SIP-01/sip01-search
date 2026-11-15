"""Offline tests for sip01-search.

No network: the relay client is replaced by a stub that returns canned
events, so these tests pin the parsing, grouping, ranking and CLI contract
without touching a relay.

Run: python3 -m unittest discover -s tests -v
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("sip01_search", ROOT / "sip01_search.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sip01_search"] = module
    spec.loader.exec_module(module)
    return module


cli = _load()


def _observation(title: str, url: str, doc_id: str, pubkey: str, description: str = "", lang: str | None = None) -> dict:
    tags = [["d", doc_id], ["u", url], ["v", "1"], ["alt", f"Web index observation: {title}"]]
    if lang:
        tags.append(["l", lang])
    content = {"title": title}
    if description:
        content["description"] = description
    return {
        "kind": 39697,
        "id": "e" * 64,
        "pubkey": pubkey,
        "created_at": 1700000000,
        "content": json.dumps(content),
        "tags": tags,
    }


class ParseObservationTests(unittest.TestCase):
    def test_parses_a_well_formed_event(self):
        event = _observation("Example", "https://example.com/", "widx:abc", "a" * 64, "A description", "en")
        row = cli._parse_observation(event)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Example")
        self.assertEqual(row["url"], "https://example.com/")
        self.assertEqual(row["d"], "widx:abc")
        self.assertEqual(row["lang"], "en")

    def test_rejects_wrong_kind(self):
        event = _observation("Example", "https://example.com/", "widx:abc", "a" * 64)
        event["kind"] = 1
        self.assertIsNone(cli._parse_observation(event))

    def test_rejects_missing_url_or_d(self):
        event = _observation("Example", "https://example.com/", "widx:abc", "a" * 64)
        event["tags"] = [t for t in event["tags"] if t[0] != "u"]
        self.assertIsNone(cli._parse_observation(event))

    def test_rejects_empty_title(self):
        event = _observation("", "https://example.com/", "widx:abc", "a" * 64)
        self.assertIsNone(cli._parse_observation(event))

    def test_strips_html_from_title_and_description(self):
        event = _observation(
            "<b>Bold</b> Title", "https://example.com/", "widx:abc", "a" * 64,
            description='<p>Some <a href="x">link</a> text</p>',
        )
        row = cli._parse_observation(event)
        self.assertEqual(row["title"], "Bold Title")
        self.assertNotIn("<", row["description"])
        self.assertNotIn(">", row["description"])


class TermTests(unittest.TestCase):
    def test_splits_on_whitespace(self):
        self.assertEqual(cli._terms("bitcoin wallet"), ["bitcoin", "wallet"])

    def test_keeps_quoted_phrases_together(self):
        self.assertEqual(cli._terms('"hello world" bitcoin'), ["hello world", "bitcoin"])

    def test_lowercases(self):
        self.assertEqual(cli._terms("BITCOIN"), ["bitcoin"])

    def test_empty_query_yields_no_terms(self):
        self.assertEqual(cli._terms(""), [])


class ScoreTests(unittest.TestCase):
    def test_title_hit_outranks_body_hit(self):
        base = {"title": "Bitcoin Wallet", "description": "", "topics": [], "url": "https://example.com"}
        other = {"title": "Nothing here", "description": "bitcoin", "topics": [], "url": "https://example.com"}
        self.assertGreater(cli._score(base, ["bitcoin"]), cli._score(other, ["bitcoin"]))

    def test_url_hit_counts(self):
        row = {"title": "A", "description": "", "topics": [], "url": "https://bitcoin.org/x"}
        self.assertGreater(cli._score(row, ["bitcoin"]), 0)

    def test_no_terms_scores_zero(self):
        row = {"title": "Bitcoin", "description": "", "topics": [], "url": "https://example.com"}
        self.assertEqual(cli._score(row, []), 0)


class SearchTests(unittest.TestCase):
    """The relay client is stubbed — no network in this suite."""

    def setUp(self):
        self.saved_query = cli._query
        self.calls = []

    def tearDown(self):
        cli._query = self.saved_query

    def _stub(self, events_by_relay):
        async def fake_query(relay, filter_obj, timeout):
            self.calls.append((relay, filter_obj))
            return {
                "events": events_by_relay.get(relay, []),
                "completeness": "complete",
                "eose": True,
                "pages": 1,
                "total_events": len(events_by_relay.get(relay, [])),
            }

        cli._query = fake_query

    def test_groups_by_document_id_and_counts_indexers(self):
        self._stub({
            "wss://a": [
                _observation("Doc", "https://example.com/", "widx:same", "a" * 64),
            ],
            "wss://b": [
                _observation("Doc", "https://example.com/", "widx:same", "b" * 64),
                _observation("Second example page", "https://example.org/other", "widx:other", "c" * 64),
            ],
        })
        result = asyncio.run(cli.search("example", relays=["wss://a", "wss://b"], limit=10))
        # Two distinct documents match; the two observations of `widx:same`
        # collapse into one entry with two independent indexers.
        self.assertEqual(result["total_documents"], 2)
        grouped = [h for h in result["hits"] if h["d"] == "widx:same"][0]
        self.assertEqual(grouped["indexers"], 2)

    def test_drops_candidates_that_do_not_match_locally(self):
        """A relay that ignores the NIP-50 search field must not produce
        false answers: non-matching candidates are filtered out locally."""
        self._stub({
            "wss://a": [
                _observation("Bitcoin Wallet", "https://example.com/", "widx:hit", "a" * 64),
                _observation("Unrelated News", "https://other.com/", "widx:miss", "b" * 64),
            ]
        })
        result = asyncio.run(cli.search("bitcoin", relays=["wss://a"], limit=10))
        self.assertEqual([h["d"] for h in result["hits"]], ["widx:hit"])

    def test_no_local_match_returns_no_results(self):
        self._stub({
            "wss://a": [
                _observation("Unrelated", "https://other.com/", "widx:miss", "a" * 64),
            ]
        })
        result = asyncio.run(cli.search("bitcoin", relays=["wss://a"], limit=10))
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["total_documents"], 0)

    def test_sends_nip50_search_field_for_free_text(self):
        self._stub({"wss://a": []})
        asyncio.run(cli.search("bitcoin wallet", relays=["wss://a"]))
        _, filter_obj = self.calls[0]
        self.assertEqual(filter_obj["search"], "bitcoin wallet")
        self.assertEqual(filter_obj["kinds"], [39697])

    def test_sends_tag_filter_for_exact_url(self):
        self._stub({"wss://a": []})
        asyncio.run(cli.search("", url="https://example.com/", relays=["wss://a"]))
        _, filter_obj = self.calls[0]
        self.assertIn("#u", filter_obj)
        self.assertNotIn("search", filter_obj)

    def test_reports_complete_when_a_relay_says_so(self):
        self._stub({"wss://a": []})
        result = asyncio.run(cli.search("x", relays=["wss://a"]))
        self.assertTrue(result["complete"])

    def test_reports_relay_errors_without_raising(self):
        async def failing(relay, filter_obj, timeout):
            raise OSError("connection refused")

        cli._query = failing
        result = asyncio.run(cli.search("x", relays=["wss://a"]))
        self.assertEqual(result["hits"], [])
        self.assertIn("error", result["relays"][0])

    def test_limit_caps_the_result_set(self):
        self._stub({
            "wss://a": [
                _observation(f"Doc {i}", f"https://example.com/{i}", f"widx:{i}", "a" * 64)
                for i in range(20)
            ]
        })
        result = asyncio.run(cli.search("doc", relays=["wss://a"], limit=5))
        self.assertLessEqual(len(result["hits"]), 5)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.saved_search = cli.search
        self.saved_query = cli._query

    def tearDown(self):
        cli.search = self.saved_search
        cli._query = self.saved_query

    def test_no_arguments_is_a_usage_error(self):
        self.assertEqual(cli.main([]), 2)

    def test_json_output_is_valid_json(self):
        import io
        import contextlib

        async def fake_search(query, **kwargs):
            return {"query": query, "hits": [], "hit_count": 0, "total_documents": 0,
                    "complete": True, "relays": [], "note": "n"}

        cli.search = fake_search
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["bitcoin", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["query"], "bitcoin")

    def test_human_output_lists_results(self):
        import io
        import contextlib

        async def fake_search(query, **kwargs):
            return {
                "query": query,
                "hits": [{"title": "Bitcoin Optech", "url": "https://bitcoinops.org", "d": "widx:x",
                          "description": "", "lang": "en", "indexers": 3, "score": 5.0}],
                "hit_count": 1, "total_documents": 1, "complete": True, "relays": [], "note": "n",
            }

        cli.search = fake_search
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["bitcoin"])
        self.assertEqual(code, 0)
        out = buffer.getvalue()
        self.assertIn("Bitcoin Optech", out)
        self.assertIn("[3x]", out)


if __name__ == "__main__":
    unittest.main()
