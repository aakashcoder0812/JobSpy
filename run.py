#!/usr/bin/env python3
"""
Job Hunter — Daily Pipeline
Searches LinkedIn, Indeed, and Seek for Australian AI/software jobs,
filters with DeepSeek AI, matches resumes, and writes to Google Sheets.
"""
from __future__ import annotations

import json
import os
import sys
import time
import yaml
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

from jobspy import scrape_jobs
from jobspy.model import Site, Country, JobPost
from jobspy.ai_filter import enrich_jobs
from jobspy.resume_matcher import match_resumes
from jobspy.sheets_exporter import export

CONFIG_PATH = "/data/.openclaw/workspace/job-hunter/config.yaml"

SITE_MAP = {
    "linkedin": Site.LINKEDIN,
    "indeed":   Site.INDEED,
    "seek":     Site.SEEK,
}


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def scrape_all(config: dict) -> tuple[list[JobPost], dict[str, str]]:
    """Run all scrapers across all search terms. Returns (jobs, source_map)."""
    sources = [SITE_MAP[s] for s in config["sources"] if s in SITE_MAP]
    terms = config["search_terms"]
    filters = config["filters"]
    location = config["location"]
    hours_old = filters.get("hours_old", 48)
    results_per_term = filters.get("results_per_term", 25)

    all_jobs: list[JobPost] = []
    source_map: dict[str, str] = {}  # job_url -> source name
    seen_urls: set[str] = set()
    counts: dict[str, int] = {s: 0 for s in config["sources"]}

    print(f"Searching {len(terms)} terms across {len(sources)} sources...")

    for term in terms:
        for source_name, site in zip(config["sources"], sources):
            try:
                df = scrape_jobs(
                    site_name=site,
                    search_term=term,
                    location=location,
                    country_indeed="australia",
                    results_wanted=results_per_term,
                    hours_old=hours_old,
                    description_format="markdown",
                    linkedin_fetch_description=True,
                )
                if df.empty:
                    continue

                for _, row in df.iterrows():
                    url = row.get("job_url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    source_map[url] = source_name
                    counts[source_name] = counts.get(source_name, 0) + 1

                    # Reconstruct JobPost from DataFrame row
                    from jobspy.model import Location, Compensation, CompensationInterval
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

                    job = JobPost(
                        id=str(row.get("id", url)),
                        title=str(row.get("title", "")),
                        company_name=str(row.get("company", "")) or None,
                        job_url=url,
                        job_url_direct=str(row.get("job_url_direct", "")) or None,
                        location=loc,
                        description=str(row.get("description", "")) or None,
                        compensation=comp,
                        date_posted=row.get("date_posted") if row.get("date_posted") else None,
                        is_remote=bool(row.get("is_remote", False)),
                        job_level=str(row.get("job_level", "")) or None,
                    )
                    all_jobs.append(job)

            except Exception as e:
                print(f"  [{source_name}] '{term}' failed: {e}")
                continue

        time.sleep(2)  # brief pause between search terms

    print(f"Scraped: {' | '.join(f'{k}: {v}' for k,v in counts.items())} → {len(all_jobs)} total unique")
    return all_jobs, source_map


def apply_salary_filter(jobs: list[JobPost], config: dict) -> list[JobPost]:
    """Drop jobs where max salary is known and below the minimum threshold."""
    min_salary = config["filters"].get("salary_min_aud", 0)
    if not min_salary:
        return jobs
    filtered = []
    for job in jobs:
        if job.compensation and job.compensation.max_amount:
            if job.compensation.max_amount < min_salary:
                continue
        filtered.append(job)
    return filtered


def write_summary(
    raw_jobs: list[JobPost],
    filtered_jobs: list[JobPost],
    matched_jobs: list[JobPost],
    source_map: dict[str, str],
    sheet_url: str,
    output_dir: str,
) -> dict:
    counts_by_source: dict[str, int] = {}
    for url, src in source_map.items():
        counts_by_source[src] = counts_by_source.get(src, 0) + 1

    visa_jobs = [j for j in filtered_jobs if j.visa_sponsorship == "yes"]
    top3 = sorted(filtered_jobs, key=lambda j: j.ai_relevance_score or 0, reverse=True)[:3]

    summary = {
        "date": date.today().isoformat(),
        "total_scraped": len(raw_jobs),
        "by_source": counts_by_source,
        "passed_filter": len(filtered_jobs),
        "visa_sponsorship_count": len(visa_jobs),
        "sheet_url": sheet_url,
        "top_matches": [
            {
                "title": j.title,
                "company": j.company_name,
                "location": j.location.display_location() if j.location else "",
                "score": j.ai_relevance_score,
                "visa": j.visa_sponsorship,
                "url": j.job_url,
            }
            for j in top3
        ],
    }

    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def print_summary(summary: dict) -> None:
    by_src = summary["by_source"]
    src_str = " | ".join(f"{k.capitalize()}: {v}" for k, v in by_src.items())
    print(f"\n{'='*50}")
    print(f"Job Hunt — {summary['date']}")
    print(f"{src_str} → {summary['total_scraped']} total")
    print(f"Passed filter: {summary['passed_filter']} jobs")
    print(f"Visa sponsorship: {summary['visa_sponsorship_count']} jobs")
    if summary["top_matches"]:
        print("\nTop matches:")
        for i, m in enumerate(summary["top_matches"], 1):
            print(f"  {i}. {m['title']} @ {m['company']} ({m['location']}) — Score {m['score']}, Visa: {m['visa']}")
    print(f"\nSheet: {summary['sheet_url']}")
    print('='*50)


def main():
    config = load_config()
    deepseek_key = config["deepseek"]["api_key"]
    resumes_dir = config["paths"]["resumes_dir"]
    output_dir = config["paths"]["output_dir"]

    # Step 1: Scrape
    raw_jobs, source_map = scrape_all(config)
    if not raw_jobs:
        print("No jobs found. Exiting.")
        return

    # Step 2: Salary pre-filter (cheap, no API call)
    jobs = apply_salary_filter(raw_jobs, config)
    print(f"After salary filter: {len(jobs)} jobs remain")

    # Step 3: AI enrichment + relevance filter
    print(f"Running AI enrichment on {len(jobs)} jobs (batches of 10)...")
    filtered_jobs = enrich_jobs(jobs, api_key=deepseek_key)
    print(f"After AI filter: {len(filtered_jobs)} jobs remain")

    # Step 4: Resume matching
    print("Matching resumes...")
    matched_jobs = match_resumes(filtered_jobs, resumes_dir=resumes_dir, api_key=deepseek_key)

    # Step 5: Export to Google Sheets
    print("Writing to Google Sheets...")
    sheet_url = export(
        raw_jobs=raw_jobs,
        filtered_jobs=filtered_jobs,
        matched_jobs=matched_jobs,
        source_map=source_map,
    )

    # Step 6: Write summary
    summary = write_summary(raw_jobs, filtered_jobs, matched_jobs, source_map, sheet_url, output_dir)
    print_summary(summary)


if __name__ == "__main__":
    main()
