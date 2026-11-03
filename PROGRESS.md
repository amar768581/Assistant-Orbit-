# ORBIT — Build Progress

Android Personal AI Assistant running inside Termux.
Repo: `amar768581/Assistant-Orbit-`

## Rule of the build

1. One file at a time.
2. After each file: `git add . && git commit -m "checkpoint: <file>"` + `git push`.
3. Update this file in the same commit before starting the next file.

---

## Phase 1 — Core Architecture (Hour 1)

| # | File | Purpose | Status |
|---|------|---------|--------|
| 1 | `PROGRESS.md` | Build tracker (this file) | ✅ done |
| 2 | `config.py` | `.env` loading, API keys, assistant settings | ✅ done |
| 3 | `.gitignore` | Keep `.env`, audio dumps and caches out of git | ✅ done |
| 4 | `brain.py` | Gemini 2.5 Flash decision engine + Groq Llama-3 fallback, strict JSON | ✅ done |
| 5 | `voice_engine.py` | Bengali TTS (edge-tts → mpv / termux-media-player → termux-tts-speak) + STT (termux-microphone-record → Groq Whisper) | ✅ done |
| 6 | `requirements.txt` | Python dependencies | ✅ done |
| 7 | `.env.example` | Template for keys and settings | ⏳ pending |

### Phase 1 decisions on record

- **Gemini via REST, not an SDK** — `requests` keeps Termux install light and avoids
  `google-genai` version drift. `thinkingBudget: 0` keeps voice-loop latency low; if the
  API rejects that field the request is retried without it.
- **Model output is never trusted.** `brain.validate_decision()` whitelists the 13 actions,
  coerces params (Bengali digits → ASCII, bool → `on`/`off`, volume clamped 0-100) and
  rewrites unknown actions or missing recipients into a `speak` clarification, so a
  hallucinated command can never reach `termux-*`.
- **Degradation is silent to the caller**: Gemini error → Groq, bad JSON → one repair pass,
  everything down → `speak` apology. `think()` always returns the 3-key contract.
- Verified with a fake HTTP session (Gemini payload shape, Groq fallback, repair path,
  13 param shapes); no live API keys are present in this build environment.
- **Voice verified for real** (not only mocked): `edge-tts` synthesised the Bengali test
  line with both `bn-IN-BashkarNeural` and `bn-IN-TanishaaNeural` (~30 KB MP3, valid MPEG
  frames). Playback and microphone paths stay untested until this runs on a real phone —
  every missing binary must degrade to `termux-tts-speak` or a logged warning, never a crash.
- `requirements.txt` pins only 3 runtime packages (`requests`, `python-dotenv`, `edge-tts`);
  installed cleanly in a fresh venv (requests 2.34.2, python-dotenv 1.2.4, edge-tts 7.3.1)
  and all three modules import against them. `python-telegram-bot` is left commented until
  Phase 3.
- Whisper requests are sent as `multipart/form-data` to `whisper-large-v3-turbo` with
  `language=bn` (pass `language="auto"` for mixed Bengali/English audio) and a Bengali
  prompt hint; 429/5xx retried twice, hard errors surface as `VoiceError`.

### Phase 1 contract (locked)

**`brain.think(user_text) -> dict`** always returns (verified by `python brain.py`):

```json
{"action": "call", "params": {"contact": "Rahim"}, "reply_bn": "রহিমকে কল দিচ্ছি।"}
```

Supported actions: `call`, `answer_call`, `sms`, `whatsapp`, `torch`, `battery`,
`volume`, `wifi`, `intruder_check`, `open_app`, `read_screen`, `market_news`, `speak`.

---

## Phase 2 — Termux Device Control (planned, not started)

- `actions.py` — dispatcher that executes the 13 actions via Termux:API
  (`termux-telephony-call`, `termux-sms-send`, `termux-torch`, `termux-battery-status`,
  `termux-volume`, `termux-wifi-connectioninfo`, `termux-camera-photo`, `am start`, ...)
- `screen_reader.py` — `read_screen` via UI Automator dump / accessibility.

## Phase 3 — Telegram bridge + always-on service (planned, not started)

- `telegram_bot.py` — admin-only long polling, voice notes in/out, inline confirmation.
- `main.py` — wake-lock loop, hotword → STT → brain → dispatch → TTS.
- `boot.sh` — `termux-wake-lock`, service start, crash restart.

## Known environment notes

- Target runtime: Termux on Android (`pkg install python mpv termux-api`).
- `termux-*` binaries and `mpv` are absent on desktop/CI — every device call must
  degrade gracefully instead of crashing (tested by unit import/compile only).
- Bengali TTS voice: `bn-IN-BashkarNeural` (male, default) / `bn-IN-TanishaaNeural` (female).
