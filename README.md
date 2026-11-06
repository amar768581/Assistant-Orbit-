# Orbit 🤖 — বাংলা ব্যক্তিগত AI সহকারী (Android / Termux)

**Orbit** is a Bengali-first personal AI assistant that runs entirely inside
**Termux on Android**. Talk or type in Bengali; Orbit decides what to do, uses
the phone's real hardware (calls, SMS, WhatsApp, torch, camera, GPS, apps), and
answers as **“বস”** — with long-term memory, a daily Thinker challenge, a morning
market/world briefing, and a **Boss-only Telegram bridge** so you can command
your phone from anywhere.

> **Status:** all 5 phases complete — 411 unit tests green, plus a non-bypassable
> **Financial & OTP Security Guardrail** that no module can skip.
> See [`PROGRESS.md`](PROGRESS.md) for the decisions behind every module.

---

## বাংলা

### এক নজরে

| ক্ষমতা | উদাহরণ |
|---|---|
| 📞 কল ও SMS | “রহিমকে কল দাও”, “মাকে SMS পাঠাও আমি আসছি” |
| 💬 হোয়াটসঅ্যাপ | “রহিমকে হোয়াটসঅ্যাপে বল কাল দেখা হবে” |
| 🔦 ফোন নিয়ন্ত্রণ | টর্চ, ভলিউম, ওয়াই-ফাই, ব্যাটারি রিপোর্ট |
| 📱 অ্যাপ ও স্ক্রিন | “ইউটিউব খোলো”, “স্ক্রিনে কী আছে বলো” (Gemini vision) |
| 🚨 নিরাপত্তা | “ঘরে কে আছে দেখো” → ছবি + GPS + Google Maps লিংক |
| ☎️ রিসেপশনিস্ট | কল এলে নিজে ধরে বাংলায় কথা বলে সারাংশ রাখে |
| 📩 নোটিফিকেশন | হোয়াটসঅ্যাপ/টেলিগ্রাম মেসেজ পড়ে, দরকারে অটো-রিপ্লাই |
| 📊 ব্রিফিং | নিফটি ৫০ + মিডক্যাপ + বিশ্বের খবর, বাংলা সংখ্যায় |
| 🧠 মেমোরি | বসের নাম, পছন্দ, অভ্যাস, কাজ — SQLite-এ স্থানীয়ভাবে |
| 📲 টেলিগ্রাম | দূর থেকে কমান্ড, ভয়েস নোট, ছবি, রিপোর্ট (শুধু বস) |
| 🔒 ব্যাংকিং সুরক্ষা | ব্যাংক/UPI/ওয়ালেট অ্যাপ, পিন, ওটিপি, ব্যাংক SMS — কখনোই নয় |

### যেভাবে কাজ করে

```
🎙️ মাইক / ⌨️ CLI / 📲 Telegram
              │
      🧠 Brain — Gemini 2.5 Flash → Groq Llama-3 fallback
         (Boss persona + memory + Thinker Catalyst)
              │  যাচাই করা {action, params, reply_bn}
      🎛️ ActionDispatcher — ১৩টি অনুমোদিত কাজ
        ├─ SystemControl   টর্চ · ব্যাটারি · ভলিউম · ওয়াই-ফাই · কন্টাক্ট
        ├─ CommManager     কল · SMS
        ├─ AdbAutomation   অ্যাপ · WhatsApp deep link · স্ক্রিন vision
        ├─ SecurityGuard   ছবি + GPS + Maps
        ├─ CallReceptionist
        ├─ BriefingAgent   RSS + Yahoo Finance
        └─ VoiceEngine     বাংলা TTS (edge-tts) · Whisper STT
              │
      🗄️ MemoryEngine (SQLite)   +   📲 Telegram push
```

### ইনস্টল (৫ ধাপ)

```bash
# ১. Termux (F-Droid) + Termux:API অ্যাপ ইনস্টল করুন
# ২. রিপো ক্লোন করুন
pkg install -y git
git clone https://github.com/amar768581/Assistant-Orbit-.git
cd Assistant-Orbit-

# ৩. এক কমান্ডে সব
bash install.sh

# ৪. কী দিন
nano .env          # GEMINI_API_KEY, GROQ_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID

# ৫. প্রস্তুতি যাচাই
python main.py doctor
```

`install.sh` হলো **idempotent** — যতবার চালান, শুধু যা নেই সেটাই যোগ করে;
আপনার `.env` কখনো overwrite করে না।

### চালানোর তিন উপায়

