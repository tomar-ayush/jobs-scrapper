"""Gemini LLM-based job relevance scorer.

Sends your resume + batches of job descriptions to Gemini and receives
structured relevance scores (1-10) with reasoning for each job.

Design:
- Batches are sized by estimated token count, not arbitrary fixed size.
- Exhausted models (429) are skipped for the rest of the run.
- Temporarily unavailable models (503) are also skipped for the rest of the run.
- A batch either fully succeeds or fully fails. Failed batches are not
  inserted into DB, so those jobs get re-scraped and re-scored next run.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any

from google import genai
from google.genai import types

from config import GEMINI_MODEL, RESUME_TEXT_PATH, FALLBACK_MODELS

logger = logging.getLogger(__name__)

_client = None


@dataclass
class RunState:
    """Per-invocation state for model availability tracking.

    Created fresh at the start of each score_jobs() call so state never
    leaks across runs, even when the process is long-lived.
    """
    # Models that returned 429 this run — skip for remaining batches
    exhausted_models: set[str] = field(default_factory=set)
    # Models that returned 503 this run — skip for remaining batches
    unavailable_models: set[str] = field(default_factory=set)


# ── Token estimation constants ────────────────────────────────────────
# Rough estimate: 1 token ≈ 4 characters for English text
CHARS_PER_TOKEN = 4
MAX_INPUT_TOKENS = 900_000  # Conservative limit (models accept 1M+)
MAX_OUTPUT_TOKENS = 60_000  # Cap output so response never truncates
DESC_CHAR_LIMIT = 1500  # Per-job description cap

# Estimated output tokens per job (~300 chars of JSON = ~75 tokens)
EST_OUTPUT_TOKENS_PER_JOB = 100
# Hard cap — even if tokens allow more, don't exceed this for reliability
HARD_MAX_BATCH_SIZE = 50


def _get_client() -> genai.Client:
    """Lazy-initialize the Gemini client (avoids import-time crashes)."""
    global _client
    if _client is None:
        from config import GEMINI_API_KEY

        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


SCORING_PROMPT = """
You are a job-fit evaluator for a software engineering candidate.

CANDIDATE RESUME:
{resume}

You will be provided with a batch of jobs.
Evaluate how well this candidate fits each job. Consider:
1. Technical skill overlap (how many required skills does the candidate have?)
2. Experience level match (candidate has ~1 year of experience as an apprentice)
3. Domain relevance (backend, data engineering, AI/LLM, full-stack)
4. Growth potential (can the candidate realistically grow into this role?)
5. Location fit (candidate is based in India)

For EACH job, provide an evaluation.
Respond with ONLY a JSON object (no markdown, no code fences). The keys should be the JOB_ID provided in the input, and the values should be the evaluation object.
Example format:
{{
    "JOB_0": {{
        "score": <1-10 integer>,
        "reasoning": "<1 sentence>",
        "matching_skills": ["<skill1>", "<skill2>"],
        "missing_skills": ["<skill1>", "<skill2>"],
        "seniority_fit": "<too_junior|good|stretch|too_senior>"
    }},
    "JOB_1": {{ ... }}
}}

Scoring guide:
- 9-10: Near-perfect match, candidate should apply immediately
- 7-8: Strong match, most skills align, worth applying
- 5-6: Decent match, some skill gaps but learnable
- 3-4: Weak match, significant gaps
- 1-2: Poor match, wrong domain or way too senior

JOBS TO EVALUATE:
{jobs_text}
"""

_resume_text: str | None = None


def _get_resume() -> str:
    """Load and cache the resume text."""
    global _resume_text
    if _resume_text is None:
        _resume_text = RESUME_TEXT_PATH.read_text(encoding="utf-8")
    return _resume_text


def _estimate_tokens(text: str) -> int:
    """Rough token count from character length."""
    return len(text) // CHARS_PER_TOKEN


def _compute_batch_sizes(
    jobs: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Split jobs into batches sized by token budget, not fixed count."""
    resume = _get_resume()
    base_prompt = SCORING_PROMPT.format(resume=resume, jobs_text="")
    base_tokens = _estimate_tokens(base_prompt)

    input_budget = MAX_INPUT_TOKENS - base_tokens
    max_jobs_by_output = MAX_OUTPUT_TOKENS // EST_OUTPUT_TOKENS_PER_JOB

    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_input_tokens = 0

    for job in jobs:
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        location = job.get("location", "Unknown")
        desc = job.get("description", "")[:DESC_CHAR_LIMIT]
        job_text = f"--- JOB_X ---\nTITLE: {title}\nCOMPANY: {company}\nLOCATION: {location}\nDESCRIPTION:\n{desc}\n"
        job_tokens = _estimate_tokens(job_text)

        would_exceed_input = (current_input_tokens + job_tokens) > input_budget
        would_exceed_output = (len(current_batch) + 1) > max_jobs_by_output
        would_exceed_hard_cap = (len(current_batch) + 1) > HARD_MAX_BATCH_SIZE

        if current_batch and (
            would_exceed_input or would_exceed_output or would_exceed_hard_cap
        ):
            batches.append(current_batch)
            current_batch = []
            current_input_tokens = 0

        current_batch.append(job)
        current_input_tokens += job_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def _build_prompt(jobs_batch: List[Dict[str, Any]]) -> str:
    """Build the scoring prompt for a batch of jobs."""
    jobs_text_parts = []
    for i, job in enumerate(jobs_batch):
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        location = job.get("location", "Unknown")
        desc = job.get("description", "")[:DESC_CHAR_LIMIT]
        jobs_text_parts.append(
            f"--- JOB_{i} ---\nTITLE: {title}\nCOMPANY: {company}\nLOCATION: {location}\nDESCRIPTION:\n{desc}\n"
        )

    return SCORING_PROMPT.format(
        resume=_get_resume(),
        jobs_text="\n".join(jobs_text_parts),
    )


