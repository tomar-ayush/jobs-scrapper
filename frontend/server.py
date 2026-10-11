"""Local admin UI for the job scraper.

Manage email recipients, company job boards, search queries and settings
stored in Astra DB. The GitHub Actions pipeline reads these tables on every run.

Run from the repo root:  python frontend/server.py
Then open:               http://127.0.0.1:8000
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import config  # noqa: E402
from db import JobDB, company_key  # noqa: E402
from scraper import BOARD_API_URLS, fetch_board  # noqa: E402

HOST, PORT = "127.0.0.1", 8000
CACHE_TTL = 60  # seconds; the overview needs a dozen Data API calls
_cache: dict[str, tuple[float, dict]] = {}
POOL = ThreadPoolExecutor(16)  # the overview fires about 18 independent Data API calls
SOURCES = ("linkedin", "indeed", "naukri", "glassdoor", "greenhouse", "lever", "ashby", "workday")
PAGE_SIZES = (20, 50, 100)
EARLY_APPLICANTS = 25  # "first 25 applicants" on LinkedIn
NO_DESCRIPTION = {"description": False}
JOB_SORTS = {
    "recent": {"scraped_at": -1},
    "score": {"score": -1, "scraped_at": -1},
    "applicants": {"applicants": 1, "score": -1},
}
LIST_SORTS = {
    "recipients": {"email": 1}, "companies": {"slug": 1, "portal": 1},
    "search_queries": {"query": 1}, "blocked_companies": {"name": 1},
}


def _show(value) -> str:
    """A setting's value as the text box shows it."""
    if isinstance(value, list):
        return ", ".join(value)
    return str(value).lower() if isinstance(value, bool) else str(value)


def _row_id(table: str, values: list[str]) -> str:
    """Document _id for a row: the key fields joined, or the normalized name for the blocklist."""
    return company_key(values[0]) if table == "blocked_companies" else ":".join(values)
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
WORKDAY_SLUG_RE = re.compile(r"^[a-z0-9-]+\.wd\d+/[A-Za-z0-9_-]{1,80}$")

# Collection name -> (fields written from the form, key fields).
# The key fields joined with ":" form the document _id.
# Collection names only ever come from this whitelist.
TABLES = {
    "recipients": (["email"], ["email"]),
    "companies": (["portal", "slug", "name", "careers_url"], ["portal", "slug"]),
    "search_queries": (["query"], ["query"]),
    "settings": (["name", "value"], ["name"]),
    "blocked_companies": (["name"], ["name"]),  # _id is company_key(name), see _row_id
}


def validate(table: str, data: dict) -> str | None:
    """Return an error message, or None if the row is valid.

    Settings are normalized in place.
    """
    if table == "recipients" and not EMAIL_RE.match(data["email"]):
        return "Invalid email address."
    if table == "search_queries" and not 1 <= len(data["query"]) <= 100:
        return "Query must be 1-100 characters."
    if table == "blocked_companies" and (len(data["name"]) > 100 or not company_key(data["name"])):
        return "Enter a company name (up to 100 characters)."
    if table == "companies":
        if data["portal"] not in BOARD_API_URLS:
            return f"Portal must be one of: {', '.join(BOARD_API_URLS)}."
        if data["portal"] == "workday" and not WORKDAY_SLUG_RE.match(data["slug"]):
            return "Workday slug looks like tenant.wd5/SiteName, taken from https://tenant.wd5.myworkdayjobs.com/SiteName."
        if data["portal"] != "workday" and not SLUG_RE.match(data["slug"]):
            return "Slug may only contain letters, digits, '.', '_' and '-'."
        if len(data["name"]) > 100:
            return "Name is too long."
        url = data["careers_url"]
        if url and not (url.startswith("https://") or url.startswith("http://")):
            return "Careers link must start with http:// or https://."
    if table == "settings":
        parse = config.EDITABLE_SETTINGS.get(data["name"])
        if not parse:
            return "Unknown setting."
        try:
            data["value"] = _show(parse(data["value"]))
        except ValueError as e:
            return f"{data['name']} {e}."
    return None


