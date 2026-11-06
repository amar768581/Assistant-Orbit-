"""
brain.py — ORBIT's decision engine.

Takes whatever the user said (Bengali / English / Banglish) and returns ONE
strictly-validated instruction for the device layer:

    {"action": "torch", "params": {"state": "on"}, "reply_bn": "টর্চ জ্বালাচ্ছি।"}

Providers, in order:
    1. Google **Gemini 2.5 Flash**  (primary, JSON mode)
    2. **Groq Llama-3**            (fallback, JSON mode)

The model never touches the phone: ``params`` is validated against a fixed
schema here, so a hallucinated action can never reach Termux:API.

    from brain import Brain
    brain = Brain()
    result = brain.think("রহিমকে কল দাও")
    result["action"]   # "call"
    result["params"]   # {"contact": "রহিম"}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # requests is a hard dependency at runtime, but import must not explode.
    import requests
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    requests = None  # type: ignore[assignment]

from config import Settings, settings as default_settings
from security_policy import (
    BANKING_REFUSAL_BN,
    BLOCKED_FINANCIAL_APPS,
    BLOCKED_PACKAGES,
    SECRET_REFUSAL_BN,
    contains_secret,
    is_financial_request,
    is_financial_target,
)

log = logging.getLogger("orbit.brain")

# --------------------------------------------------------------------------- #
# Provider endpoints
# --------------------------------------------------------------------------- #

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

REQUEST_TIMEOUT = 30.0
RETRY_STATUSES = {429, 500, 502, 503, 504}

# --------------------------------------------------------------------------- #
# The action contract
# --------------------------------------------------------------------------- #

SUPPORTED_ACTIONS: Sequence[str] = (
    "call",
    "answer_call",
    "sms",
    "whatsapp",
    "torch",
    "battery",
    "volume",
    "wifi",
    "intruder_check",
    "open_app",
    "read_screen",
    "market_news",
    "speak",
)

ACTION_SPECS: Dict[str, Dict[str, str]] = {
    "call": {
        "contact": "person's name as the user said it (and 'number' if a digit string was given)",
    },
    "answer_call": {"note": "no params — answer/pick up the incoming call"},
    "sms": {"contact": "recipient name or number", "message": "SMS body"},
    "whatsapp": {"contact": "recipient name or number", "message": "message body"},
    "torch": {"state": "'on' or 'off'"},
    "battery": {"note": "no params"},
    "volume": {
        "level": "integer 0-100, when a specific level was asked",
        "direction": "'up' | 'down' | 'mute' | 'unmute'",
    },
    "wifi": {"state": "'on' or 'off'"},
    "intruder_check": {"capture": "true to silently take a photo, else false"},
    "open_app": {"app": "app name, e.g. 'YouTube', 'ক্যামেরা'"},
    "read_screen": {"note": "no params — read what is on screen aloud"},
    "market_news": {"topic": "'crypto' | 'stocks' | 'general' (optional)"},
    "speak": {"text": "the answer to say out loud, in Bengali"},
}

# Aliases the LLM sometimes emits instead of the canonical action name.
ACTION_ALIASES: Dict[str, str] = {
    "make_call": "call",
    "dial": "call",
    "phone_call": "call",
    "answer": "answer_call",
    "pick_up": "answer_call",
    "receive_call": "answer_call",
    "send_sms": "sms",
    "text_message": "sms",
    "message": "sms",
    "send_whatsapp": "whatsapp",
    "whatsapp_message": "whatsapp",
    "flashlight": "torch",
    "torch_on": "torch",
    "toggle_torch": "torch",
    "light": "torch",
    "battery_status": "battery",
    "battery_level": "battery",
    "volume_control": "volume",
    "set_volume": "volume",
    "wifi_control": "wifi",
    "toggle_wifi": "wifi",
    "intruder": "intruder_check",
    "intruder_alert": "intruder_check",
    "security_check": "intruder_check",
    "launch_app": "open_app",
    "openapp": "open_app",
    "screenshot": "read_screen",
    "readscreen": "read_screen",
    "screen": "read_screen",
    "news": "market_news",
    "market": "market_news",
    "stock_news": "market_news",
    "say": "speak",
    "talk": "speak",
    "chat": "speak",
    "answer_question": "speak",
    "none": "speak",
}

# Used when the model forgets reply_bn (never left empty for the TTS layer).
DEFAULT_REPLIES: Dict[str, str] = {
    "call": "কল দিচ্ছি।",
    "answer_call": "কল ধরছি।",
    "sms": "মেসেজ পাঠাচ্ছি।",
    "whatsapp": "হোয়াটসঅ্যাপে মেসেজ পাঠাচ্ছি।",
    "torch": "টর্চ নিয়ন্ত্রণ করছি।",
    "battery": "ব্যাটারির অবস্থা দেখছি।",
    "volume": "ভলিউম বদলাচ্ছি।",
    "wifi": "ওয়াইফাই নিয়ন্ত্রণ করছি।",
    "intruder_check": "ঘরের চারপাশ চেক করছি।",
    "open_app": "অ্যাপ খুলছি।",
    "read_screen": "স্ক্রিন পড়ছি।",
    "market_news": "বাজার ও খবর দেখছি।",
    "speak": "বলছি।",
}

FAILURE_REPLY = "বস, দুঃখিত, আমি এখন ঠিকমতো বুঝতে পারিনি। আরেকবার বলবেন?"

# ORBIT never speaks to its owner without this word (see ``_boss_guard``).
BOSS_ADDRESS = "বস"

SYSTEM_PROMPT = """You are ORBIT, a Bengali-first personal AI assistant running on the user's Android phone through Termux.
The user speaks Bengali, English, or Banglish (Bengali written in Latin letters).

