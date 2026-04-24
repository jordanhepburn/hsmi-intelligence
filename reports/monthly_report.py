"""
HSMI Monthly Performance Report
================================

Runs on the 1st of each month. Fetches reservation data from Cloudbeds for
the previous calendar month, computes ADR, total revenue, and occupancy % by
room type, optionally compares with the same month last year, and posts a
summary to Slack #growth.

Environment variables:
  CLOUDBEDS_API_KEY    — Cloudbeds x-api-key credential (required)
  CLOUDBEDS_PROPERTY_ID — Cloudbeds property ID (required)
  SLACK_WEBHOOK_URL    — Slack #growth incoming webhook (optional)

Usage:
  python reports/monthly_report.py
"""

import calendar
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")
from typing import Optional

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
from config import ROOM_TYPE_ID_MAP, IGNORED_ROOM_TYPE_IDS  # noqa: E402

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

def _prev_month(ref: date) -> tuple[date, date]:
    """Return (first_day, last_day) for the month before ref."""
    first = ref.replace(day=1) - timedelta(days=1)
    first = first.replace(day=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first, first.replace(day=last_day)


def _same_month_last_year(first: date, last: date) -> tuple[date, date]:
    """Return the equivalent date range 12 months earlier."""
    return first.replace(year=first.year - 1), last.replace(year=last.year - 1)


# ---------------------------------------------------------------------------
# Revenue extraction
# ---------------------------------------------------------------------------

def _extract_revenue(detail: dict) -> Optional[float]:
    """
    Pull the room revenue figure from a getReservation detail response.

    Cloudbeds does not expose a guaranteed field name; tries common candidates
    in order of specificity.
    """
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

class MonthlyReport:
    """Fetches and reports monthly performance metrics from Cloudbeds."""

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

        # Build reverse maps
        self._id_to_code: dict[str, str] = {v["id"]: k for k, v in ROOM_TYPE_ID_MAP.items()}
        self._code_to_name: dict[str, str] = {k: v["name"] for k, v in ROOM_TYPE_ID_MAP.items()}
        self._code_to_total: dict[str, int] = {k: v["total_rooms"] for k, v in ROOM_TYPE_ID_MAP.items()}

    def run(self) -> None:
        today = datetime.now(_MELB).date()
        first, last = _prev_month(today)
        month_label = first.strftime("%B %Y")
        logger.info("=== HSMI Monthly Report — %s ===", month_label)

        current_metrics = self._compute_metrics(first, last)
        if current_metrics is None:
            logger.error("No data returned for %s — aborting", month_label)
            sys.exit(1)

        # Try same month last year
        py_first, py_last = _same_month_last_year(first, last)
        try:
            prior_metrics = self._compute_metrics(py_first, py_last)
        except Exception as exc:
            logger.warning("Could not fetch prior year data (%s): %s", py_first.strftime("%B %Y"), exc)
            prior_metrics = None

        self._log_metrics(month_label, current_metrics)
        message = self._format_slack_message(month_label, current_metrics, prior_metrics, py_first)
        self._post_to_slack(message)
        logger.info("=== Monthly Report complete ===")

    # ------------------------------------------------------------------
    # Metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(self, first: date, last: date) -> Optional[dict]:
        """
        Pull reservations for [first, last] and compute ADR, revenue, occupancy.

        Returns a dict with:
          "nights_sold"   : {code: int}        — occupied room-nights
          "revenue"       : {code: float}       — total room revenue
          "total_revenue" : float
          "adr_overall"   : float
          "occupancy_pct" : {code: float}       — 0.0–1.0
          "days_in_month" : int
        """
        # Use checkOutTo = last + 1 day so checkouts on the last day are included
        checkout_to = last + timedelta(days=1)
        logger.info("Fetching reservations %s → %s", first, last)

        try:
            reservations = self.client.get_reservations(first, checkout_to)
        except CloudbedsAPIError as exc:
            logger.error("Failed to fetch reservations: %s", exc)
            return None

        days_in_month = (last - first).days + 1

        nights_sold: dict[str, int] = {code: 0 for code in ROOM_TYPE_ID_MAP}
        revenue: dict[str, float]   = {code: 0.0 for code in ROOM_TYPE_ID_MAP}

        for res in reservations:
            code = self._id_to_code.get(res.get("roomTypeID", ""))
            if not code:
                continue

            # Count room-nights that fall within the report month
            try:
                res_start = date.fromisoformat(res["startDate"])
                res_end   = date.fromisoformat(res["endDate"])
            except (KeyError, ValueError):
                continue

            night = max(res_start, first)
            while night < res_end and night <= last:
                nights_sold[code] += 1
                night += timedelta(days=1)

            # Revenue from detail — fetch if not already enriched
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
                    logger.warning("Revenue fetch failed for reservation %s: %s", res["reservationID"], exc)
                    rev = 0.0

            # Apportion revenue by room-nights within the month if reservation
            # spans multiple months (rough but fair)
            total_res_nights = max((res_end - res_start).days, 1)
            nights_in_month = max(
                (min(res_end, last + timedelta(days=1)) - max(res_start, first)).days,
                0,
            )
            if total_res_nights > 0:
                revenue[code] += rev * (nights_in_month / total_res_nights)

        total_rev  = sum(revenue.values())
        total_sold = sum(nights_sold.values())
        adr_overall = total_rev / total_sold if total_sold else 0.0

        # Per-code ADR and occupancy
        adr_by_code: dict[str, float] = {}
        occ_by_code: dict[str, float] = {}
        for code in ROOM_TYPE_ID_MAP:
            sold = nights_sold[code]
            rev_code = revenue[code]
            adr_by_code[code] = rev_code / sold if sold else 0.0
            capacity = self._code_to_total[code] * days_in_month
            occ_by_code[code] = sold / capacity if capacity else 0.0

        total_capacity = sum(self._code_to_total[c] for c in ROOM_TYPE_ID_MAP) * days_in_month
        occ_overall = total_sold / total_capacity if total_capacity else 0.0

        return {
            "nights_sold":   nights_sold,
            "revenue":       revenue,
            "adr_by_code":   adr_by_code,
            "occ_by_code":   occ_by_code,
            "total_revenue": total_rev,
            "adr_overall":   adr_overall,
            "occ_overall":   occ_overall,
            "days_in_month": days_in_month,
        }

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _log_metrics(self, month_label: str, m: dict) -> None:
        logger.info("--- %s Results ---", month_label)
        logger.info("  Overall occupancy: %.1f%%", m["occ_overall"] * 100)
        logger.info("  Overall ADR:       $%.2f", m["adr_overall"])
        logger.info("  Total revenue:     $%.2f", m["total_revenue"])
        logger.info("  By room type:")
        for code in sorted(ROOM_TYPE_ID_MAP):
            logger.info(
                "    %-4s  nights=%d  occ=%.1f%%  ADR=$%.0f  rev=$%.0f",
                code,
                m["nights_sold"][code],
                m["occ_by_code"][code] * 100,
                m["adr_by_code"][code],
                m["revenue"][code],
            )

    def _delta(self, current: float, prior: float, pct: bool = False) -> str:
        """Format a delta with sign and direction arrow."""
        if prior == 0:
            return "n/a"
        diff = current - prior
        sign = "+" if diff >= 0 else ""
        if pct:
            return f"{sign}{diff * 100:.1f}pp"
        return f"{sign}{diff:.1f}%"

    def _format_slack_message(
        self,
        month_label: str,
        cur: dict,
        pri: Optional[dict],
        py_first: date,
    ) -> str:
        lines = [f"*HSMI Monthly Performance — {month_label}*"]

        if pri:
            py_label = py_first.strftime("%B %Y")
            lines.append(f"_vs {py_label}_")

        lines.append("")
        lines.append("*Overall*")

        occ_str = f"{cur['occ_overall'] * 100:.1f}%"
        adr_str = f"${cur['adr_overall']:.0f}"
        rev_str = f"${cur['total_revenue']:,.0f}"

        if pri:
            occ_d = self._delta(cur["occ_overall"], pri["occ_overall"], pct=True)
            adr_d = self._delta(cur["adr_overall"], pri["adr_overall"])
            rev_d = self._delta(cur["total_revenue"], pri["total_revenue"])
            lines.append(f"  Occupancy:     {occ_str}  ({occ_d} vs PY)")
            lines.append(f"  ADR:           {adr_str}  ({adr_d} vs PY)")
            lines.append(f"  Total revenue: {rev_str}  ({rev_d} vs PY)")
        else:
            lines.append(f"  Occupancy:     {occ_str}")
            lines.append(f"  ADR:           {adr_str}")
            lines.append(f"  Total revenue: {rev_str}")

        lines.append("")
        lines.append("*By Room Type*")
        header = f"{'Type':<6}{'Occ':>7}{'ADR':>8}{'Revenue':>12}"
        if pri:
            header += f"{'vs PY occ':>12}{'vs PY ADR':>12}"
        lines.append(f"`{header}`")

        for code in sorted(ROOM_TYPE_ID_MAP):
            occ_pct = cur["occ_by_code"][code] * 100
            adr_val = cur["adr_by_code"][code]
            rev_val = cur["revenue"][code]
            row = f"{code:<6}{occ_pct:>6.1f}%{adr_val:>7.0f} ${rev_val:>10,.0f}"
            if pri:
                occ_d = self._delta(cur["occ_by_code"][code], pri["occ_by_code"][code], pct=True)
                adr_d = self._delta(adr_val, pri["adr_by_code"][code])
                row += f"{occ_d:>12}{adr_d:>12}"
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
            logger.info("Monthly report posted to Slack")
        except requests.RequestException as exc:
            logger.warning("Failed to post to Slack: %s — printing to stdout", exc)
            print(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    MonthlyReport().run()