def _int(q: dict, key: str, default: int) -> int:
    try:
        return int(q.get(key, default))
    except ValueError:
        return default


def jobs_filter(q: dict) -> dict:
    """Data API filter for the jobs list from the page's query string."""
    f: dict = {}
    if q.get("site"):
        f["site"] = q["site"]
    if _int(q, "min", 0) > 0:
        f["score"] = {"$gte": _int(q, "min", 0)}
    bounds: dict = {}
    if q.get("sort") == "applicants":
        bounds["$gte"] = 0  # jobs with no count would otherwise sort first
    if q.get("early") == "1":
        bounds["$lte"] = EARLY_APPLICANTS
    if bounds:
        f["applicants"] = bounds
    if q.get("q", "").strip():
        f["$lexical"] = {"$match": q["q"].strip()[:100]}
    return f


class Handler(BaseHTTPRequestHandler):
    db: JobDB

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def _table(self) -> str | None:
        """Table name from /api/<table>, only if whitelisted."""
        path = urlsplit(self.path).path
        name = path.removeprefix("/api/")
        return name if path.startswith("/api/") and name in TABLES else None

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _guard(self) -> bool:
        """Block other websites from driving this server.

        The Host check stops DNS rebinding. Requiring a JSON content type
        forces a CORS preflight, which this server never approves.
        """
        if self.headers.get("Host") not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
            self._json(403, {"error": "Forbidden host."})
            return False
        if self.command != "GET" and (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json."})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        return data if isinstance(data, dict) else {}

    def handle_one_request(self):
        """Turn an Astra/network failure into a JSON error instead of a dead thread."""
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:  # noqa: BLE001 - surfaced to the page as text
            try:
                self._json(503, {"error": f"Database request failed: {type(e).__name__}. Try again."})
            except Exception:
                pass

    def _cached(self, key: str, fetch, fresh: bool):
        hit = _cache.get(key)
        if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        data = fetch()
        _cache[key] = (time.time(), data)
        return data

    def _paged(self, name: str, flt: dict, sort: dict, q: dict, projection: dict | None = None) -> dict:
        """One page plus the totals the page needs to draw its pager."""
        page = max(1, _int(q, "page", 1))
        size = _int(q, "size", 20)
        size = size if size in PAGE_SIZES else 20
        count = POOL.submit(self.db.count, name, flt)
        items = self.db.page(name, flt, sort, page, size, projection)
        total, capped = count.result()
        for item in items:
            item.pop("$lexical", None)
        return {
            "items": items, "total": total, "capped": capped, "page": page, "size": size,
            "has_more": len(items) == size if capped else page * size < total,
        }

    def _overview(self) -> dict:
        db = self.db
        saved = db.get_settings().get("MIN_SCORE_TO_NOTIFY")
        try:
            min_score = config.EDITABLE_SETTINGS["MIN_SCORE_TO_NOTIFY"](saved) if saved else config.MIN_SCORE_TO_NOTIFY
        except ValueError:
            min_score = config.MIN_SCORE_TO_NOTIFY
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        count = lambda name, flt=None: POOL.submit(db.count, name, flt)  # noqa: E731
        counts = {
            "jobs": count("jobs"),
            "last_24h": count("jobs", {"scraped_at": {"$gte": since}}),
            "worth_applying": count("jobs", {"score": {"$gte": min_score}}),
            "early": count("jobs", {"applicants": {"$lte": EARLY_APPLICANTS}}),
            "recipients": count("recipients"),
            "companies": count("companies"),
            "queries": count("search_queries"),
            "blocklist": count("blocked_companies"),
        }
        by_site = {site: count("jobs", {"site": site}) for site in SOURCES}
        top = POOL.submit(db.page, "jobs", {}, JOB_SORTS["score"], 1, 8, NO_DESCRIPTION)
        last = POOL.submit(db.page, "jobs", {}, JOB_SORTS["recent"], 1, 1, {"scraped_at": True})
        to_json = lambda f: dict(zip(("n", "capped"), f.result()))  # noqa: E731
        by_site = {k: to_json(f) for k, f in by_site.items()}
        by_site = {k: v for k, v in by_site.items() if v["n"]}
        top_jobs = top.result()
        for job in top_jobs:
            job.pop("$lexical", None)
        return {
            "min_score": min_score,
            "sites": sorted(by_site),
            "counts": {k: to_json(f) for k, f in counts.items()},
            "by_site": by_site,
            "top": top_jobs,
            "last_run": (last.result() or [{}])[0].get("scraped_at"),
        }

    def do_GET(self):
        if not self._guard():
            return
        path, q = urlsplit(self.path).path, self._query()
        if path == "/":
            return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/overview":
            return self._json(200, self._cached("overview", self._overview, q.get("fresh") == "1"))
        if path == "/api/jobs":  # read-only; descriptions left out to keep pages small
            sort = q.get("sort") if q.get("sort") in JOB_SORTS else "recent"
            return self._json(200, self._paged("jobs", jobs_filter(q), JOB_SORTS[sort], q, NO_DESCRIPTION))
        table = self._table()
        if not table:
            return self._json(404, {"error": "Not found."})
        if table != "settings":
            return self._json(200, self._paged(table, {}, LIST_SORTS[table], q))
        saved = {r["name"]: r["value"] for r in self.db.list_rows(table)}
        defaults = {n: getattr(config, n) for n in config.EDITABLE_SETTINGS}
        self._json(200, [{
            "name": n, "default": _show(d),
            "custom": n in saved,  # a saved empty list is different from "not saved"
            "value": saved[n] if n in saved else _show(d),
            "kind": "bool" if isinstance(d, bool) else "list" if isinstance(d, list) else "text",
        } for n, d in defaults.items()])

    def do_POST(self):
        if not self._guard():
            return
        table = self._table()
        if not table:
            return self._json(404, {"error": "Not found."})
        columns, _ = TABLES[table]
        try:
            raw = self._body()
        except json.JSONDecodeError:
            return self._json(400, {"error": "Invalid JSON."})
        data = {c: str(raw.get(c) or "").strip() for c in columns}
        if table == "recipients":
            data["email"] = data["email"].lower()
        error = validate(table, data)
        if error:
            return self._json(400, {"error": error})
        if table == "companies" and fetch_board(data["portal"], data["slug"]) is None:
            return self._json(400, {
                "error": f"No {data['portal']} board found for '{data['slug']}'. Check the slug.",
            })
        row_id = _row_id(table, [data[k] for k in TABLES[table][1]])
        data["added_at"] = datetime.now(timezone.utc).isoformat()
        self.db.upsert_row(table, row_id, data)
        reply = {"ok": True}
        if table == "blocked_companies":  # stored jobs from a scam company should not linger on the dashboard
            reply["purged"] = self.db.delete_company_jobs(row_id)
        _cache.clear()
        self._json(200, reply)

    def do_DELETE(self):
        if not self._guard():
            return
        table = self._table()
        if not table:
            return self._json(404, {"error": "Not found."})
        _, key = TABLES[table]
        try:
            raw = self._body()
        except json.JSONDecodeError:
            return self._json(400, {"error": "Invalid JSON."})
        values = [str(raw.get(k) or "") for k in key]
        if not all(values):
            return self._json(400, {"error": f"Missing key: {', '.join(key)}."})
        self.db.delete_row(table, _row_id(table, values))
        _cache.clear()
        self._json(200, {"ok": True})


def main():
    Handler.db = JobDB()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Admin UI running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
