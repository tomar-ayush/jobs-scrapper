"""Job scraping module.

- Indeed, Naukri and Glassdoor via python-jobspy.
- LinkedIn via its public guest pages, so we can use the
  "Under 10 applicants" filter and read the applicant count and job poster.
- Company job boards (Greenhouse, Lever, Ashby, Workday) via their public JSON APIs.
"""

import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd
from jobspy import scrape_jobs

from config import (
    ATS_EXCLUDE_TITLE_KEYWORDS,
    ATS_HOURS_OLD,
    ATS_LOCATION_KEYWORDS,
    ATS_TITLE_KEYWORDS,
    HOURS_OLD,
    LINKEDIN_EXPERIENCE_LEVELS,
    LINKEDIN_REQUEST_DELAY,
    LINKEDIN_UNDER_10_APPLICANTS,
    LOCATION,
    RESULTS_PER_QUERY,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"

BOARD_API_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    # Workday slug is "tenant.wd5/SiteName" (from https://tenant.wd5.myworkdayjobs.com/SiteName)
    "workday": "https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
}
WORKDAY_PAGE_SIZE = 20  # Workday's maximum; it lists newest first
WORKDAY_PAGES = 2  # 40 newest postings is plenty for a day


class RateLimited(Exception):
    """LinkedIn kept answering 429 after a retry."""


def _http_get(url: str, json_body: dict | None = None) -> str | None:
    """GET a URL (POST JSON if json_body given). Returns None on 404/network errors, raises RateLimited on 429."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url) from e
        logger.warning("HTTP %d for %s", e.code, url)
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("Request failed for %s: %s", url, e)
    return None


def _linkedin_get(url: str) -> str | None:
    """GET with one slow retry on 429, then give up (raises RateLimited)."""
    time.sleep(LINKEDIN_REQUEST_DELAY)
    try:
        return _http_get(url)
    except RateLimited:
        logger.info("LinkedIn 429, waiting 30s before one retry...")
        time.sleep(30)
        return _http_get(url)


def _clean(fragment: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(html.unescape(text))).strip()


def _find(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.DOTALL)
    return _clean(m.group(1)) if m else ""


# ── LinkedIn ──────────────────────────────────────────────────────────


def parse_linkedin_cards(page: str) -> list[dict]:
    """Parse a guest search results page into job dicts."""
    jobs = []
    for card in page.split("<li>")[1:]:
        job_id = re.search(r"urn:li:jobPosting:(\d+)", card)
        if not job_id:
            continue
        date = re.search(r'datetime="([\d-]+)"', card)
        jobs.append({
            "job_url": f"https://www.linkedin.com/jobs/view/{job_id.group(1)}",
            "title": _find(r'base-search-card__title">(.*?)</h3>', card),
            "company": _find(r'base-search-card__subtitle">(.*?)</h4>', card),
            "location": _find(r'job-search-card__location">(.*?)</span>', card),
            "date_posted": date.group(1) if date else "",
            "site": "linkedin",
            "description": "",
        })
    return jobs


def parse_applicants(text: str) -> int | None:
    """'Be among the first 25 applicants' -> 25, 'Over 200 applicants' -> 200."""
    m = re.search(r"(\d[\d,]*)", text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_linkedin_detail(page: str) -> dict:
    """Pull description, applicant count and job poster from a job page."""
    applicants_text = _find(r'num-applicants__caption[^>]*>(.*?)</', page)
    poster = re.search(
        r'message-the-recruiter.*?href="([^"]+)".*?<span class="sr-only">(.*?)</span>',
        page,
        re.DOTALL,
    )
    return {
        "description": _find(r'show-more-less-html__markup[^>]*>(.*?)<button', page),
        "applicants_text": applicants_text,
        "applicants": parse_applicants(applicants_text),
        "poster_name": _clean(poster.group(2)) if poster else "",
        "poster_url": html.unescape(poster.group(1)).split("?")[0] if poster else "",
    }


def _scrape_linkedin(query: str) -> list[dict]:
    params = {
        "keywords": query,
        "location": LOCATION,
        "f_TPR": f"r{HOURS_OLD * 3600}",
        "f_E": LINKEDIN_EXPERIENCE_LEVELS,
        "sortBy": "DD",
    }
    if LINKEDIN_UNDER_10_APPLICANTS:
        params["f_EA"] = "true"

    jobs: list[dict] = []
    while len(jobs) < RESULTS_PER_QUERY:
        params["start"] = str(len(jobs))
        page = _linkedin_get(f"{LINKEDIN_SEARCH_URL}?{urllib.parse.urlencode(params)}")
        cards = parse_linkedin_cards(page or "")
        if not cards:
            break
        jobs.extend(cards)
    return jobs[:RESULTS_PER_QUERY]


def enrich_linkedin(jobs: list[dict]) -> list[dict]:
    """Fetch description, applicants and poster for LinkedIn jobs.

    Called after DB dedup so we only hit LinkedIn for new jobs.
    """
    for job in jobs:
        if job.get("site") != "linkedin":
            continue
        job_id = job["job_url"].rsplit("/", 1)[-1]
        try:
            page = _linkedin_get(LINKEDIN_DETAIL_URL.format(job_id))
        except RateLimited:
            logger.warning("LinkedIn rate limit hit; remaining jobs keep title-only data.")
            break
        if page:
            job.update(parse_linkedin_detail(page))
    return jobs


# ── Company job boards ────────────────────────────────────────────────


def _parse_time(value) -> datetime | None:
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_workday_posted(text: str) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' -> datetime; '30+ Days Ago' -> None."""
    t = (text or "").lower()
    now = datetime.now(timezone.utc)
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?", t)
    if m and "+" not in t:
        return now - timedelta(days=int(m.group(1)))
    return None


