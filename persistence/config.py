"""Environment-based database configuration. Never returns or logs credentials."""
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = ROOT / "data" / "hub.sqlite3"
BACKENDS = {"sqlite", "postgres"}


class ConfigError(Exception):
    pass


def backend(env=None):
    value = (env if env is not None else os.environ).get("HUB_DB_BACKEND", "sqlite").strip().lower()
    if value not in BACKENDS:
        raise ConfigError(f"HUB_DB_BACKEND must be one of {sorted(BACKENDS)}")
    return value


def postgres_url(var="HUB_DATABASE_URL", env=None):
    url = (env if env is not None else os.environ).get(var, "").strip()
    if not url:
        raise ConfigError(f"{var} is not set")
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        scheme = ""
    if scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"{var} must be a postgresql:// URL")
    return url


def redact(url):
    """Host and database only; user, password and query string are dropped."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or '?'}{parts.path}"

