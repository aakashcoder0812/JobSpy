from __future__ import annotations

import json
import os
import glob
from pathlib import Path

import requests

from jobspy.model import JobPost

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"

SYSTEM_PROMPT = """You are an expert recruiter. You will be given a job description and several resume profiles.
Pick the resume that best matches the job requirements.

Return a JSON object with:
- resume_name: the filename of the best matching resume (without path)
- match_reason: one sentence explaining why this resume is the best fit

Return only valid JSON, no markdown, no explanation."""

USER_PROMPT_TEMPLATE = """Job Title: {title}
Company: {company}

Job Description:
{description}

Available Resumes:
{resumes}

Which resume best fits this job?"""


def load_resumes(resumes_dir: str) -> dict[str, str]:
    """Load all .md resume files from the resumes directory."""
    resumes = {}
    for path in sorted(glob.glob(os.path.join(resumes_dir, "*.md"))):
        name = Path(path).name
        with open(path) as f:
            content = f.read().strip()
        # Skip placeholder files that haven't been filled in yet
        if "[PASTE YOUR RESUME TEXT HERE]" not in content and content:
            resumes[name] = content
    return resumes


def match_resumes(jobs: list[JobPost], resumes_dir: str, api_key: str) -> list[JobPost]:
    """Assign resume_match and match_reason to each job. Modifies jobs in-place."""
    resumes = load_resumes(resumes_dir)

    if not resumes:
        # No resumes configured yet
        for job in jobs:
            job.resume_match = "resume_primary.md"
            job.match_reason = "No resume profiles configured yet — using primary resume"
        return jobs

    if len(resumes) == 1:
        # Only one resume — no need to call AI, just assign it
        resume_name = list(resumes.keys())[0]
        for job in jobs:
            job.resume_match = resume_name
            job.match_reason = "Only one resume configured"
        return jobs

    # Multiple resumes — ask DeepSeek to pick the best fit per job
    resumes_text = "\n\n".join(
        f"--- {name} ---\n{content[:1500]}"
        for name, content in resumes.items()
    )

    for job in jobs:
        desc = (job.description or "")[:2000]
        user_msg = USER_PROMPT_TEMPLATE.format(
            title=job.title or "",
            company=job.company_name or "",
            description=desc,
            resumes=resumes_text,
        )
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
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
                timeout=30,
            )
            resp.raise_for_status()
            result = json.loads(resp.json()["choices"][0]["message"]["content"])
            job.resume_match = result.get("resume_name", list(resumes.keys())[0])
            job.match_reason = result.get("match_reason", "")
        except Exception:
            job.resume_match = list(resumes.keys())[0]
            job.match_reason = "Defaulted to primary resume (AI matching failed)"

    return jobs


def match_single(job: JobPost, resumes: dict, api_key: str) -> None:
    """Match resume for a single job in-place. Pass pre-loaded resumes dict."""
    if not resumes:
        job.resume_match = 'resume_primary.md'
        job.match_reason = 'No resume profiles configured yet'
        return
    if len(resumes) == 1:
        resume_name = list(resumes.keys())[0]
        job.resume_match = resume_name
        job.match_reason = 'Only one resume configured'
        return
    # Multiple resumes — call DeepSeek to pick best fit
    resumes_text = chr(10).join(
        f"--- {name} ---{chr(10)}{content[:1500]}"
        for name, content in resumes.items()
    )
    import requests, json as _json
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                title=job.title or "",
                company=job.company_name or "",
                description=(job.description or "")[:2000],
                resumes=resumes_text,
            )},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(DEEPSEEK_API_URL, json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30)
        resp.raise_for_status()
        result = _json.loads(resp.json()["choices"][0]["message"]["content"])
        job.resume_match = result.get("resume_name", list(resumes.keys())[0])
        job.match_reason = result.get("match_reason", "")
    except Exception:
        job.resume_match = list(resumes.keys())[0]
        job.match_reason = "Defaulted to primary resume"
