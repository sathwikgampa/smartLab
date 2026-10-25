"""Application configuration.

All settings can be overridden with environment variables so the same code runs
in development, CI (pytest) and production without edits.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    for candidate in [BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
        if candidate.is_file():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break


_load_env_file()

# Default Supabase PostgreSQL connection URI (used automatically when no .env or environment variable is set)
DEFAULT_SUPABASE_URL = "postgresql://postgres.mdqojjvdxjlqbiymwxle:mkwajXpsOyMTsPam@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"

if not os.getenv("SMARTLAB_DATABASE_URL") and not os.getenv("DATABASE_URL"):
    os.environ["SMARTLAB_DATABASE_URL"] = DEFAULT_SUPABASE_URL


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


POSTGRES_SCHEMES = ("postgresql://", "postgres://", "postgresql+psycopg2://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and url.strip().lower().startswith(POSTGRES_SCHEMES)


def normalize_postgres_url(url: str) -> str:
    """libpq only accepts ``postgresql://`` / ``postgres://``; also strip SQLAlchemy-style driver suffixes."""
    url = url.strip()
    scheme, sep, rest = url.partition("://")
    return f"postgresql{sep}{rest}" if scheme.lower() in {"postgres", "postgresql", "postgresql+psycopg2"} else url


def resolve_database(environ: dict[str, str] | None = None) -> tuple[str, str | None]:
    """Pick the database engine: ``SMARTLAB_DATABASE_URL``, then ``DATABASE_URL``, else local SQLite.

    Returns ``(engine, url)`` where ``engine`` is ``"postgresql"`` or ``"sqlite"``. ``DATABASE_URL`` is a
    generic name other tools also set, so a non-PostgreSQL value there is ignored; a non-PostgreSQL
    ``SMARTLAB_DATABASE_URL`` is a configuration error (set to 'sqlite' to explicitly use local SQLite).
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("SMARTLAB_DATABASE_URL") or "").strip()
    if explicit:
        if explicit.lower() == "sqlite":
            return "sqlite", None
        if not is_postgres_url(explicit):
            raise ValueError("SMARTLAB_DATABASE_URL must be a postgresql:// or postgres:// URL "
                             "(or 'sqlite' to use the local SQLite database).")
        return "postgresql", normalize_postgres_url(explicit)
    generic = (env.get("DATABASE_URL") or "").strip()
    if is_postgres_url(generic):
        return "postgresql", normalize_postgres_url(generic)
    return "sqlite", None


def redact_url(url: str | None) -> str:
    """``postgresql://user:secret@host/db`` -> ``postgresql://user:***@host/db`` (safe to log)."""
    if not url:
        return ""
    url = re.sub(r"(?i)(password=)[^&]*", r"\1***", url)
    scheme, sep, rest = url.partition("://")
    creds, at, host = rest.rpartition("@")
    if not at or ":" not in creds:
        return url
    return f"{scheme}{sep}{creds.split(':', 1)[0]}:***@{host}"


_DB_ENGINE, _DB_URL = resolve_database()


