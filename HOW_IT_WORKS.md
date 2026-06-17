# Job Hunter — How It Works

## What We Built

A daily automated job search pipeline that:
1. Searches LinkedIn, Indeed, and Seek.com.au for AI/software jobs in Australia
2. Scores each job using DeepSeek AI (relevance, visa sponsorship, salary, tech stack)
3. Matches your resume to each job
4. Writes results to your Google Sheet in 3 tabs
5. Sends you a Telegram summary every morning at 7 AM CT

You wake up, check Telegram, open the Sheet, and your curated job list is ready.

---

## What Is NOT New

No new Docker container was created. No new service is running. Everything runs
inside your **existing OpenClaw container** (`openclaw-nl21-openclaw-1`) that was
already on this machine.

What we added inside that container:
- A folder at `/data/.openclaw/workspace/job-hunter/` with Python scripts
- A new cron job entry in OpenClaw's existing scheduler
- Your resume file
- A config file

That's it. Nothing new to maintain or monitor.

---

## Folder Structure

```
/data/.openclaw/workspace/job-hunter/
│
├── JobSpy/                        ← Our forked Python library (GitHub: aakashcoder0812/JobSpy)
│   ├── run.py                     ← MAIN SCRIPT — entry point for the whole pipeline
│   ├── jobspy/
│   │   ├── __init__.py            ← Orchestrates scraping across all sources in parallel
│   │   ├── model.py               ← Data models (JobPost, Site, Location, Compensation)
│   │   ├── linkedin/              ← LinkedIn scraper
│   │   ├── indeed/                ← Indeed scraper
│   │   ├── seek/                  ← Seek.com.au scraper (we built this from scratch)
│   │   ├── ai_filter.py           ← DeepSeek AI enrichment layer (we built this)
│   │   ├── resume_matcher.py      ← Resume selection logic (we built this)
│   │   └── sheets_exporter.py     ← Google Sheets writer (we built this)
│
├── resumes/
│   └── resume_primary.md          ← Your resume (Aakash Munjal, Amazon SDE3)
│
├── credentials/                   ← Empty — uses existing /data/.config/gcloud/service-account.json
├── output/
│   └── summary.json               ← Written after each run (job counts, top matches)
├── config.yaml                    ← Search terms, filters, API keys — edit this to customize
└── run.sh                         ← Shell wrapper that calls run.py and writes run.log
```

---

## How It's Triggered

### Daily (Automatic)
OpenClaw's built-in cron scheduler fires at **7:00 AM CT every day**.
It tells the `main` agent to run the script and send you a Telegram message.

The cron job we added to `/data/.openclaw/cron/jobs.json`:
```
Schedule : 7:00 AM CT, daily
Agent    : main (your existing OpenClaw main agent)
Action   : bash /data/.openclaw/workspace/job-hunter/run.sh
Delivery : Telegram → your chat (5580216002)
```

### Manual (From Telegram)
You can trigger it any time from Telegram by messaging your **main agent** (the same
bot you already use for stock market briefs, daily check-ins, etc.) and saying:

  "Run the job hunt script"

or more precisely:

  "bash /data/.openclaw/workspace/job-hunter/run.sh — then read the summary
   and send me the results"

No new bot, no new agent, no new phone number. Same Telegram chat you already use.

---

## Step-by-Step: What Happens When It Runs

### Step 1 — Scraping (run.py → jobspy/)

The script loops through your search terms from `config.yaml`:
  "AI engineer", "machine learning engineer", "software engineer", etc.

For each term, it calls three scrapers in parallel (via Python threads):

**LinkedIn scraper** (`jobspy/linkedin/`)
- Hits LinkedIn's unauthenticated guest API — no login required
- URL: `linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=...`
- Returns job cards as HTML, parses title/company/location/URL
- Has a 90-second timeout — if LinkedIn is slow, it skips and moves on

**Indeed scraper** (`jobspy/indeed/`)
- Hits Indeed's internal GraphQL API (same one their mobile app uses)
- URL: `apis.indeed.com/graphql`
- Returns structured JSON with salary, company details, location
- Indeed Australia (`au.indeed.com`) is fully supported

**Seek scraper** (`jobspy/seek/` — we built this)
- Step 1: Hits `seek.com.au/api/jobsearch/v5/search` — Seek's own frontend REST API
- Step 2: For each job, fetches full description via Seek's GraphQL API
  (`seek.com.au/graphql`) — this bypasses Cloudflare which blocks regular page scraping
- No login, no API key — uses the same endpoints Seek's own website uses

All scraped jobs are deduplicated by URL and collected into a list of `JobPost` objects.
A `JobPost` has: title, company, location, salary, job_type, description, url, date_posted.

### Step 2 — Salary Pre-Filter (run.py)

Before spending any AI tokens, cheaply drops jobs where the advertised salary
maximum is below your threshold (currently AUD $120,000).
No API call needed — pure Python comparison.

### Step 3 — AI Enrichment (jobspy/ai_filter.py)

This is where DeepSeek V4 Pro comes in.

Jobs are sent in batches of 10. For each batch, one API call is made to:
  `api.deepseek.com/v1/chat/completions`

