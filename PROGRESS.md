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
| 7 | `.env.example` | Template for keys and settings | ✅ done |

**Phase 1 status: COMPLETE — 7/7 files committed and pushed to `main`.**

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

## Phase 1 quick start (Termux)

```bash
pkg install python mpv termux-api jq
git clone https://github.com/amar768581/Assistant-Orbit-.git && cd Assistant-Orbit-
pip install -r requirements.txt
cp .env.example .env && nano .env        # GEMINI_API_KEY, GROQ_API_KEY

python config.py                          # shows masked config + missing keys
python brain.py "টর্চ জ্বালাও"              # -> {"action":"torch",...}
python voice_engine.py                    # speaks a Bengali greeting
python voice_engine.py --listen           # 6s mic -> Whisper transcript
```

Expected on a fresh phone: `python brain.py` prints the decision JSON plus the provider
used (`gemini`, or `groq` when Gemini fails); `voice_engine.py` reports any missing
Termux binary instead of crashing.

## Phase 2 — Device Hardware, Contacts & Security (Hour 2)

| # | File | Purpose | Status |
|---|------|---------|--------|
| 1 | `system_control.py` | Torch, battery + low/full Bengali voice alerts, volume mapping, Wi-Fi, contact fuzzy-match | ✅ done |
| 2 | `tests/test_system_control.py` | Unit tests with a fake subprocess runner | ✅ done |
| 3 | `security_guard.py` | `intruder_check`: silent camera photo + GPS + Maps link + Bengali summary | ⏳ pending |
| 4 | `tests/test_security_guard.py` | Unit tests with a fake subprocess runner | ⏳ pending |

Still to come later: `actions.py` (dispatcher over all 13 actions), `screen_reader.py`
(`read_screen` via UI Automator dump), `open_app`/`answer_call` helpers.

### Phase 2 decisions on record

- **One result shape everywhere**: every device call returns a JSON-serialisable dict
  starting with `ok`, plus `error` when it failed — Telegram can forward it unchanged.
- **Inputs are re-validated** in the device layer (state, level, stream, query) even
  though `brain.validate_decision()` already normalised them.
- **Volume is a percentage, not a stream step**: `0-100` maps onto the stream's advertised
  `max_volume` from `termux-volume`; `direction` (`up`/`down`/`mute`/`unmute`) is also
  supported because that is what `brain.py` emits for "ভলিউম বাড়াও".
- **Contact matching survives script mismatch**: exact number (last 10 digits, so
  `+880…` == `0…`) → exact name → substring → fuzzy score over both the transliterated
  name and its consonant skeleton, so "rahim" still finds "রহিম". Contacts cached 5 min.
- **Battery alerts are edge-triggered**: one alert when crossing <20% or reaching 100%,
  suppressed for 30 min, state cleared when the battery recovers — a 5-minute monitor
  must not nag every 5 minutes. The decision itself is the pure
  `battery_alert_message()`, so it is testable without threads or a phone.
- **Tests inject the runner, not `subprocess`**: `SystemControl(runner=...)` takes any
  callable returning a `CommandResult`, so the suite needs no Termux binaries and no
  monkey-patching of the module. `run_command()` itself is tested with `mock.patch`
  (success, non-zero exit, timeout → 124, missing binary → 127).
- 46 tests green: `python -m unittest discover -s tests -t .`

## Phase 3 — Telegram bridge + always-on service (planned, not started)

- `telegram_bot.py` — admin-only long polling, voice notes in/out, inline confirmation.
- `main.py` — wake-lock loop, hotword → STT → brain → dispatch → TTS.
- `boot.sh` — `termux-wake-lock`, service start, crash restart.

## Known environment notes

- Target runtime: Termux on Android (`pkg install python mpv termux-api`).
- `termux-*` binaries and `mpv` are absent on desktop/CI — every device call must
  degrade gracefully instead of crashing (tested by unit import/compile only).
- Bengali TTS voice: `bn-IN-BashkarNeural` (male, default) / `bn-IN-TanishaaNeural` (female).
