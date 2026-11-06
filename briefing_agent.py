"""
briefing_agent.py — ORBIT's morning market + world-news brief for Boss.

    from briefing_agent import BriefingAgent
    brief = BriefingAgent().build_briefing()
    brief["reply_bn"]      # spoken Bengali summary, already says "বস"
    brief["headlines"]     # [{'title', 'source', 'link'}]
    brief["indices"]       # [{'name_bn': 'নিফটি ৫০', 'price': ..., 'percent': ...}]

Data sources (all injectable through ``session``):

* Google News WORLD + BUSINESS RSS (no key needed),
* BBC World RSS as a second world source,
* Yahoo Finance chart endpoint for NIFTY 50 and NIFTY MIDCAP 50.

The summary is written in Bengali by Gemini 2.5 Flash when a key is present; when
it is offline, rate-limited or returns nothing, a deterministic Bengali brief is
built from the same facts — it always addresses Boss and always adds one
strategic "Thinker" sentence connecting the world news to the market.

Every method returns ``{"ok": bool, ...}`` and never raises.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

from brain import GEMINI_MODEL, GEMINI_URL, ensure_boss_address
from config import settings as default_settings
from system_control import to_bengali_digits

try:  # optional at import time
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

log = logging.getLogger("orbit.briefing")

REQUEST_TIMEOUT = 20.0
MAX_HEADLINES = 5
HEADLINE_CHAR_LIMIT = 160

NEWS_FEEDS: Sequence[Dict[str, str]] = (
    {
        "name": "Google News World",
        "url": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
    },
    {
        "name": "BBC World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "name": "Google News Business",
        "url": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
    },
)

INDICES: Sequence[Dict[str, str]] = (
    {"symbol": "^NSEI", "name_bn": "নিফটি ৫০"},
    {"symbol": "^NSEMDCP50", "name_bn": "নিফটি মিডক্যাপ ৫০"},
)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d"

SUMMARY_PROMPT = (
    "তুমি ORBIT, বসের ব্যক্তিগত সহকারী। নিচের বাজার তথ্য আর বিশ্বের শিরোনাম দেখে "
    "বসের জন্য ৩-৪ বাক্যের ছোট বাংলা ব্রিফিং লেখো।\n"
    "নিয়ম: শুরুতেই 'বস' বলে সম্বোধন করো; সংখ্যা বাংলা অঙ্কে লেখো; "
    "শুধু তথ্য ও তার মানে বলো, নতুন উপদেশ দিও না (ভাবনার লাইনটা পরে যোগ হবে); "
    "কোনো মার্কডাউন, বুলেট বা ইংরেজি ব্যাখ্যা নয়।\n\n"
    "বাজার:\n{market}\n\nশিরোনাম:\n{headlines}\n\nবিষয়: {topic}"
)

THINKER_UP = (
    "ভাবছি — বিশ্ব বাজারে ঝুঁকি কমলে বিদেশি লগ্নি ফিরে আসে, তাই নিফটির এই উঠতি "
    "আসল খবরের উপর দাঁড়িয়ে আছে কি না সেটাই আসল প্রশ্ন।"
)
THINKER_DOWN = (
    "ভাবছি — বিশ্বের অস্থিরতা তেল ও ডলারের দাম বাড়ায়, আর সেটা আমাদের বাজারে "
    "বিদেশি লগ্নির খরচ বাড়িয়ে দেয়; তাই আজকের পতনটা শুধু লোকাল নয়।"
)
THINKER_FLAT = (
    "ভাবছি — বাজার যখন পাশাপাশি ঘুরছে, তখন আসল সংকেত আসে খবরের শিরোনাম থেকে নয়, "
    "বিদেশি লগ্নির প্রবাহ আর ডলারের দাম থেকে।"
)
NO_DATA_BN = "বস, আজকের বাজার বা খবরের ডেটা কোনোটাই পাওয়া গেল না। নেটওয়ার্ক দেখে আবার বলুন।"


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _bn(number: float, decimals: int = 2) -> str:
    return to_bengali_digits(f"{number:,.{decimals}f}")


class BriefingAgent:
    """Builds the `market_news` brief: headlines + indices + Bengali summary."""

    def __init__(
        self,
        session: Any = None,
        settings: Optional[Any] = None,
        brain: Any = None,
        llm_callable: Optional[Callable[[str], str]] = None,
        timeout: float = REQUEST_TIMEOUT,
        max_headlines: int = MAX_HEADLINES,
        feeds: Sequence[Dict[str, str]] = NEWS_FEEDS,
        indices: Sequence[Dict[str, str]] = INDICES,
    ) -> None:
        self.settings = settings or default_settings
        self._session = session
        self.brain = brain
        self._llm = llm_callable
        self.timeout = float(timeout)
        self.max_headlines = max(1, int(max_headlines))
        self.feeds = list(feeds)
        self.indices = list(indices)
        self.last_error: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    def _get(self, url: str) -> Any:
        session = self._session
        if session is None:
            if requests is None:  # pragma: no cover
                raise RuntimeError("requests is not installed")
            session = requests.Session()
        return session.get(url, timeout=self.timeout, headers={"User-Agent": "ORBIT/1.0"})

    def _fail(self, error: str, action: str = "market_news", **extra: Any) -> Dict[str, Any]:
        self.last_error = error
        log.warning("%s: %s", action, error)
        return {"ok": False, "action": action, "error": error, **extra}

    # ------------------------------------------------------------------ #
    # News
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_rss(xml_text: str, source: str) -> List[Dict[str, str]]:
        """Titles/links out of an RSS 2.0 document (CDATA and entities included)."""
        try:
            root = ET.fromstring((xml_text or "").strip())
        except ET.ParseError:
            return []

        items: List[Dict[str, str]] = []
        for item in root.iter("item"):
            title = _clean_text(item.findtext("title"))
            if not title:
                continue
            items.append(
                {
                    "title": title[:HEADLINE_CHAR_LIMIT],
                    "link": _clean_text(item.findtext("link")),
                    "published": _clean_text(item.findtext("pubDate")),
                    "source": source,
                }
            )
        return items

    def fetch_news(self, limit: Optional[int] = None) -> Dict[str, Any]:
        wanted = limit or self.max_headlines
        headlines: List[Dict[str, str]] = []
        errors: List[str] = []
        seen: set = set()

        for feed in self.feeds:
            try:
                response = self._get(feed["url"])
            except Exception as exc:
                errors.append(f"{feed['name']}: {exc}")
                continue
            status = getattr(response, "status_code", 0)
            if status != 200:
                errors.append(f"{feed['name']}: HTTP {status}")
                continue
            for item in self.parse_rss(getattr(response, "text", ""), feed["name"]):
                key = re.sub(r"\W+", "", item["title"].lower())[:80]
                if not key or key in seen:
                    continue
                seen.add(key)
                headlines.append(item)

        if not headlines:
            return self._fail(
                "; ".join(errors) or "no headlines available",
                "market_news",
                headlines=[],
                count=0,
                errors=errors,
            )

        return {
            "ok": True,
            "headlines": headlines[:wanted],
            "count": min(len(headlines), wanted),
            "errors": errors,
        }

    # ------------------------------------------------------------------ #
    # Markets
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_quote(payload: Any, name_bn: str) -> Optional[Dict[str, Any]]:
        """Price + previous close out of a Yahoo chart response."""
        try:
            result = payload["chart"]["result"][0]
            meta = result.get("meta") or {}
            price = meta.get("regularMarketPrice")
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None or previous in (None, 0):
                closes = [
                    value
                    for value in ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                    if isinstance(value, (int, float))
                ]
                if len(closes) >= 2:
                    price = price if price is not None else closes[-1]
                    previous = previous or closes[0]
            if not isinstance(price, (int, float)) or not isinstance(previous, (int, float)) or previous == 0:
                return None
            change = float(price) - float(previous)
            return {
                "name_bn": name_bn,
                "price": round(float(price), 2),
                "previous_close": round(float(previous), 2),
                "change": round(change, 2),
                "percent": round(change / float(previous) * 100, 2),
                "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
            }
        except Exception:
            return None

    def fetch_markets(self) -> Dict[str, Any]:
        indices: List[Dict[str, Any]] = []
        errors: List[str] = []

        for entry in self.indices:
            symbol = entry["symbol"]
            try:
                response = self._get(YAHOO_URL.format(symbol=quote(symbol, safe="")))
                status = getattr(response, "status_code", 0)
                if status != 200:
                    errors.append(f"{symbol}: HTTP {status}")
                    continue
                quote_data = self.parse_quote(response.json(), entry["name_bn"])
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue
            if quote_data is None:
                errors.append(f"{symbol}: unreadable quote")
                continue
            indices.append(quote_data)

        if not indices:
            return self._fail(
                "; ".join(errors) or "no market data", "market_news", indices=[], errors=errors
            )

        return {"ok": True, "indices": indices, "errors": errors}

    # ------------------------------------------------------------------ #
    # Bengali summary
    # ------------------------------------------------------------------ #

    @staticmethod
    def market_line(indices: Sequence[Dict[str, Any]]) -> str:
        parts = []
        for item in indices:
            arrow = "বেড়েছে" if item["direction"] == "up" else ("কমেছে" if item["direction"] == "down" else "একই আছে")
            sign = "+" if item["percent"] > 0 else ""
            parts.append(
                f"{item['name_bn']} {_bn(item['price'])} ({sign}{_bn(item['percent'])}% {arrow})"
            )
        return ", ".join(parts)

    @staticmethod
    def thinker_insight(indices: Sequence[Dict[str, Any]]) -> str:
        directions = {item.get("direction") for item in indices}
        if "down" in directions and "up" not in directions:
            return THINKER_DOWN
        if "up" in directions and "down" not in directions:
            return THINKER_UP
        return THINKER_FLAT

    def _gemini(self, prompt: str) -> Optional[str]:
        if not self.settings.gemini_api_key:
            return None
        session = self._session
        if session is None:
            if requests is None:  # pragma: no cover
                return None
            session = requests.Session()

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300, "thinkingBudget": 0},
        }
        url = GEMINI_URL.format(model=GEMINI_MODEL)
        for attempt in range(2):
            try:
                response = session.post(
                    url,
                    params={"key": self.settings.gemini_api_key},
                    json=payload,
                    timeout=self.timeout,
                )
            except Exception as exc:
                log.warning("Briefing LLM failed: %s", exc)
                return None

            status = getattr(response, "status_code", 0)
            if status in (429, 500, 502, 503, 504) and attempt == 0:
                continue
            if status != 200:
                log.warning("Briefing LLM HTTP %s", status)
                return None
            try:
                parts = response.json()["candidates"][0]["content"]["parts"]
                text = " ".join(str(part.get("text", "")) for part in parts).strip()
            except Exception as exc:
                log.warning("Briefing LLM response unreadable: %s", exc)
                return None
            return text or None
        return None

    def _llm_summary(self, headlines: Sequence[Dict[str, str]], indices: Sequence[Dict[str, Any]], topic: str) -> Optional[str]:
        market = self.market_line(indices) if indices else "বাজারের ডেটা পাওয়া যায়নি"
        news = "\n".join(f"- {item['title']}" for item in headlines[:4]) or "- কোনো শিরোনাম নেই"
        prompt = SUMMARY_PROMPT.format(market=market, headlines=news, topic=topic or "general")

        if self._llm is not None:
            try:
                text = str(self._llm(prompt) or "").strip()
            except Exception as exc:
                log.warning("Injected briefing LLM failed: %s", exc)
                return None
            return text or None
        return self._gemini(prompt)

    def _deterministic_summary(
        self,
        headlines: Sequence[Dict[str, str]],
        indices: Sequence[Dict[str, Any]],
    ) -> str:
        lines: List[str] = ["বস, আজকের ব্রিফিং —"]
        if indices:
            lines.append(f"বাজার: {self.market_line(indices)}।")
        else:
            lines.append("বাজার: আজ মার্কেট ডেটা পাওয়া যায়নি।")
        if headlines:
            top = " | ".join(_clean_text(item["title"]) for item in headlines[:3])
            lines.append(f"বিশ্ব খবর: {top}।")
        else:
            lines.append("বিশ্ব খবর: আজ কোনো শিরোনাম পাওয়া যায়নি।")
        return " ".join(lines)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def build_briefing(self, topic: str = "general") -> Dict[str, Any]:
        """Fetch everything and return the `market_news` payload."""
        news = self.fetch_news()
        markets = self.fetch_markets()
        headlines = news.get("headlines") or []
        indices = markets.get("indices") or []
        errors = list(news.get("errors") or []) + list(markets.get("errors") or [])

        if not headlines and not indices:
            return {
                "ok": False,
                "action": "market_news",
                "topic": topic,
                "summary_bn": NO_DATA_BN,
                "reply_bn": NO_DATA_BN,
                "headlines": [],
                "indices": [],
                "errors": errors,
                "error": "; ".join(errors) or "no data",
            }

        body = self._llm_summary(headlines, indices, topic)
        llm_used = bool(body)
        if not body:
            body = self._deterministic_summary(headlines, indices)

        insight = self.thinker_insight(indices)
        summary = ensure_boss_address(_clean_text(body))
        reply = f"{summary} {insight}".strip()

        log.info(
            "Briefing ready: %d headlines, %d indices, llm=%s",
            len(headlines), len(indices), llm_used,
        )
        return {
            "ok": True,
            "action": "market_news",
            "topic": topic,
            "summary_bn": summary,
            "insight_bn": insight,
            "reply_bn": reply,
            "headlines": headlines,
            "indices": indices,
            "market_line_bn": self.market_line(indices) if indices else "",
            "llm_used": llm_used,
            "errors": errors,
        }

    def market_news(self, topic: str = "general") -> Dict[str, Any]:
        """Alias used by the dispatcher for the `market_news` action."""
        return self.build_briefing(topic=topic)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(BriefingAgent().build_briefing(), ensure_ascii=False, indent=2)[:4000])
