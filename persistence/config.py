"""Environment-based database configuration. Never returns or logs credentials.

HUB_ENV                       development (default) | production
HUB_DB_BACKEND                sqlite | postgres (mandatory in production)
HUB_DATABASE_URL              runtime PostgreSQL URL (secret)
HUB_MIGRATION_DATABASE_URL    optional higher-privilege URL used only by the migration/import tool (secret)
HUB_DB_SSLMODE / HUB_DB_SSLROOTCERT   TLS settings; production requires certificate verification
HUB_DB_POOL_MIN / HUB_DB_POOL_MAX / HUB_DB_POOL_TIMEOUT / HUB_DB_CONNECT_TIMEOUT   connection pool tuning
"""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = ROOT / "data" / "hub.sqlite3"
BACKENDS = {"sqlite", "postgres"}
ENVIRONMENTS = {"development", "production"}
SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
VERIFYING_SSL_MODES = {"verify-ca", "verify-full"}
RUNTIME_URL_VAR = "HUB_DATABASE_URL"
MIGRATION_URL_VAR = "HUB_MIGRATION_DATABASE_URL"


class ConfigError(Exception):
    pass


def _env(env):
    return env if env is not None else os.environ


def environment(env=None):
    value = _env(env).get("HUB_ENV", "development").strip().lower() or "development"
    if value not in ENVIRONMENTS:
        raise ConfigError(f"HUB_ENV must be one of {sorted(ENVIRONMENTS)}")
    return value


def is_production(env=None):
    return environment(env) == "production"


def backend(env=None):
    values = _env(env)
    value = values.get("HUB_DB_BACKEND", "").strip().lower()
    if not value:
        if is_production(values):
            raise ConfigError("HUB_DB_BACKEND must be set explicitly when HUB_ENV=production")
        if values.get(RUNTIME_URL_VAR, "").strip():
            raise ConfigError(f"{RUNTIME_URL_VAR} is set but HUB_DB_BACKEND is not; refusing to fall back to SQLite")
        return "sqlite"
    if value not in BACKENDS:
        raise ConfigError(f"HUB_DB_BACKEND must be one of {sorted(BACKENDS)}")
    return value


def postgres_url(var=RUNTIME_URL_VAR, env=None):
    url = _env(env).get(var, "").strip()
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
    """Scheme and database name only; user, password, host, port and query string are dropped."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://[redacted]{parts.path}"
    except ValueError:
        return "[unparseable url]"


def database_name(url):
    try:
        return urlsplit(url).path.lstrip("/")
    except ValueError:
        return ""


def _int(values, name, default, low, high):
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        number = float(raw) if isinstance(default, float) else int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def pool_settings(env=None):
    values = _env(env)
    settings = {
        "min_size": _int(values, "HUB_DB_POOL_MIN", 1, 0, 20),
        "max_size": _int(values, "HUB_DB_POOL_MAX", 5, 1, 50),
        "timeout": _int(values, "HUB_DB_POOL_TIMEOUT", 5.0, 0.1, 120.0),
        "connect_timeout": _int(values, "HUB_DB_CONNECT_TIMEOUT", 5, 1, 60),
    }
    if settings["min_size"] > settings["max_size"]:
        raise ConfigError("HUB_DB_POOL_MIN must not exceed HUB_DB_POOL_MAX")
    return settings


def connect_kwargs(url, env=None):
    """libpq keyword arguments (TLS and timeouts). Production insists on certificate verification."""
    values = _env(env)
    try:
        url_mode = (parse_qs(urlsplit(url).query).get("sslmode") or [""])[0]
    except ValueError:
        url_mode = ""
    mode = url_mode or values.get("HUB_DB_SSLMODE", "").strip().lower()
    if mode and mode not in SSL_MODES:
        raise ConfigError(f"HUB_DB_SSLMODE must be one of {sorted(SSL_MODES)}")
    if is_production(values):
        mode = mode or "verify-full"
        if mode not in VERIFYING_SSL_MODES:
            raise ConfigError("HUB_ENV=production requires sslmode verify-full (or verify-ca); certificate verification cannot be disabled")
    kwargs = {"connect_timeout": pool_settings(values)["connect_timeout"]}
    if mode:
        kwargs["sslmode"] = mode
    root = values.get("HUB_DB_SSLROOTCERT", "").strip()
    if root:
        if not Path(root).is_file():
            raise ConfigError("HUB_DB_SSLROOTCERT does not point to a readable file")
        kwargs["sslrootcert"] = root
    return kwargs