def _workday_parts(slug: str) -> tuple[str, str, str]:
    """'nvidia.wd5/NVIDIAExternalCareerSite' -> (host, tenant, site)."""
    host, site = slug.split("/", 1)
    return host, host.split(".", 1)[0], site


def _fetch_workday(slug: str) -> list[dict] | None:
    host, tenant, site = _workday_parts(slug)
    url = BOARD_API_URLS["workday"].format(host=host, tenant=tenant, site=site)
    postings = []
    for page in range(WORKDAY_PAGES):
        body = _http_get(url, {
            "appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
            "offset": page * WORKDAY_PAGE_SIZE, "searchText": "",
        })
        if body is None:
            return None if page == 0 else postings and _workday_jobs(host, tenant, site, postings)
        data = json.loads(body)
        if "jobPostings" not in data:
            return None
        postings.extend(data["jobPostings"])
        if len(data["jobPostings"]) < WORKDAY_PAGE_SIZE:
            break
    return _workday_jobs(host, tenant, site, postings)


def _workday_jobs(host: str, tenant: str, site: str, postings: list[dict]) -> list[dict]:
    return [{
        "job_url": f"https://{host}.myworkdayjobs.com/{site}{j.get('externalPath', '')}",
        "title": j.get("title", ""),
        "location": j.get("locationsText", ""),
        "description": "",  # filled later, only for jobs we keep
        "posted": _parse_workday_posted(j.get("postedOn", "")),
        "_detail": f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{j.get('externalPath', '')}",
    } for j in postings]


def _fill_workday_descriptions(jobs: list[dict]) -> None:
    for j in jobs:
        detail = j.pop("_detail", None)
        if not detail:
            continue
        try:
            body = _http_get(detail)
        except RateLimited:
            logger.warning("Rate limited by Workday; remaining descriptions left empty.")
            return
        if body:
            info = json.loads(body).get("jobPostingInfo", {})
            j["description"] = _clean(info.get("jobDescription", ""))
            # The list shows "2 Locations" for multi-site postings; the detail has the real ones
            locations = [info.get("location", ""), *info.get("additionalLocations", [])]
            j["location"] = "; ".join(filter(None, locations)) or j["location"]


def _normalize_board_jobs(portal: str, data) -> list[dict]:
    """Map each board's JSON shape to our job dict."""
    if portal == "greenhouse":
        return [{
            "job_url": j.get("absolute_url", ""),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "description": _clean(html.unescape(j.get("content", ""))),
            "posted": _parse_time(j.get("first_published") or j.get("updated_at")),
        } for j in data.get("jobs", [])]
    if portal == "lever":
        return [{
            "job_url": j.get("hostedUrl", ""),
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "description": j.get("descriptionPlain", ""),
            "posted": _parse_time(j.get("createdAt")),
        } for j in data]
    return [{  # ashby
        "job_url": j.get("jobUrl", ""),
        "title": j.get("title", ""),
        "location": j.get("location", "") + (" Remote" if j.get("isRemote") else ""),
        "description": j.get("descriptionPlain", ""),
        "posted": _parse_time(j.get("publishedAt")),
    } for j in data.get("jobs", []) if j.get("isListed", True)]


