"""Astra DB (Data API) storage.

- `jobs`: every scored job, keyed by URL, so we never re-score or re-notify.
- `recipients`, `companies`, `search_queries`, `settings`: edited from the
  frontend and read by every pipeline run. Each document's `_id` is its key.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from astrapy import DataAPIClient
from astrapy.api_options import APIOptions, TimeoutOptions
from astrapy.exceptions import TooManyDocumentsToCountException

from config import ASTRA_DB_API_ENDPOINT, ASTRA_DB_TOKEN

logger = logging.getLogger(__name__)

ADMIN_COLLECTIONS = ["recipients", "companies", "search_queries", "settings", "blocked_companies"]
LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "corp", "corporation", "co"}
# Long text fields are never filtered on; skipping their index avoids the
# Data API's indexed-string size limit.
JOBS_DEFINITION = {"indexing": {"deny": ["description", "reasoning", "matching", "missing"]}}
IN_FILTER_LIMIT = 100  # max values the Data API accepts in one $in
SORTED_FIND_MAX = 50  # the Data API returns at most 50 documents per sorted query, whatever the limit
COUNT_BOUND = 1000  # the Data API refuses to count past its own hard limit; callers show "1000+"
# Astra's default 10s per request is too tight when the pipeline is writing at the same time
API_OPTIONS = APIOptions(timeout_options=TimeoutOptions(request_timeout_ms=30_000, general_method_timeout_ms=60_000))


def company_key(name) -> str:
    """Lowercase words of a company name without legal suffixes.

    'Unified Mentor Private Limited' and 'unified mentor' give the same key,
    so a blocklist entry matches however a job board spells the name.
    """
    words = re.findall(r"[a-z0-9]+", name.lower()) if isinstance(name, str) else []  # pandas gives NaN for a missing company
    return " ".join(w for w in words if w not in LEGAL_WORDS)


class JobDB:
    """Interface to the Astra DB collections."""

    def __init__(self):
        logger.info("Connecting to Astra DB...")
        self.db = DataAPIClient(api_options=API_OPTIONS).get_database(ASTRA_DB_API_ENDPOINT, token=ASTRA_DB_TOKEN)
        existing = set(self.db.list_collection_names())
        for name in ["jobs", *ADMIN_COLLECTIONS]:
            if name not in existing:
                logger.info("Creating collection %s", name)
                self.db.create_collection(name, definition=JOBS_DEFINITION if name == "jobs" else None)
        self.jobs = self.db.get_collection("jobs")
        logger.info("Connected to Astra DB.")

    # ── Admin collections (used by the frontend and the pipeline) ─────

    def list_rows(self, name: str) -> list[dict]:
        return list(self.db.get_collection(name).find({}))

    def upsert_row(self, name: str, row_id: str, doc: dict) -> None:
        self.db.get_collection(name).replace_one({"_id": row_id}, {**doc, "_id": row_id}, upsert=True)

    def delete_row(self, name: str, row_id: str) -> None:
        self.db.get_collection(name).delete_one({"_id": row_id})

    def get_recipients(self) -> list[str]:
        return [r["email"] for r in self.list_rows("recipients")]

    def get_companies(self) -> list[dict]:
        return [
            {"portal": r["portal"], "slug": r["slug"], "name": r.get("name", "")}
            for r in self.list_rows("companies")
        ]

    def get_search_queries(self) -> list[str]:
        return [r["query"] for r in self.list_rows("search_queries")]

    def get_blocked_keys(self) -> set[str]:
        """company_key of every blocklisted company (it is the document _id)."""
        return {r["_id"] for r in self.list_rows("blocked_companies")}

    def get_settings(self) -> dict[str, str]:
        return {r["name"]: r["value"] for r in self.list_rows("settings")}

    # ── Paging and counting (used by the frontend) ────────────────────

    def count(self, name: str, flt: dict | None = None) -> tuple[int, bool]:
        """Number of matching documents, and whether the count hit the cap."""
        try:
            return self.db.get_collection(name).count_documents(flt or {}, upper_bound=COUNT_BOUND), False
        except TooManyDocumentsToCountException:
            return COUNT_BOUND, True

    def page(self, name: str, flt: dict | None, sort: dict, page: int, size: int, projection: dict | None = None) -> list[dict]:
        """One page of documents (1-based page number). Skipping needs a sort."""
        coll = self.db.get_collection(name)
        base = (page - 1) * size
        chunks = [(base + off, min(SORTED_FIND_MAX, size - off)) for off in range(0, size, SORTED_FIND_MAX)]

        def fetch(chunk: tuple[int, int]) -> list[dict]:
            skip, limit = chunk
            return list(coll.find(flt or {}, sort=sort, skip=skip, limit=limit, projection=projection))

        if len(chunks) == 1:
            return fetch(chunks[0])
        with ThreadPoolExecutor(len(chunks)) as pool:
            return [doc for part in pool.map(fetch, chunks) for doc in part]

    # ── Jobs ──────────────────────────────────────────────────────────

    def delete_company_jobs(self, key: str) -> int:
        """Delete stored jobs whose company has this company_key. Returns how many."""
        found = self.jobs.find({"$lexical": {"$match": key}}, projection={"company": True})
        ids = [d["_id"] for d in found if company_key(d.get("company")) == key]
        for i in range(0, len(ids), IN_FILTER_LIMIT):
            self.jobs.delete_many({"_id": {"$in": ids[i:i + IN_FILTER_LIMIT]}})
        return len(ids)

    def get_seen_urls(self, urls: list[str]) -> set[str]:
        """Return the subset of URLs that already exist in the database."""
        seen = set()
        for i in range(0, len(urls), IN_FILTER_LIMIT):
            chunk = urls[i:i + IN_FILTER_LIMIT]
            try:
                docs = self.jobs.find({"_id": {"$in": chunk}}, projection={"_id": True})
                seen.update(d["_id"] for d in docs)
            except Exception as e:
                # treat errors as "not seen" to avoid missing jobs
                logger.warning("Dedup lookup failed for %d URLs: %s", len(chunk), e)
        return seen

    def filter_new(self, urls: list[str]) -> set[str]:
        """Return only the URLs that are NOT in the database."""
        seen = self.get_seen_urls(urls)
        new = set(urls) - seen
        logger.info("Dedup check: %d total, %d already seen, %d new", len(urls), len(seen), len(new))
        return new

    def insert_job(self, job: dict) -> None:
        """Insert or update a single job record."""
        url = str(job.get("job_url", ""))
        try:
            self.jobs.replace_one({"_id": url}, {
                "_id": url,
                "$lexical": " ".join(filter(None, [str(job.get(k) or "") for k in ("title", "company", "location")])),
                "title": str(job.get("title", "")),
                "company": str(job.get("company", "")),
                "location": str(job.get("location", "")),
                "site": str(job.get("site", "")),
                "search_query": str(job.get("search_query", "")),
                "description": str(job.get("description", ""))[:5000],  # cap description size
                "date_posted": str(job.get("date_posted", ""))[:10],  # pandas may give a full timestamp
                "score": int(job.get("score", 0)),
                "applicants": job.get("applicants") if isinstance(job.get("applicants"), int) else None,
                "applicants_text": str(job.get("applicants_text") or ""),
                "poster_name": str(job.get("poster_name") or ""),
                "poster_url": str(job.get("poster_url") or ""),
                "seniority_fit": str(job.get("seniority_fit") or ""),
                "reasoning": str(job.get("reasoning", "")),
                "matching": str(job.get("matching", "")),
                "missing": str(job.get("missing", "")),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "notified": bool(job.get("notified", False)),
            }, upsert=True)
        except Exception as e:
            logger.error("Failed to insert job %s: %s", url, e)

    def insert_jobs(self, jobs: list[dict]) -> int:
        """Insert multiple jobs. Returns the count of jobs attempted."""
        count = 0
        for job in jobs:
            self.insert_job(job)
            count += 1
        logger.info("Inserted %d jobs into Astra DB.", count)
        return count