```bash
# ক) ২৪/৭ ডেমন + বাংলা টেক্সট CLI
python main.py

# খ) হেডলেস (Termux:Boot / স্ক্রিন বন্ধে)
python main.py daemon --no-cli

# গ) একবারের কাজ
python main.py once "টর্চ জালাও"
python main.py once "আজকের বাজার কেমন?" --json
python main.py voice                 # ৬ সেকেন্ড রেকর্ড → কাজ → বাংলায় উত্তর
python main.py briefing              # সকালের বাজার+খবর, মুখে শোনায়
python main.py doctor                # ✅/⚠️/❌ রিপোর্ট
```

### টেলিগ্রাম কমান্ড (শুধু বস)

| কমান্ড | কাজ |
|---|---|
| `/start`, `/help` | বাংলা স্বাগতম + সব ক্ষমতা |
| `/status` | ব্যাটারি, মেমোরি পরিসংখ্যান, চালু সার্ভিস |
| `/briefing` | এখনই বাজার + বিশ্ব খবর |
| `/thinker` | আজকের শেখা, উপলব্ধি ও চ্যালেঞ্জ |
| `/intruder` | ছবি + লোকেশন (Maps লিংকসহ) |
| `/receptionist on\|off` | অটো-রিসেপশনিস্ট চালু/বন্ধ |

এর বাইরে **যেকোনো বাংলা লেখা বা ভয়েস নোট** পাঠান — সেটাও Brain-এ যায়।
টেলিগ্রামে শুধু `ADMIN_CHAT_ID`-র চ্যাট কাজ করে; অন্য কেউ লিখলে একবার
ভদ্রভাবে না বলে লগে `SECURITY` সতর্কতা রাখা হয়, কোনো কাজ চলে না।

### 🔒 ব্যাংকিং ও OTP নিরাপত্তা (স্বয়ংক্রিয়, বন্ধ করা যায় না)

Orbit কখনো আপনার টাকা বা গোপন তথ্যের কাছে যায় না — এটি কনফিগ ছাড়াই সবসময় চালু:

- **ব্যাংক/UPI/ওয়ালেট/ট্রেডিং অ্যাপ খোলে না, লগইন করে না** — SBI YONO, HDFC,
  ICICI, Axis, Kotak, PNB, BoB, Google Pay/Tez, PhonePe, Paytm, BHIM, Amazon Pay,
  CRED, Angel One, Zerodha, Groww, PayPal ইত্যাদি; নামে `bank`/`upi`/`pay`/`wallet`/
  `netbanking`/`finance` থাকলেও ব্লক।
- **স্ক্রিন পড়া বন্ধ** — ফোরগ্রাউন্ডে ব্যাংকিং অ্যাপ বা `FLAG_SECURE` থাকলে ছবিই
  তোলা হয় না (dumpsys পড়তে না পারলেও বন্ধ — fail-closed)।
- **OTP/PIN/CVV/পাসওয়ার্ড** — পড়া হয় না, মনে রাখা হয় না, SMS/হোয়াটসঅ্যাপে
  পাঠানো হয় না, অটো-রিপ্লাইও হয় না।
- **ব্যাংক অ্যালার্ট** (`debited`, `credited`, `a/c`, `ব্যালেন্স`) — নোটিফিকেশন
  স্ক্যানেই বাদ, SQLite-এ কিছুই জমা হয় না।
- **টেলিগ্রাম** এমন কমান্ড Brain-এ পৌঁছানোর আগেই বাংলায় না বলে দেয়।

যাচাই করুন: `python main.py doctor` → **Banking Security Lock** সারি, অথবা
টেলিগ্রামে `/status`।

### Termux:API অনুমতি (একবার)

F-Droid থেকে **Termux:API** অ্যাপ ইনস্টল করে Android Settings → Apps →
Termux:API → Permissions-এ দিন:

- 🎤 Microphone — ভয়েস কমান্ড, কল রিসেপশন
- 📷 Camera — intruder ছবি
- 📍 Location — GPS
- 🔔 Notifications — মেসেজ পড়া
- 📞 Phone + SMS — কল ও টেক্সট

### Wireless ADB জোড়া লাগানো

অ্যাপ খোলা, স্ক্রিন পড়া, কল ধরা — এগুলো ADB দিয়ে হয়।

1. Settings → About phone → **Build number** ৭ বার ট্যাপ → Developer options
2. Developer options → **Wireless debugging** চালু
3. “Pair device with pairing code” → দেখানো `IP:PORT` ও কোড নিয়ে:

```bash
pkg install -y android-tools
adb pair 192.168.1.42:37123      # pairing port, কোড লিখুন
adb connect 192.168.1.42:5555    # wireless debugging port
adb devices                      # R58M...	device  দেখাবে
```

