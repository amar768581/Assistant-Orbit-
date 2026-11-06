#!/usr/bin/env bash
# =============================================================================
# ORBIT — idempotent Termux installer
#
#   bash install.sh                 # install packages, deps, .env, run tests + doctor
#   bash install.sh --skip-tests    # faster setup
#   bash install.sh --skip-doctor
#
# Safe to run again after a `pkg upgrade` or a fresh `git pull`: it only fills
# in what is missing and never overwrites an existing .env.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PACKAGES=(python termux-api android-tools mpv ffmpeg git)
SKIP_TESTS=0
SKIP_DOCTOR=0

for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-doctor) SKIP_DOCTOR=1 ;;
    -h|--help)
      awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

IN_TERMUX=0
if [ -n "${PREFIX:-}" ] && [ -d "${PREFIX:-}" ] && command -v pkg >/dev/null 2>&1; then
  IN_TERMUX=1
fi

if [ "$IN_TERMUX" -eq 1 ]; then
  say "Termux detected — installing system packages"
  # pkg install is idempotent; already-installed packages are skipped quickly.
  pkg update -y || warn "pkg update failed (offline?) — continuing with what is installed"
  pkg install -y "${PACKAGES[@]}"
else
  warn "Not running inside Termux. Skipping 'pkg install'."
  warn "On the phone run:  pkg install -y ${PACKAGES[*]}"
fi

command -v python >/dev/null 2>&1 || die "python not found. In Termux: pkg install python"
PY=(python)

say "Installing Python dependencies"
"${PY[@]}" -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip self-upgrade failed — continuing"
"${PY[@]}" -m pip install -r requirements.txt

say "Preparing .env"
if [ -f .env ]; then
  echo "    .env already exists — left untouched."
else
  cp .env.example .env
  chmod 600 .env
  echo "    Created .env (mode 600). Fill in GEMINI_API_KEY, GROQ_API_KEY,"
  echo "    TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID:  nano .env"
fi

say "Checking Termux:API app permissions (manual, one time)"
cat <<'EOF'
    Install the "Termux:API" app from F-Droid, then allow:
      • Microphone        (voice commands)
      • Camera            (intruder check)
      • Location          (intruder check)
      • Notifications     (reading messages)
      • Phone / SMS       (calls, texts)
    Android Settings → Apps → Termux:API → Permissions.
EOF

if [ "$SKIP_TESTS" -eq 0 ]; then
  say "Running the unit test suite"
  "${PY[@]}" -m unittest discover -s tests -t .
else
  warn "Skipping tests (--skip-tests)."
fi

if [ "$SKIP_DOCTOR" -eq 0 ]; then
  say "Environment check"
  "${PY[@]}" main.py doctor || warn "Doctor found missing items — see the table above."
else
  warn "Skipping doctor (--skip-doctor)."
fi

say "Done"
cat <<'EOF'
    Next steps:
      1. nano .env                     # keys + ADMIN_CHAT_ID
      2. python main.py doctor         # all green?
      3. python main.py once "টর্চ জালাও"
      4. python main.py                # 24/7 daemon + Bengali CLI
      5. python main.py daemon --no-cli  # headless (Termux:Boot friendly)

    Telegram: message your bot /start from the ADMIN_CHAT_ID account.
EOF
