# Job Hunter Agent — Design Document

## Goal

A daily automated pipeline that searches LinkedIn, Indeed, and Seek.com.au for AI/software jobs
in Australia, filters them by relevance and visa sponsorship signals, and surfaces curated results
in a Google Sheet — so the user opens one tab each morning and has everything they need.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                 OpenClaw (Daily Cron)                │
│                    7:00 AM CT                        │
└──────────────────────┬──────────────────────────────┘
                       │ triggers
                       ▼
┌─────────────────────────────────────────────────────┐
│           job_hunter Python script                   │
│         /workspace/job-hunter/run.py                 │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ LinkedIn │  │  Indeed  │  │   Seek.com.au    │   │
│  │ scraper  │  │  scraper │  │   scraper (new)  │   │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘   │
│       └─────────────┴─────────────────┘              │
│                       │ raw JobPost list              │
│                       ▼                              │
│           ┌─────────────────────┐                    │
│           │   AI Filter Layer   │                    │
│           │   (Nexos API)       │                    │
│           │ - relevance score   │                    │
│           │ - salary extract    │                    │
│           │ - visa signal       │                    │
│           │ - remote/hybrid     │                    │
│           └──────────┬──────────┘                    │
│                      │ enriched jobs                 │
│                      ▼                               │
│           ┌─────────────────────┐                    │
│           │  Resume Matcher     │                    │
│           │  3-4 resume types   │                    │
│           │  → best fit pick    │                    │
│           └──────────┬──────────┘                    │
└──────────────────────┼──────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │       Google Sheet           │
        │  Tab 1: Raw Jobs             │
        │  Tab 2: AI-Analyzed Jobs     │
        │  Tab 3: Resume Match         │
        └──────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │   Telegram Summary           │
        │   "Found 42 jobs, 8 strong   │
        │    matches, 3 with visa"     │
        └──────────────────────────────┘
```

---

## Component Breakdown

### 1. Base: Forked JobSpy

**Repo:** Fork of `speedyapply/JobSpy` → `aakashcoder0812/JobSpy` (MIT licensed)
**Location:** `/data/.openclaw/workspace/job-hunter/JobSpy/`

**Changes to the fork:**
- Add `Site.SEEK` to the `Site` enum in `model.py`
- Add `visa_sponsorship: str | None = None` to `JobPost` (values: `"yes"` / `"no"` / `"unknown"`)
- Add `ai_relevance_score: int | None = None` to `JobPost` (0–100)
- Add `resume_match: str | None = None` to `JobPost`
- Add `match_reason: str | None = None` to `JobPost`
- Register `Site.SEEK` in `SCRAPER_MAPPING`
- Fix Glassdoor CSRF (apply unmerged upstream PR #347)

---

### 2. Seek.com.au Scraper (New Module)

**Location:** `JobSpy/jobspy/seek/__init__.py`

**Search API:**
```
GET https://www.seek.com.au/api/jobsearch/v5/search
    ?siteKey=AU-Main&sourcesystem=houston
    &where=All+Australia&keywords=...
    &classification=6281&page=1&locale=en-AU
```

**Description fetch:** Seek GraphQL API (`https://www.seek.com.au/graphql`, `jobDetails` operation)
— avoids Cloudflare because it's the same endpoint Seek's frontend uses.

**Anti-blocking:** Safari user-agent, `same-origin` referrer header, 2–3s random delay per page,
cookie persistence between requests.

**Field mapping:**

| Seek API field          | JobPost field   |
|-------------------------|-----------------|
| title                   | title           |
| advertiser.description  | company_name    |
| salary                  | compensation    |
| workType                | job_type        |
| location                | location        |
| listingDate             | date_posted     |
| jobDescription (GQL)    | description     |
| constructed from id     | job_url         |

---

### 3. AI Filter Layer

**File:** `JobSpy/jobspy/ai_filter.py`
**Model:** DeepSeek V4 Pro ()
**API base:** https://api.deepseek.com (OpenAI-compatible)

**Per-job extractions (batched 10 at a time):**
- `relevance_score` 0–100: relevance to AI/ML/software engineering
- `visa_sponsorship`: "yes" / "no" / "unknown" — from description text signals
- `salary_min`, `salary_max`: normalized AUD annual
- `seniority`: "junior" / "mid" / "senior" / "lead" / "unknown"
- `work_model`: "remote" / "hybrid" / "onsite" / "unknown"
- `tech_stack`: list of technologies mentioned