Wi-Fi বদলালে আবার `adb connect` করতে হবে। USB ডিবাগিং চাইলে কেবল দিয়েও
`adb devices` কাজ করে।

### ২৪/৭ চালু রাখা

- Orbit নিজেই `termux-wake-lock` নেয়, বন্ধ হওয়ার সময় ছাড়ে
  (`WAKE_LOCK=false` দিলে নেবে না)।
- Android Settings → Apps → Termux → Battery → **Unrestricted**।
- Termux নোটিফিকেশনে “Acquire wakelock” চালু রাখুন।
- চাইলে **Termux:Boot** দিয়ে বুটে চালান:
  `~/.termux/boot/orbit.sh` → `cd ~/Assistant-Orbit- && python main.py daemon --no-cli`

### `.env` সেটিংস

| কী | দরকার | কাজ |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | প্রধান Brain (Gemini 2.5 Flash) + স্ক্রিন vision |
| `GROQ_API_KEY` | ✅ | fallback Brain + Whisper STT (ভয়েস) |
| `TELEGRAM_BOT_TOKEN` | ঐচ্ছিক | @BotFather থেকে বট |
| `ADMIN_CHAT_ID` | ঐচ্ছিক | @userinfobot থেকে আপনার numeric id |
| `ASSISTANT_NAME` | — | ডিফল্ট `Orbit` |
| `ASSISTANT_LANGUAGE` | — | ডিফল্ট `bn-IN` (`bn-BD`, `en-IN`, `auto`) |
| `WAKE_LOCK` | — | `true`/`false` |
| `WHISPER_MODEL` | — | ডিফল্ট `whisper-large-v3-turbo` |
| `ORBIT_MEMORY_DB` | — | ডিফল্ট `~/.orbit/orbit_memory.db` |

### নিরাপত্তা

- `.env` কখনো git-এ যায় না (`.gitignore`), ফাইল mode `600`।
- মেমোরি শুধু আপনার ফোনে, SQLite-এ — ক্লাউডে নয়।
- Brain শুধু **১৩টি অনুমোদিত action** দিতে পারে; অজানা কিছু হলে কিছুই চলে না।
- টেলিগ্রাম শুধু `ADMIN_CHAT_ID`; `ADMIN_CHAT_ID` না থাকলে সবাইকে refuse করে।
- ছবি `.intruder/`, অডিও `~/.orbit/`-এ থাকে — git-এর বাইরে।

### সমস্যা ও সমাধান

| সমস্যা | সমাধান |
|---|---|
| `termux-*` কমান্ড ঝুলে যায় | Termux:API অ্যাপ (F-Droid) ইনস্টল + permission দিন |
| `adb: no devices` | Wireless debugging চালু করে `adb pair` → `adb connect` |
| ভয়েস শোনে না | মাইক permission; `pkg install termux-api`; `GROQ_API_KEY` |
| TTS চুপ | ইন্টারনেট + `mpv`; নাহলে `termux-tts-speak` fallback |
| Telegram উত্তর দেয় না | `ADMIN_CHAT_ID` numeric কিনা, বটে `/start` পাঠান |
| ব্যাটারি সেভার থামিয়ে দেয় | Termux + Termux:API কে “Unrestricted” করুন |
| Gemini 429 | Groq স্বয়ংক্রিয় fallback; কী ও কোটা দেখুন |
| WhatsApp মেসেজ লেখা হয়, send হয় না | ফোনের ROM intent সীমিত — কীবোর্ড Enter/সেন্ড বোতাম দিন |
| ব্যাংক অ্যাপ খুলতে বললে খোলে না | এটাই ডিজাইন — নিরাপত্তা লক কখনো বন্ধ হয় না |
| OTP মনে রাখতে বললে রাখে না | ইচ্ছাকৃত — গোপন তথ্য কখনো সংরক্ষণ করা হয় না |

---

## English

### What it is

Orbit is a personal AI assistant for Android that runs in Termux. It is
Bengali-first: you speak or type Bengali, the LLM returns a strict JSON
decision, and a dispatcher executes one of **13 whitelisted actions** on the
phone. It keeps SQLite memory, learns one rule a day, briefs you on NIFTY 50,
NIFTY MIDCAP 50 and world news, and exposes an admin-only Telegram bot so the
phone can be commanded remotely.

**Design rules**

1. The model only *decides*; `brain.validate_decision()` whitelists actions and
   coerces params, so a hallucinated command never reaches `termux-*`.
