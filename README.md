# auto-linkedin-jobs 🤖📬

**Your personalized job digest, fully automated.**

A self-running pipeline that scrapes freshly posted jobs (last 4 hours) from LinkedIn, Indeed, Naukri, Glassdoor and company job boards (Greenhouse, Lever, Ashby, Workday), matches them against your resume using Gemini AI, scores each job for relevance (1–10), stores the best matches in Astra DB to avoid duplicates, and delivers a clean email digest straight to your inbox. Runs every 4 hours via GitHub Actions — completely hands-free.

---

## How It Works

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Scrape     │───▶│  Deduplicate │───▶│  AI Scoring   │───▶│  Store in DB │───▶│  Email You   │
│  LinkedIn    │    │  (Astra DB)  │    │  (Gemini)     │    │  (Astra DB)  │    │  Top Matches │
│  Indeed      │    │  Skip seen   │    │  Score 1-10   │    │  For dedup   │    │  HTML Digest  │
│  Boards      │    │  jobs        │    │  per job      │    │  next run    │    │              │
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

1. **Scrape** — Pulls jobs posted in the last 4 hours across LinkedIn, Indeed, Naukri, Glassdoor and company job boards using customizable search queries (e.g., "Backend Developer", "AI Engineer").
2. **Deduplicate** — Checks every scraped job URL against Astra DB. Already seen it? Skip it.
3. **Score** — Sends your resume + each batch of new jobs to Gemini AI. Gets back a relevance score (1–10), matched/missing skills, and seniority fit. Automatically falls back to alternate Gemini models if one hits its quota limit.
4. **Store** — Saves all scored jobs to Astra DB so they're never processed again.
5. **Email** — Filters for high-scoring jobs (default: score ≥ 6), sorts by score, and sends you an HTML email with the top matches.

---

## Project Structure

| File | Purpose |
|---|---|
| `main.py` | Orchestrator — runs the 5-step pipeline |
| `scraper.py` | Indeed, Naukri and Glassdoor (via `python-jobspy`), LinkedIn guest pages (under-10-applicants filter, applicant count, job poster), Greenhouse/Lever/Ashby/Workday company boards |
| `scorer.py` | Gemini AI scoring with automatic model fallback |
| `db.py` | Astra DB interface — dedup + storage |
| `notifier.py` | HTML email builder + Gmail SMTP sender |
| `config.py` | All configuration in one place (queries, models, thresholds) |
| `frontend/` | Local admin UI to manage recipients, company boards, search queries, the company blocklist and settings |
| `resume_text.txt` | Your plain-text resume (used by Gemini for scoring) |
| `.github/workflows/job_scan.yml` | GitHub Actions — runs every 4 hours |

---

## Quick Start

### Prerequisites
- Python 3.12+
- [DataStax Astra DB](https://astra.datastax.com) account (free tier — 40 GB)
- [Google Gemini API Key](https://aistudio.google.com/apikey)
- Gmail account with an [App Password](https://myaccount.google.com/apppasswords)

### Local Setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/auto-linkedin-jobs.git
cd auto-linkedin-jobs

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your Astra DB API endpoint + token, Gemini, and Gmail credentials
# Endpoint and token: Astra Dashboard → your database → Overview

# 4. Add your resume
# Edit resume_text.txt with your plain-text resume

# 5. Run
python main.py
```

### Admin UI (recipients, company boards, search queries, settings)

The admin page runs on your own machine only while you use it; nothing is hosted.
It needs only `ASTRA_DB_API_ENDPOINT` and `ASTRA_DB_TOKEN` in `.env`.

```bash
cd jobs-scrapper                      # the repo root
source venv/bin/activate              # or: python -m venv venv && source venv/bin/activate
pip install -r requirements.txt       # first time only
python frontend/server.py
# open http://127.0.0.1:8000, make changes, press Ctrl+C when done
```

Everything is stored in Astra DB collections (`recipients`, `companies`, `search_queries`, `blocked_companies`, `settings`) and read by every pipeline run.
Empty collections fall back to `NOTIFY_EMAIL` and `SEARCH_QUERIES` in `config.py`.
The **Blocklist** page (or the Block button on any job row) adds a scam company: its jobs are dropped before scoring and never emailed, and any stored jobs from it are deleted. Matching ignores case and endings like Pvt Ltd.
The **Settings** page is a table of every tunable (location, time windows, LinkedIn filters, request delay, and the title and location keyword lists for company job boards). Edit a value and press Save; Reset returns it to the default in `config.py`. Keyword lists are comma-separated and match at the start of a word.
Every list shows its total and numbered rows, and loads one page at a time (20, 50 or 100 per page).
Job search matches whole words in the title, company and location (for example `react`, `chennai`, `full stack`).
Totals stop at "1000+" because the Data API will not count higher.
Company slugs are checked against the board's public API before saving.
Workday slugs look like `nvidia.wd5/NVIDIAExternalCareerSite`, taken from the board URL `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`.

### GitHub Actions (Fully Automated)

Add these **repository secrets** (`Settings` → `Secrets and variables` → `Actions`):

| Secret | Description |
|---|---|
| `ASTRA_DB_API_ENDPOINT` | `https://<db-id>-<region>.apps.astra.datastax.com` |
| `ASTRA_DB_TOKEN` | Starts with `AstraCS:...` |
| `GEMINI_API_KEY` | From Google AI Studio |
| `SMTP_EMAIL` | Your Gmail address |
| `SMTP_PASSWORD` | Gmail App Password |
| `NOTIFY_EMAIL` | Email to receive the digest |

Once secrets are configured, the pipeline runs automatically every 4 hours. You can also trigger it manually from the Actions tab.

---

## Configuration

All tunables live in [`config.py`](config.py):

| Setting | Default | Description |
|---|---|---|
| `SEARCH_QUERIES` | Backend, Python, AI, etc. | Job titles to search for |
| `LOCATION` | India | Geographic filter |
| `HOURS_OLD` | 4 | Only scrape jobs posted within this window |
| `GEMINI_MODEL` | `gemini-3.7-flash` | Primary scoring model |
| `FALLBACK_MODELS` | 6 fallback models | Auto-switches when quota is hit |
| `MIN_SCORE_TO_NOTIFY` | 6 | Only email jobs scoring ≥ this |
| `MAX_JOBS_IN_EMAIL` | 50 | Cap on jobs per email |

---

## Tech Stack

- **Scraping**: [python-jobspy](https://github.com/Bunsly/JobSpy)
- **Database**: [DataStax Astra DB](https://astra.datastax.com) (Data API via `astrapy`)
- **AI Scoring**: [Google Gemini](https://ai.google.dev) (`google-genai` SDK)
- **Email**: Gmail SMTP
- **CI/CD**: GitHub Actions

## License

MIT
