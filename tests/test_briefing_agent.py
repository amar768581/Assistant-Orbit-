"""
tests/test_briefing_agent.py — RSS parsing, Nifty snapshots and the Bengali
brief. Network, LLM and clock are all faked.
"""

from __future__ import annotations

import unittest

from briefing_agent import (
    NO_DATA_BN,
    THINKER_DOWN,
    THINKER_FLAT,
    THINKER_UP,
    BriefingAgent,
)

WORLD_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Global leaders meet on trade deal</title>
    <link>https://news.example/1</link>
    <pubDate>Mon, 05 Oct 2026 06:00:00 GMT</pubDate>
  </item>
  <item>
    <title><![CDATA[Oil prices climb &amp; markets wobble]]></title>
    <link>https://news.example/2</link>
  </item>
</channel></rss>
"""

BUSINESS_RSS = """<?xml version="1.0"?>
<rss><channel>
  <item><title>Rupee slips against dollar</title><link>https://news.example/3</link></item>
  <item><title>Global leaders meet on trade deal</title><link>https://news.example/dup</link></item>
</channel></rss>
"""

NIFTY_JSON = {
    "chart": {
        "result": [
            {
                "meta": {"regularMarketPrice": 25432.10, "chartPreviousClose": 25325.00},
                "indicators": {"quote": [{"close": [25325.0, 25432.1]}]},
            }
        ],
        "error": None,
    }
}

MIDCAP_JSON = {
    "chart": {
        "result": [
            {
                "meta": {"regularMarketPrice": 58120.55, "chartPreviousClose": 58400.00},
                "indicators": {"quote": [{"close": [58400.0, 58120.55]}]},
            }
        ]
    }
}


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, routes=None, post_text=None, post_status=200):
        self.routes = routes or {}
        self.post_text = post_text
        self.post_status = post_status
        self.gets = []
        self.posts = []

    def get(self, url, timeout=None, headers=None):
        self.gets.append(url)
        for needle, response in self.routes.items():
            if needle in url:
                return response
        return FakeResponse(status_code=404, text="not found")

    def post(self, url, params=None, json=None, timeout=None):
        self.posts.append({"url": url, "params": params, "json": json})
        if self.post_status != 200:
            return FakeResponse(status_code=self.post_status, text="error")
        payload = {"candidates": [{"content": {"parts": [{"text": self.post_text}]}}]}
        return FakeResponse(status_code=200, payload=payload)


class FakeSettings:
    def __init__(self, gemini_key=""):
        self.gemini_api_key = gemini_key


def full_session(**kwargs):
    return FakeSession(
        routes={
            "section/topic/WORLD": FakeResponse(200, WORLD_RSS),
            "bbci.co.uk": FakeResponse(200, WORLD_RSS.replace("Global leaders", "BBC exclusive")),
            "section/topic/BUSINESS": FakeResponse(200, BUSINESS_RSS),
            "NSEI": FakeResponse(200, payload=NIFTY_JSON),
            "NSEMDCP50": FakeResponse(200, payload=MIDCAP_JSON),
        },
        **kwargs,
    )


def make(session=None, **kwargs):
    return BriefingAgent(session=session or full_session(), settings=kwargs.pop("settings", FakeSettings()), **kwargs)


# --------------------------------------------------------------------------- #
# RSS
# --------------------------------------------------------------------------- #

class TestNews(unittest.TestCase):
    def test_parse_rss_handles_cdata_entities_and_tags(self):
        items = BriefingAgent.parse_rss(
            "<rss><channel><item><title><![CDATA[<b>Big</b> &amp; bold]]></title>"
            "<link>https://x/1</link></item></channel></rss>",
            "Test",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Big & bold")
        self.assertEqual(items[0]["link"], "https://x/1")
        self.assertEqual(items[0]["source"], "Test")

    def test_parse_rss_broken(self):
        self.assertEqual(BriefingAgent.parse_rss("<rss><channel>", "Test"), [])
        self.assertEqual(BriefingAgent.parse_rss("", "Test"), [])

    def test_fetch_news_merges_and_dedupes(self):
        result = make().fetch_news()
        self.assertTrue(result["ok"])
        titles = [item["title"] for item in result["headlines"]]
        self.assertIn("Global leaders meet on trade deal", titles)
        self.assertIn("Oil prices climb & markets wobble", titles)
        self.assertIn("Rupee slips against dollar", titles)
        self.assertEqual(len(titles), len(set(titles)))

    def test_fetch_news_limit(self):
        result = make(max_headlines=2).fetch_news()
        self.assertEqual(len(result["headlines"]), 2)

    def test_one_feed_down_still_works(self):
        session = FakeSession(
            routes={
                "section/topic/WORLD": FakeResponse(200, WORLD_RSS),
                "bbci.co.uk": FakeResponse(503, "unavailable"),
                "section/topic/BUSINESS": FakeResponse(200, BUSINESS_RSS),
            }
        )
        result = make(session=session).fetch_news()
        self.assertTrue(result["ok"])
        self.assertTrue(any("BBC" in error for error in result["errors"]))

    def test_all_feeds_down(self):
        result = make(session=FakeSession()).fetch_news()
        self.assertFalse(result["ok"])
        self.assertEqual(result["headlines"], [])


# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #

class TestMarkets(unittest.TestCase):
    def test_fetch_markets(self):
        result = make().fetch_markets()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["indices"]), 2)
        nifty = result["indices"][0]
        self.assertEqual(nifty["name_bn"], "নিফটি ৫০")
        self.assertAlmostEqual(nifty["price"], 25432.10)
        self.assertAlmostEqual(nifty["change"], 107.10)
        self.assertEqual(nifty["direction"], "up")
        self.assertEqual(result["indices"][1]["direction"], "down")

    def test_parse_quote_without_meta_close(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"regularMarketPrice": 100.0},
                        "indicators": {"quote": [{"close": [None, 98.0, 100.0]}]},
                    }
                ]
            }
        }
        quote_data = BriefingAgent.parse_quote(payload, "নিফটি ৫০")
        self.assertIsNotNone(quote_data)
        self.assertAlmostEqual(quote_data["percent"], round(2 / 98.0 * 100, 2))

    def test_parse_quote_garbage(self):
        self.assertIsNone(BriefingAgent.parse_quote({}, "x"))
        self.assertIsNone(BriefingAgent.parse_quote({"chart": {"result": []}}, "x"))

    def test_partial_market_failure(self):
        session = FakeSession(routes={"NSEI": FakeResponse(200, payload=NIFTY_JSON)})
        result = make(session=session).fetch_markets()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["indices"]), 1)
        self.assertTrue(result["errors"])

    def test_all_market_data_missing(self):
        result = make(session=FakeSession()).fetch_markets()
        self.assertFalse(result["ok"])
        self.assertEqual(result["indices"], [])


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

class TestSummary(unittest.TestCase):
    def test_market_line_uses_bengali_digits(self):
        indices = [
            {"name_bn": "নিফটি ৫০", "price": 25432.10, "percent": 0.42, "direction": "up"},
            {"name_bn": "নিফটি মিডক্যাপ ৫০", "price": 58120.55, "percent": -0.48, "direction": "down"},
        ]
        line = BriefingAgent.market_line(indices)
        self.assertIn("২৫,৪৩২.১০", line)
        self.assertIn("+০.৪২%", line)
        self.assertIn("কমেছে", line)

    def test_thinker_insight_direction(self):
        self.assertEqual(BriefingAgent.thinker_insight([{"direction": "up"}]), THINKER_UP)
        self.assertEqual(BriefingAgent.thinker_insight([{"direction": "down"}]), THINKER_DOWN)
        self.assertEqual(
            BriefingAgent.thinker_insight([{"direction": "up"}, {"direction": "down"}]), THINKER_FLAT
        )
        self.assertEqual(BriefingAgent.thinker_insight([]), THINKER_FLAT)

    def test_deterministic_brief_without_key(self):
        result = make().build_briefing()

        self.assertTrue(result["ok"])
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["reply_bn"].startswith("বস"))
        self.assertEqual(result["summary_bn"], result["reply_bn"][: len(result["summary_bn"])])
        self.assertIn("নিফটি ৫০", result["reply_bn"])
        self.assertIn("Global leaders meet on trade deal", result["reply_bn"])
        self.assertIn(result["insight_bn"], result["reply_bn"])
        self.assertEqual(result["action"], "market_news")
        self.assertTrue(result["headlines"])
        self.assertTrue(result["indices"])

    def test_llm_summary_is_used_and_gets_boss_prefix(self):
        prompts = []

        def fake_llm(prompt):
            prompts.append(prompt)
            return "আজ নিফটি বেড়েছে, আর বিশ্ব বাণিজ্য আলোচনা চলছে।"

        result = make(llm_callable=fake_llm).build_briefing(topic="stocks")

        self.assertTrue(result["llm_used"])
        self.assertTrue(result["summary_bn"].startswith("বস, "))
        self.assertIn("নিফটি বেড়েছে", result["reply_bn"])
        self.assertTrue(result["reply_bn"].endswith(result["insight_bn"]))
        self.assertIn("নিফটি ৫০", prompts[0])
        self.assertIn("Global leaders", prompts[0])
        self.assertIn("stocks", prompts[0])

    def test_llm_failure_falls_back(self):
        def broken(prompt):
            raise RuntimeError("quota exhausted")

        result = make(llm_callable=broken).build_briefing()
        self.assertTrue(result["ok"])
        self.assertFalse(result["llm_used"])
        self.assertIn("আজকের ব্রিফিং", result["summary_bn"])

    def test_llm_empty_falls_back(self):
        result = make(llm_callable=lambda prompt: "   ").build_briefing()
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["ok"])

    def test_gemini_post_path(self):
        session = full_session(post_text="বস, বাজার মিশ্র, বিশ্বে বাণিজ্য আলোচনা চলছে।")
        result = make(session=session, settings=FakeSettings("test-key")).build_briefing()

        self.assertTrue(result["llm_used"])
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["params"], {"key": "test-key"})
        self.assertIn("বাজার", result["summary_bn"])

    def test_gemini_http_error_falls_back(self):
        session = full_session(post_status=400)
        result = make(session=session, settings=FakeSettings("test-key")).build_briefing()
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["ok"])

    def test_news_only(self):
        session = FakeSession(
            routes={
                "section/topic/WORLD": FakeResponse(200, WORLD_RSS),
                "bbci.co.uk": FakeResponse(500, ""),
                "section/topic/BUSINESS": FakeResponse(500, ""),
            }
        )
        result = make(session=session).build_briefing()
        self.assertTrue(result["ok"])
        self.assertEqual(result["indices"], [])
        self.assertIn("মার্কেট ডেটা পাওয়া যায়নি", result["reply_bn"])
        self.assertEqual(result["insight_bn"], THINKER_FLAT)

    def test_markets_only(self):
        session = FakeSession(routes={"NSEI": FakeResponse(200, payload=NIFTY_JSON)})
        result = make(session=session).build_briefing()
        self.assertTrue(result["ok"])
        self.assertEqual(result["headlines"], [])
        self.assertIn("কোনো শিরোনাম পাওয়া যায়নি", result["reply_bn"])

    def test_no_data_at_all(self):
        result = make(session=FakeSession()).build_briefing()
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary_bn"], NO_DATA_BN)
        self.assertTrue(result["reply_bn"].startswith("বস"))
        self.assertTrue(result["errors"])

    def test_alias_and_topic(self):
        result = make().market_news(topic="crypto")
        self.assertEqual(result["action"], "market_news")
        self.assertEqual(result["topic"], "crypto")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
