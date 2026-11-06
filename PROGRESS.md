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
| 3 | `security_guard.py` | `intruder_check`: silent camera photo + GPS + Maps link + Bengali summary | ✅ done |
| 4 | `tests/test_security_guard.py` | Unit tests with a fake subprocess runner | ✅ done |

**Phase 2 status: COMPLETE — 4/4 files done, 70 tests green**
(`python -m unittest discover -s tests -t .`).

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
- **One evidence package for `intruder_check`**: `{'ok', 'photo_path', 'camera', 'lat',
  'lon', 'accuracy', 'provider', 'maps_url', 'timestamp', 'epoch', 'summary_bn',
  'attempts'}` — JSON-serialisable so Phase 3 can post it to Telegram as a photo with
  `summary_bn` as the caption and `maps_url` as the tappable location. `ok` is true when
  *either* the photo or the fix succeeded; partial evidence still gets delivered.
- **Front camera first** (`-c 1`), rear (`-c 0`) as fallback; a capture counts only if the
  JPEG is ≥1 KB, so a permission-denied stub file can't masquerade as evidence. Small
  files are deleted immediately. Photos go to `.intruder/` (already git-ignored).
- **Location is network-then-GPS** with every attempt recorded; null markers, out-of-range
  coordinates and unreadable JSON all fall through to the next provider instead of
  producing a wrong pin. Summary wording distinguishes "not requested" from "failed".
- Security tests use a scripted runner that *writes the JPEG* Termux would have written,
  so the ≥1 KB and fallback logic is exercised for real, plus a fixed clock so timestamps
  and the Bengali `HH:MM` in the summary are deterministic. 70 tests total across both
  modules — still green after every edit.

## Phase 3 — Memory, Boss/Thinker Persona, Communication (Hour 3)

| # | File | Purpose | Status |
|---|------|---------|--------|
| 1 | `memory_engine.py` | SQLite episodes/profile/self-upgrades/thinker-notes + daily self-upgrade | ✅ done |
| 2 | `tests/test_memory_engine.py` | 27 tests: schema, extraction, context, daily upgrade | ✅ done |
| 3 | `brain.py` (update) | "বস" persona guarantee, memory hook, Thinker Catalyst prompt | ✅ done |
| 4 | `tests/test_brain.py` | Persona guard + memory integration tests | ✅ done |
| 5 | `comm_manager.py` | `make_call` / `send_sms` via resolved contacts | ✅ done |
| 6 | `tests/test_comm_manager.py` | Resolve + command-construction tests | ✅ done |
| 7 | `call_receptionist.py` | Incoming-call watch, answer, Bengali greeting, STT reply, call log | ✅ done |
| 8 | `tests/test_call_receptionist.py` | dumpsys parsing + full reception flow tests | ✅ done |

**Phase 3 status: COMPLETE — 8/8 files done, 156 tests green**
(`python -m unittest discover -s tests -t .`).

### Phase 3 decisions on record

- **Four tables, one file**: `episodes` (every turn), `boss_profile` (facts), `self_upgrades`
  (rules/lessons) and `thinker_notes` (daily challenge). `rule_key` is UNIQUE so re-learning
  the same lesson updates instead of duplicating; Bengali keys are hashed into a stable
  `bn_<sha1>` slug so SQL and logs stay readable.
- **DB path is configurable**: `~/.orbit/orbit_memory.db` by default, overridable with
  `ORBIT_MEMORY_DB`, `Settings.memory_db` or `MemoryEngine(db_path=...)` — tests always use
  a temp file, so the suite never touches the real memory.
- **Memory never breaks the assistant**: a missing/unwritable DB marks `ok=False`, every
  method returns an error dict instead of raising, and `build_memory_context()` returns `""`
  so the brain prompt simply has no memory section.
- **Learning is deterministic first**: regex extraction of name/preferences/family/habits/
  goals/projects/corrections in Bengali, English and Banglish; an optional `llm_callable`
  only *sharpens* the daily rule and always falls back to the deterministic one.
  Bengali vowel signs are non-word characters, so no `\b` anchors after Bengali words.
- `daily_self_upgrade()` derives today's rule from failure counts / most-used action and
  one of six rotating Thinker Challenges (date-seeded, always ends with a question mark).
- **Boss is unskippable and untouched by luck**: `ensure_boss_address()` runs at the end of
  `validate_decision()` and only ever touches `reply_bn` — an existing "বস" survives
  verbatim (so the model's warmth isn't flattened), otherwise a natural "বস, " prefix is
  added. Action and params are identical to before, which keeps the 13-action contract and
  all Phase 2 tests intact.
- **Memory is optional and non-blocking**: `Brain(memory=...)` injects an engine,
  `use_memory=False` opts out, otherwise the shared `memory_engine.get_memory()` is created
  lazily. `learn_from_text` runs *before* the prompt so today's new fact is already usable,
  `remember_turn` runs *after* the decision, and any memory exception is logged, never
  raised — a locked DB cannot take the assistant down.
- **Thinker Catalyst lives in the prompt, not the code**: ideas/strategy questions get one
  sharp Bengali second-order insight; device commands do not, so "টর্চ জালাও" stays short.
- **Commander never invents a number**: `make_call`/`send_sms` always go through
  `resolve_contact()`, so `+880…`/`0…`, Bengali digits and fuzzy names behave exactly like
  the rest of the assistant; an unresolved name returns an error dict and *no* dial command
  is issued. A spoken number wins over the address book, and the exact number string is
  what reaches `termux-telephony-call` / `termux-sms-send -n`.
