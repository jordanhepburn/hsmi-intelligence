"""
HSMI Weekly Performance Report
================================

Runs every Monday. Fetches reservation data from Cloudbeds for the previous
Mon–Sun week, computes ADR, total revenue, and occupancy % by room type,
optionally compares with the same week last year, and posts a summary to
Slack #growth.

Environment variables:
  CLOUDBEDS_API_KEY     — Cloudbeds x-api-key credential (required)
  CLOUDBEDS_PROPERTY_ID — Cloudbeds property ID (required)
  SLACK_WEBHOOK_URL     — Slack #growth incoming webhook (optional)

Usage:
  python reports/weekly_report.py
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")

import requests

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or from reports/ directory
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), os.path.join(_REPO_ROOT, "pricing_engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError  # noqa: E402
from config import ROOM_TYPE_ID_MAP  # noqa: E402

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
# Date helpers
# ---------------------------------------------------------------------------

def _prev_week(ref: date) -> tuple[date, date]:
    """Return (monday, sunday) for the calendar week before ref's week."""
    current_week_monday = ref - timedelta(days=ref.weekday())
    last_monday = current_week_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _same_week_last_year(first: date, last: date) -> tuple[date, date]:
    return first.replace(year=first.year - 1), last.replace(year=last.year - 1)


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
# Core report logic
# ---------------------------------------------------------------------------

