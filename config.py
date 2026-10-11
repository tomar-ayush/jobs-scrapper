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

# LinkedIn (public guest pages, no login)
LINKEDIN_UNDER_10_APPLICANTS = True  # LinkedIn's "Under 10 applicants" filter (f_EA)
LINKEDIN_EXPERIENCE_LEVELS = "1,2"  # f_E: 1=Internship, 2=Entry level, 3=Associate
LINKEDIN_REQUEST_DELAY = 2.0  # seconds between LinkedIn requests (avoids 429s)

# Company job boards (Greenhouse / Lever / Ashby), managed from the frontend
ATS_HOURS_OLD = 24  # boards are polled; DB dedup drops repeats
ATS_TITLE_KEYWORDS = ["engineer", "developer", "software", "sde", "programmer"]
ATS_EXCLUDE_TITLE_KEYWORDS = [
    "senior", "sr", "staff", "principal", "lead", "manager",
    "director", "head of", "architect", "vp",
]
# Empty list = any location
ATS_LOCATION_KEYWORDS = [
    "india", "remote", "bengaluru", "bangalore", "hyderabad", "pune",
    "mumbai", "delhi", "gurgaon", "gurugram", "noida", "chennai",
]

# ── Astra DB ──────────────────────────────────────────────────────────

ASTRA_DB_API_ENDPOINT = os.getenv("ASTRA_DB_API_ENDPOINT", "").strip()
ASTRA_DB_TOKEN = os.getenv("ASTRA_DB_TOKEN", "").strip()

# ── Gemini ────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
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

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "").strip()  # your Gmail address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()  # Gmail App Password
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

# ── Settings editable from the frontend ───────────────────────────────
# name -> parser that turns the stored string into a value (ValueError if bad).
# Saved values override the defaults above on every pipeline run.


def _int_between(lo: int, hi: int):
    def parse(raw: str) -> int:
        try:
            n = int(raw)
        except ValueError:
            raise ValueError("must be a whole number") from None
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return n
    return parse


def _float_between(lo: float, hi: float):
    def parse(raw: str) -> float:
        try:
            n = float(raw)
        except ValueError:
            raise ValueError("must be a number") from None
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo:g} and {hi:g}")
        return n
    return parse


def _word_list(allow_empty: bool):
    """Comma-separated words -> lowercase list without blanks or repeats."""
    def parse(raw: str) -> list[str]:
        words = list(dict.fromkeys(w.strip().lower() for w in raw.split(",") if w.strip()))
        if not words and not allow_empty:
            raise ValueError("needs at least one word")
        if len(words) > 100 or any(len(w) > 40 for w in words):
            raise ValueError("allows up to 100 words of up to 40 characters")
        return words
    return parse


def _bool(raw: str) -> bool:
    if raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    raise ValueError("must be true or false")


def _location(raw: str) -> str:
    if not 1 <= len(raw.strip()) <= 60:
        raise ValueError("must be 1-60 characters")
    return raw.strip()


def _levels(raw: str) -> str:
    levels = [x.strip() for x in raw.split(",")]
    if not all(x in {"1", "2", "3", "4", "5", "6"} for x in levels):
        raise ValueError("must be comma-separated numbers from 1 to 6")
    return ",".join(levels)


EDITABLE_SETTINGS = {
    "LOCATION": _location,
    "HOURS_OLD": _int_between(1, 168),
    "LINKEDIN_UNDER_10_APPLICANTS": _bool,
    "LINKEDIN_EXPERIENCE_LEVELS": _levels,
    "MIN_SCORE_TO_NOTIFY": _int_between(1, 10),
    "LINKEDIN_REQUEST_DELAY": _float_between(0, 30),
    "ATS_HOURS_OLD": _int_between(1, 720),
    "ATS_TITLE_KEYWORDS": _word_list(allow_empty=False),
    "ATS_EXCLUDE_TITLE_KEYWORDS": _word_list(allow_empty=True),
    "ATS_LOCATION_KEYWORDS": _word_list(allow_empty=True),
}

# ── Resume ────────────────────────────────────────────────────────────

RESUME_TEXT_PATH = Path(__file__).parent / "resume_text.txt"


def validate():
    """Check that all required env vars are set. Raises ValueError if not."""
    required = {
        "ASTRA_DB_API_ENDPOINT": ASTRA_DB_API_ENDPOINT,
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

    if not RESUME_TEXT_PATH.exists():
        raise FileNotFoundError(
            f"Resume text file not found: {RESUME_TEXT_PATH}"
        )