def _parse_response(text: str) -> dict | None:
    """Strip markdown fences and parse JSON. Returns None on failure."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    return None


def _call_model(prompt: str, state: RunState) -> dict | None:
    """Try each non-exhausted, non-unavailable model. Returns parsed dict or None."""
    available = [
        m for m in FALLBACK_MODELS
        if m not in state.exhausted_models and m not in state.unavailable_models
    ]

    if not available:
        logger.error("All models exhausted or unavailable for this run.")
        return None

    for model_name in available:
        try:
            response = _get_client().models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                ),
            )

            result = _parse_response(response.text)
            if result:
                return result

            logger.warning(
                "Model %s returned 200 but unparseable JSON. Trying next...",
                model_name,
            )
            continue

        except Exception as e:
            error_msg = str(e).lower()
            logger.warning("Gemini API error with model %s: %s", model_name, e)

            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                state.exhausted_models.add(model_name)
                remaining = len(FALLBACK_MODELS) - len(state.exhausted_models) - len(state.unavailable_models)
                logger.info(
                    "Model %s exhausted. %d/%d models remaining.",
                    model_name,
                    remaining,
                    len(FALLBACK_MODELS),
                )
            elif "503" in error_msg or "unavailable" in error_msg:
                state.unavailable_models.add(model_name)
                logger.info(
                    "Model %s temporarily unavailable (503), skipping for this run.",
                    model_name,
                )
            else:
                logger.info("Non-quota error on %s, trying next...", model_name)
            continue

    return None


def _score_batch(jobs_batch: List[Dict[str, Any]], state: RunState) -> dict | None:
    """Score a batch. Returns parsed results dict, or None if all attempts fail.

    Retries once on total failure (handles transient errors when all current
    models are temporarily down but state has been updated).
    """
    prompt = _build_prompt(jobs_batch)

    result = _call_model(prompt, state)
    if result:
        return result

    # One retry for transient failures
    logger.info("Batch failed. Retrying in 5s...")
    time.sleep(5)
    result = _call_model(prompt, state)
    if result:
        return result

    logger.error(
        "Batch failed after retry. %d jobs will be retried next run.",
        len(jobs_batch),
    )
    return None


def score_jobs(jobs: list[dict], delay: float = 2.0) -> list[dict]:
    """Score jobs in token-optimized batches.

    A batch either fully succeeds or fully fails.
    - Succeeded: all jobs in the batch get scores and are returned.
    - Failed: all jobs in the batch are dropped (not returned), so they
      won't be inserted into DB and will be re-scraped next run.

    Args:
        jobs: List of job dicts with keys: title, company, location, description
        delay: Seconds to wait between API calls (rate limiting)

    Returns:
        List of successfully scored job dicts only.
    """
    # Fresh state per invocation — no leak across runs in the same process
    state = RunState()

    total = len(jobs)
    batches = _compute_batch_sizes(jobs)

    logger.info(
        "Scoring %d jobs with Gemini (%s): %d batches (token-optimized, sizes: %s)",
        total,
        GEMINI_MODEL,
        len(batches),
        [len(b) for b in batches],
    )

    scored_jobs: list[dict] = []

    for batch_idx, batch in enumerate(batches, 1):
        batch_results = _score_batch(batch, state)

        if batch_results is None:
            logger.warning(
                "Batch %d/%d FAILED (%d jobs dropped).",
                batch_idx,
                len(batches),
                len(batch),
            )
            continue

        for j, job in enumerate(batch):
            job_key = f"JOB_{j}"
            res = batch_results.get(job_key, {})

            raw_score = res.get("score", 0)
            try:
                raw_score = int(raw_score)
            except (ValueError, TypeError):
                raw_score = 0

            job["score"] = (
                max(1, min(10, raw_score)) if raw_score > 0 else 1
            )
            job["reasoning"] = res.get("reasoning", "")
            job["matching"] = ", ".join(
                res.get("matching_skills", [])
                if isinstance(res.get("matching_skills"), list)
                else []
            )
            job["missing"] = ", ".join(
                res.get("missing_skills", [])
                if isinstance(res.get("missing_skills"), list)
                else []
            )
            job["seniority_fit"] = res.get("seniority_fit", "unknown")
            scored_jobs.append(job)

        logger.info(
            "  Batch %d/%d OK (%d jobs scored, %d total so far)",
            batch_idx,
            len(batches),
            len(batch),
            len(scored_jobs),
        )

        if delay > 0 and batch_idx < len(batches):
            time.sleep(delay)

    failed_count = total - len(scored_jobs)
    logger.info(
        "Scoring complete: %d/%d scored, %d failed (will retry next run).",
        len(scored_jobs),
        total,
        failed_count,
    )
    return scored_jobs
