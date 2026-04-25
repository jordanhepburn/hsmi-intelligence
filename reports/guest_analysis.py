"""
HSMI Guest Repeat & Churn Analysis
====================================

Looks back over the previous 12 months of reservations, identifies unique
guests by name, counts repeat stays, flags churned guests (2+ stays with
last visit > 6 months ago), and posts a summary to Slack #growth.

Runs monthly on the 1st at 8am AEST, or on demand via workflow_dispatch.

Environment variables:
  CLOUDBEDS_API_KEY     — Cloudbeds x-api-key credential (required)
  CLOUDBEDS_PROPERTY_ID — Cloudbeds property ID (required)
  SLACK_WEBHOOK_URL     — Slack #growth incoming webhook (optional)
  LOOKBACK_MONTHS       — integer lookback window, default 12 (optional)
  CHURN_MONTHS          — months since last stay to consider churned, default 6 (optional)

Usage:
  python reports/guest_analysis.py
"""

import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")

import requests
from dateutil.relativedelta import relativedelta

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or from reports/ directory
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), os.path.join(_REPO_ROOT, "pricing_engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError, CANCELLED_STATUSES  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def _normalize_name(raw: str) -> str:
    """Lowercase + collapse whitespace — used as deduplication key."""
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def _display_name(raw: str) -> str:
    return " ".join(w.capitalize() for w in (raw or "").split())


# ---------------------------------------------------------------------------
# Revenue extraction
# ---------------------------------------------------------------------------

def _extract_revenue(detail: dict) -> Optional[float]:
    for key in ("roomTotal", "roomRevenue", "subTotal", "total", "grandTotal"):
        val = detail.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Core analysis logic
# ---------------------------------------------------------------------------

class GuestAnalysis:
    """Identifies repeat and churned guests from Cloudbeds reservation history."""

    def __init__(self) -> None:
        api_key = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
        property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
        missing = [v for v, k in [("CLOUDBEDS_API_KEY", api_key), ("CLOUDBEDS_PROPERTY_ID", property_id)] if not k]
        if missing:
            logger.critical("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        self.client = CloudbedsClient(api_key=api_key, property_id=property_id)
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        self.lookback_months = int(os.environ.get("LOOKBACK_MONTHS", "12"))
        self.churn_months = int(os.environ.get("CHURN_MONTHS", "6"))

        if not self.slack_webhook:
            logger.warning("SLACK_WEBHOOK_URL not set — report will be logged only")

    def run(self) -> None:
        today = datetime.now(_MELB).date()
        window_start = today - relativedelta(months=self.lookback_months)
        logger.info("=== HSMI Guest Analysis — %s to %s ===", window_start, today)

        guests = self._fetch_guest_data(window_start, today)
        if not guests:
            logger.warning("No guest data found — aborting")
            sys.exit(1)

        stats = self._compute_stats(guests, today)
        message = self._format_slack_message(stats, today, window_start)
        self._post_to_slack(message)
        logger.info("=== Guest Analysis complete ===")

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _get_reservation_ids(self, check_out_from: date, check_out_to: date) -> dict[str, dict]:
        """
        Fetch paginated reservation summaries and return unique active reservation IDs.

        Returns {reservationID: {"startDate": str, "endDate": str}}.
        """
        result: dict[str, dict] = {}
        page = 1
        while True:
            params = {
                "checkOutFrom": check_out_from.strftime("%Y-%m-%d"),
                "checkOutTo": check_out_to.strftime("%Y-%m-%d"),
                "pageNum": page,
            }
            response = self.client._get("getReservations", params=params)
            data = response.get("data", [])
            items = data if isinstance(data, list) else data.get("reservations", [])
            if not items:
                break

            for res in items:
                status = (
                    res.get("status") or res.get("reservationStatus") or ""
                ).lower().replace(" ", "_")
                if status in CANCELLED_STATUSES:
                    continue
                res_id = str(res.get("reservationID") or "")
                if res_id and res_id not in result:
                    result[res_id] = {
                        "startDate": str(res.get("startDate") or ""),
                        "endDate": str(res.get("endDate") or ""),
                    }

            count = int(response.get("count") or len(items))
            total = int(response.get("total") or 0)
            if not items or (total and page * count >= total):
                break
            page += 1

        return result

    def _fetch_guest_data(self, window_start: date, window_end: date) -> dict[str, dict]:
        """
        Fetch all reservations in the window and group by normalised guest name.

        Returns {norm_name: {"display_name": str, "stays": [{"id", "check_in", "revenue"}]}}.
        """
        res_ids = self._get_reservation_ids(window_start, window_end + timedelta(days=1))
        logger.info("Found %d unique reservations — fetching guest details", len(res_ids))

        guests: dict[str, dict] = {}

        for i, (res_id, meta) in enumerate(res_ids.items()):
            if i > 0 and i % 50 == 0:
                logger.info("Processing reservation %d / %d", i + 1, len(res_ids))
            try:
                detail_resp = self.client._get("getReservation", params={"reservationID": res_id})
                detail = detail_resp.get("data", detail_resp)
            except CloudbedsAPIError as exc:
                logger.warning("Skipping reservation %s: %s", res_id, exc)
                continue

            raw_name = (
                detail.get("guestName")
                or detail.get("fullName")
                or detail.get("guestFullName")
                or ""
            )
            if not raw_name:
                first_n = detail.get("firstName") or detail.get("guestFirstName") or ""
                last_n = detail.get("lastName") or detail.get("guestLastName") or ""
                raw_name = f"{first_n} {last_n}".strip()

            norm = _normalize_name(raw_name)
            if not norm:
                logger.debug("No guest name for reservation %s — skipping", res_id)
                continue

            try:
                check_in = date.fromisoformat(meta["startDate"])
            except (KeyError, ValueError):
                check_in = window_start

            revenue = _extract_revenue(detail) or 0.0

            if norm not in guests:
                guests[norm] = {"display_name": _display_name(raw_name), "stays": []}

            guests[norm]["stays"].append({
                "id": res_id,
                "check_in": check_in,
                "revenue": revenue,
            })

        logger.info("Identified %d unique guests from %d reservations", len(guests), len(res_ids))
        return guests

    # ------------------------------------------------------------------
    # Stats computation
    # ------------------------------------------------------------------

    def _compute_stats(self, guests: dict, today: date) -> dict:
        churn_threshold = today - relativedelta(months=self.churn_months)
        new_threshold = today - timedelta(days=30)

        total_unique = len(guests)
        repeat_guests: list[dict] = []
        churned: list[dict] = []
        new_this_month: list[str] = []
        total_revenue = 0.0
        repeat_revenue = 0.0

        for norm, g in guests.items():
            stays = sorted(g["stays"], key=lambda s: s["check_in"])
            n_stays = len(stays)
            first_stay = stays[0]["check_in"]
            last_stay = stays[-1]["check_in"]
            rev = sum(s["revenue"] for s in stays)
            total_revenue += rev

            if n_stays >= 2:
                repeat_guests.append({
                    "name": g["display_name"],
                    "stays": n_stays,
                    "last_stay": last_stay,
                    "first_stay": first_stay,
                    "revenue": rev,
                })
                repeat_revenue += rev
                if last_stay < churn_threshold:
                    churned.append({
                        "name": g["display_name"],
                        "stays": n_stays,
                        "last_stay": last_stay,
                    })

            if first_stay >= new_threshold and n_stays == 1:
                new_this_month.append(g["display_name"])

        repeat_guests.sort(key=lambda g: (-g["stays"], -g["revenue"]))
        churned.sort(key=lambda g: g["last_stay"])

        return {
            "total_unique": total_unique,
            "repeat_guests": repeat_guests,
            "churned": churned,
            "new_this_month": new_this_month,
            "total_revenue": total_revenue,
            "repeat_revenue": repeat_revenue,
        }

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_slack_message(self, stats: dict, today: date, window_start: date) -> str:
        repeat = stats["repeat_guests"]
        churned = stats["churned"]
        new_guests = stats["new_this_month"]
        total_unique = stats["total_unique"]
        total_rev = stats["total_revenue"]
        repeat_rev = stats["repeat_revenue"]

        repeat_rate = len(repeat) / total_unique * 100 if total_unique else 0.0
        rev_share = repeat_rev / total_rev * 100 if total_rev else 0.0
        avg_stays = sum(g["stays"] for g in repeat) / len(repeat) if repeat else 0.0

        lines = [
            f"*HSMI Guest Repeat & Churn — {window_start.strftime('%-d %b %Y')} to {today.strftime('%-d %b %Y')}*",
            "",
            "*Summary*",
            f"  Unique guests:       {total_unique}",
            f"  Repeat guests:       {len(repeat)}  ({repeat_rate:.1f}%)",
            f"  Avg stays (repeats): {avg_stays:.1f}",
            f"  Revenue — repeats:   ${repeat_rev:,.0f}  ({rev_share:.1f}% of total)",
            f"  Revenue — new:       ${total_rev - repeat_rev:,.0f}",
        ]

        if repeat:
            top_n = min(10, len(repeat))
            lines.append("")
            lines.append(f"*Top {top_n} Returning Guests*")
            header = f"{'Guest':<28}{'Stays':>6}{'Last stay':>14}{'Revenue':>11}"
            lines.append(f"`{header}`")
            for g in repeat[:top_n]:
                name = g["name"][:27]
                last = g["last_stay"].strftime("%-d %b %Y")
                row = f"{name:<28}{g['stays']:>6}{last:>14}  ${g['revenue']:>8,.0f}"
                lines.append(f"`{row}`")

        if churned:
            lines.append("")
            lines.append(
                f"*Churned Guests* _(2+ stays, last visit >{self.churn_months} months ago)_"
            )
            lines.append(f"  Count: {len(churned)}")
            oldest = ", ".join(g["name"] for g in churned[:5])
            lines.append(f"  Longest absent: {oldest}")

        if new_guests:
            lines.append("")
            lines.append(f"*New Guests — last 30 days*  {len(new_guests)} first-time visitors")

        return "\n".join(lines)

    def _post_to_slack(self, message: str) -> None:
        if not self.slack_webhook:
            logger.info("Slack not configured — printing report to stdout only")
            print(message)
            return

        try:
            response = requests.post(
                self.slack_webhook,
                json={"text": message, "username": "Ops Agent", "icon_emoji": ":busts_in_silhouette:"},
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Guest analysis posted to Slack")
        except requests.RequestException as exc:
            logger.warning("Failed to post to Slack: %s — printing to stdout", exc)
            print(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    GuestAnalysis().run()