def fetch_board(portal: str, slug: str) -> list[dict] | None:
    """All jobs on a company board, or None if the board doesn't exist."""
    try:
        if portal == "workday":
            return _fetch_workday(slug)
        body = _http_get(BOARD_API_URLS[portal].format(urllib.parse.quote(slug, safe="")))
    except RateLimited:
        logger.warning("Rate limited by %s board %s", portal, slug)
        return None
    if body is None:
        return None
    try:
        return _normalize_board_jobs(portal, json.loads(body))
    except (ValueError, AttributeError, KeyError) as e:
        logger.warning("Unexpected %s response for %s: %s", portal, slug, e)
        return None


def _starts_a_word(text: str, words: list[str]) -> bool:
    """True if any keyword begins a word in text, so 'engineer' matches 'engineering' but 'sr' not 'disruptive'."""
    return any(re.search(r"\b" + re.escape(w), text) for w in words)


def is_relevant(title: str, location: str) -> bool:
    """Entry-level engineering roles in a wanted location."""
    t, loc = title.lower(), location.lower()
    if not _starts_a_word(t, ATS_TITLE_KEYWORDS):
        return False
    if _starts_a_word(t, ATS_EXCLUDE_TITLE_KEYWORDS):
        return False
    if re.fullmatch(r"\d+ locations", loc):  # Workday multi-location; resolved after the detail fetch
        return True
    return not ATS_LOCATION_KEYWORDS or _starts_a_word(loc, ATS_LOCATION_KEYWORDS)


def _scrape_boards(companies: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ATS_HOURS_OLD)
    jobs = []
    for c in companies:
        board = fetch_board(c["portal"], c["slug"])
        if board is None:
            logger.warning("  ✗ %s/%s → board not found", c["portal"], c["slug"])
            continue
        fresh = [
            j for j in board
            if j["job_url"] and j["posted"] and j["posted"] >= cutoff
            and is_relevant(j["title"], j["location"])
        ]
        if c["portal"] == "workday":
            _fill_workday_descriptions(fresh)
            fresh = [j for j in fresh if is_relevant(j["title"], j["location"])]
        for j in fresh:
            j.pop("_detail", None)
            posted = j.pop("posted")
            j.update({
                "company": c.get("name") or c["slug"],
                "site": c["portal"],
                "date_posted": posted.date().isoformat(),
                "search_query": "company board",
            })
        jobs.extend(fresh)
        logger.info("  ✓ %s/%-20s → %d fresh of %d", c["portal"], c["slug"], len(fresh), len(board))
    return jobs


# ── Orchestration ─────────────────────────────────────────────────────


def scrape_all(queries: list[str], companies: list[dict]) -> pd.DataFrame:
    """Scrape Indeed + LinkedIn for each query, plus all company boards.

    Returns a deduplicated DataFrame with columns including:
    job_url, title, company, location, description, site, date_posted.
    LinkedIn rows have no description yet; see enrich_linkedin().
    """
    all_jobs: list[pd.DataFrame] = []

    logger.info("Scraping jobs in %s (last %dh)", LOCATION, HOURS_OLD)
    logger.info("Queries: %s", ", ".join(queries))

    linkedin_blocked = False
    for query in queries:
        try:
            jobs = scrape_jobs(
                site_name=["indeed", "naukri", "glassdoor"],
                search_term=query,
                location=LOCATION,
                results_wanted=RESULTS_PER_QUERY,
                hours_old=HOURS_OLD,
                country_indeed="India",
            )
        except Exception as e:
            logger.warning("  ✗ %-25s (indeed) → %s", query, e)
            jobs = pd.DataFrame()

        linkedin: list[dict] = []
        if not linkedin_blocked:
            try:
                linkedin = _scrape_linkedin(query)
            except RateLimited:
                linkedin_blocked = True
                logger.warning("LinkedIn rate limit hit; skipping LinkedIn for the rest of this run.")

        jobs = pd.concat([jobs, pd.DataFrame(linkedin)], ignore_index=True)
        if not jobs.empty:
            jobs["search_query"] = query
            all_jobs.append(jobs)
        logger.info("  ✓ %-25s → %d jobs (%d linkedin)", query, len(jobs), len(linkedin))

    if companies:
        logger.info("Checking %d company boards (last %dh)...", len(companies), ATS_HOURS_OLD)
        board_jobs = _scrape_boards(companies)
        if board_jobs:
            all_jobs.append(pd.DataFrame(board_jobs))

    if not all_jobs:
        logger.warning("No jobs found across any query.")
        return pd.DataFrame()

    df = pd.concat(all_jobs, ignore_index=True)
    before = len(df)
    df.drop_duplicates(subset=["job_url"], inplace=True)
    logger.info(
        "Total: %d scraped, %d dupes removed → %d unique",
        before,
        before - len(df),
        len(df),
    )

    return df
