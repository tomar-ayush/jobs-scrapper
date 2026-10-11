"""Tests for the NaN description fix, the new scrapers and the admin server."""

import importlib.util
import json
import math
import threading
import urllib.error
import urllib.request
from collections import namedtuple
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import scorer
import scraper

ROOT = Path(__file__).resolve().parent.parent


# ── Scorer: crash on NaN description ──────────────────────────────────


def test_nan_description_does_not_crash(monkeypatch):
    monkeypatch.setattr(scorer, "_get_resume", lambda: "resume")
    jobs = [{"title": "SDE", "description": math.nan}, {"title": "SDE", "description": "x" * 5000}]
    assert len(scorer._compute_batch_sizes(jobs)) == 1
    prompt = scorer._build_prompt(jobs)
    assert "DESCRIPTION:\nnan" not in prompt
    assert "x" * scorer.DESC_CHAR_LIMIT in prompt
    assert "x" * (scorer.DESC_CHAR_LIMIT + 1) not in prompt


# ── LinkedIn parsing (fixtures mirror the real guest HTML) ────────────

CARD = """<ul><li>
  <div class="base-card" data-entity-urn="urn:li:jobPosting:4478305463">
    <h3 class="base-search-card__title">
        Full Stack Engineer
    </h3>
    <h4 class="base-search-card__subtitle">
      <a class="hidden-nested-link" href="x">
        Royal Chain Limited
      </a>
    </h4>
    <span class="job-search-card__location">
      Navi Mumbai, Maharashtra, India
    </span>
    <time class="job-search-card__listdate--new" datetime="2026-10-11">1 minute ago</time>
  </div>
</li></ul>"""

DETAIL = """
<figcaption class="num-applicants__caption">
  Be among the first 25 applicants
</figcaption>
<div class="message-the-recruiter">
  <a class="base-card__full-link" href="https://in.linkedin.com/in/ojas-patil-42006a77?trk=x">
    <span class="sr-only">
      Ojas Patil
    </span>
  </a>
</div>
<div class="show-more-less-html__markup relative"><p>We&amp;#39;re hiring</p><ul><li>Python</li></ul>
</div>
<button class="show-more-less-html__button">Show more</button>
"""


def test_parse_linkedin_cards():
    [job] = scraper.parse_linkedin_cards(CARD)
    assert job == {
        "job_url": "https://www.linkedin.com/jobs/view/4478305463",
        "title": "Full Stack Engineer",
        "company": "Royal Chain Limited",
        "location": "Navi Mumbai, Maharashtra, India",
        "date_posted": "2026-10-11",
        "site": "linkedin",
        "description": "",
    }


def test_parse_linkedin_detail():
    d = scraper.parse_linkedin_detail(DETAIL)
    assert d["applicants"] == 25
    assert d["applicants_text"] == "Be among the first 25 applicants"
    assert d["poster_name"] == "Ojas Patil"
    assert d["poster_url"] == "https://in.linkedin.com/in/ojas-patil-42006a77"
    assert d["description"] == "We're hiring Python"


@pytest.mark.parametrize("text,expected", [
    ("Be among the first 25 applicants", 25),
    ("Over 200 applicants", 200),
    ("1,024 applicants", 1024),
    ("", None),
])
def test_parse_applicants(text, expected):
    assert scraper.parse_applicants(text) == expected


# ── Company boards ────────────────────────────────────────────────────


@pytest.mark.parametrize("title,location,expected", [
    ("Software Engineer I", "Bengaluru, India", True),
    ("Software Engineering Intern", "Remote", True),
    ("Senior Software Engineer", "Bengaluru, India", False),
    ("Account Manager", "Bengaluru, India", False),
    ("Software Engineer", "San Francisco", False),
])
def test_is_relevant(title, location, expected):
    assert scraper.is_relevant(title, location) is expected


def test_normalize_lever_and_ashby():
    lever = scraper._normalize_board_jobs("lever", [{
        "hostedUrl": "u", "text": "SDE", "categories": {"location": "Pune"},
        "descriptionPlain": "d", "createdAt": 1786469891368,
    }])
    assert lever[0]["posted"].year == 2026 and lever[0]["location"] == "Pune"
    ashby = scraper._normalize_board_jobs("ashby", {"jobs": [
        {"jobUrl": "u", "title": "SDE", "location": "India", "isListed": True,
         "publishedAt": "2026-03-12T16:38:15.322+00:00"},
        {"jobUrl": "hidden", "title": "SDE", "location": "India", "isListed": False},
    ]})
    assert [j["job_url"] for j in ashby] == ["u"]