- SMS refuses an empty body before touching the radio ("মেসেজে কী লিখব বলবেন?"), and both
  paths return `message_bn` so the caller can speak the outcome without re-deriving it.
- **Reception is a state machine over dumpsys, not a blocking dialer**: `monitor_once()` is a
  single poll (`mCallState=1` ringing, `=2` offhook, `=0` idle). The ring timer starts on the
  first ringing sighting, resets on idle *and* on a different caller, so a cancelled call or a
  second caller never inherits an old timer. Answer when `ring_seconds` (default 8s) elapses
  or `auto_reception` is on.
- **Answer = KEYCODE_CALL (5)** then best-effort speaker routing (HEADSET_PLUG broadcast, then
  HEADSETHOOK 79). Speaker control is ROM-dependent, so failures are recorded in
  `speaker.attempts` and never block the greeting or the call itself.
- **The receptionist degrades in layers**: no brain -> a polite Bengali holding line; no mic or
  a failed recording -> ask again and still log the call; TTS/STT/memory exceptions are logged
  and swallowed. `receive()` always returns `{ok, caller, transcript, transcript_text,
  summary_bn, speaker}` so "কে কল করেছিল?" is answerable from memory later.
- Reception is logged through `memory.remember_turn` with `action="call_reception"` and
  `outcome="incoming_call"`, which means the existing `build_memory_context()` surfaces it to
  the brain without a new table.

### Phase 3 runtime prerequisites (on the phone)

- `adb` reachable from Termux (Android 11+: `pkg install android-tools` + Wireless debugging
  pairing). `dumpsys`/`input` need the paired device; without it `current_call()` reports the
  adb error and never pretends a call was answered.
- `termux-api` permissions for `termux-telephony-call` / `termux-sms-send`, plus microphone for
  STT. Speaker routing during calls is attempted, not guaranteed.

## Phase 4 — UI Automation, Notifications, Briefing, Dispatcher (Hour 4)

| # | File | Purpose | Status |
|---|------|---------|--------|
| 1 | `adb_automation.py` | `open_app` (Bengali/English aliases), WhatsApp deep link, `read_screen` via Gemini vision | ✅ done |
| 2 | `tests/test_adb_automation.py` | 31 tests: app table, number normalisation, links, vision | ✅ done |
| 3 | `notification_responder.py` | Read/filter/dedupe notifications, log to memory, auto-reply | ⏳ pending |
| 4 | `tests/test_notification_responder.py` | Fake notification list + reply routing tests | ⏳ pending |
| 5 | `briefing_agent.py` | `market_news`: world news RSS + Nifty snapshot + Bengali Thinker brief | ⏳ pending |
| 6 | `tests/test_briefing_agent.py` | Fake RSS/HTTP, deterministic fallback tests | ⏳ pending |
| 7 | `actions.py` | `ActionDispatcher` over all 13 actions + speak + memory | ⏳ pending |
| 8 | `tests/test_actions.py` | Dispatch table, failure and speak tests | ⏳ pending |

### Phase 4 decisions on record

- **ADB is a runner, never a global**: `AdbAutomation(runner=...)` mirrors `SystemControl`,
  so app launching, the WhatsApp intent and the screenshot pull are all exercised with a
  fake in CI. `adb` missing simply fails the plan.
- **`monkey` lies about success.** It exits 0 even when the package does not exist, so a
  launch counts only when its output has no "No activities found" / "monkey aborted".
  Each app has a fallback chain (monkey → `am start -n` → alternative packages → intent),
  and the winning command is returned in `detail` with every attempt recorded.
- **App names are matched, not guessed**: Bengali, Banglish and English aliases map onto a
  small table (YouTube, WhatsApp, Facebook, Telegram, Chrome, Camera, Settings, Clock,
  Angel One), noise words like "অ্যাপটা খোলো" are stripped, direct package names work, and
  an unknown app returns the available Bengali names instead of opening the wrong app.
- **WhatsApp numbers are normalised, never invented**: `01712345678` -> `+8801712345678`,
  `9876543210` -> `+919876543210`, `+…` preserved; the link drops the `+` for
  `api.whatsapp.com` and percent-encodes the Bengali body. ADB `VIEW` first,
  `termux-open-url` as the fallback. `press_send` taps known coordinates when given,
  otherwise attempts KEYCODE_ENTER and reports that the human may still need to press send.
- **Screen reading is vision-first with honest failure**: `screencap` → `adb pull` → verify
  PNG magic and ≥1 KB → base64 → Gemini 2.5 Flash `inline_data`. A tiny/corrupt file is
  deleted and never sent; the temp screenshot is removed after the call unless
  `keep_screenshot=True`.

## Phase 5 — Telegram bridge + always-on service (planned, not started)

- `telegram_bot.py` — admin-only long polling, voice notes in/out, inline confirmation.
- `main.py` — wake-lock loop, hotword → STT → brain → dispatch → TTS.
- `boot.sh` — `termux-wake-lock`, service start, crash restart.

## Known environment notes

- Target runtime: Termux on Android (`pkg install python mpv termux-api`).
- `termux-*` binaries and `mpv` are absent on desktop/CI — every device call must
  degrade gracefully instead of crashing (tested by unit import/compile only).
- Bengali TTS voice: `bn-IN-BashkarNeural` (male, default) / `bn-IN-TanishaaNeural` (female).
