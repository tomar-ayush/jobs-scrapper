"""Email notification module.

Sends a minimal HTML digest of the top scored jobs via Gmail SMTP.

Build notes (email-render-builder): single-column table layout, inline styles,
explicit light and dark colors, no images, text links, plain-text twin. The
markup per job is kept small because Gmail clips messages over about 102KB.
"""

import html
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import (
    SMTP_EMAIL,
    SMTP_PASSWORD,
    SMTP_HOST,
    SMTP_PORT,
    MAX_JOBS_IN_EMAIL,
)

logger = logging.getLogger(__name__)

STRONG_SCORE = 8  # at or above this a job goes under "Strong matches"
EARLY_APPLICANTS = 25  # LinkedIn's "first 25 applicants"
MAX_SKILLS = 5
SITE_NAMES = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri", "glassdoor": "Glassdoor"}

# Light palette (inline) and its dark twin (media query). Contrast on the card: text 17:1, muted 5:1, link 6:1.
CSS = """
@media (prefers-color-scheme: dark) {
  .bg { background: #0e0f11 !important; }
  .card { background: #17181b !important; }
  .t1 { color: #f2f1ed !important; }
  .t2 { color: #b4b3ac !important; }
  .rule { border-color: #2a2b2f !important; }
  .lk { color: #8bb8f5 !important; }
  .ok { color: #5fd38a !important; }
  .hi { background: #f2f1ed !important; color: #141414 !important; border-color: #f2f1ed !important; }
  .lo { border-color: #55565b !important; color: #f2f1ed !important; }
}
@media (max-width: 620px) {
  .wrap { width: 100% !important; }
  .pad { padding-left: 20px !important; padding-right: 20px !important; }
}
"""
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _e(value) -> str:
    return html.escape(value, quote=True)


def _text(job: dict, key: str) -> str:
    """A job field as clean text. Missing values arrive from pandas as NaN, not None."""
    value = job.get(key)
    return value.strip() if isinstance(value, str) else ""


def _url(job: dict, key: str) -> str:
    """Only real web links are allowed into the email."""
    url = _text(job, key)
    return url if url.startswith(("http://", "https://")) else ""


def _applicants(job: dict) -> int | None:
    n = job.get("applicants")
    return int(n) if isinstance(n, (int, float)) and n == n else None


def _is_early(job: dict) -> bool:
    n = _applicants(job)
    return n is not None and n <= EARLY_APPLICANTS


def _score(job: dict) -> int:
    n = job.get("score")
    return int(n) if isinstance(n, (int, float)) and n == n else 0


def _skills(job: dict) -> str:
    skills = [s.strip() for s in _text(job, "matching").split(",") if s.strip()]
    return ", ".join(skills[:MAX_SKILLS])


def _groups(jobs: list[dict]) -> list[tuple[str, list[dict]]]:
    strong = [j for j in jobs if _score(j) >= STRONG_SCORE]
    rest = [j for j in jobs if _score(j) < STRONG_SCORE]
    return [(name, items) for name, items in (("Strong matches", strong), ("Worth a look", rest)) if items]


def _when() -> str:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return f"{now:%a} {now.day} {now:%b}, {now.strftime('%I:%M %p').lstrip('0').lower()} IST"


def _summary(jobs: list[dict]) -> tuple[str, str]:
    early = sum(_is_early(j) for j in jobs)
    noun = "match" if len(jobs) == 1 else "matches"
    return f"{len(jobs)} new {noun}", (f"{early} with {EARLY_APPLICANTS} applicants or fewer" if early else "")


def _job_html(job: dict, first: bool) -> str:
    score = _score(job)
    title = _text(job, "title") or "Untitled role"
    link = _url(job, "job_url")
    where = " · ".join(p for p in (_text(job, "company"), _text(job, "location")) if p)
    reasoning = _text(job, "reasoning")
    skills = _skills(job)
    poster, poster_url = _text(job, "poster_name"), _url(job, "poster_url")
    applicants = _text(job, "applicants_text").replace("Be among the first", "First")

    title_html = (
        f'<a href="{_e(link)}" class="lk" style="color:#1f5fbf;text-decoration:none;">{_e(title)}</a>'
        if link else _e(title)
    )
    meta = []
    if applicants:
        cls, color = ("ok", "#0f7b3f") if _is_early(job) else ("t2", "#5f5e59")
        meta.append(f'<span class="{cls}" style="color:{color};font-weight:600;">{_e(applicants)}</span>')
    if _text(job, "site"):
        meta.append(_e(SITE_NAMES.get(_text(job, "site"), _text(job, "site").title())))
    if poster:
        who = f'<a href="{_e(poster_url)}" class="lk" style="color:#1f5fbf;">{_e(poster)}</a>' if poster_url else _e(poster)
        meta.append(f"Posted by {who}")

    badge = "hi" if score >= STRONG_SCORE else "lo"
    badge_style = (
        "background:#141414;color:#ffffff;border:1px solid #141414;"
        if badge == "hi" else "color:#141414;border:1px solid #c9c8c2;"
    )
    border = "" if first else "border-top:1px solid #ececE8;"
    rows = [f'<div class="t1" style="font-size:16px;line-height:22px;font-weight:600;">{title_html}</div>']
    if where:
        rows.append(f'<div class="t2" style="font-size:14px;line-height:20px;color:#5f5e59;margin-top:2px;">{_e(where)}</div>')
    if reasoning:
        rows.append(f'<div class="t1" style="font-size:14px;line-height:21px;margin-top:8px;">{_e(reasoning)}</div>')
    if meta:
        rows.append(f'<div class="t2" style="font-size:13px;line-height:19px;color:#5f5e59;margin-top:8px;">{" &nbsp;·&nbsp; ".join(meta)}</div>')
    if skills:
        rows.append(f'<div class="t2" style="font-size:12.5px;line-height:18px;color:#6f6e69;margin-top:4px;">Matches: {_e(skills)}</div>')
    return (
        f'<tr><td class="pad rule" style="padding:18px 32px;{border}"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="44" valign="top"><div class="{badge}" style="width:30px;height:30px;line-height:30px;border-radius:15px;text-align:center;font-size:14px;font-weight:600;{badge_style}">{score}</div></td>'
        f'<td valign="top">{"".join(rows)}</td></tr></table></td></tr>'
    )


