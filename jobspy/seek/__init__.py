from __future__ import annotations

import random
import time
import json
import re
from datetime import datetime, date
from typing import Optional

import requests
from requests import Session

from jobspy.model import (
    Scraper,
    ScraperInput,
    JobPost,
    JobResponse,
    Location,
    Compensation,
    CompensationInterval,
    JobType,
    Country,
    Site,
)

SEEK_SEARCH_URL = "https://www.seek.com.au/api/jobsearch/v5/search"
SEEK_GRAPHQL_URL = "https://www.seek.com.au/graphql"

SEARCH_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-AU,en;q=0.9",
    "referer": "https://www.seek.com.au/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
}

GQL_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-AU,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://www.seek.com.au",
    "referer": "https://www.seek.com.au/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
}

JOB_DETAILS_QUERY = """
query JobDetails($jobId: ID!, $locale: Locale!) {
  jobDetails(id: $jobId, tracking: { channel: "WEB" }) {
    job {
      id
      title
      description(format: HTML)
      salary {
        currencyLabel(locale: $locale)
        label
      }
      advertiser {
        id
        name
      }
      location {
        label(locale: $locale, type: SUBURB_AND_REGION)
      }
      workTypes {
        label(locale: $locale)
      }
    }
  }
}
"""

WORK_TYPE_MAP = {
    "Full time": JobType.FULL_TIME,
    "Part time": JobType.PART_TIME,
    "Contract/Temp": JobType.CONTRACT,
    "Casual/Vacation": JobType.PART_TIME,
}


class Seek(Scraper):
    def __init__(self, proxies=None, ca_cert=None, user_agent=None):
        super().__init__(Site.SEEK, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.session = Session()
        self.session.headers.update(SEARCH_HEADERS)

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        search_term = scraper_input.search_term or ""
        results_wanted = scraper_input.results_wanted or 25
        hours_old = scraper_input.hours_old

        page = 1
        while len(jobs) < results_wanted:
            params = {
                "siteKey": "AU-Main",
                "sourcesystem": "houston",
                "where": "All Australia",
                "keywords": search_term,
                "page": page,
                "seekSelectAllPages": "true",
                "locale": "en-AU",
                "pageSize": 25,
            }

            try:
                resp = self.session.get(
                    SEEK_SEARCH_URL, params=params, timeout=scraper_input.request_timeout
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                break

            listings = data.get("data", [])
            if not listings:
                break

            for item in listings:
                job_id = str(item.get("id", ""))
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Parse posted date
                posted_date = None
                listing_date_str = item.get("listingDate") or item.get("bulletPoints", [None])[0]
                listing_date_raw = item.get("listingDate", "")
                if listing_date_raw:
                    try:
                        posted_date = datetime.fromisoformat(
                            listing_date_raw.replace("Z", "+00:00")
                        ).date()
                    except Exception:
                        pass

                # Filter by hours_old
                if hours_old and posted_date:
                    age_hours = (date.today() - posted_date).total_seconds() / 3600
                    if age_hours > hours_old:
                        continue

                # Parse location
                location_str = item.get("suburb") or item.get("area") or ""
                location_parts = [p.strip() for p in location_str.split(",") if p.strip()]
                city = location_parts[0] if location_parts else None
                state = location_parts[1] if len(location_parts) > 1 else None
                location = Location(country=Country.AUSTRALIA, city=city, state=state)

                # Parse salary
                salary_label = item.get("salary", "")
                compensation = _parse_salary(salary_label)

                # Parse work type
                work_type_str = item.get("workType", "")
                job_type = [WORK_TYPE_MAP[work_type_str]] if work_type_str in WORK_TYPE_MAP else None

                # Is remote
                is_remote = "remote" in location_str.lower() or work_type_str.lower() == "remote"

                job_url = f"https://www.seek.com.au/job/{job_id}"

                job = JobPost(
                    id=job_id,
                    title=item.get("title", ""),
                    company_name=item.get("advertiser", {}).get("description")
                    or item.get("companyName"),
                    job_url=job_url,
                    location=location,
                    compensation=compensation,
                    job_type=job_type,
                    date_posted=posted_date,
                    is_remote=is_remote,
                    description=None,  # fetched below
                )
                jobs.append(job)

                if len(jobs) >= results_wanted:
                    break

            if len(listings) < 25:
                break

            page += 1
            time.sleep(random.uniform(2, 3))

        # Fetch descriptions via GraphQL for all collected jobs
        for job in jobs:
            desc = _fetch_description(self.session, job.id, scraper_input.request_timeout)
            if desc:
                job.description = desc
            time.sleep(random.uniform(1, 2))

        return JobResponse(jobs=jobs)


def _parse_salary(salary_str: str) -> Optional[Compensation]:
    if not salary_str:
        return None
    # Extract numbers from salary string like "$80,000 - $120,000"
    numbers = re.findall(r"[\d,]+", salary_str.replace(" ", ""))
    amounts = []
    for n in numbers:
        try:
            amounts.append(float(n.replace(",", "")))
        except ValueError:
            pass
    if not amounts:
        return None
    interval = CompensationInterval.YEARLY
    if "hour" in salary_str.lower():
        interval = CompensationInterval.HOURLY
    elif "day" in salary_str.lower():
        interval = CompensationInterval.DAILY
    elif "month" in salary_str.lower():
        interval = CompensationInterval.MONTHLY
    return Compensation(
        interval=interval,
        min_amount=min(amounts),
        max_amount=max(amounts) if len(amounts) > 1 else min(amounts),
        currency="AUD",
    )


def _fetch_description(session: Session, job_id: str, timeout: int) -> Optional[str]:
    payload = {
        "operationName": "JobDetails",
        "query": JOB_DETAILS_QUERY,
        "variables": {"jobId": job_id, "locale": "en-AU"},
    }
    try:
        resp = session.post(
            SEEK_GRAPHQL_URL,
            json=payload,
            headers=GQL_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        job_data = data.get("data", {}).get("jobDetails", {}).get("job", {})
        return job_data.get("description")
    except Exception:
        return None
