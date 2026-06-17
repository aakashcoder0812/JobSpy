#!/usr/bin/env python3
"""
Job Hunter — Daily Pipeline
Scrapes LinkedIn, Indeed, Seek → AI filters job-by-job → writes to Google Sheet live.
"""
from __future__ import annotations

import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date

import yaml

sys.path.insert(0, os.path.dirname(__file__))

from jobspy import scrape_jobs
from jobspy.model import Site, Country, JobPost, Location, Compensation, CompensationInterval
from jobspy.ai_filter import enrich_single
from jobspy.resume_matcher import load_resumes, match_single
from jobspy.sheets_exporter import open_sheet, append_raw, append_analyzed, append_match

CONFIG_PATH = "/data/.openclaw/workspace/job-hunter/config.yaml"

SITE_MAP = {"linkedin": Site.LINKEDIN, "indeed": Site.INDEED, "seek": Site.SEEK}


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def build_job(row, source_name: str) -> JobPost:
    """Build a JobPost from a scrape_jobs() DataFrame row."""
    loc = None
    if row.get("location"):
        loc = Location(country=Country.AUSTRALIA, city=str(row["location"]))

    comp = None
    if row.get("min_amount"):
        try:
            interval_val = row.get("interval", "yearly")
            interval = CompensationInterval(interval_val) if interval_val else CompensationInterval.YEARLY
            comp = Compensation(
                interval=interval,
                min_amount=float(row["min_amount"]),
                max_amount=float(row["max_amount"]) if row.get("max_amount") else float(row["min_amount"]),
                currency=str(row.get("currency", "AUD")),
            )
        except Exception:
            pass

    return JobPost(
        id=str(row.get("id", row.get("job_url", ""))),
        title=str(row.get("title", "")),
        company_name=str(row.get("company", "")) or None,
        job_url=str(row.get("job_url", "")),
        job_url_direct=str(row.get("job_url_direct", "")) or None,
        location=loc,
        description=str(row.get("description", "")) or None,
        compensation=comp,
        date_posted=row.get("date_posted") if row.get("date_posted") else None,
        is_remote=bool(row.get("is_remote", False)),
        job_level=str(row.get("job_level", "")) or None,
    )


def run_scraper(site, term, location, country, results_wanted, hours_old):
    """Run one scraper with a hard 90s timeout. Returns DataFrame or None."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(scrape_jobs,
            site_name=site,
            search_term=term,
            location=location,
            country_indeed=country,
            results_wanted=results_wanted,
            hours_old=hours_old,
            description_format="markdown",
            linkedin_fetch_description=False,
        )
        try:
            return f.result(timeout=90)
        except FutureTimeout:
            return None
        except Exception as e:
            print(f"    Error: {e}")
            return None


def main():
    config = load_config()
    api_key = config["deepseek"]["api_key"]
    resumes_dir = config["paths"]["resumes_dir"]
    output_dir = config["paths"]["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    sources = [(name, SITE_MAP[name]) for name in config["sources"] if name in SITE_MAP]
    terms = config["search_terms"]
    filters = config["filters"]
    location = config["location"]
    min_score = filters.get("min_relevance_score", 40)
    salary_floor = filters.get("salary_min_aud", 0)
    hours_old = filters.get("hours_old", 48)
    results_per_term = filters.get("results_per_term", 25)

    # Load resumes once
    resumes = load_resumes(resumes_dir)
    print(f"Loaded {len(resumes)} resume(s): {list(resumes.keys())}")

    # Open Google Sheet once — reuse connection throughout
    print("Connecting to Google Sheet...")
    sh, ws_raw, ws_ai, ws_match = open_sheet()

    # Load existing URLs to avoid duplicates
    seen_raw   = set(r[10] for r in ws_raw.get_all_values()[1:]   if len(r) > 10)
    seen_ai    = set(r[13] for r in ws_ai.get_all_values()[1:]    if len(r) > 13)
    seen_match = set(r[9]  for r in ws_match.get_all_values()[1:] if len(r) > 9)
    print(f"Existing rows — Raw: {len(seen_raw)}, AI: {len(seen_ai)}, Match: {len(seen_match)}")

    # Counters
    counts = {name: 0 for name, _ in sources}
    total_raw = 0
    total_filtered = 0
    total_visa = 0
    top_matches = []
    all_seen_urls: set[str] = set()

    print(f"\nStarting: {len(terms)} terms × {len(sources)} sources\n")

    for term in terms:
        for source_name, site in sources:
            print(f"  [{source_name}] '{term}'...", end=" ", flush=True)
            df = run_scraper(site, term, location, "australia", results_per_term, hours_old)

            if df is None or df.empty:
                print("0 results or timed out")
                continue

            new_jobs = 0
            for _, row in df.iterrows():
                url = str(row.get("job_url", ""))
                if not url or url in all_seen_urls:
                    continue
                all_seen_urls.add(url)

                job = build_job(row, source_name)

                # Salary pre-filter
                if salary_floor and job.compensation and job.compensation.max_amount:
                    if job.compensation.max_amount < salary_floor:
                        continue

                # ── Step 1: Write raw job to Tab 1 immediately ──
                append_raw(ws_raw, job, source_name, seen_raw)
                total_raw += 1
                new_jobs += 1
                counts[source_name] = counts.get(source_name, 0) + 1

                # ── Step 2: AI enrich this job ──
                passes = enrich_single(job, api_key)

                if not passes:
                    continue

                # ── Step 3: Write to Tab 2 immediately ──
                append_analyzed(ws_ai, job, source_name, seen_ai)
                total_filtered += 1
                if job.visa_sponsorship == "yes":
                    total_visa += 1

                # ── Step 4: Match resume ──
                match_single(job, resumes, api_key)

                # ── Step 5: Write to Tab 3 immediately ──
                append_match(ws_match, job, seen_match)

                # Track top matches
                top_matches.append({
                    "title": job.title,
                    "company": job.company_name,
                    "location": job.location.display_location() if job.location else "",
                    "score": job.ai_relevance_score or 0,
                    "visa": job.visa_sponsorship,
                    "url": job.job_url,
                })

            print(f"{new_jobs} new jobs")

        time.sleep(2)  # brief pause between search terms

    # Sort top matches by score
    top_matches = sorted(top_matches, key=lambda x: x["score"], reverse=True)[:3]

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sh.id}"

    summary = {
        "date": date.today().isoformat(),
        "total_scraped": total_raw,
        "by_source": counts,
        "passed_filter": total_filtered,
        "visa_sponsorship_count": total_visa,
        "sheet_url": sheet_url,
        "top_matches": top_matches,
    }

    with open(os.path.join(output_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    # Print summary
    src_str = " | ".join(f"{k.capitalize()}: {v}" for k, v in counts.items())
    print(f"\n{'='*52}")
    print(f"Job Hunt — {summary['date']}")
    print(f"{src_str} → {total_raw} total")
    print(f"Passed AI filter: {total_filtered} jobs")
    print(f"Visa sponsorship: {total_visa} jobs")
    if top_matches:
        print("\nTop matches:")
        for i, m in enumerate(top_matches, 1):
            print(f"  {i}. {m['title']} @ {m['company']} ({m['location']}) — Score {m['score']}, Visa: {m['visa']}")
    print(f"\nSheet: {sheet_url}")
    print("="*52)


if __name__ == "__main__":
    main()
