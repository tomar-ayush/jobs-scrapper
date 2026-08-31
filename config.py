"""Centralized configuration — loaded from environment variables."""

import os
from pathlib import Path

# ── Scraping ──────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    "Software Engineer",
    "Backend Developer",
    "Python Developer",
    "Data Engineer",
    "AI Engineer",
    "LLM Engineer",
    "Full Stack Developer",
    "Platform Engineer",
]

LOCATION = "India"
RESULTS_PER_QUERY = 50
HOURS_OLD = 4  # only jobs from the last 4 hours

# ── Astra DB ──────────────────────────────────────────────────────────

ASTRA_DB_BUNDLE_PATH = os.getenv(
    "ASTRA_DB_BUNDLE_PATH",
    str(Path(__file__).parent / "secure-connect-jobs_store.zip"),
)
ASTRA_DB_CLIENT_ID = os.getenv("ASTRA_DB_CLIENT_ID", "")
ASTRA_DB_CLIENT_SECRET = os.getenv("ASTRA_DB_CLIENT_SECRET", "")
ASTRA_DB_TOKEN = os.getenv("ASTRA_DB_TOKEN", "")
ASTRA_DB_KEYSPACE = os.getenv("ASTRA_DB_KEYSPACE", "jobs_store")

# ── Gemini ────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = (
    "gemini-3.7-flash"  # fast + cheap, good enough for scoring
)
FALLBACK_MODELS = [
    GEMINI_MODEL,
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

# ── Email ─────────────────────────────────────────────────────────────

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")  # your Gmail address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")  # Gmail App Password
NOTIFY_EMAILS: list[str] = [
    addr.strip()
    for addr in os.getenv("NOTIFY_EMAIL", "").split(",")
    if addr.strip()
]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Scoring & Notification ────────────────────────────────────────────

MIN_SCORE_TO_NOTIFY = 6  # Gemini scores 1-10; email only jobs >= this
MAX_JOBS_IN_EMAIL = 50  # top N jobs per email

# ── Resume ────────────────────────────────────────────────────────────

RESUME_TEXT_PATH = Path(__file__).parent / "resume_text.txt"


def validate():
    """Check that all required env vars are set. Raises ValueError if not."""
    required = {
        "ASTRA_DB_TOKEN": ASTRA_DB_TOKEN,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "SMTP_EMAIL": SMTP_EMAIL,
        "SMTP_PASSWORD": SMTP_PASSWORD,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    if not NOTIFY_EMAILS:
        raise ValueError(
            "NOTIFY_EMAIL must contain at least one email address."
        )

    if not RESUME_TEXT_PATH.exists():
        raise FileNotFoundError(
            f"Resume text file not found: {RESUME_TEXT_PATH}"
        )

    if not Path(ASTRA_DB_BUNDLE_PATH).exists():
        raise FileNotFoundError(
            f"Astra secure connect bundle not found: {ASTRA_DB_BUNDLE_PATH}"
        )