def _build_html(jobs: list[dict]) -> str:
    headline, sub = _summary(jobs)
    preheader = f"{headline}{', ' + sub if sub else ''}."
    sections = ""
    for name, items in _groups(jobs):
        sections += (
            f'<tr><td class="pad t2" style="padding:24px 32px 8px;font-size:12px;line-height:16px;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:#5f5e59;font-weight:600;">{name} &nbsp;·&nbsp; {len(items)}</td></tr>'
            + "".join(_job_html(j, first=(i == 0)) for i, j in enumerate(items))
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>Job digest: {_e(headline)}</title>
<style>{CSS}</style>
</head>
<body class="bg" style="margin:0;padding:0;background:#f5f5f2;font-family:{FONT};">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{_e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg" style="background:#f5f5f2;"><tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" class="wrap card" style="width:600px;max-width:600px;background:#ffffff;border-radius:12px;font-family:{FONT};color:#141414;">
<tr><td class="pad t1" style="padding:32px 32px 8px;">
<div class="t2" style="font-size:12px;line-height:16px;letter-spacing:0.08em;text-transform:uppercase;color:#5f5e59;font-weight:600;">Job digest</div>
<div style="font-size:26px;line-height:32px;font-weight:600;margin-top:6px;">{_e(headline)}</div>
<div class="t2" style="font-size:14px;line-height:20px;color:#5f5e59;margin-top:4px;">{_e(_when())}{' &nbsp;·&nbsp; ' + _e(sub) if sub else ''}</div>
</td></tr>
{sections}
<tr><td class="pad t2 rule" style="padding:20px 32px 28px;border-top:1px solid #ececE8;font-size:12px;line-height:18px;color:#6f6e69;">Scored against your resume by Gemini. Applicant counts and poster names come from LinkedIn when available. Filters, recipients and the blocklist are managed in your dashboard.</td></tr>
</table>
</td></tr></table>
</body>
</html>
"""


def _build_plain(jobs: list[dict]) -> str:
    headline, sub = _summary(jobs)
    lines = [f"JOB DIGEST: {headline}", f"{_when()}{' · ' + sub if sub else ''}", ""]
    n = 0
    for name, items in _groups(jobs):
        lines += [f"{name.upper()} ({len(items)})", ""]
        for job in items:
            n += 1
            where = ", ".join(p for p in (_text(job, "company"), _text(job, "location")) if p)
            lines.append(f"{n}. [{_score(job)}/10] {_text(job, 'title') or 'Untitled role'}" + (f" - {where}" if where else ""))
            if _text(job, "reasoning"):
                lines.append(f"   {_text(job, 'reasoning')}")
            extra = [p for p in (
                _text(job, "applicants_text"),
                f"Posted by {_text(job, 'poster_name')} {_url(job, 'poster_url')}".strip() if _text(job, "poster_name") else "",
            ) if p]
            if extra:
                lines.append(f"   {' | '.join(extra)}")
            if _skills(job):
                lines.append(f"   Matches: {_skills(job)}")
            if _url(job, "job_url"):
                lines.append(f"   {_url(job, 'job_url')}")
            lines.append("")
    lines.append("Scored against your resume by Gemini. Manage filters, recipients and the blocklist in your dashboard.")
    return "\n".join(lines) + "\n"


def build_digest(jobs: list[dict]) -> tuple[str, str, str]:
    """(subject, plain text, html) for the jobs in the digest."""
    return f"Daily Job Digest ({len(jobs)} matches)", _build_plain(jobs), _build_html(jobs)


def send_email(jobs: list[dict], recipients: list[str]) -> bool:
    """Send the digest.

    Args:
        jobs: List of job dicts, already sorted by score descending.
              Only the top MAX_JOBS_IN_EMAIL will be included.
        recipients: Email addresses to send to.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    if not jobs:
        logger.info("No jobs to notify about. Skipping email.")
        return False

    if not recipients:
        logger.error("No recipients: add some in the frontend or set NOTIFY_EMAIL.")
        return False

    top_jobs = jobs[:MAX_JOBS_IN_EMAIL]
    subject, plain, html_body = build_digest(top_jobs)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, recipients, msg.as_string())

        logger.info(
            "✅ Email sent to %d recipient(s): %s with %d job matches.",
            len(recipients),
            ", ".join(recipients),
            len(top_jobs),
        )
        return True

    except Exception as e:
        logger.error("❌ Failed to send email: %s", e)
        return False