2. Every module returns `{ok, ...}` and never raises at the boundary.
3. Hardware is injected (`runner=`/`session=`), so the whole stack is tested on
   a desktop with fakes.
4. Every reply addresses Boss (`ensure_boss_address`) and failures are spoken
   honestly, not as a false success.
5. Financial safety is not a prompt: bank/UPI/wallet/trading apps, permission
   screens and OTP/PIN/bank-alert text are blocked in code at every layer
   (decision, ADB, notifications, memory, Telegram).

### Architecture

```
Voice / CLI / Telegram
        │
   Brain (Gemini → Groq, memory context, Boss persona)
        │ validated decision {action, params, reply_bn}
   ActionDispatcher ──┬── SystemControl (torch/battery/volume/wifi/contacts)
        │             ├── CommManager (call/SMS)
        │             ├── AdbAutomation (apps, WhatsApp, screen vision)
        │             ├── SecurityGuard (photo + GPS + Maps)
        │             ├── CallReceptionist (answer, greet, summarise)
        │             ├── BriefingAgent (RSS + Yahoo quotes, Bengali brief)
        │             └── VoiceEngine (edge-tts TTS, Groq Whisper STT)
        │
   MemoryEngine (SQLite: episodes, profile, rules, thinker notes)
        │
   TelegramController (admin-only long polling + push)
```

| File | Role |
|---|---|
| `config.py` | `.env` loader, frozen `Settings`, masked diagnostics |
| `brain.py` | Gemini/Groq decision engine, JSON repair, 13-action validation |
| `voice_engine.py` | Bengali TTS (edge-tts → mpv → Termux) + Whisper STT |
| `system_control.py` | torch, battery alerts, volume, Wi-Fi, contact matching |
| `security_guard.py` | silent photo + GPS evidence + Bengali summary |
| `memory_engine.py` | SQLite memory, deterministic learning, daily upgrade |
| `comm_manager.py` | calls and SMS through resolved contacts |
| `call_receptionist.py` | dumpsys state machine, answer, greet, log call |
| `adb_automation.py` | open apps, WhatsApp deep links, screenshot → Gemini vision |
| `notification_responder.py` | read/dedupe/log notifications, optional auto-reply |
| `briefing_agent.py` | world news + NIFTY snapshots + Thinker insight |
| `security_policy.py` | blocked financial apps/packages, OTP & bank-alert patterns, screen guard, refusals |
| `actions.py` | the single dispatch table, speaks and records outcomes |
| `telegram_controller.py` | Boss-only bot, voice notes, commands, push helpers |
| `main.py` | wake-lock, five supervised threads, CLI, `doctor` |
| `install.sh` | idempotent Termux setup |
| `tests/` | 411 tests, no phone required |

### Requirements

