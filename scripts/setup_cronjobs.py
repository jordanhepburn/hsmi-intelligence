"""
HSMI Cron-job.org Setup
=======================
Creates or updates all HSMI scheduled jobs in cron-job.org via their REST API.
Idempotent — safe to run repeatedly.

Existing jobs are matched by title. If a job with the same title exists it is
updated (PATCH); otherwise it is created (PUT).

The pricing engine job (ID 7516426) is always updated by ID, never duplicated.

Environment variables:
  CRONJOB_API_KEY — cron-job.org API key (required)
  CRON_SECRET     — shared secret sent in x-cron-secret header (required)

Usage:
  python scripts/setup_cronjobs.py
"""

import logging
import os
import sys

import requests

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE    = "https://api.cron-job.org"
RAILWAY_URL = "https://web-production-687b3.up.railway.app"

# Existing pricing engine job — always update by ID, never create a duplicate.
PRICING_ENGINE_JOB_ID = 7516426

# ---------------------------------------------------------------------------
# Job definitions
# All times are UTC. AEST = UTC+10 (no DST in April–October; AEDT = UTC+11).
# ---------------------------------------------------------------------------

def _make_job(title: str, path: str, hours: list[int], minutes: list[int], cron_secret: str) -> dict:
    return {
        "url":           f"{RAILWAY_URL}{path}",
        "title":         title,
        "enabled":       True,
        "saveResponses": True,
        "requestMethod": 1,  # POST
        "schedule": {
            "timezone": "UTC",
            "hours":    hours,
            "minutes":  minutes,
            "mdays":    [-1],   # every day of month
            "months":   [-1],   # every month
            "wdays":    [-1],   # every weekday
        },
        "extendedData": {
            "headers": [{"key": "x-cron-secret", "value": cron_secret}],
        },
    }


def build_jobs(cron_secret: str) -> list[dict]:
    return [
        {
            "id":  PRICING_ENGINE_JOB_ID,   # update existing, never duplicate
            **_make_job(
                title="HSMI Pricing Engine",
                path="/cron/pricing-engine",
                # 7pm–7am AEST = 9am–9pm UTC (covers 7am–7pm Melbourne time)
                hours=list(range(21, 24)) + list(range(0, 10)),
                minutes=[0],
                cron_secret=cron_secret,
            ),
        },
        _make_job(
            title="HSMI HK Report",
            path="/cron/housekeeping-report",
            hours=[21],   # 21:00 UTC = 7:00 AEST
            minutes=[0],
            cron_secret=cron_secret,
        ),
        _make_job(
            title="HSMI HK Roster",
            path="/cron/housekeeping-roster",
            hours=[22],   # 22:00 UTC = 8:00 AEST
            minutes=[0],
            cron_secret=cron_secret,
        ),
        _make_job(
            title="HSMI Competitor Signal",
            path="/cron/competitor-signal",
            hours=[23],   # 23:00 UTC = 9:00 AEST
            minutes=[0],
            cron_secret=cron_secret,
        ),
    ]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }


def _list_jobs(api_key: str) -> list[dict]:
    resp = requests.get(f"{API_BASE}/jobs", headers=_headers(api_key), timeout=15)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def _update_job(api_key: str, job_id: int, job: dict) -> None:
    resp = requests.patch(
        f"{API_BASE}/job/{job_id}",
        headers=_headers(api_key),
        json={"job": job},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info("Updated job %d: %s", job_id, job["title"])


def _create_job(api_key: str, job: dict) -> int:
    resp = requests.put(
        f"{API_BASE}/job",
        headers=_headers(api_key),
        json={"job": job},
        timeout=15,
    )
    resp.raise_for_status()
    job_id = resp.json().get("jobId")
    logger.info("Created job %s → ID %s", job["title"], job_id)
    return job_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key     = os.environ.get("CRONJOB_API_KEY", "").strip()
    cron_secret = os.environ.get("CRON_SECRET", "").strip()

    missing = [v for v, k in [("CRONJOB_API_KEY", api_key), ("CRON_SECRET", cron_secret)] if not k]
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    jobs = build_jobs(cron_secret)

    logger.info("Fetching existing cron-job.org jobs…")
    existing = _list_jobs(api_key)
    existing_by_title = {j["title"]: j["jobId"] for j in existing}
    logger.info("Found %d existing jobs", len(existing))

    for job in jobs:
        job_id   = job.pop("id", None)          # explicit ID wins
        title    = job["title"]

        if job_id is None:
            job_id = existing_by_title.get(title)  # match by title

        if job_id is not None:
            _update_job(api_key, job_id, job)
        else:
            _create_job(api_key, job)

    logger.info("=== setup_cronjobs complete ===")


if __name__ == "__main__":
    main()
