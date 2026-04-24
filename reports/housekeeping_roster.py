"""
HSMI 14-Day Housekeeping Roster
================================
Posts a 14-day rolling housekeeping capacity forecast to Slack #operations
at 8am AEST (10pm UTC, cron '0 22 * * *').

For each day in the next 14 days it shows:
  - TO  = turnovers (same roomID checking out AND in — must clean 10am–2pm)
  - CO  = checkouts only (no new arrival — flexible timing)
  - Flag = capacity alert based on day-of-week staffing rules

Staffing capacity (turnovers):
  Mon  6 | Tue  3 | Wed  0 (D+L off) | Thu  3 | Fri  6 | Sat  6 | Sun  6

Environment variables:
  CLOUDBEDS_API_KEY            — Cloudbeds API key (required)
  CLOUDBEDS_PROPERTY_ID        — Cloudbeds property ID (required)
  SLACK_OPERATIONS_WEBHOOK_URL — Slack #operations incoming webhook (required)
"""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), os.path.join(_REPO_ROOT, "pricing_engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError  # noqa: E402

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
# Constants
# ---------------------------------------------------------------------------
TOTAL_ROOMS = 18
LOOKAHEAD   = 14

# Max turnovers Dwayne + Leanne can handle per day (0=Mon … 6=Sun)
TURNOVER_CAPACITY = {0: 6, 1: 3, 2: 0, 3: 3, 4: 6, 5: 6, 6: 6}
SUNDAY_MIN_CLEAN  = 8

_CANCELLED = {"cancelled", "canceled", "no_show", "no-show", "noshow"}


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class HousekeepingRoster:
    """Fetches 14-day reservation data and posts a capacity forecast to Slack."""

    def __init__(self) -> None:
        api_key     = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
        property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
        missing = [v for v, k in [("CLOUDBEDS_API_KEY", api_key), ("CLOUDBEDS_PROPERTY_ID", property_id)] if not k]
        if missing:
            logger.critical("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        self.client  = CloudbedsClient(api_key=api_key, property_id=property_id)
        self.webhook = os.environ.get("SLACK_OPERATIONS_WEBHOOK_URL", "").strip()
        if not self.webhook:
            logger.warning("SLACK_OPERATIONS_WEBHOOK_URL not set — report will print to stdout only")

        self.today = datetime.now(ZoneInfo("Australia/Melbourne")).date()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("=== HSMI Housekeeping Roster — %s ===", self.today)

        today_str = self.today.strftime("%Y-%m-%d")
        end_date  = self.today + timedelta(days=LOOKAHEAD)
        end_str   = end_date.strftime("%Y-%m-%d")

        # Step 1: Collect reservation IDs covering the window
        logger.info("Fetching checkout reservation IDs %s → %s", today_str, end_str)
        checkout_ids = self._fetch_res_ids(checkOutFrom=today_str, checkOutTo=end_str)
        logger.info("Fetching checkin reservation IDs %s → %s", today_str, end_str)
        checkin_ids  = self._fetch_res_ids(checkInFrom=today_str,  checkInTo=end_str)

        all_ids = checkout_ids | checkin_ids
        logger.info(
            "Checkout IDs: %d  Checkin IDs: %d  Unique: %d",
            len(checkout_ids), len(checkin_ids), len(all_ids),
        )

        # Step 2: Fetch each reservation detail once, populate per-date room sets
        checkins:  dict[str, set[str]] = defaultdict(set)
        checkouts: dict[str, set[str]] = defaultdict(set)

        for res_id in all_ids:
            detail = self._fetch_detail(res_id)
            if not detail:
                continue
            for assignment in (detail.get("assigned") or detail.get("rooms") or []):
                if not isinstance(assignment, dict):
                    continue
                room_id    = str(assignment.get("roomID") or "")
                start_d    = str(assignment.get("startDate") or "")
                end_d      = str(assignment.get("endDate") or "")
                if not room_id:
                    continue
                if today_str <= start_d <= end_str:
                    checkins[start_d].add(room_id)
                if today_str <= end_d <= end_str:
                    checkouts[end_d].add(room_id)

        logger.info(
            "Room assignments indexed — checkin dates: %d  checkout dates: %d",
            len(checkins), len(checkouts),
        )

        # Step 3: Build per-day rows
        rows: list[dict] = []
        sunday_deferrals = 0

        for i in range(LOOKAHEAD):
            d     = self.today + timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            dow   = d.weekday()  # 0=Mon … 6=Sun

            checkins_today  = checkins.get(d_str, set())
            checkouts_today = checkouts.get(d_str, set())

            turnovers_set      = checkins_today & checkouts_today
            checkouts_only_set = checkouts_today - checkins_today

            n_to = len(turnovers_set)
            n_co = len(checkouts_only_set)

            # Rooms occupied tonight (new checkins staying over, not same-day checkouts)
            tonight_occupied = len(checkins_today - checkouts_today)
            vacant = TOTAL_ROOMS - tonight_occupied

            # ------------------------------------------------------------------
            # Capacity / flag logic
            # ------------------------------------------------------------------

            if dow == 5:  # Saturday — full reset day
                total = n_to + n_co
                if total > 6:
                    flag = f"🚨 ARRANGE CASUALS — {total} rooms (full reset)"
                elif total == 6:
                    flag = "✅ at capacity — block late checkouts"
                elif total > 0:
                    flag = "✅"
                else:
                    flag = "✅ quiet"

            elif dow == 6:  # Sunday — always clean at least SUNDAY_MIN_CLEAN, defer rest to Monday
                extras   = max(0, 2 - max(0, vacant - n_to))
                must     = max(n_to + extras, SUNDAY_MIN_CLEAN)
                deferred = max(0, (n_to + n_co) - must)
                sunday_deferrals = deferred
                if must > 6:
                    flag = f"⚠️  consider casuals — {must} needed | defer {deferred} to Mon"
                elif deferred > 0:
                    flag = f"✅ clean {must} | defer {deferred} to Mon"
                else:
                    flag = "✅ quiet"

            elif dow == 0:  # Monday — absorb Sunday deferrals
                eff  = n_to + sunday_deferrals
                note = f" +{sunday_deferrals} deferred from Sun" if sunday_deferrals else ""
                sunday_deferrals = 0
                flag = f"🚨 arrange casuals — {eff} turnovers{note}" if eff > 6 else f"✅{note}"
                n_to = eff

            elif dow == 2:  # Wednesday — D+L off
                if n_to > 0:
                    flag = f"🚨 D+L off — arrange casuals ({n_to} turnovers)"
                elif n_co > 0:
                    flag = f"✅ D+L off — {n_co} checkouts only"
                else:
                    flag = "✅ D+L off — quiet"

            else:  # Tue, Thu, Fri
                cap  = TURNOVER_CAPACITY[dow]
                flag = f"🚨 arrange casuals — {n_to} turnovers (cap {cap})" if n_to > cap else "✅"

            rows.append({"date": d, "n_to": n_to, "n_co": n_co, "flag": flag})

        logger.info("Roster built — %d days", len(rows))
        message = self._build_message(rows)
        self._post(message)
        logger.info("=== Housekeeping Roster complete ===")

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _fetch_res_ids(self, **params) -> set[str]:
        """Paginated getReservations fetch. Returns set of active reservation IDs."""
        ids: set[str] = set()
        page = 1
        while True:
            try:
                resp = self.client._get("getReservations", params={**params, "pageNum": page, "pageSize": 100})
            except CloudbedsAPIError as exc:
                logger.warning("getReservations failed (params=%s page=%d): %s", params, page, exc)
                break

            data  = resp.get("data", [])
            items: list[dict] = (
                data if isinstance(data, list)
                else data.get("reservations", []) if isinstance(data, dict)
                else []
            )
            if not items:
                break

            for item in items:
                status = (
                    item.get("status") or item.get("reservationStatus") or ""
                ).lower().replace(" ", "_")
                if status in _CANCELLED:
                    continue
                res_id = str(item.get("reservationID") or "")
                if res_id:
                    ids.add(res_id)

            count = int(resp.get("count") or len(items))
            total = int(resp.get("total") or 0)
            if not items or (total and page * count >= total) or len(items) < 100:
                break
            page += 1

        return ids

    def _fetch_detail(self, res_id: str) -> dict:
        """Return full reservation detail dict from getReservation (singular)."""
        try:
            resp = self.client._get("getReservation", params={"reservationID": res_id})
            return resp.get("data", resp)
        except CloudbedsAPIError as exc:
            logger.warning("getReservation %s failed: %s", res_id, exc)
            return {}

    # ------------------------------------------------------------------
    # Message builder
    # ------------------------------------------------------------------

    def _build_message(self, rows: list[dict]) -> str:
        today_label = self.today.strftime("%a %d %b")
        lines = [
            f"*HSMI Housekeeping Roster — 14 days from {today_label}*",
            "",
        ]

        for r in rows:
            date_label = r["date"].strftime("%a %d %b")
            lines.append(
                f"📅 {date_label:<11}  {r['n_to']:>2} turnovers  {r['n_co']:>2} checkouts   {r['flag']}"
            )

        lines += [
            "",
            "_turnovers = same room checking out + in (must clean 10am–2pm) | checkouts = departure only (flexible)_",
            "_Capacity: Mon/Fri/Sat/Sun 6 | Tue/Thu 3 | Wed 0 (D+L off)_",
        ]

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Slack post
    # ------------------------------------------------------------------

    def _post(self, message: str) -> None:
        if not self.webhook:
            print(message)
            return
        try:
            resp = requests.post(
                self.webhook,
                json={"text": message, "username": "Ops Agent", "icon_emoji": ":calendar:"},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Housekeeping roster posted to Slack #operations")
        except requests.RequestException as exc:
            logger.error("Slack post failed: %s — printing to stdout", exc)
            print(message)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    HousekeepingRoster().run()
