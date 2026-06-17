from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests

from jobspy.model import JobPost

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
BATCH_SIZE = 10

SYSTEM_PROMPT = """You are an expert job market analyst. You will be given a batch of job listings and must analyze each one.

For each job return a JSON object with these exact fields:
- id: the job id provided
- relevance_score: integer 0-100, how relevant this job is to AI/ML/software engineering roles
  (100 = perfect fit for AI/ML engineer, 0 = completely unrelated)
- visa_sponsorship: one of "yes", "no", or "unknown"
  "yes" = job explicitly offers visa sponsorship (look for: "visa sponsorship", "482 visa", "employer sponsored", "open to sponsorship", "will sponsor")
  "no" = job explicitly excludes sponsorship (look for: "must be Australian citizen", "PR required", "no sponsorship", "must have full work rights", "citizens and PR only")
  "unknown" = no clear signal either way
- salary_min_aud: integer annual AUD minimum salary, or null if not determinable
- salary_max_aud: integer annual AUD maximum salary, or null if not determinable
  (if hourly, multiply by 2080; if monthly, multiply by 12)
- seniority: one of "junior", "mid", "senior", "lead", "executive", "unknown"
- work_model: one of "remote", "hybrid", "onsite", "unknown"
- tech_stack: array of specific technologies mentioned (languages, frameworks, tools, platforms)

Return a JSON array of objects, one per job. No markdown, no explanation, just valid JSON."""

USER_PROMPT_TEMPLATE = """Analyze these {n} job listings:

{jobs_json}

Return a JSON array with {n} objects, one per job, in the same order."""


def enrich_jobs(jobs: list[JobPost], api_key: str) -> list[JobPost]:
    """Enrich all jobs with AI-extracted fields. Modifies jobs in-place, returns filtered list."""
    enriched = []
    for i in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[i: i + BATCH_SIZE]
        _enrich_batch(batch, api_key)
        time.sleep(1)

    # Apply filter: drop relevance < 40, but keep all with visa_sponsorship = "yes"
    for job in jobs:
        score = job.ai_relevance_score or 0
        visa = job.visa_sponsorship or "unknown"
        if score >= 40 or visa == "yes":
            enriched.append(job)

    return enriched


def _enrich_batch(batch: list[JobPost], api_key: str) -> None:
    """Call DeepSeek API for a batch of jobs and write results back onto each JobPost."""
    jobs_data = []
    for job in batch:
        # Truncate description to keep tokens manageable
        desc = (job.description or "")[:2000]
        jobs_data.append({
            "id": job.id,
            "title": job.title,
            "company": job.company_name,
            "location": job.location.display_location() if job.location else "",
            "description": desc,
        })

    user_msg = USER_PROMPT_TEMPLATE.format(
        n=len(batch),
        jobs_json=json.dumps(jobs_data, ensure_ascii=False, indent=2),
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]

        # DeepSeek may wrap in {"jobs": [...]} or return array directly
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            results = parsed.get("jobs") or list(parsed.values())[0]
        else:
            results = parsed

        # Map results back to JobPost objects by position
        for job, result in zip(batch, results):
            job.ai_relevance_score = _safe_int(result.get("relevance_score"))
            job.visa_sponsorship = result.get("visa_sponsorship", "unknown")
            job.tech_stack = result.get("tech_stack") or []

            # Overwrite compensation with AI-normalized AUD values if better
            salary_min = _safe_int(result.get("salary_min_aud"))
            salary_max = _safe_int(result.get("salary_max_aud"))
            if salary_min and not (job.compensation and job.compensation.min_amount):
                from jobspy.model import Compensation, CompensationInterval
                job.compensation = Compensation(
                    interval=CompensationInterval.YEARLY,
                    min_amount=float(salary_min),
                    max_amount=float(salary_max or salary_min),
                    currency="AUD",
                )

            seniority = result.get("seniority", "unknown")
            if seniority and seniority != "unknown":
                job.job_level = seniority

            work_model = result.get("work_model", "unknown")
            if work_model == "remote":
                job.is_remote = True
            elif work_model in ("hybrid", "onsite"):
                job.is_remote = False
            job.work_from_home_type = work_model if work_model != "unknown" else None

    except Exception as e:
        # On failure, mark batch jobs as unknown so they still pass through
        for job in batch:
            if job.ai_relevance_score is None:
                job.ai_relevance_score = 50
            if job.visa_sponsorship is None:
                job.visa_sponsorship = "unknown"


def _safe_int(val) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None
