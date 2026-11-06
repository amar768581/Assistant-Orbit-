"""
memory_engine.py — ORBIT's long-term memory and self-evolution engine.

SQLite, one file, four tables (default ``~/.orbit/orbit_memory.db``):

    episodes       every turn: what Boss said, what ORBIT did, how it went
    boss_profile   durable facts about Boss (name, family, preferences, goals)
    self_upgrades  lessons + custom rules ORBIT derived from corrections
    thinker_notes  daily strategic insights and the "Thinker Challenge"

    from memory_engine import MemoryEngine
    mem = MemoryEngine()
    mem.learn_from_text("আমার নাম রাসেল, আমি প্রতিদিন ভোরে উঠি")
    mem.remember_turn("টর্চ জালাও", {"action": "torch", ...})
    mem.build_memory_context("ব্যাটারি কেমন")   # -> prompt-ready text
    mem.daily_self_upgrade()                     # -> new rule + challenge

Everything is deterministic and local: no network, no LLM required. An
optional ``llm_callable`` lets ``brain`` sharpen the daily upgrade, but a
pure-Python fallback always produces a usable rule and question.

Every method returns ``{"ok": ...}`` (``build_memory_context`` returns a
string, since it is injected straight into the prompt) and never raises for
a missing/unwritable database — the failure is reported in ``error``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from config import settings as default_settings
from security_policy import (
    BLOCKED_FINANCIAL_APPS,
    BLOCKED_PACKAGES,
    SENSITIVE_SKIP_BN,
    contains_secret,
    is_sensitive_text,
)

log = logging.getLogger("orbit.memory")

DEFAULT_DB_PATH = Path.home() / ".orbit" / "orbit_memory.db"
CONTEXT_CHAR_LIMIT = 1800
TEXT_LIMIT = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    user_text   TEXT NOT NULL,
    action      TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    reply_bn    TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(timestamp DESC);

CREATE TABLE IF NOT EXISTS boss_profile (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'general',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS self_upgrades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_key   TEXT NOT NULL UNIQUE,
    rule_text  TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thinker_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL,
    insight_bn TEXT NOT NULL,
    question_bn TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_time ON thinker_notes(created_at DESC);
"""

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_BN_RANGE = "\u0980-\u09FF"


def _now() -> datetime:
    return datetime.now().astimezone()


def _clean(text: Any) -> str:
    """Collapse whitespace and trim stray punctuation/quotes."""
    value = str(text or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\"'")


def _slug(text: str, limit: int = 32) -> str:
    """Stable key fragment: latin words kept, Bengali hashed."""
    value = _clean(text).lower()
    latin = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    if latin and len(latin) >= 2:
        return latin[:limit]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"bn_{digest}"


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(rf"[a-z0-9{_BN_RANGE}]+", (text or "").lower()) if len(t) >= 3]