class WeeklyReport:
    """Fetches and reports weekly performance metrics from Cloudbeds."""

    def __init__(self) -> None:
        api_key = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
        property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
        missing = [v for v, k in [("CLOUDBEDS_API_KEY", api_key), ("CLOUDBEDS_PROPERTY_ID", property_id)] if not k]
        if missing:
            logger.critical("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        self.client = CloudbedsClient(api_key=api_key, property_id=property_id)

        slack_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if not slack_url:
            logger.warning("SLACK_WEBHOOK_URL not set — report will be logged only")
        self.slack_webhook = slack_url

        self._id_to_code: dict[str, str] = {v["id"]: k for k, v in ROOM_TYPE_ID_MAP.items()}
        self._code_to_total: dict[str, int] = {k: v["total_rooms"] for k, v in ROOM_TYPE_ID_MAP.items()}

    def run(self) -> None:
        today = datetime.now(_MELB).date()
        first, last = _prev_week(today)
        week_label = f"{first.strftime('%-d %b')} – {last.strftime('%-d %b %Y')}"
        logger.info("=== HSMI Weekly Report — %s ===", week_label)

        current = self._compute_metrics(first, last)
        if current is None:
            logger.error("No data for %s — aborting", week_label)
            sys.exit(1)

        py_first, py_last = _same_week_last_year(first, last)
        try:
            prior = self._compute_metrics(py_first, py_last)
        except Exception as exc:
            logger.warning("Could not fetch prior year data: %s", exc)
            prior = None

        message = self._format_slack_message(week_label, first, current, prior, py_first)
        self._post_to_slack(message)
        logger.info("=== Weekly Report complete ===")

    # ------------------------------------------------------------------
    # Metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(self, first: date, last: date) -> Optional[dict]:
        checkout_to = last + timedelta(days=1)
        logger.info("Fetching reservations %s → %s", first, last)

        try:
            reservations = self.client.get_reservations(first, checkout_to)
        except CloudbedsAPIError as exc:
            logger.error("Failed to fetch reservations: %s", exc)
            return None

        days = (last - first).days + 1
        nights_sold: dict[str, int] = {code: 0 for code in ROOM_TYPE_ID_MAP}
        revenue: dict[str, float] = {code: 0.0 for code in ROOM_TYPE_ID_MAP}

        for res in reservations:
            code = self._id_to_code.get(res.get("roomTypeID", ""))
            if not code:
                continue

            try:
                res_start = date.fromisoformat(res["startDate"])
                res_end = date.fromisoformat(res["endDate"])
            except (KeyError, ValueError):
                continue

            night = max(res_start, first)
            while night < res_end and night <= last:
                nights_sold[code] += 1
                night += timedelta(days=1)

            rev = res.get("_revenue")
            if rev is None:
                try:
                    detail_resp = self.client._get(
                        "getReservation",
                        params={"reservationID": res["reservationID"]},
                    )
                    detail = detail_resp.get("data", detail_resp)
                    rev = _extract_revenue(detail) or 0.0
                except Exception as exc:
                    logger.warning("Revenue fetch failed for %s: %s", res["reservationID"], exc)
                    rev = 0.0

            total_res_nights = max((res_end - res_start).days, 1)
            nights_in_window = max(
                (min(res_end, last + timedelta(days=1)) - max(res_start, first)).days, 0
            )
            if total_res_nights > 0:
                revenue[code] += rev * (nights_in_window / total_res_nights)

        total_rev = sum(revenue.values())
        total_sold = sum(nights_sold.values())
        adr_overall = total_rev / total_sold if total_sold else 0.0

        adr_by_code: dict[str, float] = {}
        occ_by_code: dict[str, float] = {}
        for code in ROOM_TYPE_ID_MAP:
            sold = nights_sold[code]
            adr_by_code[code] = revenue[code] / sold if sold else 0.0
            capacity = self._code_to_total[code] * days
            occ_by_code[code] = sold / capacity if capacity else 0.0

        total_capacity = sum(self._code_to_total[c] for c in ROOM_TYPE_ID_MAP) * days
        occ_overall = total_sold / total_capacity if total_capacity else 0.0

        return {
            "nights_sold": nights_sold,
            "revenue": revenue,
            "adr_by_code": adr_by_code,
            "occ_by_code": occ_by_code,
            "total_revenue": total_rev,
            "adr_overall": adr_overall,
            "occ_overall": occ_overall,
            "days": days,
        }

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _delta_pp(self, cur: float, pri: float) -> str:
        """Format an occupancy delta in percentage points."""
        if pri == 0:
            return "n/a"
        diff = (cur - pri) * 100
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff:.1f}pp"

    def _delta_pct(self, cur: float, pri: float) -> str:
        """Format a revenue/ADR delta as a percentage change."""
        if pri == 0:
            return "n/a"
        diff = (cur - pri) / pri * 100
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff:.1f}%"

    def _format_slack_message(
        self,
        week_label: str,
        first: date,
        cur: dict,
        pri: Optional[dict],
        py_first: date,
    ) -> str:
        lines = [f"*HSMI Weekly Performance — {week_label}*"]

        if pri:
            py_last = py_first + timedelta(days=6)
            py_label = f"{py_first.strftime('%-d %b')} – {py_last.strftime('%-d %b %Y')}"
            lines.append(f"_vs {py_label}_")

        lines.append("")
        lines.append("*Overall*")

        occ_str = f"{cur['occ_overall'] * 100:.1f}%"
        adr_str = f"${cur['adr_overall']:.0f}"
        rev_str = f"${cur['total_revenue']:,.0f}"

        if pri:
            occ_d = self._delta_pp(cur["occ_overall"], pri["occ_overall"])
            adr_d = self._delta_pct(cur["adr_overall"], pri["adr_overall"])
            rev_d = self._delta_pct(cur["total_revenue"], pri["total_revenue"])
            lines.append(f"  Occupancy:     {occ_str}  ({occ_d} vs LY)")
            lines.append(f"  ADR:           {adr_str}  ({adr_d} vs LY)")
            lines.append(f"  Total revenue: {rev_str}  ({rev_d} vs LY)")
        else:
            lines.append(f"  Occupancy:     {occ_str}")
            lines.append(f"  ADR:           {adr_str}")
            lines.append(f"  Total revenue: {rev_str}")

        lines.append("")
        lines.append("*By Room Type*")
        header = f"{'Type':<6}{'Nights':>7}{'Occ':>7}{'ADR':>7}{'Revenue':>10}"
        if pri:
            header += f"{'vs LY occ':>11}{'vs LY ADR':>11}"
        lines.append(f"`{header}`")

        for code in sorted(ROOM_TYPE_ID_MAP):
            sold = cur["nights_sold"][code]
            occ_pct = cur["occ_by_code"][code] * 100
            adr_val = cur["adr_by_code"][code]
            rev_val = cur["revenue"][code]
            row = f"{code:<6}{sold:>7}{occ_pct:>6.1f}%{adr_val:>6.0f} ${rev_val:>8,.0f}"
            if pri:
                occ_d = self._delta_pp(cur["occ_by_code"][code], pri["occ_by_code"][code])
                adr_d = self._delta_pct(adr_val, pri["adr_by_code"][code])
                row += f"{occ_d:>11}{adr_d:>11}"
            lines.append(f"`{row}`")

        return "\n".join(lines)

    def _post_to_slack(self, message: str) -> None:
        if not self.slack_webhook:
            logger.info("Slack not configured — printing report to stdout only")
            print(message)
            return

        try:
            response = requests.post(
                self.slack_webhook,
                json={"text": message, "username": "Ops Agent", "icon_emoji": ":bar_chart:"},
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Weekly report posted to Slack")
        except requests.RequestException as exc:
            logger.warning("Failed to post to Slack: %s — printing to stdout", exc)
            print(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    WeeklyReport().run()
