# ORBIT 🪐 — Windows PC Personal AI Assistant

**বাংলা-প্রথম ব্যক্তিগত AI সহকারী — এখন Windows PC-র জন্য।**
A Bengali-first personal assistant that runs directly on Windows 10/11: chat in
Bengali/English/Banglish, control the PC, hear natural Bengali speech, and keep
a local SQLite memory — with a non-bypassable banking/OTP guardrail.

> **Status:** all phases complete, **441 tests green**, no Termux or Android
> binaries required for the PC feature set. Phone actions (call/SMS/WhatsApp/
> screen) remain optional through ADB and are disabled on Windows by default.

---

## ⚡ Quick start (Windows)

```powershell
git clone https://github.com/amar768581/Assistant-Orbit-.git
cd Assistant-Orbit-
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer checks Python 3.10+, installs `requirements.txt`, creates `.env`
from `.env.example`, makes `~\.orbit\`, and runs `doctor`.

Then edit `.env` and add at least one AI key:

```ini
GEMINI_API_KEY=...
GROQ_API_KEY=...          # also used for voice transcription
TELEGRAM_BOT_TOKEN=...    # optional
ADMIN_CHAT_ID=...         # optional
```

Start chatting:

```powershell
python main.py
# বস, Orbit তৈরি। কথা বলুন (বন্ধ করতে 'exit')।
```

or double-click `run_orbit.bat`.

---

## 🧠 What it does

| Feature | Bengali | Details |
|---|---|---|
| 💬 Chat | বাংলা/English/Banglish | Gemini → Groq fallback, always answers Boss |
| 🖥 PC control | কম্পিউটার کنترل | open websites, folders and desktop apps |
| 📊 PC health | সিস্টেম হেলথ | CPU, RAM, disk, battery, uptime, top processes (psutil) |
| 🔊 Voice out | বাংলা কথা | edge-tts neural voice + pygame (no media-player popups) |
| 🎙 Voice in | ভয়েস কমান্ড | microphone → Groq Whisper (optional sounddevice) |
| 🧠 Memory | স্থানীয় মেমোরি | SQLite at `~\.orbit\orbit_memory.db` |
| 📰 Briefing | সকালের ব্রিফিং | market + world news, spoken |
| 📲 Telegram | দূর থেকে নিয়ন্ত্রণ | optional, admin-only |
| 🔒 Guardrail | ব্যাংকিং সুরক্ষা | bank/UPI/wallet apps, OTP/PIN, bank alerts blocked everywhere |

### PC commands you can say

```powershell
python main.py once "notepad খোলো"
python main.py once "github.com খোলো"
python main.py once "Downloads ফোল্ডার খোলো"
python main.py once "কম্পিউটারের অবস্থা কেমন?"
python main.py once "আজকের বাজার কেমন?"
```

New actions: `open_website`, `open_folder`, `pc_health` (plus the existing
`open_app`, `battery`, `volume`, `speak`, …). `open_app` opens real desktop apps
such as `notepad`, `chrome`, `code`, `calculator`.

---

## 🖥 PC health

```powershell
python main.py health
# বস, সিপিইউ ১২.৫ শতাংশ, র‍্যাম ৮.০ / ১৬.০ জিবি (৫০%), ডিস্কে ২০০.০ জিবি খালি। ব্যাটারি ৮৭ শতাংশ, চার্জে।
```

`doctor` checks the same things and prints a readiness table:

```powershell
python main.py doctor
```

Rows include `.env`, API keys, SQLite, Python, Windows, **PC Health**, voice
output, microphone, **Banking Security Lock**, Telegram and optional ADB.

---

## 🔊 Voice on Windows

- **Output:** `edge-tts` renders Bengali MP3 to a temp folder, `pygame.mixer`
  plays it in-process, and the file is deleted immediately afterwards — no mpv,
  no `termux-tts-speak`, no external window.
- **Fallback:** Windows SAPI through PowerShell if edge-tts/pygame is missing.
- **Input:** install `sounddevice` + `soundfile` (already in requirements) and
  run `python main.py voice`.

```powershell
python main.py voice --seconds 6
```

---

## 💾 Where data lives

Everything is under your Windows home directory:

| Path | Contents |
|---|---|
| `~\.orbit\orbit_memory.db` | SQLite episodes, facts, rules, thinker notes |
| `~\.orbit\orbit.log` | rotating log file |
| `~\.orbit\recordings\` | temporary microphone recordings |
| `~\.orbit\contacts.json` | optional `[{"name": "...", "number": "..."}]` |
| `%TEMP%\orbit_tts\` | TTS files (deleted right after speaking) |

Override the database with `ORBIT_MEMORY_DB` in `.env`.

---

## 🔒 Banking & OTP guardrail (cannot be disabled)

| Layer | Refuses |
|---|---|
| `brain.validate_decision` | opening a bank/UPI/wallet/trading app or website, PIN/OTP entry, financial requests |
| `pc_automation` | launching financial desktop apps, opening banking URLs |
| `adb_automation` (optional phone) | blocked packages and `FLAG_SECURE` screenshots |
| `notification_responder` | OTP/PIN/CVV/password and debit/credit alerts — never stored or auto-replied |
| `memory_engine` | sensitive turns, facts and rules are not written to SQLite |
| `telegram_controller` | secret/financial messages refused before the dispatcher |

Every refusal is Bengali and starts with **বস**. Verify with
`python main.py doctor` → **Banking Security Lock**. See
`tests/test_security_policy.py` and `tests/test_pc_automation.py`.

---

## 🛠 Requirements

- Windows 10/11, Python 3.10+
- `pip install -r requirements.txt`:
  `requests`, `python-dotenv`, `edge-tts`, `pygame`, `psutil`, `groq`,
  `pydantic`, `sounddevice`, `soundfile`
- Optional: ADB + a paired Android phone for phone-specific actions.

No mpv, no Termux, no Linux-only commands.

---

## 🧱 Project layout

| File | Role |
|---|---|
| `main.py` | CLI (`chat`, `once`, `voice`, `briefing`, `health`, `doctor`, `daemon`) |
| `brain.py` | LLM decision contract + financial guard |
| `actions.py` | dispatch table and honest Bengali replies |
| `pc_automation.py` | browser, folders, apps, psutil health |
| `system_control.py` | battery, volume, Wi-Fi, contacts, monitor |
| `voice_engine.py` | edge-tts + pygame TTS, Groq Whisper STT |
| `memory_engine.py` | local SQLite memory |
| `security_policy.py` | one source of truth for the guardrail |
| `adb_automation.py` / `notification_responder.py` | optional phone features |
| `telegram_controller.py` | admin-only Telegram bridge |
| `briefing_agent.py`, `call_receptionist.py`, `comm_manager.py` | news, calls, comms |
| `install.ps1`, `run_orbit.bat` | Windows setup and launcher |
| `tests/` | 441 tests, no hardware or network needed |

---

## 🧪 Tests

```powershell
python -m unittest discover -s tests -t .
# Ran 441 tests ... OK
```

The suite fakes psutil, pygame, the OS opener, HTTP and ADB — it opens no
window, plays no sound and needs no API key.

---

## 🩺 Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found | Reinstall Python and tick “Add python.exe to PATH” |
| No sound | `pip install pygame edge-tts`; check the default output device |
| Mic does not record | `pip install sounddevice soundfile`; allow microphone access |
| Wi-Fi toggle denied | Windows needs admin; use Settings or run elevated |
| Volume moves in steps | Windows media keys; absolute mixing needs a third-party mixer |
| `psutil` missing | `pip install psutil` (the PC Health row turns yellow) |
| Gemini 429 | Groq fallback is automatic; check quotas |
| Bank app will not open | By design — the security lock cannot be disabled |

---

## 📜 License / safety

Local-first: memory stays on your PC, keys stay in `.env` (git-ignored).
The guardrail is intentionally broad: blocking a harmless app is acceptable,
letting ORBIT touch a bank is not.