class MemoryEngine:
    """SQLite-backed episodes, Boss profile, learned rules and thinker notes."""

    def __init__(
        self,
        db_path: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        settings: Optional[Any] = None,
    ) -> None:
        self.settings = settings or default_settings
        configured = db_path or getattr(self.settings, "memory_db", None) or DEFAULT_DB_PATH
        self.db_path = Path(configured).expanduser()
        self._clock = clock or _now
        self._lock = threading.RLock()
        self.last_error: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # -- plumbing ---------------------------------------------------------- #

    def _open(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.executescript(SCHEMA)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:  # pragma: no cover - exotic filesystems
                pass
            log.info("Memory ready at %s", self.db_path)
        except Exception as exc:
            self._conn = None
            self.last_error = f"cannot open memory db {self.db_path}: {exc}"
            log.error(self.last_error)

    @property
    def ok(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> "MemoryEngine":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _fail(self, message: str, **extra: Any) -> Dict[str, Any]:
        self.last_error = message
        log.warning(message)
        return {"ok": False, "error": message, **extra}

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _query(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                return list(self._conn.execute(sql, tuple(args)))
            except sqlite3.Error as exc:
                log.warning("memory query failed: %s", exc)
                return []

    # ------------------------------------------------------------------ #
    # Facts
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sensitive_skip() -> Dict[str, Any]:
        return {"ok": True, "skipped": "sensitive", "message_bn": SENSITIVE_SKIP_BN}

    def remember_fact(self, key: str, value: str, category: str = "general") -> Dict[str, Any]:
        """Insert/update one durable fact about Boss."""
        fact_key = _slug(key)
        fact_value = _clean(value)
        if not fact_value:
            return self._fail("empty fact value", key=key)
        if is_sensitive_text(fact_value) or is_sensitive_text(str(key).replace("_", " ")):
            log.warning("Refused to store a fact containing a secret/bank alert")
            return {**self._sensitive_skip(), "key": fact_key}
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable", key=fact_key)

        stamp = self._stamp()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO boss_profile(key, value, category, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = excluded.updated_at
                """,
                (fact_key, fact_value, (category or "general").strip().lower(), stamp),
            )
        log.info("Remembered %s = %r (%s)", fact_key, fact_value[:60], category)
        return {"ok": True, "key": fact_key, "value": fact_value, "category": category, "updated_at": stamp}

    def get_fact(self, key: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM boss_profile WHERE key = ?", (_slug(key),))
        return dict(rows[0]) if rows else None

    def forget_fact(self, key: str) -> Dict[str, Any]:
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable")
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM boss_profile WHERE key = ?", (_slug(key),))
        return {"ok": True, "key": _slug(key), "deleted": cursor.rowcount}

    def profile(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if category:
            rows = self._query(
                "SELECT * FROM boss_profile WHERE category = ? ORDER BY updated_at DESC",
                (category.lower(),),
            )
        else:
            rows = self._query("SELECT * FROM boss_profile ORDER BY category, updated_at DESC")
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #

    def add_rule(self, rule_key: str, rule_text: str, source: str = "user") -> Dict[str, Any]:
        """Store a self-upgrade rule; the same key updates instead of duplicating."""
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable")
        text = _clean(rule_text)[:TEXT_LIMIT]
        if not text:
            return self._fail("empty rule text")
        if is_sensitive_text(text) or is_sensitive_text(str(rule_key).replace("_", " ")):
            log.warning("Refused to store a rule containing a secret/bank alert")
            return {**self._sensitive_skip(), "rule_key": None}
        key = _slug(rule_key)[:64] or f"rule_{_slug(text)}"
        stamp = self._stamp()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO self_upgrades(rule_key, rule_text, source, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rule_key) DO UPDATE SET
                    rule_text = excluded.rule_text,
                    source = excluded.source
                """,
                (key, text, source, stamp),
            )
        log.info("Learned rule %s (%s)", key, source)
        return {"ok": True, "rule_key": key, "rule_text": text, "source": source, "created_at": stamp}

    def rules(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM self_upgrades ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def active_rules(self, limit: int = 8) -> List[Dict[str, Any]]:
        return self.rules(limit=limit)

    # ------------------------------------------------------------------ #
    # Episodes
    # ------------------------------------------------------------------ #

    def remember_turn(self, user_text: str, decision: Any, outcome: str = "ok") -> Dict[str, Any]:
        """Record what Boss said and what ORBIT decided to do."""
        said = _clean(user_text)[:TEXT_LIMIT]
        if not isinstance(decision, dict):
            decision = {}
        if is_sensitive_text(said) or is_sensitive_text(
            json.dumps(decision, ensure_ascii=False, default=str)
        ):
            log.warning("Refused to remember a turn containing a secret/bank alert")
            return {**self._sensitive_skip(), "episode_id": None}
        action = _clean(decision.get("action")) or "speak"
        reply = _clean(decision.get("reply_bn"))[:TEXT_LIMIT]
        try:
            params = json.dumps(decision.get("params") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            params = "{}"

        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable")

        stamp = self._stamp()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO episodes(timestamp, user_text, action, params_json, reply_bn, outcome)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stamp, said, action, params, reply, _clean(outcome) or "ok"),
            )
            episode_id = int(cursor.lastrowid)
        return {"ok": True, "episode_id": episode_id, "timestamp": stamp}

    def recent_episodes(self, limit: int = 5, outcome: Optional[str] = None) -> List[Dict[str, Any]]:
        if outcome:
            rows = self._query(
                "SELECT * FROM episodes WHERE outcome = ? ORDER BY id DESC LIMIT ?", (outcome, limit)
            )
        else:
            rows = self._query("SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def relevant_episodes(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Episodes whose text/reply mentions any meaningful query token."""
        words = _tokens(query)
        if not words or self._conn is None:
            return self.recent_episodes(limit=limit)

        clauses = []
        args: List[Any] = []
        for word in words[:6]:
            clauses.append("(user_text LIKE ? OR reply_bn LIKE ? OR action LIKE ?)")
            like = f"%{word}%"
            args.extend([like, like, like])
        rows = self._query(
            "SELECT * FROM episodes WHERE " + " OR ".join(clauses) + " ORDER BY id DESC LIMIT ?",
            (*args, limit),
        )
        return [dict(row) for row in rows] if rows else self.recent_episodes(limit=limit)

    def episode_count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM episodes")
        return int(rows[0]["n"]) if rows else 0

    def failure_count(self, limit: int = 50) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM episodes WHERE outcome != 'ok' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return int(rows[0]["n"]) if rows else 0

    # ------------------------------------------------------------------ #
    # Learning from Boss's own words
    # ------------------------------------------------------------------ #

    _NAME_RE = re.compile(
        rf"(?:আমার\s+নাম|আমার\s+নাম\s+হলো|আমি\s+নাম|amar\s+na+am?|my\s+name\s+is)"
        rf"\s*(?:হলো|hocche|is)?\s*[:\-]?\s*([^\n।,.;!?]{{2,40}})",
        re.IGNORECASE,
    )
    _DISLIKE_RE = re.compile(
        rf"(?:আমি|i|ami)\s+([^\n।,.;!?]{{2,40}}?)\s+"
        rf"(?:পছন্দ\s+করি\s+না|পছন্দ\s+না|ভালোবাসি\s+না|dislike|hate|posondo\s+kori\s+na)",
        re.IGNORECASE,
    )
    _LIKE_RE = re.compile(
        rf"(?:আমি|i|ami)\s+([^\n।,.;!?]{{2,40}}?)\s+"
        rf"(?:খুব\s+)?(?:পছন্দ\s+করি|ভালোবাসি|like|love|posondo\s+kori)",
        re.IGNORECASE,
    )
    _FAVOURITE_RE = re.compile(
        rf"(?:আমার\s+প্রিয়|my\s+favou?rite)\s+([^\n।,.;!?]{{2,30}}?)\s+(?:হলো|is)\s+([^\n।,.;!?]{{2,40}})",
        re.IGNORECASE,
    )
    _RELATION_RE = re.compile(
        rf"(?:আমার|my)\s+(স্ত্রী|মা|বাবা|ছেলে|মেয়ে|ভাই|বোন|ম্যানেজার|বস|স্যার|ডাক্তার|সহকর্মী|"
        rf"wife|mother|father|son|daughter|brother|sister|manager|boss|sir|doctor|colleague)"
        rf"(?:য়ের|ের|র|এর|'s)?\s*(?:নাম|name)\s*(?:হলো|is)?\s*[:\-]?\s*([^\n।,.;!?]{{2,40}})",
        re.IGNORECASE,
    )
    _HABIT_RE = re.compile(
        rf"(?:প্রতিদিন|প্রতিদিনই|every\s*day|daily)\s+([^\n।,.;!?]{{3,60}})", re.IGNORECASE
    )
    _GOAL_RE = re.compile(
        rf"(?:আমার\s+লক্ষ্য|আমার\s+স্বপ্ন|my\s+goal|my\s+dream)\s*(?:হলো|is)?\s*[:\-]?\s*([^\n।,.;!?]{{3,80}})",
        re.IGNORECASE,
    )
    _PROJECT_RE = re.compile(
        rf"(?:আমার\s+প্রজেক্ট(?:ের)?\s+নাম|আমার\s+প্রজেক্ট|my\s+project\s+is|my\s+project)"
        rf"\s*(?:হলো|is)?\s*[:\-]?\s*([^\n।,.;!?]{{2,60}})",
        re.IGNORECASE,
    )
    _BUILDING_RE = re.compile(
        rf"(?:আমি|i)\s+([^\n।,.;!?]{{2,40}}?)\s+(?:বানাচ্ছি|তৈরি\s+করছি|building|creating)",
        re.IGNORECASE,
    )
    _RULE_TRIGGERS = (
        r"মনে\s+রাখো", r"মনে\s+রাখবেন", r"মনে\s+রাখ", r"এরপর\s+থেকে", r"আজ\s+থেকে",
        r"from\s+now\s+on", r"always\b", r"never\b", r"আর\s+কখনো", r"কখনো\s+না",
        r"remember\s+that", r"remember\s*:",
    )
    _CORRECTION_WORDS = ("ভুল", "ভুলে", "wrong", "mistake", "correct", "সংশোধন", "don't do", "করবে না")

    _RELATION_MAP = {
        "স্ত্রী": "wife", "মা": "mother", "বাবা": "father", "ছেলে": "son",
        "মেয়ে": "daughter", "ভাই": "brother", "বোন": "sister",
        "wife": "wife", "mother": "mother", "father": "father", "son": "son",
        "daughter": "daughter", "brother": "brother", "sister": "sister",
        "ম্যানেজার": "manager", "বস": "boss", "স্যার": "sir", "ডাক্তার": "doctor",
        "সহকর্মী": "colleague", "manager": "manager", "boss": "boss", "sir": "sir",
        "doctor": "doctor", "colleague": "colleague",
    }

    def learn_from_text(self, user_text: str) -> Dict[str, Any]:
        """Deterministically pull facts, preferences and lessons out of a turn."""
        text = _clean(user_text)
        if not text:
            return {"ok": False, "error": "empty text", "facts": [], "rules": []}
        if is_sensitive_text(text):
            log.warning("Refused to learn from a text containing a secret/bank alert")
            return {
                **self._sensitive_skip(),
                "facts": [],
                "rules": [],
                "learned": False,
            }
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable", facts=[], rules=[])

        facts: List[Dict[str, Any]] = []
        rules: List[Dict[str, Any]] = []

        def fact(key: str, value: str, category: str) -> None:
            value = _clean(value)
            if len(value) < 2 or value.lower() in ("না", "no", "yes", "হ্যাঁ"):
                return
            stored = self.remember_fact(key, value, category)
            if stored.get("ok"):
                facts.append(stored)

        match = self._NAME_RE.search(text)
        if match:
            fact("name", match.group(1), "identity")

        disliked_spans: List[Tuple[int, int]] = []
        for match in self._DISLIKE_RE.finditer(text):
            disliked_spans.append(match.span(1))
            fact(f"dislike_{_slug(match.group(1))}", match.group(1), "preference")

        for match in self._LIKE_RE.finditer(text):
            start, _end = match.span(1)
            if any(s <= start < e for s, e in disliked_spans):
                continue
            if text[match.end():match.end() + 4].strip().startswith("না"):
                continue  # "পছন্দ করি না" is a dislike, already captured
            fact(f"like_{_slug(match.group(1))}", match.group(1), "preference")

        match = self._FAVOURITE_RE.search(text)
        if match:
            fact(f"favourite_{_slug(match.group(1))}", match.group(2), "preference")

        for match in self._RELATION_RE.finditer(text):
            relation = self._RELATION_MAP.get(match.group(1).lower(), match.group(1).lower())
            fact(f"{relation}_name", match.group(2), "family")

        match = self._HABIT_RE.search(text)
        if match:
            fact(f"habit_{_slug(match.group(1))}", match.group(1), "habit")

        match = self._GOAL_RE.search(text)
        if match:
            fact("goal", match.group(1), "goal")

        match = self._PROJECT_RE.search(text)
        if match:
            fact("project", match.group(1), "project")
        else:
            match = self._BUILDING_RE.search(text)
            if match:
                fact(f"project_{_slug(match.group(1))}", match.group(1), "project")

        lowered = text.lower()
        trigger = next((t for t in self._RULE_TRIGGERS if re.search(t, lowered)), None)
        correction = any(word in lowered for word in self._CORRECTION_WORDS)
        if trigger or correction:
            rule_text = text[:300]
            if correction:
                rule_text = f"সংশোধন: {rule_text}"
            source = "correction" if correction else "user"
            stored = self.add_rule(f"lesson_{_slug(rule_text)}", rule_text, source=source)
            if stored.get("ok"):
                rules.append(stored)

        return {"ok": True, "facts": facts, "rules": rules, "learned": bool(facts or rules)}

    # ------------------------------------------------------------------ #
    # Context for the brain prompt
    # ------------------------------------------------------------------ #

    def build_memory_context(self, query: str = "", limit: int = 5) -> str:
        """Compact prompt block: Boss facts, learned rules, relevant episodes."""
        if self._conn is None:
            return ""
        profile = self.profile()
        rules = self.active_rules(limit=6)
        episodes = self.relevant_episodes(query, limit=limit) if query else self.recent_episodes(limit=limit)

        lines: List[str] = []
        if profile:
            lines.append("BOSS PROFILE")
            for row in profile[:12]:
                lines.append(f"- {row['key']}: {row['value']} [{row['category']}]")
        if rules:
            lines.append("LEARNED RULES (follow these)")
            for row in rules:
                lines.append(f"- {row['rule_key']}: {row['rule_text']}")
        if episodes:
            lines.append("RECENT TURNS")
            for row in episodes:
                lines.append(
                    f"- [{row['timestamp'][:16]}] Boss: {row['user_text'][:120]} -> "
                    f"{row['action']} ({row['outcome']})"
                )

        text = "\n".join(lines)
        if len(text) > CONTEXT_CHAR_LIMIT:
            text = text[:CONTEXT_CHAR_LIMIT].rsplit("\n", 1)[0] + "\n- …"
        return text

    # ------------------------------------------------------------------ #
    # Daily self-upgrade + Thinker Challenge
    # ------------------------------------------------------------------ #

    _CHALLENGES = (
        "বস, আজ যে কাজটা আপনি নিজে করেছেন — সেটা যদি আমাকে দিয়ে করাতেন, ওই সময়ে কোন বড় কাজটা এগিয়ে নিতেন?",
        "বস, আপনার আজকের সবচেয়ে বড় সিদ্ধান্তটার দ্বিতীয়-ধাপের ফল কী হবে বলে মনে করছেন?",
        "বস, এখনকার পরিকল্পনায় কোন একটা জিনিস বাদ দিলে সবচেয়ে বেশি সময় বাঁচবে বলে মনে হয়?",
        "বস, আপনার প্রতিযোগীরা আজ কোন কাজটা করলে আপনাকে সবচেয়ে বেশি চাপে ফেলত?",
        "বস, আজকের কাজগুলোর মধ্যে কোনটা তিন মাস পরেও গুরুত্বপূর্ণ থাকবে?",
        "বস, এই সিদ্ধান্তটা যদি উল্টো করে ভাবতেন, তাহলে কী হতো?",
    )

    def _top_action(self, limit: int = 20) -> Optional[str]:
        rows = self._query(
            "SELECT action, COUNT(*) AS n FROM episodes WHERE action != 'speak' "
            "GROUP BY action ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        return rows[0]["action"] if rows else None

    def _recent_failed_action(self, limit: int = 20) -> Optional[str]:
        rows = self._query(
            "SELECT action FROM episodes WHERE outcome != 'ok' ORDER BY id DESC LIMIT ?", (limit,)
        )
        for row in rows:
            if row["action"]:
                return str(row["action"])
        return None

    def daily_self_upgrade(self, llm_callable: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
        """Derive today's self-improvement rule and a sharp Bengali challenge."""
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable")

        today = self._clock().date().isoformat()
        total = self.episode_count()
        failures = self.failure_count()
        failed_action = self._recent_failed_action()
        top_action = self._top_action()
        challenge = self._CHALLENGES[int(hashlib.sha1(today.encode()).hexdigest(), 16) % len(self._CHALLENGES)]

        if failures and failed_action:
            rule_text = (
                f"গত সময়ে {failures}টি কাজ ব্যর্থ হয়েছে (সবচেয়ে বেশি: {failed_action}); "
                "ব্যর্থ হলে সাথে সাথে বিকল্প উপায়ে আবার চেষ্টা করব এবং বসকে জানাব।"
            )
            insight = f"{failed_action} কাজটায় বারবার বাধা আসছে — আগে প্রয়োজন ও পারমিশন যাচাই করা দরকার।"
        elif total:
            rule_text = (
                f"গত {total}টি কথোপকথন থেকে শিখেছি; যে কাজগুলো বারবার আসে সেগুলোর জন্য "
                "ছোট শর্টকাট বানিয়ে দ্রুত উত্তর দেব।"
            )
            insight = f"বসের সবচেয়ে বেশি দরকার {top_action or 'সাধারণ কথাবার্তা'} — সেখানেই আগে দক্ষতা বাড়ানো উচিত।"
        else:
            rule_text = "শুরুতে সবকিছু মন দিয়ে শুনব, ভুল হলে সংশোধন মনে রাখব এবং বসকে সংক্ষেপে জানাব।"
            insight = "প্রথম দিনেই অভ্যাস গড়ে ওঠে — ছোট ছোট সঠিক উত্তরই বিশ্বাস তৈরি করে।"

        question = challenge
        source = f"daily:{today}"

        if llm_callable is not None:
            try:
                prompt = (
                    "You are ORBIT's self-improvement engine. Based on this summary, return ONE JSON object "
                    '{"rule_text": "...", "insight_bn": "...", "question_bn": "..."} in Bengali. '
                    f"Episodes: {total}, failures: {failures}, most used action: {top_action}, "
                    f"recent failure: {failed_action}. Keep rule_text under 30 Bengali words and "
                    "question_bn one sharp sentence."
                )
                raw = llm_callable(prompt)
                parsed = json.loads(str(raw).strip())
                if isinstance(parsed, dict):
                    rule_text = _clean(parsed.get("rule_text")) or rule_text
                    insight = _clean(parsed.get("insight_bn")) or insight
                    question = _clean(parsed.get("question_bn")) or question
                    source = f"daily_llm:{today}"
            except Exception as exc:
                log.warning("daily_self_upgrade LLM failed, using deterministic rule: %s", exc)

        rule = self.add_rule(f"daily_{today}", rule_text, source=source)
        note = self.add_thinker_note(topic=f"daily:{today}", insight_bn=insight, question_bn=question)

        return {
            "ok": bool(rule.get("ok")),
            "date": today,
            "rule": rule,
            "note": note,
            "insight_bn": insight,
            "question_bn": question,
            "stats": {"episodes": total, "failures": failures, "top_action": top_action},
        }

    # ------------------------------------------------------------------ #
    # Thinker notes
    # ------------------------------------------------------------------ #

    def add_thinker_note(self, topic: str, insight_bn: str, question_bn: str = "") -> Dict[str, Any]:
        if self._conn is None:
            return self._fail(self.last_error or "memory unavailable")
        stamp = self._stamp()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO thinker_notes(topic, insight_bn, question_bn, created_at) VALUES (?, ?, ?, ?)",
                (_clean(topic)[:120] or "general", _clean(insight_bn)[:TEXT_LIMIT], _clean(question_bn)[:TEXT_LIMIT], stamp),
            )
            note_id = int(cursor.lastrowid)
        return {
            "ok": True,
            "id": note_id,
            "topic": _clean(topic),
            "insight_bn": _clean(insight_bn),
            "question_bn": _clean(question_bn),
            "created_at": stamp,
        }

    def thinker_notes(self, limit: int = 5) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM thinker_notes ORDER BY id DESC LIMIT ?", (max(1, limit),)
        )
        return [dict(row) for row in rows]

    def latest_thinker_note(self) -> Optional[Dict[str, Any]]:
        notes = self.thinker_notes(limit=1)
        return notes[0] if notes else None

    def stats(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "db_path": str(self.db_path),
            "episodes": self.episode_count(),
            "facts": len(self.profile()),
            "rules": len(self.rules(limit=1000)),
            "notes": len(self.thinker_notes(limit=1000)),
            "failures": self.failure_count(),
        }


# --------------------------------------------------------------------------- #
# Shared instance
# --------------------------------------------------------------------------- #

_shared: Optional[MemoryEngine] = None
_shared_lock = threading.Lock()


def get_memory(db_path: Optional[Any] = None) -> MemoryEngine:
    """Process-wide MemoryEngine (brain uses this by default)."""
    global _shared
    with _shared_lock:
        if _shared is None or (db_path is not None and Path(db_path) != _shared.db_path):
            _shared = MemoryEngine(db_path=db_path)
        return _shared


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    engine = MemoryEngine()
    if len(sys.argv) > 1 and sys.argv[1] == "upgrade":
        print(json.dumps(engine.daily_self_upgrade(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "hear":
        print(json.dumps(engine.learn_from_text(" ".join(sys.argv[2:])), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
        print(engine.build_memory_context("আজ কী কাজ"))