- Android phone with [Termux](https://f-droid.org/packages/com.termux/) and
  [Termux:API](https://f-droid.org/packages/com.termux.api/) from F-Droid.
- Python 3.9+ (`pkg install python`).
- `termux-api`, `android-tools`, `mpv`, `ffmpeg`, `git` (installed by the script).
- Gemini key (primary) and Groq key (fallback + Whisper).
- Optional: Telegram bot token + your numeric chat id.

### Install

```bash
pkg install -y git
git clone https://github.com/amar768581/Assistant-Orbit-.git
cd Assistant-Orbit-
bash install.sh                  # packages → pip → .env → tests → doctor
nano .env                        # add keys
python main.py doctor
```

### Run

```bash
python main.py                    # daemon + interactive Bengali CLI
python main.py daemon --no-cli    # headless 24/7
python main.py once "টর্চ জালাও"    # one command, Bengali reply
python main.py once "..." --json  # full dispatch JSON
python main.py voice --seconds 6  # one recording → action → spoken reply
python main.py briefing           # market + world news, spoken
python main.py doctor --strict    # exit 1 if not ready
```

The daemon starts five supervised threads:

| Service | What it does |
|---|---|
| Battery | polls `termux-battery-status`; speaks + pushes edge-triggered low/full alerts |
| Reception | while enabled, answers ringing calls, greets, transcribes, pushes a summary |
| Notifications | polls messaging apps, logs to memory, optional Bengali auto-reply |
| Daily | once per day: `daily_self_upgrade()` + morning briefing, pushed and spoken |
| Telegram | long polling, admin-gated, handles text/voice/commands |

A crashed thread is restarted with capped backoff; SIGINT/SIGTERM/`atexit`
stop the threads and release the wake-lock.

### Telegram

Create a bot with **@BotFather**, get your numeric id from **@userinfobot**, put
both in `.env`, then send `/start`. Only that chat id is obeyed.

Text, voice notes and audio are accepted. Intruder checks arrive as a photo with
the Maps link in the caption. Commands: `/start` `/help` `/status` `/briefing`
`/thinker` `/intruder` `/receptionist on|off`.

### Termux:API permissions

Install the Termux:API **app** (the `termux-api` package alone is not enough),
then grant: Microphone, Camera, Location, Notifications, Phone, SMS.

### Wireless ADB

Needed for app launching, screen reading and call answering.

```bash
# 1) Developer options → Wireless debugging → ON
# 2) "Pair device with pairing code"
pkg install -y android-tools
adb pair <IP>:<PAIRING_PORT>
adb connect <IP>:<DEBUG_PORT>
adb devices          # must show "device"
```

Re-run `adb connect` after switching Wi-Fi. On Android 10+ `termux-wifi-enable`
may be refused by the OS; that failure is reported, not hidden.

### 24/7 operation

- Orbit holds `termux-wake-lock` and releases it on shutdown.
- Set Termux battery usage to **Unrestricted**.
- Enable the Termux notification wake-lock.
- Optional Termux:Boot script:
  `~/.termux/boot/orbit.sh` containing
  `cd ~/Assistant-Orbit- && python main.py daemon --no-cli`.

### Financial & secret guardrail

Non-bypassable and on by default — no setting turns it off.

| Layer | What it refuses |
|---|---|
| `brain.validate_decision` | opening a bank/UPI/wallet/trading app; sending an OTP/PIN/password; a financial request mapped to any `open_app` |
| `adb_automation.open_app` | 26 financial apps / 37 packages plus keyword packages (`bank`, `upi`, `pay`, `wallet`, `netbanking`, `finance`, `broking`) |
| `adb_automation.read_screen` | financial foreground app, `FLAG_SECURE`, unreadable/unparseable `dumpsys window` — **before** `screencap` (fail-closed) |
| `notification_responder` | OTP/PIN/CVV/password/verification codes and `debited`/`credited`/`a/c`/UPI/balance alerts — never listed, logged, stored or auto-replied |
| `memory_engine` | sensitive turns, facts and rules are not written to SQLite (`skipped: sensitive`) |
| `telegram_controller` | secret/financial Boss messages are refused before the dispatcher runs |

Every refusal is Bengali, starts with **বস**, and explains why. Verify with
`python main.py doctor` (the **Banking Security Lock** row) or `/status` in Telegram.
Edge cases are tested in [`tests/test_security_policy.py`](tests/test_security_policy.py)
and in each module's suite.

### Security model

- Secrets live only in `.env` (mode 600, git-ignored); logs mask them.
- Telegram is gated by `ADMIN_CHAT_ID`; unset means *nobody*, and strangers get
  one refusal plus a `SECURITY` log line.
- The LLM cannot invent an action outside the 13-action whitelist, and cannot
  supply an unvalidated recipient for calls/SMS.
- Memory, intruder photos and generated audio stay on the device.
- Outbound network is limited to Gemini, Groq, Google News/BBC RSS, Yahoo
  Finance and the Telegram Bot API.

### Testing

```bash
python -m unittest discover -s tests -t .
# Ran 411 tests ... OK
```

Tests use fake runners, fake HTTP sessions, fake bots and temporary SQLite
files. They cover the 13 actions, confirmation/validation, memory learning,
notification dedupe, RSS/quote parsing, Telegram security gating and all CLI
paths — no Termux binary, API key or phone required.

### Limitations

- Hardware paths (ADB, Termux:API, mpv, real Telegram) are verified only with
  fakes in this build environment; they must be confirmed on a phone.
- ADB cannot type Bengali into another app, so WhatsApp/SMS use deep links and
  `termux-sms-send`; the send button may still need a human tap on some ROMs.
- In-call speaker routing is ROM-dependent (best-effort, recorded).
- Gemini vision and LLM-written briefs need a valid key; deterministic Bengali
  fallbacks cover outages.

---

## Project status

| Phase | Scope | Status |
|---|---|---|
| 1 | Config, Brain, Voice, requirements | ✅ |
| 2 | Device control + security evidence | ✅ |
| 3 | Memory, Boss/Thinker persona, comms, receptionist | ✅ |
| 4 | ADB automation, notifications, briefing, dispatcher | ✅ |
| 5 | Telegram bridge, daemon/CLI, installer, docs | ✅ |
| 🔒 | Financial & OTP Security Guardrail (all layers) | ✅ |

**411 tests · 5/5 phases + guardrail · built one file at a time, every checkpoint pushed.**

Personal project — use it, fork it, teach it your own rules.
