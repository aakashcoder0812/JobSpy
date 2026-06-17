from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from jobspy.model import JobPost

SHEET_ID = "1qPwS17qsvvaXVgil6L4fjANR1Sw4rBCBQ3SGA3zmkgY"
SA_PATH = "/data/.config/gcloud/service-account.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

RAW_HEADERS    = ["run_date","source","title","company","location","salary_min","salary_max","currency","job_type","job_url","posted_date"]
AI_HEADERS     = ["run_date","source","title","company","location","salary_min","salary_max","relevance_score","visa_sponsorship","seniority","work_model","tech_stack","job_url","posted_date"]
MATCH_HEADERS  = ["run_date","title","company","location","relevance_score","visa_sponsorship","resume_to_use","match_reason","job_url"]

RAW_RETENTION_DAYS   = 7
MATCH_RETENTION_DAYS = 30


def _client() -> gspread.Client:
    creds = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _location_str(job: JobPost) -> str:
    if job.location:
        return job.location.display_location()
    return ""


def _salary(job: JobPost, field: str) -> Optional[float]:
    if job.compensation:
        return getattr(job.compensation, field, None)
    return None


def _currency(job: JobPost) -> str:
    if job.compensation and job.compensation.currency:
        return job.compensation.currency
    return "AUD"


def _job_type_str(job: JobPost) -> str:
    if job.job_type:
        return ", ".join(jt.value[0] for jt in job.job_type)
    return ""


def _tech_stack_str(job: JobPost) -> str:
    if job.tech_stack:
        return ", ".join(job.tech_stack)
    return ""


def _prune_old_rows(ws: gspread.Worksheet, retention_days: int) -> None:
    """Remove rows older than retention_days based on run_date in column A."""
    records = ws.get_all_values()
    if len(records) <= 1:
        return
    cutoff = date.today() - timedelta(days=retention_days)
    rows_to_delete = []
    for i, row in enumerate(records[1:], start=2):  # skip header
        try:
            row_date = date.fromisoformat(row[0])
            if row_date < cutoff:
                rows_to_delete.append(i)
        except (ValueError, IndexError):
            pass
    # Delete from bottom up to preserve row indices
    for row_idx in reversed(rows_to_delete):
        ws.delete_rows(row_idx)


def _dedup_urls(ws: gspread.Worksheet, url_col_index: int, new_urls: set[str]) -> set[str]:
    """Return set of URLs already present in the sheet to avoid duplicates."""
    records = ws.get_all_values()
    existing = set()
    for row in records[1:]:
        if len(row) > url_col_index:
            existing.add(row[url_col_index])
    return new_urls & existing  # intersection = already present


def export(
    raw_jobs: list[JobPost],
    filtered_jobs: list[JobPost],
    matched_jobs: list[JobPost],
    source_map: dict[str, str],  # job_url -> source name
) -> str:
    """
    Write results to all 3 Google Sheet tabs.
    Returns the sheet URL.
    """
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    today = date.today().isoformat()

    ws_raw   = sh.worksheet("Raw Jobs")
    ws_ai    = sh.worksheet("AI Analyzed")
    ws_match = sh.worksheet("Resume Match")

    # --- Tab 1: Raw Jobs ---
    _prune_old_rows(ws_raw, RAW_RETENTION_DAYS)
    existing_raw = set(r[9] for r in ws_raw.get_all_values()[1:] if len(r) > 9)
    raw_rows = []
    for job in raw_jobs:
        if job.job_url in existing_raw:
            continue
        raw_rows.append([
            today,
            source_map.get(job.job_url, ""),
            job.title or "",
            job.company_name or "",
            _location_str(job),
            _salary(job, "min_amount") or "",
            _salary(job, "max_amount") or "",
            _currency(job),
            _job_type_str(job),
            job.job_url or "",
            str(job.date_posted) if job.date_posted else "",
        ])
    if raw_rows:
        ws_raw.append_rows(raw_rows, value_input_option="USER_ENTERED")

    # --- Tab 2: AI Analyzed ---
    _prune_old_rows(ws_ai, MATCH_RETENTION_DAYS)
    existing_ai = set(r[12] for r in ws_ai.get_all_values()[1:] if len(r) > 12)
    ai_rows = []
    for job in filtered_jobs:
        if job.job_url in existing_ai:
            continue
        ai_rows.append([
            today,
            source_map.get(job.job_url, ""),
            job.title or "",
            job.company_name or "",
            _location_str(job),
            _salary(job, "min_amount") or "",
            _salary(job, "max_amount") or "",
            job.ai_relevance_score or "",
            job.visa_sponsorship or "unknown",
            job.job_level or "unknown",
            job.work_from_home_type or "unknown",
            _tech_stack_str(job),
            job.job_url or "",
            str(job.date_posted) if job.date_posted else "",
        ])
    if ai_rows:
        ws_ai.append_rows(ai_rows, value_input_option="USER_ENTERED")

    # --- Tab 3: Resume Match ---
    _prune_old_rows(ws_match, MATCH_RETENTION_DAYS)
    existing_match = set(r[8] for r in ws_match.get_all_values()[1:] if len(r) > 8)
    match_rows = []
    for job in matched_jobs:
        if job.job_url in existing_match:
            continue
        match_rows.append([
            today,
            job.title or "",
            job.company_name or "",
            _location_str(job),
            job.ai_relevance_score or "",
            job.visa_sponsorship or "unknown",
            job.resume_match or "",
            job.match_reason or "",
            job.job_url or "",
        ])
    if match_rows:
        ws_match.append_rows(match_rows, value_input_option="USER_ENTERED")

    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