**Visa signal keywords:**
- Positive: "visa sponsorship", "sponsor work visa", "482 visa", "employer sponsored"
- Negative: "must be Australian citizen", "PR required", "no sponsorship", "full work rights only"

**Filter cutoffs:**
- Drop `relevance_score < 40`
- Always keep `visa_sponsorship = "yes"` regardless of score
- Flag but keep `visa_sponsorship = "no"` jobs in Sheet

---

### 4. Resume Matcher

**File:** `JobSpy/jobspy/resume_matcher.py`
**Profiles:** `/data/.openclaw/workspace/job-hunter/resumes/` (JSON text summaries, not PDFs)

Resume variants:
- `resume_ai_ml.json` — AI/ML engineer focus
- `resume_backend.json` — Backend/cloud engineering
- `resume_fullstack.json` — Full-stack product engineer
- `resume_data.json` — Data engineering

**Logic:** For each filtered job, send description + all resume summaries to Nexos AI.
Picks best match and writes `resume_match` + `match_reason` onto the JobPost.

---

### 5. Google Sheets Output

**Auth:** Service account JSON at `/data/.openclaw/workspace/job-hunter/credentials/google_sheets.json`

**Tab 1 — Raw Jobs** (all scraped, 7-day retention):
`run_date | source | title | company | location | salary | job_url | posted_date`

**Tab 2 — AI Analyzed** (filtered + enriched, 30-day retention):
`run_date | source | title | company | location | salary_min | salary_max | relevance_score | visa_sponsorship | seniority | work_model | tech_stack | job_url`

**Tab 3 — Resume Match** (strong matches only, 30-day retention):
`run_date | title | company | relevance_score | visa_sponsorship | resume_to_use | match_reason | job_url`

**Write strategy:** Append rows each run with `run_date`. Old rows pruned per retention policy.
Deduplication by job URL within each daily run.

---

### 6. OpenClaw Integration

**Script:** `/data/.openclaw/workspace/job-hunter/run.sh` → calls `python3 run.py`
**Cron:** 7:00 AM CT daily, agent: `main`, delivery: Telegram to `5580216002`

**Telegram format:**
```
🔍 Job Hunt — <date>
LinkedIn: N | Indeed: N | Seek: N → N total
✅ Passed filter: N jobs
🛂 Visa sponsorship: N jobs

Top matches:
1. <title> @ <company> (<location>) — Score N, Visa: YES/Unknown
2. ...
3. ...

📊 Sheet: https://docs.google.com/...
```

---

## Search Configuration (`config.yaml`)

```yaml
search_terms:
  - "AI engineer"
  - "machine learning engineer"
  - "software engineer"
  - "backend engineer"
  - "platform engineer"
  - "LLM engineer"

location: "Australia"

filters:
  min_relevance_score: 40
  salary_min_aud: 100000
  hours_old: 48
  results_per_source: 50

sources:
  - linkedin
  - indeed
  - seek
```

---

## Tech Stack

| Component            | Technology                                      |
|----------------------|-------------------------------------------------|
| Base scraping lib    | Fork of `speedyapply/JobSpy` (MIT)              |
| Seek scraper         | New module — Seek unofficial REST + GraphQL API |
| AI filtering         | DeepSeek V4 Pro (already configured in OpenClaw) |
| Google Sheets        | `gspread` + service account                     |
| Scheduling           | OpenClaw native cron                            |
| Delivery             | OpenClaw → Telegram                             |
| Config               | YAML                                            |
| Language             | Python 3.12                                     |

---

## Out of Scope (v1)

- Auto-submitting applications (manual apply only)
- LinkedIn login / Easy Apply automation
- ZipRecruiter, Glassdoor (broken upstream, low AU coverage)
- Web UI / dashboard (Google Sheet is the UI)

---

## Task List

| # | Task                                                        | Status  |
|---|-------------------------------------------------------------|---------|
| 1 | Fork JobSpy to GitHub + clone into OpenClaw workspace       | pending |
| 2 | Extend JobPost model (visa, score, resume fields)           | pending |
| 3 | Build Seek.com.au scraper module                            | pending |
| 4 | Fix Glassdoor CSRF bug (unmerged upstream PR)               | pending |
| 5 | Build AI filter layer (DeepSeek V4 Pro)                           | pending |
| 6 | Build resume matcher                                        | pending |
| 7 | Set up Google Cloud service account + Sheet                 | pending |
| 8 | Build Google Sheets exporter                                | pending |
| 9 | Build main orchestration script + config.yaml               | pending |
| 10| Wire OpenClaw cron + Telegram delivery                      | pending |
| 11| End-to-end test + validation                                | pending |