class Config:
    """Default (development) configuration."""

    SECRET_KEY = os.getenv("SMARTLAB_SECRET_KEY", "dev-only-change-me")
    DATA_DIR = Path(os.getenv("SMARTLAB_DATA_DIR", BASE_DIR / "instance"))
    DATABASE_PATH = Path(os.getenv("SMARTLAB_DATABASE", DATA_DIR / "smartlab.db"))
    # PostgreSQL (e.g. Supabase) when SMARTLAB_DATABASE_URL / DATABASE_URL is set; otherwise DATABASE_PATH.
    DATABASE_ENGINE = _DB_ENGINE
    DATABASE_URL = _DB_URL
    # Connections are opened lazily. Size for the busiest case in one process: every Gunicorn thread
    # (gunicorn.conf.py, 16) plus every agent-tool thread (agents/tools.py, 8) holding one at once.
    DB_POOL_MAX = int(os.getenv("SMARTLAB_DB_POOL_MAX", "24"))
    UPLOAD_DIR = Path(os.getenv("SMARTLAB_UPLOAD_DIR", DATA_DIR / "uploads"))
    # The vector index is keyed by chunk id, so a PostgreSQL database gets its own index directory
    # rather than reusing one built from the SQLite file (it is rebuilt from the chunks table).
    INDEX_DIR = Path(os.getenv("SMARTLAB_INDEX_DIR", DATA_DIR / ("index" if _DB_ENGINE == "sqlite" else "index-postgres")))

    # Upload limits (bytes). Flask rejects bodies larger than MAX_CONTENT_LENGTH with 413.
    MAX_CONTENT_LENGTH = int(os.getenv("SMARTLAB_MAX_UPLOAD_MB", "25")) * 1024 * 1024
    MAX_PDF_PAGES = int(os.getenv("SMARTLAB_MAX_PDF_PAGES", "600"))
    MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard

    # RAG settings
    EMBEDDING_BACKEND = os.getenv("SMARTLAB_EMBEDDINGS", "auto")  # auto | sentence-transformers | hashing
    EMBEDDING_MODEL = os.getenv("SMARTLAB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    VECTOR_BACKEND = os.getenv("SMARTLAB_VECTOR_STORE", "auto")  # auto | faiss | numpy
    CHUNK_SIZE = int(os.getenv("SMARTLAB_CHUNK_SIZE", "700"))  # characters
    CHUNK_OVERLAP = int(os.getenv("SMARTLAB_CHUNK_OVERLAP", "120"))
    TOP_K = int(os.getenv("SMARTLAB_TOP_K", "5"))
    # Minimum cosine similarity for a chunk to count as evidence. Below this the
    # assistant reports missing evidence instead of answering. When unset, each
    # embedder's calibrated default is used (lexical and dense scores differ).
    MIN_EVIDENCE_SCORE = float(os.environ["SMARTLAB_MIN_EVIDENCE_SCORE"]) if os.getenv("SMARTLAB_MIN_EVIDENCE_SCORE") else None

    # LLM settings (Claude). If no credentials are available the app falls back to
    # deterministic extractive answers and local image heuristics.
    LLM_ENABLED = _env_bool("SMARTLAB_LLM_ENABLED", True)
    LLM_MODEL = os.getenv("SMARTLAB_LLM_MODEL", "claude-opus-5-5")
    LLM_EFFORT = os.getenv("SMARTLAB_LLM_EFFORT", "medium")

    # Literature search (Europe PMC public REST API, no key needed)
    LITERATURE_ONLINE = _env_bool("SMARTLAB_LITERATURE_ONLINE", True)
    LITERATURE_TIMEOUT = float(os.getenv("SMARTLAB_LITERATURE_TIMEOUT", "8"))

    # Lab co-pilot (agent) settings.
    # There is no authentication yet, so every request is scoped to this organisation.
    # TODO(auth): take the organisation from the signed-in user instead of this default.
    DEFAULT_ORGANIZATION_ID = int(os.getenv("SMARTLAB_DEFAULT_ORG_ID", "1"))
    AGENT_TOOL_TIMEOUT_S = float(os.getenv("SMARTLAB_AGENT_TOOL_TIMEOUT_S", "10"))
    AGENT_TIMEOUT_S = float(os.getenv("SMARTLAB_AGENT_TIMEOUT_S", "90"))  # whole request
    AGENT_LLM_TIMEOUT_S = float(os.getenv("SMARTLAB_AGENT_LLM_TIMEOUT_S", "60"))  # one model call
    AGENT_MAX_LLM_CALLS = int(os.getenv("SMARTLAB_AGENT_MAX_LLM_CALLS", "6"))  # including retries
    AGENT_MAX_QUESTION_CHARS = 2000
    # Rate limits for /api/chat, /api/chat/stream and /api/agent/uploads (requests per minute per client).
    AGENT_RATE_LIMIT_PER_MIN = int(os.getenv("SMARTLAB_AGENT_RATE_LIMIT_PER_MIN", "20"))
    AGENT_UPLOAD_MAX_MB = int(os.getenv("SMARTLAB_AGENT_UPLOAD_MAX_MB", "8"))

    # SmartLab Visualizer (scientific image generation). auto | openai | google | off. "auto" uses
    # OPENAI_API_KEY when set, else GOOGLE_API_KEY / GEMINI_API_KEY; with neither the feature is disabled.
    IMAGE_PROVIDER = os.getenv("SMARTLAB_IMAGE_PROVIDER", "auto")
    IMAGE_TIMEOUT_S = float(os.getenv("SMARTLAB_IMAGE_TIMEOUT_S", "120"))  # below Gunicorn's 150 s worker timeout
    # Generations per minute per client (each one is a paid provider call).
    IMAGE_RATE_LIMIT_PER_MIN = int(os.getenv("SMARTLAB_IMAGE_RATE_LIMIT_PER_MIN", "6"))

    CORS_ORIGINS = [
        o.strip()
        for o in os.getenv(
            "SMARTLAB_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174,http://localhost:5175,http://127.0.0.1:5175,http://localhost:5176,http://127.0.0.1:5176,http://localhost:3000,http://127.0.0.1:3000,https://smart-lab-ai.onrender.com,https://smartlab-ai.onrender.com",
        ).split(",")
        if o.strip()
    ] + [r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", r"^https://[a-zA-Z0-9-]+\.onrender\.com$"]

    SEED_DEMO_DATA = _env_bool("SMARTLAB_SEED_DEMO", True)
    # Number of trusted reverse-proxy hops in front of the app (Render: 1). 0 trusts no X-Forwarded-* header.
    TRUST_PROXY_HOPS = int(os.getenv("SMARTLAB_TRUST_PROXY_HOPS", "0"))
    # Lab co-pilot demo SOPs, stock lots and fixes (added only to a database that holds the demo records).
    SEED_AGENT_DEMO = _env_bool("SMARTLAB_SEED_AGENT_DEMO", True)
    TESTING = False

    # Authentication. Every /api route except /api/health and /api/auth/* requires a signed-in session.
    AUTH_REQUIRED = _env_bool("SMARTLAB_AUTH_REQUIRED", True)
    SESSION_DAYS = int(os.getenv("SMARTLAB_SESSION_DAYS", "14"))
    AUTH_COOKIE_NAME = "smartlab_session"
    # "auto": Secure flag only when the request arrived over HTTPS. Set "true" in production behind HTTPS.
    AUTH_COOKIE_SECURE = os.getenv("SMARTLAB_COOKIE_SECURE", "auto").strip().lower()
    RESET_TOKEN_MINUTES = int(os.getenv("SMARTLAB_RESET_TOKEN_MINUTES", "30"))
    # Base URL of the web app, used to build password-reset links.
    PUBLIC_URL = os.getenv("SMARTLAB_PUBLIC_URL", "https://smart-lab-ai.onrender.com").rstrip("/")
    # Outgoing mail for password resets. Without SMTP_HOST the reset link is written to the server log.
    SMTP_HOST = os.getenv("SMARTLAB_SMTP_HOST", "").strip() or None
    SMTP_PORT = int(os.getenv("SMARTLAB_SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMARTLAB_SMTP_USER", "").strip() or None
    SMTP_PASSWORD = os.getenv("SMARTLAB_SMTP_PASSWORD") or None
    SMTP_FROM = os.getenv("SMARTLAB_SMTP_FROM", "").strip() or None
    SMTP_STARTTLS = _env_bool("SMARTLAB_SMTP_STARTTLS", True)
    # Attempts allowed per 15 minutes, per client address and per email address.
    LOGIN_ATTEMPT_LIMIT = int(os.getenv("SMARTLAB_LOGIN_ATTEMPT_LIMIT", "10"))


class TestConfig(Config):
    """Configuration used by the pytest suite: offline, deterministic, isolated."""

    TESTING = True
    # The feature tests exercise the API directly; tests/test_auth.py turns this on.
    AUTH_REQUIRED = False
    # Never inherit a cloud database from the environment or .env; PostgreSQL tests opt in explicitly.
    DATABASE_ENGINE = "sqlite"
    DATABASE_URL = None
    LLM_ENABLED = False
    LITERATURE_ONLINE = False
    IMAGE_PROVIDER = "off"  # tests install a fake generator
    EMBEDDING_BACKEND = "hashing"
    VECTOR_BACKEND = "numpy"
    SEED_DEMO_DATA = True
    SEED_AGENT_DEMO = False  # the co-pilot scenario tests opt in (tests/test_agent_scenarios.py)
    PUBLIC_URL = "http://localhost:5173"

