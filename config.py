"""Paths, station config, secrets, and environment defaults (desktop + Android)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent
_mobile_base_dir: Path | None = None

# --- Tunables (override via environment) ---

ALERT_DEDUP_SECONDS = float(os.getenv("ALERT_DEDUP_SECONDS", "90"))
COMMERCIAL_AI_MIN_INTERVAL_SEC = float(os.getenv("COMMERCIAL_AI_MIN_INTERVAL_SEC", "45"))
CONTEST_AI_MIN_INTERVAL_SEC = float(os.getenv("CONTEST_AI_MIN_INTERVAL_SEC", "60"))
DELAY_SMOOTHING_ALPHA = float(os.getenv("DELAY_SMOOTHING_ALPHA", "0.35"))
MAX_DELAY_SYNC_SECONDS = float(os.getenv("MAX_DELAY_SYNC_SECONDS", "300"))

MODEL_SIZE = os.getenv("MODEL_SIZE", "small").strip() or "small"
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu").strip() or "cpu"
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"
VAD_FILTER = os.getenv("VAD_FILTER", "1").strip().lower() not in ("0", "false", "no")

GROQ_CONTEST_MODEL = os.getenv("GROQ_CONTEST_MODEL", "llama-3.1-8b-instant").strip()
COMMERCIAL_DETECT_MODEL = os.getenv("COMMERCIAL_DETECT_MODEL", "gpt-4o-mini").strip()

TRANSCRIBE_WORKERS = int(os.getenv("TRANSCRIBE_WORKERS", "2"))

DELETE_AUDIO_CHUNKS_AFTER_TRANSCRIBE = os.getenv(
    "DELETE_AUDIO_CHUNKS_AFTER_TRANSCRIBE",
    "1" if sys.platform == "android" else "0",
).strip().lower() in ("1", "true", "yes")


def is_android() -> bool:
    if sys.platform == "android":
        return True
    try:
        import android  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return bool(os.getenv("ANDROID_ARGUMENT"))


def set_mobile_base_dir(path: str | Path) -> None:
    global _mobile_base_dir
    _mobile_base_dir = Path(path)


def project_root() -> Path:
    return _PROJECT_ROOT


def data_dir() -> Path:
    if _mobile_base_dir is not None:
        return _mobile_base_dir / "radiomonitor"
    env = os.getenv("RADIO_MONITOR_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return _PROJECT_ROOT / "data"


def _refresh_path_constants() -> None:
    global DATA_DIR, LOG_DIR, STATIONS_FILE, SECRETS_FILE
    DATA_DIR = data_dir()
    LOG_DIR = DATA_DIR / "logs"
    STATIONS_FILE = DATA_DIR / "stations.json"
    SECRETS_FILE = DATA_DIR / "secrets.json"


_refresh_path_constants()


@dataclass
class StationConfig:
    name: str
    url: str
    live_terms: list[str] = field(default_factory=list)
    dev_terms: list[str] = field(default_factory=list)
    chunk_time_seconds: int = 10
    fuzzy_max_distance: int = 0
    enabled: bool = True
    discord_webhook_url: str = ""
    discord_username: str = "Radio Keyword Bot"
    discord_mention: str = ""
    station_timezone: str = "America/Toronto"
    call_in_number: str = ""
    call_in_page_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "live_terms": list(self.live_terms),
            "dev_terms": list(self.dev_terms),
            "chunk_time_seconds": int(self.chunk_time_seconds),
            "fuzzy_max_distance": int(self.fuzzy_max_distance),
            "enabled": bool(self.enabled),
            "discord_webhook_url": self.discord_webhook_url,
            "discord_username": self.discord_username,
            "discord_mention": self.discord_mention,
            "station_timezone": self.station_timezone,
            "call_in_number": self.call_in_number,
            "call_in_page_url": self.call_in_page_url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StationConfig:
        return cls(
            name=str(d.get("name", "")).strip(),
            url=str(d.get("url", "")).strip(),
            live_terms=_str_list(d.get("live_terms")),
            dev_terms=_str_list(d.get("dev_terms")),
            chunk_time_seconds=int(d.get("chunk_time_seconds", 10) or 10),
            fuzzy_max_distance=int(d.get("fuzzy_max_distance", 0) or 0),
            enabled=bool(d.get("enabled", True)),
            discord_webhook_url=str(d.get("discord_webhook_url", "")).strip(),
            discord_username=str(d.get("discord_username", "Radio Keyword Bot")).strip()
            or "Radio Keyword Bot",
            discord_mention=str(d.get("discord_mention", "")).strip(),
            station_timezone=str(d.get("station_timezone", "America/Toronto")).strip()
            or "America/Toronto",
            call_in_number=str(d.get("call_in_number", "")).strip(),
            call_in_page_url=str(d.get("call_in_page_url", "")).strip(),
        )


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _bundled_stations_path() -> Path:
    return _PROJECT_ROOT / "stations.json"


def ensure_app_initialized() -> None:
    """Create data dirs and copy default stations.json on first run."""
    _refresh_path_constants()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not STATIONS_FILE.is_file():
        bundled = _bundled_stations_path()
        if bundled.is_file():
            shutil.copy2(bundled, STATIONS_FILE)
        else:
            STATIONS_FILE.write_text("[]\n", encoding="utf-8")


def load_stations() -> list[StationConfig]:
    ensure_app_initialized()
    try:
        raw = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[StationConfig] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            out.append(StationConfig.from_dict(item))
    return out


def save_stations(stations: list[StationConfig]) -> None:
    ensure_app_initialized()
    payload = [s.to_dict() for s in stations]
    STATIONS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_secrets() -> dict[str, str]:
    ensure_app_initialized()
    if not SECRETS_FILE.is_file():
        return {}
    try:
        raw = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and str(v).strip()}


def save_secrets(secrets: dict[str, str]) -> None:
    ensure_app_initialized()
    clean = {k: v.strip() for k, v in secrets.items() if v and v.strip()}
    SECRETS_FILE.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")


def apply_secrets_to_environ() -> None:
    """Push secrets.json values into os.environ for engine and transcribers."""
    for key, val in load_secrets().items():
        os.environ[key] = val.strip()


def reload_paths_after_mobile_base() -> None:
    _refresh_path_constants()


# Convenience: read keys from env (after apply_secrets_to_environ on mobile startup)
def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


GROQ_API_KEY = _env("GROQ_API_KEY")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")


def refresh_env_keys() -> None:
    """Re-read module-level key constants after secrets change."""
    global GROQ_API_KEY, OPENAI_API_KEY, DISCORD_WEBHOOK_URL
    GROQ_API_KEY = _env("GROQ_API_KEY")
    OPENAI_API_KEY = _env("OPENAI_API_KEY")
    DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")


def default_transcribe_backend_name() -> str:
    explicit = os.getenv("TRANSCRIBE_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if is_android():
        return "api"
    return "local"