# ── Admin server ──────────────────────────────────────────────────────


class FakeDB:
    """In-memory stand-in for JobDB's collection helpers."""

    def __init__(self):
        self.collections: dict[str, dict[str, dict]] = {}

    def list_rows(self, name):
        return list(self.collections.get(name, {}).values())

    def upsert_row(self, name, row_id, doc):
        self.collections.setdefault(name, {})[row_id] = {**doc, "_id": row_id}

    def delete_row(self, name, row_id):
        self.collections.get(name, {}).pop(row_id, None)

    def get_settings(self):
        return {r["name"]: r["value"] for r in self.list_rows("settings")}

    def delete_company_jobs(self, key):
        self.purged = getattr(self, "purged", []) + [key]
        return 3

    def _rows(self, name):
        if name == "jobs":
            return [{"_id": f"https://x/{i}", "title": f"SDE {i}", "score": 8, "site": "linkedin", "applicants": 25, "$lexical": "x"} for i in range(45)]
        return self.list_rows(name)

    def count(self, name, flt=None):
        if name == "jobs" and flt and "site" in flt:
            return (45 if flt["site"] == "linkedin" else 0), False
        return len(self._rows(name)), False

    def page(self, name, flt, sort, page, size, projection=None):
        self.last_query = (name, flt, sort, page, size)
        return [dict(r) for r in self._rows(name)[(page - 1) * size: page * size]]