The prompt sends the job title, company, location, and description (truncated to
2,000 characters to keep costs low) and asks DeepSeek to return for each job:

  - relevance_score (0–100): How relevant to AI/software engineering?
  - visa_sponsorship: "yes" / "no" / "unknown"
    (reads the description for signals like "482 visa", "employer sponsored",
    "must be Australian citizen", "PR only", etc.)
  - salary_min_aud / salary_max_aud: Normalized to annual AUD figures
  - seniority: junior / mid / senior / lead / unknown
  - work_model: remote / hybrid / onsite / unknown
  - tech_stack: list of technologies mentioned

After enrichment, jobs with relevance_score < 40 are dropped — UNLESS they have
visa_sponsorship = "yes" (those always pass through regardless of score).

### Step 4 — Resume Matching (jobspy/resume_matcher.py)

Reads all `.md` files from the `resumes/` folder.

Right now there is one: `resume_primary.md` (your Amazon SDE3 resume).
Since there's only one resume, it auto-assigns it to every job — no AI call needed,
no cost. The `match_reason` is set to "Only one resume configured".

When you add more resume variants (e.g. resume_ai_ml.md, resume_backend.md),
the matcher will call DeepSeek once per job to pick the best fit and write a
one-line explanation.

### Step 5 — Google Sheets Export (jobspy/sheets_exporter.py)

Connects to your Google Sheet using the existing service account:
  `am-open-claw@pristine-cairn-491811-s5.iam.gserviceaccount.com`

Writes to 3 tabs:

**Tab 1 — Raw Jobs** (everything scraped, kept for 7 days)
  run_date | source | title | company | location | salary | job_url | posted_date

**Tab 2 — AI Analyzed** (only jobs that passed the relevance filter, kept 30 days)
  + relevance_score | visa_sponsorship | seniority | work_model | tech_stack

**Tab 3 — Resume Match** (same as Tab 2 but adds resume recommendation)
  + resume_to_use | match_reason

Deduplication: if a job URL was already written today, it won't be written again.
Retention: old rows are automatically deleted based on the retention window.

### Step 6 — Summary (run.py)

Writes `/data/.openclaw/workspace/job-hunter/output/summary.json` with:
  - Total jobs scraped per source
  - How many passed the AI filter
  - How many have visa sponsorship
  - Top 3 highest-scoring job matches
  - Sheet URL

The OpenClaw `main` agent reads this file and sends you the Telegram message.

---

## What the OpenClaw Agent Actually Does

The `main` agent (existing, not new) does two things:

1. **Runs the bash script** — executes `run.sh` which calls `run.py`.
   The Python code does all the real work. The agent is just the trigger.

2. **Reads summary.json and sends Telegram** — after the script finishes,
   the agent reads the output and formats a message like:

```
🔍 Job Hunt — 2026-06-17
LinkedIn: 45 | Indeed: 38 | Seek: 62 → 145 total
✅ Passed filter: 23 jobs
🛂 Visa sponsorship: 4 jobs

Top matches:
1. Senior ML Engineer @ Atlassian (Sydney) — Score 91, Visa: YES
2. AI Platform Lead @ Canva (Remote AU) — Score 87, Visa: Unknown
3. Backend Engineer @ Airtasker (Sydney) — Score 82, Visa: YES

📊 Sheet: https://docs.google.com/spreadsheets/d/1qPwS17...
```

---

## How to Trigger From Telegram

Open your existing Telegram chat with the main OpenClaw bot and send:

**To run immediately:**
```
Run the job hunt now: bash /data/.openclaw/workspace/job-hunter/run.sh
Then read /data/.openclaw/workspace/job-hunter/output/summary.json and send me the results.
```

**To check last run results without re-running:**
```
Read /data/.openclaw/workspace/job-hunter/output/summary.json and show me the results
```

**To change search terms:**
```
Edit /data/.openclaw/workspace/job-hunter/config.yaml and add "data engineer" to the search terms
```

**To check if it's running:**
```
Run: ps aux | grep run.py
```

---

## What It Costs to Run Daily

| Component        | Cost                                               |
|------------------|----------------------------------------------------|
| Scraping         | Free — no APIs, just HTTP requests                 |
| DeepSeek V4 Pro  | ~$0.05–0.15/day (100 jobs × 2K tokens = ~200K tokens/day) |
| Google Sheets    | Free                                               |
| OpenClaw         | Already running on your Hostinger VPS              |
| Total            | **< $5/month**                                     |

---

## How to Customize

All customization is in one file: `/data/.openclaw/workspace/job-hunter/config.yaml`

Tell the main agent: "Edit the job hunter config and change X" — or edit directly.

Key settings:
- `search_terms` — what to search for
- `filters.min_relevance_score` — lower = more jobs, higher = stricter (default: 40)
- `filters.salary_min_aud` — minimum salary threshold (default: 120,000)
- `filters.hours_old` — only jobs posted in last N hours (default: 48)
- `sources` — which job boards to use (linkedin, indeed, seek)

---

## Adding More Resume Variants (Later)

1. Create a new file in `/data/.openclaw/workspace/job-hunter/resumes/`
   e.g. `resume_ai_ml.md` — paste the text of that resume variant
2. The matcher automatically detects multiple files and switches to AI-based
   matching (DeepSeek picks the best fit per job)
3. No code changes needed
