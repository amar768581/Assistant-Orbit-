"""
tests/test_brain.py — the Boss persona guarantee, memory integration and the
Thinker Catalyst contract. No network: providers and memory are fakes.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import brain
from brain import (
    BOSS_ADDRESS,
    Brain,
    build_user_prompt,
    ensure_boss_address,
    validate_decision,
)


class FakeProvider:
    """Returns queued replies (str) or raises queued exceptions."""

    name = "fake"

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.replies:
            raise RuntimeError("no more fake replies")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeMemory:
    def __init__(self, context: str = "BOSS PROFILE\n- name: Rasel") -> None:
        self.context = context
        self.learned = []
        self.turns = []
        self.fail_read = False
        self.fail_write = False

    def learn_from_text(self, text):
        self.learned.append(text)
        return {"ok": True, "facts": [], "rules": []}

    def build_memory_context(self, query, limit=5):
        if self.fail_read:
            raise RuntimeError("db locked")
        return self.context

    def remember_turn(self, text, decision, outcome="ok"):
        if self.fail_write:
            raise RuntimeError("disk full")
        self.turns.append((text, decision, outcome))
        return {"ok": True, "episode_id": 1}


def decision_json(action="torch", params=None, reply="টর্চ জ্বালাচ্ছি।") -> str:
    return json.dumps({"action": action, "params": params or {}, "reply_bn": reply}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Boss persona guard
# --------------------------------------------------------------------------- #

class TestBossPersona(unittest.TestCase):
    def test_ensure_boss_address(self):
        self.assertEqual(ensure_boss_address(""), "")
        self.assertEqual(ensure_boss_address("টর্চ জ্বালাচ্ছি।"), "বস, টর্চ জ্বালাচ্ছি।")
        self.assertEqual(ensure_boss_address("বস, কল দিচ্ছি।"), "বস, কল দিচ্ছি।")

    def test_every_action_gets_boss(self):
        for action, params in (
            ("torch", {"state": "on"}),
            ("battery", {}),
            ("volume", {"level": 40}),
            ("intruder_check", {"capture": True}),
            ("speak", {"text": "আজ আবহাওয়া ভালো।"}),
        ):
            result = validate_decision({"action": action, "params": params, "reply_bn": "ঠিক আছে।"})
            self.assertEqual(result["action"], action)
            self.assertIn(BOSS_ADDRESS, result["reply_bn"], action)

    def test_existing_boss_not_duplicated(self):
        result = validate_decision({"action": "torch", "params": {"state": "on"}, "reply_bn": "বস, টর্চ জ্বালাচ্ছি।"})
        self.assertEqual(result["reply_bn"], "বস, টর্চ জ্বালাচ্ছি।")
        self.assertEqual(result["reply_bn"].count("বস"), 1)

    def test_missing_reply_gets_default_with_boss(self):
        result = validate_decision({"action": "battery", "params": {}})
        self.assertIn(BOSS_ADDRESS, result["reply_bn"])

    def test_unknown_action_and_clarification_have_boss(self):
        unknown = validate_decision({"action": "hack_nasa", "params": {}, "reply_bn": ""})
        self.assertEqual(unknown["action"], "speak")
        self.assertIn(BOSS_ADDRESS, unknown["reply_bn"])

        clarify = validate_decision({"action": "call", "params": {}, "reply_bn": ""})
        self.assertEqual(clarify["action"], "speak")
        self.assertIn(BOSS_ADDRESS, clarify["reply_bn"])

    def test_params_untouched_by_guard(self):
        result = validate_decision({"action": "volume", "params": {"level": 40}, "reply_bn": "ভলিউম বাড়াচ্ছি।"})
        self.assertEqual(result["params"], {"level": 40})

    def test_system_prompt_has_persona_and_catalyst(self):
        self.assertIn("BOSS PERSONA", brain.SYSTEM_PROMPT)
        self.assertIn("THINKER CATALYST", brain.SYSTEM_PROMPT)
        self.assertIn("EVERY reply_bn MUST contain", brain.SYSTEM_PROMPT)


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

class TestPrompt(unittest.TestCase):
    def test_memory_section_included_and_ordered(self):
        prompt = build_user_prompt("কেমন আছ", memory_context="BOSS PROFILE\n- name: Rasel")
        self.assertIn("MEMORY (established facts", prompt)
        self.assertIn("name: Rasel", prompt)
        self.assertLess(prompt.index("MEMORY"), prompt.index("USER SAID"))

    def test_memory_section_omitted_when_empty(self):
        for value in (None, "", "   "):
            prompt = build_user_prompt("কেমন আছ", memory_context=value)
            self.assertNotIn("MEMORY (established", prompt)

    def test_history_and_context_still_work(self):
        prompt = build_user_prompt(
            "টর্চ জালাও",
            context={"battery": 20},
            history=[{"role": "user", "content": "আগের কথা"}],
            memory_context="BOSS PROFILE\n- name: Rasel",
        )
        self.assertIn("RECENT CONVERSATION", prompt)
        self.assertIn("DEVICE CONTEXT", prompt)
        self.assertIn("আগের কথা", prompt)
        self.assertIn("MEMORY", prompt)


# --------------------------------------------------------------------------- #
# think() + memory
# --------------------------------------------------------------------------- #

class TestThinkWithMemory(unittest.TestCase):
    def make_brain(self, provider, memory=None, **kwargs):
        return Brain(providers=[provider], memory=memory, **kwargs)

    def test_learns_then_injects_then_records(self):
        provider = FakeProvider([decision_json()])
        memory = FakeMemory()
        result = self.make_brain(provider, memory).think("টর্চ জালাও")

        self.assertEqual(result["action"], "torch")
        self.assertEqual(result["params"], {"state": "on"})
        self.assertIn(BOSS_ADDRESS, result["reply_bn"])

        # learned before the prompt was built, context injected
        self.assertEqual(memory.learned, ["টর্চ জালাও"])
        self.assertIn("name: Rasel", provider.calls[0][1])

        # recorded once, after deciding, with the final decision
        self.assertEqual(len(memory.turns), 1)
        text, decision, outcome = memory.turns[0]
        self.assertEqual(text, "টর্চ জালাও")
        self.assertEqual(decision["action"], "torch")
        self.assertEqual(outcome, "ok")

    def test_shared_memory_is_default(self):
        memory = FakeMemory(context="BOSS PROFILE\n- name: Default")
        with mock.patch("memory_engine.get_memory", return_value=memory):
            provider = FakeProvider([decision_json()])
            result = Brain(providers=[provider]).think("ব্যাটারি কত")
        self.assertEqual(result["action"], "torch")  # whatever the provider said
        self.assertIn("name: Default", provider.calls[0][1])
        self.assertEqual(len(memory.turns), 1)

    def test_memory_read_failure_does_not_break_thinking(self):
        provider = FakeProvider([decision_json()])
        memory = FakeMemory()
        memory.fail_read = True
        result = self.make_brain(provider, memory).think("টর্চ জালাও")
        self.assertEqual(result["action"], "torch")
        self.assertEqual(len(memory.turns), 1)

    def test_memory_write_failure_does_not_break_thinking(self):
        provider = FakeProvider([decision_json()])
        memory = FakeMemory()
        memory.fail_write = True
        result = self.make_brain(provider, memory).think("টর্চ জালাও")
        self.assertEqual(result["action"], "torch")

    def test_no_provider_records_failure_with_boss_address(self):
        memory = FakeMemory()
        result = Brain(providers=[], memory=memory).think("টর্চ জালাও")
        self.assertEqual(result["action"], "speak")
        self.assertIn(BOSS_ADDRESS, result["reply_bn"])
        self.assertEqual(memory.turns[0][2], "failed")

    def test_repair_attempt_still_records(self):
        provider = FakeProvider(["not json at all", decision_json("battery", {}, "ব্যাটারি দেখছি।")])
        memory = FakeMemory()
        result = self.make_brain(provider, memory).think("ব্যাটারি কত")
        self.assertEqual(result["action"], "battery")
        self.assertEqual(len(memory.turns), 1)

    def test_total_failure_returns_boss_apology(self):
        provider = FakeProvider([RuntimeError("down"), RuntimeError("down"), RuntimeError("down"), RuntimeError("down")])
        memory = FakeMemory()
        result = self.make_brain(provider, memory).think("কিছু একটা")
        self.assertEqual(result["action"], "speak")
        self.assertIn(BOSS_ADDRESS, result["reply_bn"])
        self.assertEqual(result["reply_bn"], brain.FAILURE_REPLY)
        self.assertEqual(memory.turns[0][2], "failed")

    def test_memory_opt_out(self):
        provider = FakeProvider([decision_json()])
        with mock.patch("memory_engine.get_memory", side_effect=AssertionError("must not connect")):
            result = Brain(providers=[provider], use_memory=False).think("টর্চ জালাও")
        self.assertEqual(result["action"], "torch")

    def test_empty_text_does_not_touch_memory(self):
        memory = FakeMemory()
        result = Brain(providers=[FakeProvider([])], memory=memory).think("   ")
        self.assertEqual(result["action"], "speak")
        self.assertIn(BOSS_ADDRESS, result["reply_bn"])
        self.assertEqual(memory.learned, [])
        self.assertEqual(memory.turns, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
