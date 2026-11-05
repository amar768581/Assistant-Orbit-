"""
config.py — ORBIT configuration layer.

Everything secret lives in ``.env`` (never hard-coded, never committed).
Everything else — the assistant's identity and runtime behaviour — lives in
the small :class:`Settings` dataclass so the rest of ORBIT imports one object.

    from config import settings
    settings.GEMINI_API_KEY
    settings.name        # "Orbit"
    settings.language    # "bn-IN"
    settings.wake_lock   # True

Environment overrides always win over the file, which makes it easy to test
with ``GEMINI_API_KEY=test python -c "import config"``.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("orbit.config")

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Keys ORBIT cannot do its job without (AI answers + Whisper transcription).
REQUIRED_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY")
# Keys only needed once the Telegram bridge / admin gating is switched on.
OPTIONAL_KEYS = ("TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_ID")


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #

def _parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal .env parser used when ``python-dotenv`` is not installed.

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments, inline
    comments after an unquoted value and single/double quoted values.
    """
    values: Dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value[:1] in ("'", '"'):
            # Quoted: take the quoted chunk literally, ignore trailing comment.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted: strip an inline comment (space + #).
            for marker in (" #", "\t#"):
                idx = value.find(marker)
                if idx != -1:
                    value = value[:idx]
            value = value.strip()

        values[key] = value
    return values


def load_env(path: Path = ENV_PATH, override: bool = False) -> Dict[str, str]:
    """Load ``.env`` into ``os.environ``.

    Uses ``python-dotenv`` when available and falls back to the built-in
    parser. Existing process variables are preserved unless ``override``.
    Returns the key/value pairs read from the file.
    """
    path = Path(path)
    loaded: Dict[str, str] = {}

    try:  # Preferred path.
        from dotenv import dotenv_values  # type: ignore

        if path.is_file():
            loaded = {
                k: v for k, v in dotenv_values(path, encoding="utf-8").items()
                if v is not None
            }
    except Exception:  # ImportError or an unreadable/malformed file.
        loaded = _parse_env_file(path)

    if not loaded:
        loaded = _parse_env_file(path)

    for key, value in loaded.items():
        if override or key not in os.environ:
            os.environ[key] = value

    if path.is_file():
        log.debug("Loaded %d variables from %s", len(loaded), path)
    else:
        log.debug("No .env at %s — using process environment only", path)
    return loaded


def _env_str(name: str, default: str = "", required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            log.warning("Missing required environment variable: %s", name)
        return default
    return value


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring %s=%r — not an integer", name, raw)
        return default


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the assistant configuration."""

    # --- credentials -------------------------------------------------------
    gemini_api_key: str = ""
    groq_api_key: str = ""
    telegram_bot_token: str = ""
    admin_chat_id: Optional[int] = None

    # --- assistant behaviour ----------------------------------------------
    name: str = "Orbit"
    language: str = "bn-IN"
    wake_lock: bool = True

    # --- runtime details (kept here so callers never re-read os.environ) ---
    env_path: Path = field(default=ENV_PATH, repr=False)
    # Long-term memory database (override with ORBIT_MEMORY_DB or the ctor).
    memory_db: Path = field(default_factory=lambda: Path.home() / ".orbit" / "orbit_memory.db")

    # -- helpers ------------------------------------------------------------

    def missing_keys(self, include_telegram: bool = False) -> list:
        """Return the required keys that are still empty."""
        keys = REQUIRED_KEYS if not include_telegram else REQUIRED_KEYS + OPTIONAL_KEYS
        return [key for key in keys if not getattr(self, key.lower())]

    @property
    def ai_ready(self) -> bool:
        """True when at least one LLM provider is configured."""
        return bool(self.gemini_api_key or self.groq_api_key)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.admin_chat_id)

    def redacted(self) -> Dict[str, object]:
        """Safe view for logs: booleans, never key material."""

        def mask(value: str) -> str:
            if not value:
                return "<unset>"
            return f"<set:{len(value)} chars>"

        return {
            "GEMINI_API_KEY": mask(self.gemini_api_key),
            "GROQ_API_KEY": mask(self.groq_api_key),
            "TELEGRAM_BOT_TOKEN": mask(self.telegram_bot_token),
            "ADMIN_CHAT_ID": self.admin_chat_id or "<unset>",
            "name": self.name,
            "language": self.language,
            "wake_lock": self.wake_lock,
            "MEMORY_DB": str(self.memory_db),
        }


@lru_cache(maxsize=1)
def _build_settings(env_file: str = str(ENV_PATH), override: bool = False) -> Settings:
    load_env(Path(env_file), override=override)

    settings = Settings(
        gemini_api_key=_env_str("GEMINI_API_KEY"),
        groq_api_key=_env_str("GROQ_API_KEY"),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        admin_chat_id=_env_int("ADMIN_CHAT_ID"),
        name=_env_str("ASSISTANT_NAME", "Orbit") or "Orbit",
        language=_env_str("ASSISTANT_LANGUAGE", "bn-IN") or "bn-IN",
        wake_lock=_env_bool("WAKE_LOCK", True),
        env_path=Path(env_file),
        memory_db=Path(
            _env_str("ORBIT_MEMORY_DB") or (Path.home() / ".orbit" / "orbit_memory.db")
        ).expanduser(),
    )
    return settings


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide settings object (cached)."""
    if refresh:
        _build_settings.cache_clear()
        load_env(ENV_PATH, override=False)
    return _build_settings(str(ENV_PATH))


# Import-time singleton used across ORBIT.
load_env(ENV_PATH, override=False)
settings = get_settings()


if __name__ == "__main__":  # quick sanity check: python config.py
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"env file      : {ENV_PATH} ({'found' if ENV_PATH.is_file() else 'missing'})")
    for key, value in settings.redacted().items():
        print(f"{key:<20}: {value}")
    missing = settings.missing_keys(include_telegram=True)
    print(f"missing keys  : {', '.join(missing) if missing else 'none'}")
    print(f"ai ready      : {settings.ai_ready}")
