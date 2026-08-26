"""Email notification module.

Sends a clean HTML email with the top scored jobs via Gmail SMTP.
"""

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    SMTP_EMAIL,
    SMTP_PASSWORD,
    SMTP_HOST,
    SMTP_PORT,
    NOTIFY_EMAIL,
    MAX_JOBS_IN_EMAIL,
)

logger = logging.getLogger(__name__)


def _score_color(score: int) -> str:
    """Return a hex color for the score badge."""
    if score >= 8:
        return "#16a34a"  # green
    elif score >= 6:
        return "#ca8a04"  # amber
    elif score >= 4:
        return "#ea580c"  # orange
    else:
        return "#dc2626"  # red


def _build_html(jobs: list[dict]) -> str:
    """Build the HTML email body."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    count = len(jobs)

    rows = ""
    for i, job in enumerate(jobs, 1):
        score = job.get("score", 0)
        color = _score_color(score)
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        location = job.get("location", "—")
        reasoning = job.get("reasoning", "")
        matching = job.get("matching", "")
        url = job.get("job_url", "#")
        seniority = job.get("seniority_fit", "")

        rows += f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
            <td style="padding: 16px 12px; text-align: center; vertical-align: top;">
                <span style="display: inline-block; background: {color}; color: white;
                             font-weight: 700; font-size: 18px; padding: 6px 14px;
                             border-radius: 8px; min-width: 30px;">{score}</span>
            </td>
            <td style="padding: 16px 12px; vertical-align: top;">
                <a href="{url}" style="color: #1d4ed8; text-decoration: none;
                          font-weight: 600; font-size: 15px;">{title}</a>
                <div style="color: #374151; font-size: 14px; margin-top: 2px;">
                    {company} &middot; {location}
                </div>
                <div style="color: #6b7280; font-size: 13px; margin-top: 6px;
                            line-height: 1.4;">{reasoning}</div>
                <div style="margin-top: 6px;">
                    <span style="font-size: 11px; color: #059669; background: #ecfdf5;
                                 padding: 2px 8px; border-radius: 4px;">{matching}</span>
                </div>
            </td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin: 0; padding: 0; background: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        <div style="max-width: 680px; margin: 0 auto; padding: 24px;">
            <div style="background: white; border-radius: 12px; overflow: hidden;
                        box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                <!-- Header -->
                <div style="background: linear-gradient(135deg, #1e293b, #334155);
                            padding: 28px 24px; color: white;">
                    <h1 style="margin: 0; font-size: 22px; font-weight: 700;">🎯 Job Matches Found</h1>
                    <p style="margin: 6px 0 0; font-size: 14px; color: #94a3b8;">
                        {count} relevant jobs &middot; {now}
                    </p>
                </div>

                <!-- Jobs Table -->
                <table style="width: 100%; border-collapse: collapse;">
                    {rows}
                </table>

                <!-- Footer -->
                <div style="padding: 16px 24px; background: #f9fafb; text-align: center;
                            font-size: 12px; color: #9ca3af;">
                    Automated job scan &middot; Scored by Gemini AI
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_email(jobs: list[dict]) -> bool:
    """Send an HTML email with the top scored jobs.

    Args:
        jobs: List of job dicts, already sorted by score descending.
              Only the top MAX_JOBS_IN_EMAIL will be included.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    if not jobs:
        logger.info("No jobs to notify about. Skipping email.")
        return False

    # Take only the top N
    top_jobs = jobs[:MAX_JOBS_IN_EMAIL]

    # Build email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f" Daily Job Digest ({len(top_jobs)} matches)"
    msg["From"] = SMTP_EMAIL
    msg["To"] = NOTIFY_EMAIL

    # Plain text fallback
    plain = "Top job matches:\\n\\n"
    for j in top_jobs:
        plain += f"[{{j.get('score', 0)}}/10] {{j.get('title', '')}} at {{j.get('company', '')}}\\n"
        plain += f"  {{j.get('job_url', '')}}\\n"
        plain += f"  {{j.get('reasoning', '')}}\\n\\n"

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(_build_html(top_jobs), "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info("✅ Email sent to %s with %d job matches.", msg["To"], len(top_jobs))
        return True

    except Exception as e:
        logger.error("❌ Failed to send email: %s", e)
        return False