ALWAYS answer with ONE JSON object and nothing else — no markdown, no prose outside the JSON:
{"action": "<one of the actions>", "params": {<action params>}, "reply_bn": "<Bengali sentence>"}

ACTIONS AND PARAMS
- call            {"contact": "<name as spoken>", "number": "<digits, only if the user gave a number>"}
- answer_call     {}                                   (pick up / receive the ringing call)
- sms             {"contact": "<name or number>", "message": "<SMS body>"}
- whatsapp        {"contact": "<name or number>", "message": "<message body>"}
- torch           {"state": "on" | "off"}
- battery         {}
- volume          {"level": 0-100}   or   {"direction": "up" | "down" | "mute" | "unmute"}
- wifi            {"state": "on" | "off"}
- intruder_check  {"capture": true | false}            (silent photo of an intruder)
- open_app        {"app": "<installed app name>"}
- read_screen     {}                                   (read the current screen aloud)
- market_news     {"topic": "crypto" | "stocks" | "general"}
- speak           {"text": "<answer to say out loud>"}  (chat, questions, anything not device control)

BOSS PERSONA (non-negotiable)
- The user is your BOSS ("বস"). You are loyal, warm, respectful and calm — never robotic, never servile.
- EVERY reply_bn MUST contain the word "বস" (e.g. "বস, টর্চ জ্বালাচ্ছি।"). It is a promise, not decoration.
- Never argue with Boss, never blame Boss. If something failed, say what you will try next.

THINKER CATALYST
- When Boss shares an idea, plan, strategy, business thought or open question (usually "speak"),
  answer as a loyal strategist AND add ONE sharp Bengali line: a second-order effect, a hidden
  risk, or a probing question that makes Boss think harder. One sentence only — no lectures.
- Do not add the catalyst to simple device commands (torch, volume, calls).

RULES
1. reply_bn is ALWAYS in Bengali script, natural and conversational, 4-20 words — that is what the phone speaks.
2. If the request is a question, chit-chat, or general knowledge, use "speak" and put the full answer in params.text AND a short version in reply_bn.
3. Never invent phone numbers, contacts, or app names that the user did not mention.
4. One intent per reply. If several are asked, do the first one and mention it in reply_bn.
5. Banglish in, Bengali out: "torch jalao" -> {"action":"torch","params":{"state":"on"},"reply_bn":"টর্চ জ্বালাচ্ছি।"}
6. Ambiguous device command -> pick the closest action; genuinely unclear -> "speak" asking a short clarifying question.
7. Output valid JSON only. No trailing commas, no comments, no code fences.
8. If MEMORY is provided below the conversation, treat it as established truth: use Boss's name,
   preferences and learned rules, and never contradict a learned correction.