@pytest.fixture
def server(monkeypatch):
    spec = importlib.util.spec_from_file_location("admin_server", ROOT / "frontend" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(mod, "PORT", port)
    fake_db = FakeDB()
    mod.Handler.db = fake_db
    monkeypatch.setattr(mod, "fetch_board", lambda portal, slug: [] if slug == "airbnb" else None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", fake_db
    httpd.shutdown()


def call(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(
        base + path, method=method, headers=h,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers["Content-Type"] else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_recipient_add_list_remove(server):
    base, db = server
    assert call(base, "POST", "/api/recipients", {"email": "Me@Example.com"})[0] == 200
    status, data = call(base, "GET", "/api/recipients")
    assert status == 200 and [r["email"] for r in data["items"]] == ["me@example.com"]
    assert data["total"] == 1 and data["page"] == 1 and data["has_more"] is False
    assert call(base, "DELETE", "/api/recipients", {"email": "me@example.com"})[0] == 200
    assert call(base, "GET", "/api/recipients")[1]["items"] == []


def test_rejects_bad_input(server):
    base, _ = server
    assert call(base, "POST", "/api/recipients", {"email": "nope"})[0] == 400
    assert call(base, "POST", "/api/companies", {"portal": "workday", "slug": "x"})[0] == 400
    assert call(base, "POST", "/api/companies", {"portal": "lever", "slug": "../x"})[0] == 400
    status, body = call(base, "POST", "/api/companies", {"portal": "lever", "slug": "missing"})
    assert status == 400 and "No lever board" in body["error"]
    assert call(base, "POST", "/api/companies", {"portal": "greenhouse", "slug": "airbnb"})[0] == 200
    assert call(base, "GET", "/api/nope")[0] == 404


def test_blocks_cross_site_requests(server):
    base, _ = server
    assert call(base, "GET", "/api/recipients", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/recipients", method="POST",
                                 data=b'{"email":"a@b.co"}', headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 415


def test_serves_index(server):
    base, _ = server
    status, body = call(base, "GET", "/")
    assert status == 200 and b"Job Scraper" in body
    status, data = call(base, "GET", "/api/jobs")
    assert status == 200 and data["items"][0]["title"] == "SDE 0"


# ── Admin server: settings and keys ───────────────────────────────────


def test_company_id_is_portal_and_slug(server):
    base, db = server
    assert call(base, "POST", "/api/companies", {"portal": "greenhouse", "slug": "airbnb", "name": "Airbnb"})[0] == 200
    assert list(db.collections["companies"]) == ["greenhouse:airbnb"]
    assert call(base, "DELETE", "/api/companies", {"portal": "greenhouse", "slug": "airbnb"})[0] == 200
    assert db.collections["companies"] == {}


def test_settings_validate_and_show_defaults(server):
    base, db = server
    assert call(base, "POST", "/api/settings", {"name": "HOURS_OLD", "value": "500"})[0] == 400
    assert call(base, "POST", "/api/settings", {"name": "NOT_A_SETTING", "value": "1"})[0] == 400
    assert call(base, "POST", "/api/settings", {"name": "LINKEDIN_UNDER_10_APPLICANTS", "value": "False"})[0] == 200
    assert call(base, "POST", "/api/settings", {"name": "HOURS_OLD", "value": " 6 "})[0] == 200
    rows = {r["name"]: r for r in call(base, "GET", "/api/settings")[1]}
    assert rows["HOURS_OLD"]["value"] == "6" and rows["HOURS_OLD"]["default"] == "4"
    assert rows["HOURS_OLD"]["custom"] is True and rows["HOURS_OLD"]["kind"] == "text"
    assert rows["LINKEDIN_UNDER_10_APPLICANTS"]["value"] == "false" and rows["LINKEDIN_UNDER_10_APPLICANTS"]["kind"] == "bool"
    assert rows["LOCATION"]["value"] == "India" and rows["LOCATION"]["custom"] is False  # untouched settings show their default
    assert call(base, "DELETE", "/api/settings", {"name": "HOURS_OLD"})[0] == 200
    after = {r["name"]: r for r in call(base, "GET", "/api/settings")[1]}["HOURS_OLD"]
    assert after["value"] == "4" and after["custom"] is False


def test_list_settings_are_normalized_and_an_empty_list_is_a_real_override(server):
    base, db = server
    ok = call(base, "POST", "/api/settings", {"name": "ATS_TITLE_KEYWORDS", "value": " Engineer, DEVELOPER ,engineer,, sde "})
    assert ok[0] == 200
    rows = {r["name"]: r for r in call(base, "GET", "/api/settings")[1]}
    assert rows["ATS_TITLE_KEYWORDS"]["value"] == "engineer, developer, sde" and rows["ATS_TITLE_KEYWORDS"]["kind"] == "list"
    assert "senior" in rows["ATS_EXCLUDE_TITLE_KEYWORDS"]["value"] and "sr" in rows["ATS_EXCLUDE_TITLE_KEYWORDS"]["value"].split(", ")
    assert call(base, "POST", "/api/settings", {"name": "ATS_TITLE_KEYWORDS", "value": " , "})[0] == 400  # must keep at least one
    assert call(base, "POST", "/api/settings", {"name": "ATS_LOCATION_KEYWORDS", "value": ""})[0] == 200  # empty = any location
    loc = {r["name"]: r for r in call(base, "GET", "/api/settings")[1]}["ATS_LOCATION_KEYWORDS"]
    assert loc["custom"] is True and loc["value"] == "" and "india" in loc["default"]


def test_number_settings_report_friendly_errors(server):
    base, db = server
    status, body = call(base, "POST", "/api/settings", {"name": "HOURS_OLD", "value": "abc"})
    assert status == 400 and "whole number" in body["error"]
    assert call(base, "POST", "/api/settings", {"name": "LINKEDIN_REQUEST_DELAY", "value": "2.5"})[0] == 200
    assert call(base, "POST", "/api/settings", {"name": "LINKEDIN_REQUEST_DELAY", "value": "99"})[0] == 400
    assert call(base, "POST", "/api/settings", {"name": "ATS_HOURS_OLD", "value": "0"})[0] == 400


def test_keywords_match_at_the_start_of_a_word(monkeypatch):
    monkeypatch.setattr(scraper, "ATS_EXCLUDE_TITLE_KEYWORDS", ["senior", "sr", "lead"])
    ok = lambda title: scraper.is_relevant(title, "Pune, India")  # noqa: E731
    assert ok("Software Engineering Intern")  # 'engineer' starts a word in 'engineering'
    assert ok("Disruptive Software Engineer")  # 'sr' is inside 'disruptive', not at a word start
    assert not ok("Sr. Software Engineer") and not ok("Sr Software Engineer")
    assert not ok("Tech Lead, Software Engineer") and not ok("Senior Software Engineer")
    assert ok("Software Engineer", ) and not scraper.is_relevant("Software Engineer", "Berlin")
    monkeypatch.setattr(scraper, "ATS_LOCATION_KEYWORDS", [])
    assert scraper.is_relevant("Software Engineer", "Berlin")  # empty location list accepts anything


# ── Workday ───────────────────────────────────────────────────────────


def test_workday_posted_parsing():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    days_ago = lambda text: round((now - scraper._parse_workday_posted(text)).total_seconds() / 86400)
    assert days_ago("Posted Today") == 0
    assert days_ago("Posted Yesterday") == 1
    assert days_ago("Posted 3 Days Ago") == 3
    assert scraper._parse_workday_posted("Posted 30+ Days Ago") is None
    assert scraper._parse_workday_posted("") is None


def test_workday_slug_and_urls():
    assert scraper._workday_parts("nvidia.wd5/NVIDIAExternalCareerSite") == ("nvidia.wd5", "nvidia", "NVIDIAExternalCareerSite")
    [job] = scraper._workday_jobs("nvidia.wd5", "nvidia", "Site", [
        {"title": "Software Engineer", "externalPath": "/job/India-Pune/SWE_JR1", "locationsText": "2 Locations", "postedOn": "Posted Today"},
    ])
    assert job["job_url"] == "https://nvidia.wd5.myworkdayjobs.com/Site/job/India-Pune/SWE_JR1"
    assert job["_detail"] == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/Site/job/India-Pune/SWE_JR1"
    assert scraper.is_relevant(job["title"], job["location"])  # resolved after the detail fetch


def test_workday_detail_fills_description_and_locations(monkeypatch):
    monkeypatch.setattr(scraper, "_http_get", lambda url, json_body=None: json.dumps({"jobPostingInfo": {
        "jobDescription": "<p>Build <b>things</b></p>", "location": "India, Pune", "additionalLocations": ["India, Bengaluru"],
    }}))
    jobs = [{"title": "SWE", "location": "2 Locations", "description": "", "_detail": "https://x/detail"}]
    scraper._fill_workday_descriptions(jobs)
    assert jobs[0]["description"] == "Build things"
    assert jobs[0]["location"] == "India, Pune; India, Bengaluru"
    assert "_detail" not in jobs[0]


def test_workday_slug_validation(server):
    base, db = server
    assert call(base, "POST", "/api/companies", {"portal": "workday", "slug": "nvidia"})[0] == 400
    assert call(base, "POST", "/api/companies", {"portal": "workday", "slug": "nvidia.wd5/Nope Site"})[0] == 400


# ── Paging, totals and search ─────────────────────────────────────────


def _load_server_module():
    spec = importlib.util.spec_from_file_location("admin_server_f", ROOT / "frontend" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_jobs_filter_builds_data_api_filter():
    f = _load_server_module().jobs_filter
    assert f({}) == {}
    assert f({"site": "indeed", "min": "6"}) == {"site": "indeed", "score": {"$gte": 6}}
    assert f({"early": "1"}) == {"applicants": {"$lte": 25}}
    assert f({"sort": "applicants"}) == {"applicants": {"$gte": 0}}
    assert f({"sort": "applicants", "early": "1"}) == {"applicants": {"$gte": 0, "$lte": 25}}
    assert f({"q": "  full stack "}) == {"$lexical": {"$match": "full stack"}}
    assert f({"min": "abc", "q": "   "}) == {}


def test_jobs_are_paged_with_totals(server):
    base, db = server
    status, p1 = call(base, "GET", "/api/jobs?page=1&size=20")
    assert status == 200 and len(p1["items"]) == 20 and p1["total"] == 45 and p1["has_more"] is True
    assert "$lexical" not in p1["items"][0]
    _, p3 = call(base, "GET", "/api/jobs?page=3&size=20")
    assert len(p3["items"]) == 5 and p3["has_more"] is False
    assert db.last_query[3:] == (3, 20)
    _, odd = call(base, "GET", "/api/jobs?page=0&size=7&sort=nonsense")
    assert odd["page"] == 1 and odd["size"] == 20  # bad input falls back to defaults
    assert db.last_query[2] == {"scraped_at": -1}


def test_overview_has_totals_for_every_list(server):
    base, db = server
    call(base, "POST", "/api/recipients", {"email": "a@b.co"})
    status, o = call(base, "GET", "/api/overview")
    assert status == 200 and o["min_score"] == 6 and o["sites"] == ["linkedin"] and list(o["by_site"]) == ["linkedin"]
    assert o["counts"]["jobs"] == {"n": 45, "capped": False} and o["counts"]["recipients"]["n"] == 1
    assert len(o["top"]) == 8 and "$lexical" not in o["top"][0]
    call(base, "POST", "/api/recipients", {"email": "c@d.co"})  # an edit must drop the cached overview
    assert call(base, "GET", "/api/overview")[1]["counts"]["recipients"]["n"] == 2


# ── Sorted queries return at most 50 documents ─────────────────────────


def test_page_of_100_is_fetched_as_two_sorted_requests_of_50():
    from db import JobDB

    calls = []

    class Coll:
        def find(self, flt, sort, skip, limit, projection):
            calls.append((skip, limit))
            return iter([{"_id": i} for i in range(skip, min(skip + min(limit, 50), 171))])  # server cap: 50

    db = object.__new__(JobDB)
    db.db = type("D", (), {"get_collection": lambda self, name: Coll()})()
    rows = db.page("jobs", {}, {"x": 1}, page=1, size=100)
    assert [r["_id"] for r in rows] == list(range(100))
    assert sorted(calls) == [(0, 50), (50, 50)]
    calls.clear()
    assert [r["_id"] for r in db.page("jobs", {}, {"x": 1}, page=2, size=100)] == list(range(100, 171))
    assert sorted(calls) == [(100, 50), (150, 50)]
    calls.clear()
    assert len(db.page("jobs", {}, {"x": 1}, page=1, size=20)) == 20 and calls == [(0, 20)]


# ── Blocklist ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("a,b", [
    ("Nexal IIT", "nexal iit"),
    ("Unified Mentor Private Limited", "Unified Mentor"),
    ("Acme Pvt. Ltd.", "ACME"),
    ("internmo", "Internmo Inc."),
])
def test_company_key_ignores_case_punctuation_and_legal_suffixes(a, b):
    from db import company_key
    assert company_key(a) == company_key(b) != ""


def test_company_key_handles_missing_and_keeps_distinct_names_apart():
    from db import company_key
    assert company_key(None) == company_key(float("nan")) == company_key("") == company_key("Pvt Ltd")
    assert company_key("Nexal IIT") != company_key("Nexal Labs")


def test_drop_blocked_removes_only_blocklisted_companies():
    import pandas as pd
    import main
    df = pd.DataFrame([
        {"job_url": "1", "company": "Nexal IIT"},
        {"job_url": "2", "company": "INTERNMO Private Limited"},
        {"job_url": "3", "company": "Stripe"},
        {"job_url": "4", "company": None},
    ])
    kept, removed = main.drop_blocked(df, {"nexal iit", "internmo"})
    assert removed == 2 and list(kept["job_url"]) == ["3", "4"]
    same, none_removed = main.drop_blocked(df, set())
    assert none_removed == 0 and len(same) == 4
    empty, zero = main.drop_blocked(pd.DataFrame(), {"x"})
    assert zero == 0 and empty.empty


def test_blocking_stores_normalized_key_purges_stored_jobs_and_unblocks(server):
    base, db = server
    status, body = call(base, "POST", "/api/blocked_companies", {"name": "  Nexal IIT Pvt Ltd "})
    assert status == 200 and body == {"ok": True, "purged": 3}
    assert db.purged == ["nexal iit"]
    assert list(db.collections["blocked_companies"]) == ["nexal iit"]
    assert db.collections["blocked_companies"]["nexal iit"]["name"] == "Nexal IIT Pvt Ltd"
    _, listing = call(base, "GET", "/api/blocked_companies")
    assert listing["total"] == 1
    assert call(base, "GET", "/api/overview")[1]["counts"]["blocklist"]["n"] == 1
    assert call(base, "DELETE", "/api/blocked_companies", {"name": "NEXAL IIT"})[0] == 200  # any spelling unblocks
    assert db.collections["blocked_companies"] == {}


def test_blocklist_rejects_empty_names(server):
    base, db = server
    assert call(base, "POST", "/api/blocked_companies", {"name": ""})[0] == 400
    assert call(base, "POST", "/api/blocked_companies", {"name": "Pvt Ltd"})[0] == 400  # nothing left after legal words
    assert call(base, "POST", "/api/blocked_companies", {"name": "x" * 101})[0] == 400


def test_delete_company_jobs_only_deletes_exact_company_matches():
    from db import JobDB

    stored = {
        "a": {"_id": "a", "company": "Nexal IIT"},
        "b": {"_id": "b", "company": "nexal iit pvt ltd"},
        "c": {"_id": "c", "company": "Nexal Labs"},  # shares a word, different company
    }
    deleted = []

    class Jobs:
        def find(self, flt, projection=None):
            assert flt == {"$lexical": {"$match": "nexal iit"}}
            return iter(stored.values())

        def delete_many(self, flt):
            deleted.extend(flt["_id"]["$in"])

    db = object.__new__(JobDB)
    db.jobs = Jobs()
    assert db.delete_company_jobs("nexal iit") == 2
    assert sorted(deleted) == ["a", "b"]


# ── Email digest ───────────────────────────────────────────────────────

NAN = float("nan")


def _job(i=0, **over):
    job = {
        "job_url": f"https://www.linkedin.com/jobs/view/{i}", "title": f"Backend Engineer {i}", "company": "Acme",
        "location": "Pune, India", "score": 9, "reasoning": "Strong fit for the candidate's Python and Kafka work.",
        "matching": "Python, Kafka, Airflow, SQL, Neo4j, Docker, AWS", "site": "linkedin", "applicants": 12,
        "applicants_text": "Be among the first 25 applicants", "poster_name": "Asha Rao",
        "poster_url": "https://in.linkedin.com/in/asha-rao",
    }
    job.update(over)
    return job


def test_email_escapes_scraped_text_and_drops_unsafe_links():
    import notifier
    evil = _job(title='<script>alert(1)</script> & "Co"', company="<b>Evil</b>", poster_name="<img src=x>",
                job_url="javascript:alert(1)", poster_url="data:text/html,hi")
    page = notifier._build_html([evil])
    assert "<script>" not in page and "<b>Evil</b>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "&amp;" in page
    assert "javascript:" not in page and "data:text" not in page


def test_email_survives_missing_values_from_pandas():
    import notifier
    bare = {"job_url": NAN, "title": NAN, "company": NAN, "location": None, "score": NAN, "reasoning": NAN,
            "matching": NAN, "applicants": NAN, "applicants_text": NAN, "poster_name": NAN, "site": NAN}
    page = notifier._build_html([bare, _job(applicants=None, applicants_text=NAN, poster_name=NAN)])
    plain = notifier._build_plain([bare])
    assert "nan" not in page.lower().replace("nan>", "") and "Untitled role" in page and "Untitled role" in plain
    assert "None" not in page and "None" not in plain


def test_email_groups_jobs_counts_early_applicants_and_names_sources():
    import notifier
    page = notifier._build_html([_job(1), _job(2, score=7, applicants=60, applicants_text="Over 60 applicants", site="indeed")])
    assert "Strong matches" in page and "Worth a look" in page and "2 new matches" in page
    assert "1 with 25 applicants or fewer" in page and "First 25 applicants" in page
    assert "LinkedIn" in page and "Indeed" in page and "Posted by" in page
    only_rest = notifier._build_html([_job(1, score=6)])
    assert "Strong matches" not in only_rest and "Worth a look" in only_rest and "1 new match<" in only_rest


def test_email_with_50_jobs_stays_under_gmails_clipping_limit():
    import notifier
    jobs = [_job(i, title="A long job title for a backend engineering role " * 2, reasoning="Reasoning text " * 14) for i in range(50)]
    assert len(notifier._build_html(jobs).encode()) < 100_000


def test_plain_text_carries_the_same_links_and_message_as_the_html():
    import notifier
    jobs = [_job(1), _job(2, score=7)]
    subject, plain, page = notifier.build_digest(jobs)
    assert subject == "Daily Job Digest (2 matches)"
    for job in jobs:
        for text in (job["job_url"], job["title"], job["reasoning"]):
            assert text in plain and notifier._e(text) in page
    assert "Posted by Asha Rao https://in.linkedin.com/in/asha-rao" in plain
    assert "STRONG MATCHES (1)" in plain and "WORTH A LOOK (1)" in plain
    assert "<table" not in plain and "<div" not in plain  # no markup in the text part
    assert "prefers-color-scheme: dark" in page and 'lang="en"' in page and "<img" not in page
