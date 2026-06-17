from __future__ import annotations

from datetime import date
from typing import Optional

import time
import gspread
from google.oauth2.service_account import Credentials


def _retry(fn, *args, retries=3, **kwargs):
    """Retry a gspread call up to 3 times on connection errors."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"    [sheets] transient error ({e.__class__.__name__}), retrying in 3s...")
            time.sleep(3)

from jobspy.model import JobPost

SHEET_ID = "1qPwS17qsvvaXVgil6L4fjANR1Sw4rBCBQ3SGA3zmkgY"
SA_PATH = "/data/.config/gcloud/service-account.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

RAW_HEADERS   = ["run_date","source","title","company","location","salary_min","salary_max","avg_salary","currency","job_type","job_url","posted_date"]
AI_HEADERS    = ["run_date","source","title","company","location","salary_min","salary_max","avg_salary","relevance_score","visa_sponsorship","seniority","work_model","tech_stack","job_url","posted_date"]
MATCH_HEADERS = ["run_date","title","company","location","avg_salary","relevance_score","visa_sponsorship","resume_to_use","match_reason","job_url"]


def _client() -> gspread.Client:
    creds = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet():
    """Return (spreadsheet, ws_raw, ws_ai, ws_match) — call once per run."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    return sh, sh.worksheet("Raw Jobs"), sh.worksheet("AI Analyzed"), sh.worksheet("Resume Match")


def _location_str(job: JobPost) -> str:
    return job.location.display_location() if job.location else ""


def _salary(job: JobPost, field: str) -> str:
    if job.compensation:
        val = getattr(job.compensation, field, None)
        return str(int(val)) if val else ""
    return ""


def _avg_salary(job: JobPost) -> str:
    if job.compensation and job.compensation.min_amount:
        mn = job.compensation.min_amount
        mx = job.compensation.max_amount or mn
        return str(int((mn + mx) / 2))
    return ""


def _currency(job: JobPost) -> str:
    return job.compensation.currency if job.compensation and job.compensation.currency else "AUD"


def _job_type_str(job: JobPost) -> str:
    return ", ".join(jt.value[0] for jt in job.job_type) if job.job_type else ""


def _tech_stack_str(job: JobPost) -> str:
    return ", ".join(job.tech_stack) if job.tech_stack else ""


def _existing_urls(ws: gspread.Worksheet, url_col: int) -> set[str]:
    """Return set of job URLs already in this sheet tab."""
    rows = ws.get_all_values()
    return {row[url_col] for row in rows[1:] if len(row) > url_col and row[url_col]}


def append_raw(ws: gspread.Worksheet, job: JobPost, source: str, existing_urls: set[str]) -> bool:
    """Write one job to Tab 1. Returns True if written, False if duplicate."""
    if job.job_url in existing_urls:
        return False
    existing_urls.add(job.job_url)
    _retry(ws.append_row, [
        date.today().isoformat(),
        source,
        job.title or "",
        job.company_name or "",
        _location_str(job),
        _salary(job, "min_amount"),
        _salary(job, "max_amount"),
        _avg_salary(job),
        _currency(job),
        _job_type_str(job),
        job.job_url or "",
        str(job.date_posted) if job.date_posted else "",
    ], value_input_option="USER_ENTERED")
    return True


def append_analyzed(ws: gspread.Worksheet, job: JobPost, source: str, existing_urls: set[str]) -> bool:
    """Write one AI-analyzed job to Tab 2."""
    if job.job_url in existing_urls:
        return False
    existing_urls.add(job.job_url)
    _retry(ws.append_row, [
        date.today().isoformat(),
        source,
        job.title or "",
        job.company_name or "",
        _location_str(job),
        _salary(job, "min_amount"),
        _salary(job, "max_amount"),
        _avg_salary(job),
        job.ai_relevance_score or "",
        job.visa_sponsorship or "unknown",
        job.job_level or "unknown",
        job.work_from_home_type or "unknown",
        _tech_stack_str(job),
        job.job_url or "",
        str(job.date_posted) if job.date_posted else "",
    ], value_input_option="USER_ENTERED")
    return True


def append_match(ws: gspread.Worksheet, job: JobPost, existing_urls: set[str]) -> bool:
    """Write one resume-matched job to Tab 3."""
    if job.job_url in existing_urls:
        return False
    existing_urls.add(job.job_url)
    _retry(ws.append_row, [
        date.today().isoformat(),
        job.title or "",
        job.company_name or "",
        _location_str(job),
        _avg_salary(job),
        job.ai_relevance_score or "",
        job.visa_sponsorship or "unknown",
        job.resume_match or "",
        job.match_reason or "",
        job.job_url or "",
    ], value_input_option="USER_ENTERED")
    return True
