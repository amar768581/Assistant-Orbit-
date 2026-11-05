"""
tests/test_memory_engine.py — SQLite memory, self-upgrade extraction and the
Thinker Challenge. Every test uses a throwaway database and a fixed clock.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from memory_engine import DEFAULT_DB_PATH, MemoryEngine

FIXED_TIME = datetime(2026, 11, 5, 9, 30, 0, tzinfo=timezone.utc)
DECISION = {"action": "torch", "params": {"state": "on"}, "reply_bn": "বস, টর্চ জ্বালাচ্ছি।"}


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orbit_mem_")
        self.db_path = Path(self.tmp) / "memory.db"
        self.mem = MemoryEngine(db_path=self.db_path, clock=lambda: FIXED_TIME)

    def tearDown(self):
        self.mem.close()


# --------------------------------------------------------------------------- #
# Schema / lifecycle
# --------------------------------------------------------------------------- #

class TestLifecycle(MemoryCase):
    def test_tables_created(self):
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"episodes", "boss_profile", "self_upgrades", "thinker_notes"} <= names)

    def test_default_path_is_home_orbit(self):
        self.assertEqual(DEFAULT_DB_PATH, Path.home() / ".orbit" / "orbit_memory.db")

    def test_stats_and_empty_things(self):
        stats = self.mem.stats()
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["episodes"], 0)
        self.assertEqual(stats["facts"], 0)
        self.assertEqual(self.mem.build_memory_context("some query"), "")
        self.assertIsNone(self.mem.latest_thinker_note())

    def test_unwritable_path_reports_error_not_crash(self):
        bad = MemoryEngine(db_path=Path(self.tmp) / "memory.db" / "nested" / "x.db")
        self.assertFalse(bad.ok)
        self.assertFalse(bad.remember_fact("name", "Rasel")["ok"])
        self.assertFalse(bad.remember_turn("hi", DECISION)["ok"])
        self.assertEqual(bad.build_memory_context("hi"), "")
        bad.close()


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #

class TestEpisodes(MemoryCase):
    def test_remember_turn(self):
        result = self.mem.remember_turn("টর্চ জালাও", DECISION, outcome="ok")
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["episode_id"], int)
        self.assertEqual(result["timestamp"], FIXED_TIME.isoformat(timespec="seconds"))

        episodes = self.mem.recent_episodes(limit=5)
        self.assertEqual(len(episodes), 1)
        row = episodes[0]
        self.assertEqual(row["user_text"], "টর্চ জালাও")
        self.assertEqual(row["action"], "torch")
        self.assertEqual(row["reply_bn"], "বস, টর্চ জ্বালাচ্ছি।")
        self.assertEqual(json.loads(row["params_json"]), {"state": "on"})

    def test_remember_turn_survives_odd_decisions(self):
        for decision in (None, "nonsense", {"action": None, "params": {"x": object()}}):
            self.assertTrue(self.mem.remember_turn("কিছু", decision)["ok"])
        rows = self.mem.recent_episodes(limit=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["action"], "speak")

    def test_failure_count_and_filter(self):
        self.mem.remember_turn("a", DECISION)
        self.mem.remember_turn("b", DECISION, outcome="failed")
        self.mem.remember_turn("c", DECISION, outcome="failed")
        self.assertEqual(self.mem.failure_count(), 2)
        self.assertEqual(len(self.mem.recent_episodes(outcome="failed")), 2)

    def test_relevant_episodes_match_and_fallback(self):
        self.mem.remember_turn("ব্যাটারি কত", {"action": "battery", "params": {}, "reply_bn": "৮৫%"})
        self.mem.remember_turn("গান চালাও", {"action": "speak", "params": {}, "reply_bn": "ঠিক আছে"})
        hits = self.mem.relevant_episodes("ব্যাটারি")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["action"], "battery")
        # No token match -> recent turns instead of nothing.
        self.assertEqual(len(self.mem.relevant_episodes("সম্পূর্ণ অচেনা শব্দ")), 2)


# --------------------------------------------------------------------------- #
# Profile facts
# --------------------------------------------------------------------------- #

class TestProfile(MemoryCase):
    def test_remember_and_update_fact(self):
        first = self.mem.remember_fact("Name", "Rasel", "identity")
        self.assertTrue(first["ok"])
        self.assertEqual(first["key"], "name")

        second = self.mem.remember_fact("name", "Rasel Ahmed", "identity")
        self.assertTrue(second["ok"])
        self.assertEqual(self.mem.get_fact("name")["value"], "Rasel Ahmed")
        self.assertEqual(len(self.mem.profile()), 1)

    def test_bengali_key_hashed(self):
        result = self.mem.remember_fact("প্রিয় খাবার", "বিরিয়ানি", "preference")
        self.assertTrue(result["key"].startswith("bn_"))
        self.assertEqual(self.mem.get_fact(result["key"])["value"], "বিরিয়ানি")

    def test_empty_value_and_forget(self):
        self.assertFalse(self.mem.remember_fact("x", "   ")["ok"])
        self.mem.remember_fact("goal", "ORBIT চালু করা")
        self.assertTrue(self.mem.forget_fact("goal")["ok"])
        self.assertIsNone(self.mem.get_fact("goal"))

    def test_category_filter(self):
        self.mem.remember_fact("name", "Rasel", "identity")
        self.mem.remember_fact("like_tea", "চা", "preference")
        self.assertEqual(len(self.mem.profile("preference")), 1)
        self.assertEqual(self.mem.profile("identity")[0]["key"], "name")


# --------------------------------------------------------------------------- #
# learn_from_text
# --------------------------------------------------------------------------- #

class TestLearning(MemoryCase):
    def test_name_bengali_and_banglish(self):
        self.assertTrue(self.mem.learn_from_text("আমার নাম রাসেল।")["learned"])
        self.assertEqual(self.mem.get_fact("name")["value"], "রাসেল")
        self.mem.learn_from_text("my name is Rasel Ahmed")
        self.assertEqual(self.mem.get_fact("name")["value"], "Rasel Ahmed")

    def test_like_and_dislike(self):
        self.mem.learn_from_text("আমি চা পছন্দ করি")
        likes = [f for f in self.mem.profile() if f["key"].startswith("like_")]
        self.assertEqual(len(likes), 1)
        self.assertEqual(likes[0]["value"], "চা")
        self.assertEqual(likes[0]["category"], "preference")

        before = len(self.mem.profile())
        self.mem.learn_from_text("আমি ভোরে উঠতে পছন্দ করি না")
        prefs = [f for f in self.mem.profile() if f["category"] == "preference"]
        self.assertEqual(len(self.mem.profile()), before + 1)
        self.assertTrue(any(f["key"].startswith("dislike_") for f in prefs))

    def test_favourite_family_habit_goal_project(self):
        self.mem.learn_from_text("আমার প্রিয় খাবার হলো বিরিয়ানি")
        self.mem.learn_from_text("আমার মায়ের নাম রেহানা")
        self.mem.learn_from_text("আমি প্রতিদিন সকালে হাঁটি")
        self.mem.learn_from_text("আমার লক্ষ্য একটি স্টার্টআপ দাঁড় করানো")
        self.mem.learn_from_text("আমার প্রজেক্টের নাম অরবিট")

        keys = {f["key"]: f for f in self.mem.profile()}
        self.assertTrue(any(k.startswith("favourite_") for k in keys))
        self.assertEqual(keys["mother_name"]["value"], "রেহানা")
        self.assertEqual(keys["mother_name"]["category"], "family")
        self.assertTrue(any(k.startswith("habit_") for k in keys))
        self.assertEqual(keys["goal"]["category"], "goal")
        self.assertEqual(keys["project"]["value"], "অরবিট")

    def test_building_pattern(self):
        self.mem.learn_from_text("আমি একটা অ্যাপ বানাচ্ছি")
        project = [f for f in self.mem.profile() if f["category"] == "project"]
        self.assertTrue(project)
        self.assertIn("অ্যাপ", project[0]["value"])

    def test_rule_from_remember_and_correction(self):
        self.mem.learn_from_text("মনে রাখো, প্রতিবার কল দেওয়ার আগে নামটা নিশ্চিত করবে")
        rules = self.mem.rules()
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0]["rule_key"].startswith("lesson_"))
        self.assertEqual(rules[0]["source"], "user")

        self.mem.learn_from_text("এটা ভুল করেছ, ওভাবে করবে না")
        rules = {r["rule_key"]: r for r in self.mem.rules()}
        self.assertEqual(len(rules), 2)
        self.assertTrue(any(r["rule_text"].startswith("সংশোধন:") for r in rules.values()))
        self.assertTrue(any(r["source"] == "correction" for r in rules.values()))

    def test_same_rule_not_duplicated(self):
        text = "মনে রাখো সবসময় বাংলায় উত্তর দেবে"
        self.mem.learn_from_text(text)
        self.mem.learn_from_text(text)
        self.assertEqual(len(self.mem.rules()), 1)

    def test_empty_or_noisy_text_learns_nothing(self):
        self.assertFalse(self.mem.learn_from_text("")["ok"])
        result = self.mem.learn_from_text("আজ আবহাওয়া ভালো")
        self.assertTrue(result["ok"])
        self.assertFalse(result["learned"])
        self.assertEqual(self.mem.profile(), [])


# --------------------------------------------------------------------------- #
# Context building
# --------------------------------------------------------------------------- #

class TestContext(MemoryCase):
    def test_context_contains_profile_rules_and_turns(self):
        self.mem.remember_fact("name", "Rasel", "identity")
        self.mem.add_rule("lesson_calls", "কল দেওয়ার আগে নাম নিশ্চিত করব")
        self.mem.remember_turn("টর্চ জালাও", DECISION)

        context = self.mem.build_memory_context("টর্চ")
        self.assertIn("BOSS PROFILE", context)
        self.assertIn("name: Rasel", context)
        self.assertIn("LEARNED RULES", context)
        self.assertIn("কল দেওয়ার আগে নাম নিশ্চিত করব", context)
        self.assertIn("RECENT TURNS", context)
        self.assertIn("টর্চ জালাও", context)

    def test_context_is_capped(self):
        for i in range(80):
            self.mem.remember_fact(f"fact_{i}", f"value number {i} " * 5, "general")
            self.mem.add_rule(f"rule_{i}", f"rule text {i} " * 20)
        context = self.mem.build_memory_context("value", limit=5)
        self.assertLessEqual(len(context), 1900)
        self.assertTrue(context.endswith("…"))


# --------------------------------------------------------------------------- #
# Daily self-upgrade
# --------------------------------------------------------------------------- #

class TestDailyUpgrade(MemoryCase):
    def test_rule_and_challenge_from_failures(self):
        self.mem.remember_turn("কল দাও", {"action": "call", "params": {}, "reply_bn": "বস, কল দিচ্ছি।"}, outcome="failed")
        self.mem.remember_turn("কল দাও", {"action": "call", "params": {}, "reply_bn": "বস, কল দিচ্ছি।"})

        result = self.mem.daily_self_upgrade()
        self.assertTrue(result["ok"])
        self.assertEqual(result["date"], "2026-11-05")
        self.assertIn("call", result["stats"]["top_action"])
        self.assertIn("ব্যর্থ", result["rule"]["rule_text"])
        self.assertTrue(result["rule"]["rule_key"].startswith("daily_"))
        self.assertTrue(result["question_bn"].endswith("?"))
        self.assertIn("বস", result["question_bn"])

        note = self.mem.latest_thinker_note()
        self.assertEqual(note["question_bn"], result["question_bn"])
        self.assertTrue(note["insight_bn"])

    def test_first_day_without_history(self):
        result = self.mem.daily_self_upgrade()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["episodes"], 0)
        self.assertTrue(result["rule"]["rule_text"])
        self.assertTrue(result["insight_bn"])

    def test_same_day_upgrade_updates_one_rule(self):
        self.mem.daily_self_upgrade()
        self.mem.daily_self_upgrade()
        daily = [r for r in self.mem.rules() if r["rule_key"].startswith("daily_")]
        self.assertEqual(len(daily), 1)

    def test_llm_callable_used(self):
        payload = {"rule_text": "বসের কাজ আগে শেষ করব", "insight_bn": "সময়ই মূলধন।", "question_bn": "বস, আজ কী বাদ দেবেন?"}
        result = self.mem.daily_self_upgrade(llm_callable=lambda prompt: json.dumps(payload, ensure_ascii=False))
        self.assertEqual(result["rule"]["rule_text"], "বসের কাজ আগে শেষ করব")
        self.assertEqual(result["question_bn"], "বস, আজ কী বাদ দেবেন?")
        self.assertTrue(result["rule"]["source"].startswith("daily_llm"))

    def test_llm_failure_falls_back(self):
        def broken(_prompt):
            raise RuntimeError("no network")

        result = self.mem.daily_self_upgrade(llm_callable=broken)
        self.assertTrue(result["ok"])
        self.assertTrue(result["rule"]["rule_text"])
        self.assertFalse(result["rule"]["source"].startswith("daily_llm"))

    def test_thinker_notes(self):
        note = self.mem.add_thinker_note("focus", "একসাথে সব নয়।", "বস, আজ কোনটি সবচেয়ে জরুরি?")
        self.assertTrue(note["ok"])
        self.assertEqual(self.mem.thinker_notes(limit=1)[0]["id"], note["id"])
        self.assertEqual(self.mem.latest_thinker_note()["topic"], "focus")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