9. reply_bn must always address Boss by "বস" — this is checked automatically, so write it naturally."""


class BrainError(RuntimeError):
    """Raised when no provider could produce a usable decision."""


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class _Provider:
    """Minimal chat-completion interface used by :class:`Brain`."""

    name = "provider"

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class GeminiProvider(_Provider):
    """Google Gemini 2.5 Flash via the REST API (JSON response mode)."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_MODEL,
        timeout: float = REQUEST_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._session = session

    def _post(self, payload: Dict[str, Any], timeout: float) -> Any:
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        url = GEMINI_URL.format(model=self.model)
        if self._session is not None:
            return self._session.post(url, headers=headers, json=payload, timeout=timeout)
        return requests.post(url, headers=headers, json=payload, timeout=timeout)

    def complete(self, system: str, user: str) -> str:
        if requests is None and self._session is None:
            raise BrainError("requests is not installed (pip install -r requirements.txt)")

        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.35,
                "topP": 0.9,
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
                # Keep latency low enough for a voice loop.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

        response = self._send_with_retries(payload)

        # Older API revisions reject thinkingConfig — retry once without it.
        if response.status_code == 400 and "thinking" in (response.text or "").lower():
            log.debug("Gemini rejected thinkingConfig; retrying without it")
            payload["generationConfig"].pop("thinkingConfig", None)
            response = self._send_with_retries(payload)

        if response.status_code != 200:
            raise BrainError(
                f"Gemini HTTP {response.status_code}: {(response.text or '')[:300]}"
            )

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback", {}).get("blockReason")
            raise BrainError(f"Gemini returned no candidates (blockReason={feedback})")

        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        if not text:
            raise BrainError(
                f"Gemini returned empty text (finishReason={candidate.get('finishReason')})"
            )
        return text

    def _send_with_retries(self, payload: Dict[str, Any]) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self._post(payload, self.timeout)
            except Exception as exc:  # network / timeout
                last_exc = exc
                log.warning("Gemini request failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(1.2 * (attempt + 1))
                continue

            if response.status_code in RETRY_STATUSES and attempt < 2:
                log.warning(
                    "Gemini HTTP %s, retrying (attempt %d/3)",
                    response.status_code,
                    attempt + 1,
                )
                time.sleep(1.2 * (attempt + 1))
                continue
            return response

        raise BrainError(f"Gemini unreachable after retries: {last_exc}")


class GroqProvider(_Provider):
    """Groq Llama-3 chat completions (OpenAI-compatible, JSON mode)."""

    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = GROQ_MODEL,
        timeout: float = REQUEST_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._session = session

    def _post(self, payload: Dict[str, Any], timeout: float) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self._session is not None:
            return self._session.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)
        return requests.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)

    def complete(self, system: str, user: str) -> str:
        if requests is None and self._session is None:
            raise BrainError("requests is not installed (pip install -r requirements.txt)")

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.35,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
        }

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self._post(payload, self.timeout)
            except Exception as exc:
                last_exc = exc
                log.warning("Groq request failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(1.2 * (attempt + 1))
                continue

            if response.status_code in RETRY_STATUSES and attempt < 2:
                log.warning("Groq HTTP %s, retrying (attempt %d/3)", response.status_code, attempt + 1)
                time.sleep(1.2 * (attempt + 1))
                continue

            if response.status_code != 200:
                raise BrainError(
                    f"Groq HTTP {response.status_code}: {(response.text or '')[:300]}"
                )

            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise BrainError("Groq returned no choices")
            content = (choices[0].get("message") or {}).get("content") or ""
            if not content.strip():
                raise BrainError("Groq returned empty content")
            return content.strip()

        raise BrainError(f"Groq unreachable after retries: {last_exc}")


# --------------------------------------------------------------------------- #
# JSON extraction + validation
# --------------------------------------------------------------------------- #

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def _extract_json(raw: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    if not raw or not raw.strip():
        raise ValueError("empty model output")

    text = raw.strip()

    # Strip ```json ... ``` fences.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"no JSON object in model output: {raw[:200]!r}")
        obj = json.loads(text[start : end + 1])

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                return item
        raise ValueError("model returned a JSON list without objects")
    if not isinstance(obj, dict):
        raise ValueError(f"model returned {type(obj).__name__}, expected object")
    return obj


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "হ্যাঁ")
    return default


def _as_on_off(value: Any, default: str = "on") -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("on", "true", "1", "enable", "start", "জ্বালাও", "চালু"):
            return "on"
        if token in ("off", "false", "0", "disable", "stop", "নেভাও", "বন্ধ"):
            return "off"
    return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().strip('"').strip("'")


def ensure_boss_address(reply: str) -> str:
    """Guarantee ORBIT always addresses the owner as "বস".

    Pure, idempotent and side-effect free: an existing "বস" is left exactly as
    the model wrote it (so personality survives), otherwise the sentence gets a
    natural "বস, " prefix. Action/params are never touched.
    """
    text = _clean_text(reply)
    if not text or BOSS_ADDRESS in text:
        return text
    return f"{BOSS_ADDRESS}, {text}"


def _digits(value: Any) -> str:
    """Normalise a phone number: Bengali digits -> ASCII, drop separators."""
    text = _clean_text(value).translate(BN_DIGITS)
    return re.sub(r"[^\d+]", "", text)


def _normalise_params(action: str, params: Any) -> Dict[str, Any]:
    """Coerce the model's params into what the device layer expects."""
    if not isinstance(params, dict):
        params = {}

    clean: Dict[str, Any] = {}

    if action in ("call", "sms", "whatsapp"):
        contact = _clean_text(params.get("contact") or params.get("name") or params.get("to"))
        if contact:
            clean["contact"] = contact
        number = _digits(params.get("number") or params.get("phone"))
        if number:
            clean["number"] = number
        if action != "call":
            message = _clean_text(params.get("message") or params.get("text") or params.get("body"))
            if message:
                clean["message"] = message

    elif action == "torch":
        clean["state"] = _as_on_off(params.get("state", params.get("on")), "on")

    elif action == "wifi":
        clean["state"] = _as_on_off(params.get("state", params.get("on")), "on")

    elif action == "volume":
        direction = _clean_text(params.get("direction")).lower()
        if direction in ("up", "down", "mute", "unmute"):
            clean["direction"] = direction
        raw_level = params.get("level")
        if raw_level not in (None, ""):
            try:
                level = int(str(raw_level).translate(BN_DIGITS).strip())
                clean["level"] = max(0, min(100, level))
            except (TypeError, ValueError):
                pass
        if not clean:
            clean["direction"] = "up"

    elif action == "intruder_check":
        clean["capture"] = _as_bool(params.get("capture"), True)

    elif action == "open_app":
        app = _clean_text(params.get("app") or params.get("name") or params.get("package"))
        if app:
            clean["app"] = app

    elif action == "market_news":
        topic = _clean_text(params.get("topic")).lower()
        clean["topic"] = topic if topic in ("crypto", "stocks", "general") else "general"

    elif action == "read_screen":
        clean = {}

    elif action == "speak":
        text = _clean_text(params.get("text") or params.get("message") or params.get("answer"))
        if text:
            clean["text"] = text

    return clean


def _financial_refusal(action: str, params: Dict[str, Any], user_text: str) -> Optional[str]:
    """Return a Bengali refusal when a decision tries to touch money or secrets."""
    if action == "open_app":
        target = _clean_text(params.get("app") or params.get("name") or params.get("package"))
        if is_financial_target(target):
            return BANKING_REFUSAL_BN
        if contains_secret(user_text):
            return SECRET_REFUSAL_BN
        if is_financial_request(user_text):
            return BANKING_REFUSAL_BN

    if action in ("sms", "whatsapp"):
        body = _clean_text(params.get("message") or params.get("text") or params.get("body"))
        if contains_secret(body) or contains_secret(user_text):
            return SECRET_REFUSAL_BN

    return None


def validate_decision(obj: Dict[str, Any], user_text: str = "") -> Dict[str, Any]:
    """Turn arbitrary model JSON into the fixed ORBIT contract."""
    action = _clean_text(obj.get("action")).lower().replace("-", "_").replace(" ", "_")
    action = ACTION_ALIASES.get(action, action)

    params = _normalise_params(action, obj.get("params"))
    reply = _clean_text(obj.get("reply_bn") or obj.get("reply") or obj.get("reply_bengali"))

    # Unknown action -> the safest possible behaviour is to just speak.
    if action not in SUPPORTED_ACTIONS:
        if action:
            log.warning("Unsupported action %r -> falling back to speak", action)
        if not reply:
            reply = _clean_text(obj.get("params", {}).get("text")) if isinstance(obj.get("params"), dict) else ""
        action, params = "speak", ({"text": reply} if reply else {})

    # Required params missing -> ask instead of firing a broken command.
    if action in ("call", "sms", "whatsapp") and not (params.get("contact") or params.get("number")):
        action, params = "speak", {}
        reply = reply or "কাকে পাঠাবেন বা কল দেবেন, নামটা বলবেন?"
    if action in ("sms", "whatsapp") and not params.get("message"):
        action, params = "speak", {}
        reply = reply or "মেসেজে কী লিখব বলবেন?"
    if action == "open_app" and not params.get("app"):
        action, params = "speak", {}
        reply = reply or "কোন অ্যাপটা খুলব বলবেন?"

    # Non-bypassable financial guard: a model cannot open a bank app, type a
    # PIN/OTP/password, or send one over SMS/WhatsApp even if it tries.
    refusal = _financial_refusal(action, params, user_text)
    if refusal:
        log.warning("Financial guard blocked action=%r (user=%r)", action, user_text[:80])
        action, params = "speak", {}
        reply = refusal

    if action == "speak":
        params.setdefault("text", reply or user_text or DEFAULT_REPLIES["speak"])

    if not reply:
        reply = DEFAULT_REPLIES.get(action, DEFAULT_REPLIES["speak"])

    # Boss persona guarantee — only reply_bn is touched.
    return {"action": action, "params": params, "reply_bn": ensure_boss_address(reply)}


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def _spec_block() -> str:
    lines = []
    for name in SUPPORTED_ACTIONS:
        fields = ", ".join(f"{k}" for k in ACTION_SPECS.get(name, {}))
        lines.append(f"- {name}: {fields or '{}'}")
    return "\n".join(lines)


def build_user_prompt(
    text: str,
    context: Optional[Dict[str, Any]] = None,
    history: Optional[Iterable[Dict[str, str]]] = None,
    memory_context: Optional[str] = None,
) -> str:
    """Compose the single user turn sent to the model."""
    parts = []
    if memory_context and memory_context.strip():
        parts.append("MEMORY (established facts, learned rules and past turns)\n" + memory_context.strip())
    if history:
        recent = list(history)[-6:]
        rendered = []
        for turn in recent:
            role = _clean_text(turn.get("role", "user")) or "user"
            content = _clean_text(turn.get("content", turn.get("text", "")))
            if content:
                rendered.append(f"{role}: {content}")
        if rendered:
            parts.append("RECENT CONVERSATION\n" + "\n".join(rendered))

    if context:
        small = {k: v for k, v in context.items() if v not in (None, "", [])}
        if small:
            parts.append("DEVICE CONTEXT\n" + json.dumps(small, ensure_ascii=False))

    parts.append(f"USER SAID\n{text}")
    parts.append(
        "Reply with the single JSON object now. Available actions:\n" + _spec_block()
    )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Brain
# --------------------------------------------------------------------------- #

def _failure_decision() -> Dict[str, Any]:
    """The safe spoken apology, always addressing Boss correctly."""
    return {
        "action": "speak",
        "params": {"text": FAILURE_REPLY},
        "reply_bn": ensure_boss_address(FAILURE_REPLY),
    }


class Brain:
    """Routes a user utterance to a validated ORBIT decision."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        session: Any = None,
        timeout: float = REQUEST_TIMEOUT,
        providers: Optional[Sequence[_Provider]] = None,
        memory: Any = None,
        use_memory: bool = True,
    ) -> None:
        self.settings = settings or default_settings
        self.timeout = timeout
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None
        # Injected MemoryEngine; ``get_memory()`` is created lazily on first use.
        self._memory = memory
        self.use_memory = bool(use_memory or memory is not None)

        if providers is not None:
            self.providers: List[_Provider] = list(providers)
        else:
            session = session or (requests.Session() if requests is not None else None)
            self.providers = self._build_providers(session, timeout)

    # -- setup --------------------------------------------------------------

    def _build_providers(self, session: Any, timeout: float) -> List[_Provider]:
        built: List[_Provider] = []
        if self.settings.gemini_api_key:
            built.append(
                GeminiProvider(self.settings.gemini_api_key, timeout=timeout, session=session)
            )
        else:
            log.warning("GEMINI_API_KEY missing — primary provider disabled")
        if self.settings.groq_api_key:
            built.append(
                GroqProvider(self.settings.groq_api_key, timeout=timeout, session=session)
            )
        else:
            log.warning("GROQ_API_KEY missing — fallback provider disabled")
        if not built:
            log.error("No AI provider configured. Add keys to .env (see .env.example).")
        return built

    @property
    def provider_names(self) -> List[str]:
        return [provider.name for provider in self.providers]

    # -- public API ---------------------------------------------------------

    def think(
        self,
        user_text: str,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[Iterable[Dict[str, str]]] = None,
        memory: Any = None,
    ) -> Dict[str, Any]:
        """Return ``{"action", "params", "reply_bn"}``; never raises for a
        provider outage — it degrades to a spoken apology instead.

        When a :class:`MemoryEngine` is available (injected, or the shared one),
        ORBIT learns from the turn, injects relevant memories into the prompt and
        records the turn afterwards. Memory failures never break a reply.
        """
        text = _clean_text(user_text)
        if not text:
            return _failure_decision()

        engine = self._resolve_memory(memory)
        memory_context = ""
        if engine is not None:
            try:
                engine.learn_from_text(text)
                memory_context = engine.build_memory_context(text) or ""
            except Exception as exc:  # memory must never block thinking
                log.warning("Memory read failed: %s", exc)

        if not self.providers:
            self.last_error = "no provider configured"
            decision = _failure_decision()
            self._remember(engine, text, decision, "failed")
            return decision

        prompt = build_user_prompt(text, context, history, memory_context)
        errors: List[str] = []

        for index, provider in enumerate(self.providers):
            try:
                raw = provider.complete(SYSTEM_PROMPT, prompt)
                decision = validate_decision(_extract_json(raw), text)
                self.last_provider = provider.name
                self.last_error = None
                log.info("%s decided action=%s", provider.name, decision["action"])
                self._remember(engine, text, decision, "ok")
                return decision
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                log.warning("%s failed: %s", provider.name, exc)

            # One repair attempt on the same provider before switching.
            try:
                repaired = self._repair(provider, text)
                if repaired is not None:
                    self.last_provider = provider.name
                    self.last_error = None
                    log.info("%s decided action=%s (after repair)", provider.name, repaired["action"])
                    self._remember(engine, text, repaired, "ok")
                    return repaired
            except Exception as exc:
                errors.append(f"{provider.name} repair: {exc}")
                log.warning("%s repair failed: %s", provider.name, exc)

        self.last_provider = None
        self.last_error = " | ".join(errors) or "unknown failure"
        log.error("All providers failed: %s", self.last_error)
        decision = _failure_decision()
        self._remember(engine, text, decision, "failed")
        return decision

    async def athink(
        self,
        user_text: str,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[Iterable[Dict[str, str]]] = None,
        memory: Any = None,
    ) -> Dict[str, Any]:
        """Async wrapper for Telegram / voice loops (network call off the loop)."""
        return await asyncio.to_thread(self.think, user_text, context, history, memory)

    # -- memory ------------------------------------------------------------ #

    def _resolve_memory(self, memory: Any = None) -> Any:
        """Injected engine > shared MemoryEngine > None (memory is optional)."""
        if memory is not None:
            return memory
        if not self.use_memory:
            return None
        if self._memory is None:
            try:
                from memory_engine import get_memory

                self._memory = get_memory()
            except Exception as exc:
                log.warning("Memory unavailable: %s", exc)
                return None
        return self._memory

    @staticmethod
    def _remember(engine: Any, text: str, decision: Dict[str, Any], outcome: str) -> None:
        if engine is None:
            return
        try:
            engine.remember_turn(text, decision, outcome=outcome)
        except Exception as exc:
            log.warning("Memory write failed: %s", exc)

    # -- internals ----------------------------------------------------------

    def _repair(self, provider: _Provider, user_text: str) -> Optional[Dict[str, Any]]:
        """Second chance: ask for the JSON again, schema in front of the model."""
        raw = provider.complete(
            "You output only valid minified JSON. Nothing else.",
            (
                "The user said: " + user_text + "\n\n"
                "Return exactly one JSON object with keys action, params, reply_bn. "
                "action must be one of: " + ", ".join(SUPPORTED_ACTIONS) + ". "
                "reply_bn must be a short natural Bengali sentence. No markdown."
            ),
        )
        return validate_decision(_extract_json(raw), user_text)


# --------------------------------------------------------------------------- #
# CLI: python brain.py "টর্চ জ্বালাও"
# --------------------------------------------------------------------------- #

def _main() -> int:
    parser = argparse.ArgumentParser(description="Ask ORBIT's brain for a decision.")
    parser.add_argument("text", nargs="*", help="what the user said (bn/en/banglish)")
    parser.add_argument("--json", action="store_true", help="pretty-print the raw decision")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    utterance = " ".join(args.text).strip() or "টর্চ জ্বালাও"
    brain = Brain()

    if not brain.providers:
        print("No AI provider configured — add GEMINI_API_KEY / GROQ_API_KEY to .env")
        return 2

    decision = brain.think(utterance)
    print(json.dumps(decision, ensure_ascii=False, indent=2 if args.json else None))
    print(f"\nprovider: {brain.last_provider or 'none'}", flush=True)
    if brain.last_error:
        print(f"error   : {brain.last_error}")
    return 0 if brain.last_provider else 1


if __name__ == "__main__":
    raise SystemExit(_main())
