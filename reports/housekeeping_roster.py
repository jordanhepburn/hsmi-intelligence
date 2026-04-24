"""
HSMI 14-Day Housekeeping Roster
================================
Posts a 14-day rolling housekeeping capacity forecast to Slack #operations
at 8am AEST (10pm UTC, cron '0 22 * * *').

For each day in the next 14 days it shows:
  - turnovers  = same room checking out AND in (must clean 10am–2pm)
  - checkouts  = departure only, no arrival (flexible timing)
  - Flag       = capacity alert based on rostered hours

Staffing (rostered hours = 1hr per room):
  Lisa:   Mon 6 | Tue 2 | Wed 0 | Thu 0 | Fri 5 | Sat 6 | Sun 6
  Dwayne: Mon 6 | Tue 2 | Wed 0 | Thu 0 | Fri 5 | Sat 6 | Sun 6
  Total:  Mon 12 | Tue 4 | Wed/Thu 0 (D+L off) | Fri 10 | Sat/Sun 12

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

# Rostered hours per day (0=Mon … 6=Sun).  1 hr = 1 room cleaned.
DWAYNE_HOURS = {0: 6, 1: 2, 2: 0, 3: 0, 4: 5, 5: 6, 6: 6}
LISA_HOURS   = {0: 6, 1: 2, 2: 0, 3: 0, 4: 5, 5: 6, 6: 6}
JODIE_HOURS  = {0: 6, 1: 0, 2: 4, 3: 4, 4: 0, 5: 2, 6: 0}

TOTAL_HOURS = {d: DWAYNE_HOURS[d] + LISA_HOURS[d] + JODIE_HOURS[d] for d in range(7)}
# Mon:18, Tue:4, Wed:4, Thu:4, Fri:10, Sat:14, Sun:12

# Max turnovers (10am–2pm hard window, based on real timesheet data).
# Sunday capped at 6 — higher pay rate, Dwayne on maintenance unless needed.
MAX_TURNOVERS = {0: 9, 1: 4, 2: 4, 3: 4, 4: 9, 5: 9, 6: 6}

# Jodie: housekeeping & laundry only (not maintenance/reno)
# Dwayne: cleans first, maintenance/reno/garden with leftover time
# Lisa:   cleans only

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

            # ------------------------------------------------------------------
            # Capacity / flag logic
            # ------------------------------------------------------------------

            total_available = n_to + n_co
            max_to    = MAX_TURNOVERS[dow]
            total_hrs = TOTAL_HOURS[dow]
            dwayne_hrs = DWAYNE_HOURS[dow]
            lisa_hrs   = LISA_HOURS[dow]
            jodie_hrs  = JODIE_HOURS[dow]

            # Roster string for display
            roster_parts = []
            if dwayne_hrs: roster_parts.append(f"Dwayne {dwayne_hrs}h")
            if lisa_hrs:   roster_parts.append(f"Lisa {lisa_hrs}h")
            if jodie_hrs:  roster_parts.append(f"Jodie {jodie_hrs}h")
            roster_str = ", ".join(roster_parts) if roster_parts else "No staff rostered"

            if dow in (2, 3):  # Wednesday / Thursday — Jodie only, D+L off
                if n_to > max_to:
                    flag = f"🚨 Jodie only — arrange casuals ({n_to} turnovers, cap {max_to})"
                elif n_to > 0:
                    flag = f"✅ Jodie: {n_to} turnovers + {n_co} checkouts"
                elif n_co > 0:
                    flag = f"✅ Jodie: {n_co} checkouts"
                else:
                    flag = "✅ quiet"

            elif dow == 5:  # Saturday — D+L+Jodie, full reset
                rooms_cleaned      = min(total_available, total_hrs)
                dwayne_maintenance = max(0, dwayne_hrs - max(0, rooms_cleaned - lisa_hrs - jodie_hrs))
                if n_to > max_to:
                    flag = f"🚨 ARRANGE CASUALS — {n_to} turnovers (cap {max_to})"
                else:
                    maint_note = f" | Dwayne: {dwayne_maintenance}h maintenance" if dwayne_maintenance > 0 else ""
                    flag = f"✅ clean {rooms_cleaned}{maint_note}"

            elif dow == 6:  # Sunday — D+L, always clean turnovers + 2 minimum, defer rest to Monday
                must_clean         = max(n_to + 2, n_to)  # always at least turnovers + 2
                sunday_deferrals   = max(0, total_available - must_clean)
                dwayne_maintenance = max(0, dwayne_hrs - max(0, must_clean - lisa_hrs))
                if n_to > max_to:
                    flag = f"🚨 consider casuals — {n_to} turnovers (cap {max_to}, high pay day)"
                else:
                    defer_note = f" | defer {sunday_deferrals} to Mon (keeps Jodie busy)" if sunday_deferrals > 0 else ""
                    maint_note = f" | Dwayne: {dwayne_maintenance}h maintenance" if dwayne_maintenance > 0 else ""
                    flag = f"✅ clean {must_clean}{defer_note}{maint_note}"

            elif dow == 0:  # Monday — D+L+Jodie, absorbs Sunday deferrals
                total_monday       = n_to + sunday_deferrals + n_co
                rooms_cleaned      = min(total_monday, total_hrs)
                deferred_mon       = max(0, total_monday - rooms_cleaned)
                sun_note           = f" +{sunday_deferrals} from Sun" if sunday_deferrals else ""
                sunday_deferrals   = 0
                dwayne_maintenance = max(0, dwayne_hrs - max(0, rooms_cleaned - lisa_hrs - jodie_hrs))
                if n_to > max_to:
                    flag = f"🚨 arrange casuals — {n_to} turnovers{sun_note} (cap {max_to})"
                elif deferred_mon > 0:
                    flag = f"⚠️  {deferred_mon} rooms carry to Tue | clean {rooms_cleaned}{sun_note}"
                else:
                    maint_note = f" | Dwayne: {dwayne_maintenance}h maintenance" if dwayne_maintenance > 0 else ""
                    flag = f"✅ clean {rooms_cleaned}{sun_note}{maint_note}"

            else:  # Tuesday and Friday
                rooms_cleaned      = min(total_available, total_hrs)
                deferred           = max(0, total_available - rooms_cleaned)
                dwayne_maintenance = max(0, dwayne_hrs - max(0, rooms_cleaned - lisa_hrs))
                if n_to > max_to:
                    flag = f"🚨 arrange casuals — {n_to} turnovers (cap {max_to})"
                elif deferred > 0:
                    flag = f"✅ clean {rooms_cleaned} | defer {deferred}"
                else:
                    maint_note = f" | Dwayne: {dwayne_maintenance}h maintenance" if dwayne_maintenance > 0 else ""
                    flag = f"✅ clean {rooms_cleaned}{maint_note}" if rooms_cleaned > 0 else "✅ quiet"

            rows.append({"date": d, "n_to": n_to, "n_co": n_co, "roster": roster_str, "flag": flag})

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
                f"📅 {date_label:<11}  {r['n_to']:>2} turnovers  {r['n_co']:>2} checkouts   "
                f"{r['roster']:<32}  {r['flag']}"
            )

        lines += [
            "",
            "_Capacity: Mon 9 TO/18 cleans | Tue 4/4 | Wed/Thu 4/4 (Jodie only) | Fri 9/10 | Sat 9/14 | Sun 6/12_",
            "_TO = turnovers (10am–2pm) | Jodie = housekeeping & laundry only | Dwayne = cleans first, maintenance with spare time_",
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
