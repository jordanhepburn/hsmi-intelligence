"""
HSMI Dynamic Pricing Engine
============================

Calculates optimal nightly rates for Hepburn Springs Motor Inn across a
rolling 60-day window and pushes updates to Cloudbeds when a rate changes
by more than the configured threshold ($5).

Environment variables (required unless marked optional):
  CLOUDBEDS_API_KEY       — Cloudbeds x-api-key credential
  CLOUDBEDS_PROPERTY_ID   — Cloudbeds property ID
  NOTION_API_KEY          — Notion integration token (pricing tiers loaded from Notion at startup)
  SLACK_WEBHOOK_URL       — Incoming webhook for the pricing summary (optional)
  ANTHROPIC_API_KEY       — Reserved for future AI-assisted pricing (optional)

Usage:
  python pricing_engine/pricing_engine.py
"""

import logging
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or from pricing_engine/ directory
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError  # noqa: E402
from config import IGNORED_ROOM_TYPE_IDS, LOOKAHEAD_DAYS, RATE_CHANGE_THRESHOLD, ROOM_TYPE_ID_MAP  # noqa: E402
from holidays import is_peak_date  # noqa: E402
from notion_loader import load_pricing_tiers  # noqa: E402

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
WEEKEND_DAYS = {4, 5, 6}  # Friday=4, Saturday=5, Sunday=6 (weekday() values)


# ---------------------------------------------------------------------------
# PricingEngine
# ---------------------------------------------------------------------------


class PricingEngine:
    """Orchestrates occupancy calculation, rate recommendation, and Cloudbeds sync."""

    def __init__(self) -> None:
        api_key = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
        property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
        missing = [v for v, k in [("CLOUDBEDS_API_KEY", api_key), ("CLOUDBEDS_PROPERTY_ID", property_id)] if not k]
        if missing:
            logger.critical("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        self.client = CloudbedsClient(api_key=api_key, property_id=property_id)
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        self.today = date.today()

        # Daily base run at 20:00 UTC (6am AEST) uses full 60-day window.
        # All other runs (hourly) use a 14-day window to stay fast.
        utc_hour = datetime.utcnow().hour
        is_base_run = (utc_hour == 20)
        lookahead = LOOKAHEAD_DAYS if is_base_run else 14
        self.end_date = self.today + timedelta(days=lookahead)
        logger.info(
            "Run type: %s (UTC hour=%d) — lookahead=%d days",
            "DAILY BASE" if is_base_run else "HOURLY",
            utc_hour,
            lookahead,
        )

        # Load pricing tiers from Notion (falls back to config.py if unavailable)
        self.room_types = load_pricing_tiers()
        logger.info("Pricing tiers loaded (%d room types):", len(self.room_types))
        for code, tiers in sorted(self.room_types.items()):
            logger.info(
                "  %s  floor=$%.0f  midweek=$%.0f  weekend=$%.0f  peak=$%.0f  ceiling=$%.0f",
                code,
                tiers["floor"], tiers["midweek"], tiers["weekend"],
                tiers["peak"], tiers["ceiling"],
            )

        # Populated during run()
        self._room_type_map: dict[str, dict] = {}   # code → {id, total_rooms, rate_id}
        self._rate_plan_id: str = ""
        self._occupancy: dict[str, dict[str, float]] = {}  # date_str → {code: occ_pct}
        self._target_rates: dict[str, dict[str, float]] = {}  # date_str → {code: rate}
        self._current_rates: dict[str, dict[str, float]] = {}  # date_str → {code: rate}
        self._updates_pushed: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Orchestrate the full pricing cycle."""
        try:
            logger.info("=== HSMI Pricing Engine starting — %s ===", self.today)
            self._load_room_types()
            self._load_rate_plans()
            self._calculate_occupancy()
            self._calculate_rates()
            self._fetch_current_rates()
            self._push_updates()
            self._send_slack_summary()
            logger.info("=== Pricing Engine complete — %d updates pushed ===", len(self._updates_pushed))
        except SystemExit:
            raise
        except Exception:
            logger.critical("Unhandled exception in pricing engine:\n%s", traceback.format_exc())
            sys.exit(1)

    # ------------------------------------------------------------------
    # Step 1: Load room types
    # ------------------------------------------------------------------

    def _load_room_types(self) -> None:
        """
        Populate _room_type_map from the hardcoded ROOM_TYPE_ID_MAP in config.py.

        No API call or name matching needed — IDs were confirmed directly from
        the Cloudbeds property and stored in config.  getRoomTypes is still
        called so the full API response is visible in logs for sanity-checking.
        """
        logger.info("Step 1: Loading room types")

        # Still call the API so any future discrepancies show up in logs
        raw_room_types = self.client.get_room_types()
        logger.info("Cloudbeds returned %d room type(s) from API:", len(raw_room_types))
        for rt in raw_room_types:
            rt_id  = str(rt.get("roomTypeID") or rt.get("id") or "?")
            name   = str(rt.get("roomTypeName") or rt.get("name") or "?")
            short  = str(rt.get("roomTypeShortName") or rt.get("shortName") or rt.get("code") or "—")
            total  = rt.get("totalRooms") or rt.get("roomsCount") or rt.get("count") or "?"
            prefix = "  IGNORED" if rt_id in IGNORED_ROOM_TYPE_IDS else "  "
            logger.info("%s  id=%-18s  name=%-30s  short=%-6s  total=%s", prefix, rt_id, name, short, total)

        # Build map directly from hardcoded IDs
        for code, entry in ROOM_TYPE_ID_MAP.items():
            self._room_type_map[code] = {
                "id": entry["id"],
                "total_rooms": entry["total_rooms"],
            }
            logger.info(
                "MAPPED: %s → id=%s ('%s'), %d room(s)",
                code, entry["id"], entry["name"], entry["total_rooms"],
            )

        logger.info("Room type map loaded: %d tiers", len(self._room_type_map))

    # ------------------------------------------------------------------
    # Step 2: Load rate plans
    # ------------------------------------------------------------------

    def _load_rate_plans(self) -> None:
        """
        Fetch rate plans and extract per-room-type rateIDs needed for patchRate.

        The Cloudbeds getRatePlans endpoint (with detailedRates=true) returns a
        flat list of individual rate entries under data[].  Each entry has:
          rateID, roomTypeID, isDerived, ratePlanID (absent on standalone rates)

        Strategy:
          1. Collect all non-derived entries for our room types.
          2. Group by ratePlanID; select the plan covering the most room types.
          3. Fall back to standalone rates (no ratePlanID) for any gaps.
        """
        logger.info("Step 2: Loading rate plans")
        response = self.client.get_rate_plans(self.today, self.end_date)

        entries = response.get("data", [])
        if isinstance(entries, dict):
            entries = list(entries.values())

        if not entries:
            logger.critical("getRatePlans returned no entries — cannot continue")
            sys.exit(1)

        our_ids = {rt["id"] for rt in self._room_type_map.values()}

        # plan_coverage[planID][roomTypeID] = rateID  (non-derived only)
        plan_coverage: dict[str, dict[str, str]] = {}
        # standalone[roomTypeID] = rateID  (non-derived, no ratePlanID)
        standalone: dict[str, str] = {}

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            room_type_id = str(entry.get("roomTypeID") or "")
            rate_id      = str(entry.get("rateID") or "")
            is_derived   = entry.get("isDerived", False)
            plan_id      = str(entry.get("ratePlanID") or "")

            if is_derived or not room_type_id or not rate_id:
                continue
            if room_type_id not in our_ids:
                continue

            if plan_id:
                plan_coverage.setdefault(plan_id, {})
                plan_coverage[plan_id].setdefault(room_type_id, rate_id)
            else:
                standalone.setdefault(room_type_id, rate_id)

        # Log all non-derived plans and their room-type coverage
        logger.info("Non-derived rate plans found (by coverage of our %d room types):", len(our_ids))
        for plan_id, coverage in sorted(plan_coverage.items(), key=lambda x: -len(x[1])):
            codes = [c for c, rt in self._room_type_map.items() if rt["id"] in coverage]
            logger.info("  planID=%-22s  covers %d/%d: %s", plan_id, len(coverage), len(our_ids), codes)
        if standalone:
            codes = [c for c, rt in self._room_type_map.items() if rt["id"] in standalone]
            logger.info("  standalone (no planID)          covers %d/%d: %s", len(standalone), len(our_ids), codes)

        # Select the plan covering the most of our room types
        if plan_coverage:
            best_plan_id = max(plan_coverage, key=lambda pid: len(plan_coverage[pid]))
            selected = plan_coverage[best_plan_id]
            self._rate_plan_id = best_plan_id
            logger.info("Selected planID=%s (%d/%d room types)", best_plan_id, len(selected), len(our_ids))
        else:
            selected = {}
            self._rate_plan_id = ""
            logger.warning("No plan-based rates found — will use standalone rates only")

        # Build final rateID map: prefer plan rates, fill gaps with standalone
        rate_id_by_room_type_id: dict[str, str] = {}
        for room_type_id in our_ids:
            rid = selected.get(room_type_id) or standalone.get(room_type_id)
            if rid:
                rate_id_by_room_type_id[room_type_id] = rid

        # Store on each room type and log the result
        for code, rt in self._room_type_map.items():
            rate_id = rate_id_by_room_type_id.get(rt["id"], "")
            rt["rate_id"] = rate_id
            if rate_id:
                logger.info("  %s (roomTypeID=%s): rateID=%s", code, rt["id"], rate_id)
            else:
                logger.warning(
                    "  %s (roomTypeID=%s): no rateID found — rate updates for this type will be skipped",
                    code, rt["id"],
                )

        enabled = sum(1 for rt in self._room_type_map.values() if rt.get("rate_id"))
        total = len(self._room_type_map)
        if enabled:
            logger.info("RATE PUSH ENABLED: %d/%d room types have rateIDs", enabled, total)
        else:
            logger.warning("RATE PUSH DISABLED: no rateIDs found — all rate pushes will be skipped")

    # ------------------------------------------------------------------
    # Step 3: Calculate occupancy
    # ------------------------------------------------------------------

    def _calculate_occupancy(self) -> dict[str, dict[str, float]]:
        """
        Fetch reservations and compute occupancy per date per room type.

        A reservation covers night D when start_date <= D < end_date
        (check-out morning convention).

        Returns
        -------
        dict
            ``{date_str: {room_type_code: occupancy_pct}}``
        """
        logger.info("Step 3: Calculating occupancy over next %d days", LOOKAHEAD_DAYS)
        reservations = self.client.get_reservations(self.today, self.end_date)

        # Build reverse map: room_type_id → code
        id_to_code = {v["id"]: k for k, v in self._room_type_map.items()}

        # Initialise occupancy dict
        occ_counts: dict[str, dict[str, int]] = {}
        current = self.today
        while current < self.end_date:
            d_str = current.strftime("%Y-%m-%d")
            occ_counts[d_str] = {code: 0 for code in self._room_type_map}
            current += timedelta(days=1)

        for res in reservations:
            code = id_to_code.get(res["roomTypeID"])
            if not code:
                continue
            try:
                res_start = date.fromisoformat(res["startDate"])
                res_end = date.fromisoformat(res["endDate"])
            except ValueError:
                logger.warning("Bad dates on reservation %s — skipping", res["reservationID"])
                continue

            night = max(res_start, self.today)
            while night < res_end and night < self.end_date:
                d_str = night.strftime("%Y-%m-%d")
                if d_str in occ_counts:
                    occ_counts[d_str][code] = occ_counts[d_str].get(code, 0) + 1
                night += timedelta(days=1)

        # Convert counts to percentages
        for d_str, code_counts in occ_counts.items():
            self._occupancy[d_str] = {}
            for code, count in code_counts.items():
                total = self._room_type_map[code]["total_rooms"]
                self._occupancy[d_str][code] = min(count / total, 1.0)

        return self._occupancy

    # ------------------------------------------------------------------
    # Step 4: Calculate target rates
    # ------------------------------------------------------------------

    def _calculate_rates(self) -> dict[str, dict[str, float]]:
        """
        Apply pricing rules for every room type × every date in the window.

        Returns
        -------
        dict
            ``{date_str: {room_type_code: target_rate}}``
        """
        logger.info("Step 4: Calculating target rates")

        current = self.today
        while current < self.end_date:
            d_str = current.strftime("%Y-%m-%d")
            days_out = (current - self.today).days
            is_weekend = current.weekday() in WEEKEND_DAYS
            self._target_rates[d_str] = {}

            for code, cfg in self.room_types.items():
                if code not in self._room_type_map:
                    current += timedelta(days=1)
                    continue

                occ_pct = self._occupancy.get(d_str, {}).get(code, 0.0)
                base_rate = cfg["weekend"] if is_weekend else cfg["midweek"]
                floor_ = cfg["floor"]
                ceiling_ = cfg["ceiling"]

                # ---- Priority 1: Peak date (public holiday or school holiday) ----
                if is_peak_date(current):
                    rate = cfg["peak"]
                    reason = "peak date (holiday/school holiday) — min 2-night stay recommended"

                # ---- Priority 2: Very high occupancy (> 90%) ----
                elif occ_pct > 0.90:
                    rate = base_rate * 1.25
                    reason = "occ >90% +25% — min 2-night stay recommended"

                # ---- Priority 3: Weekend ----
                elif is_weekend:
                    if days_out < 7 and occ_pct > 0.80:
                        rate = cfg["weekend"] * 1.20
                        reason = "weekend last-minute high occ +20%"
                    elif days_out >= 7 and occ_pct > 0.70:
                        rate = cfg["weekend"] * 1.10
                        reason = "weekend advance high occ +10%"
                    else:
                        rate = cfg["weekend"]
                        reason = "weekend base"

                # ---- Priority 4: Midweek ----
                else:
                    if days_out < 7 and occ_pct < 0.25:
                        rate = cfg["midweek"] * 0.85
                        reason = "midweek last-minute low occ -15%"
                    elif 7 <= days_out < 14 and occ_pct < 0.30:
                        rate = cfg["midweek"] * 0.90
                        reason = "midweek advance low occ -10%"
                    elif days_out >= 14 and occ_pct < 0.40:
                        rate = cfg["midweek"]
                        reason = "midweek advance low occ (no discount)"
                    else:
                        rate = cfg["midweek"]
                        reason = "midweek base"

                # Clamp to [floor, ceiling]
                rate = max(floor_, min(ceiling_, round(rate)))

                self._target_rates[d_str][code] = rate
                logger.debug(
                    "%s %s → $%.0f (%s | %.0f%% occ | %dd out)",
                    code, d_str, rate, reason, occ_pct * 100, days_out,
                )

            current += timedelta(days=1)

        return self._target_rates

    # ------------------------------------------------------------------
    # Step 5: Fetch current rates from Cloudbeds
    # ------------------------------------------------------------------

    def _fetch_current_rates(self) -> None:
        """
        Pull the rates currently live in Cloudbeds for comparison.
        Populates ``self._current_rates``.
        """
        logger.info("Step 5: Fetching current rates from Cloudbeds")
        for code, rt in self._room_type_map.items():
            try:
                rates = self.client.get_rate(
                    room_type_id=rt["id"],
                    start_date=self.today,
                    end_date=self.end_date,
                )
                for d_str, rate in rates.items():
                    if d_str not in self._current_rates:
                        self._current_rates[d_str] = {}
                    self._current_rates[d_str][code] = rate
            except CloudbedsAPIError as exc:
                logger.warning("Could not fetch current rates for %s: %s", code, exc)

    # ------------------------------------------------------------------
    # Step 6: Push updates
    # ------------------------------------------------------------------

    def _push_updates(self) -> None:
        """
        Compare target vs current rates. Push updates where the difference
        exceeds RATE_CHANGE_THRESHOLD. Log every decision.
        """
        logger.info("Step 6: Pushing rate updates (threshold=$%.0f)", RATE_CHANGE_THRESHOLD)

        current = self.today
        while current < self.end_date:
            d_str = current.strftime("%Y-%m-%d")
            days_out = (current - self.today).days

            for code, rt in self._room_type_map.items():
                new_rate = self._target_rates.get(d_str, {}).get(code)
                if new_rate is None:
                    continue

                current_rate = self._current_rates.get(d_str, {}).get(code)
                occ_pct = self._occupancy.get(d_str, {}).get(code, 0.0)

                # Determine direction for logging
                if current_rate is None:
                    direction = "set"
                    diff = abs(new_rate)
                elif new_rate > current_rate:
                    direction = "raised"
                    diff = new_rate - current_rate
                elif new_rate < current_rate:
                    direction = "dropped"
                    diff = current_rate - new_rate
                else:
                    direction = "unchanged"
                    diff = 0.0

                # Decide whether to push
                if diff > RATE_CHANGE_THRESHOLD:
                    reason = _rate_reason(code, current, occ_pct, days_out, self.room_types[code])
                    logger.info(
                        "%s %s: %s to $%.0f — %s (%.0f%% occ, %dd out)",
                        code, d_str, direction, new_rate, reason, occ_pct * 100, days_out,
                    )
                    if not rt.get("rate_id"):
                        logger.warning(
                            "%s %s: skipping push — no rateID available for this room type",
                            code, d_str,
                        )
                    else:
                        try:
                            self.client.patch_rate(
                                rate_id=rt["rate_id"],
                                date_str=d_str,
                                rate=new_rate,
                            )
                            self._updates_pushed.append(
                                f"{code} {d_str}: {direction} → ${new_rate:.0f}"
                            )
                        except CloudbedsAPIError as exc:
                            logger.error("Failed to push rate for %s on %s: %s", code, d_str, exc)
                else:
                    logger.debug(
                        "%s %s: unchanged at $%.0f (diff=%.2f, threshold=%.0f)",
                        code, d_str, new_rate, diff, RATE_CHANGE_THRESHOLD,
                    )

            current += timedelta(days=1)

    # ------------------------------------------------------------------
    # Step 7: Slack summary
    # ------------------------------------------------------------------

    def _send_slack_summary(self) -> None:
        """Post a summary of rate changes to Slack."""
        if not self.slack_webhook:
            logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
            return

        n = len(self._updates_pushed)
        if n == 0:
            logger.info("No rate changes required — skipping Slack notification")
            return

        summary_lines = "\n".join(f"  • {line}" for line in self._updates_pushed[:50])
        if n > 50:
            summary_lines += f"\n  … and {n - 50} more"

        enabled = sum(1 for rt in self._room_type_map.values() if rt.get("rate_id"))
        total = len(self._room_type_map)
        if enabled:
            push_status = f"✅ RATE PUSH ENABLED: {enabled}/{total} room types have rateIDs"
        else:
            push_status = f"⛔ RATE PUSH DISABLED: no rateIDs found — no updates were pushed"

        payload = {
            "text": (
                f"*HSMI Pricing Engine — {self.today}*\n"
                f"{n} rate update{'s' if n != 1 else ''} pushed\n"
                f"{summary_lines}\n"
                f"{push_status}\n"
                f"📋 To adjust pricing tiers: https://www.notion.so/349c905ced6b81d1be30d33aa3cf15eb"
            )
        }

        try:
            response = requests.post(self.slack_webhook, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Slack summary sent (%d updates)", n)
        except requests.RequestException as exc:
            logger.warning("Failed to send Slack summary: %s", exc)


# ---------------------------------------------------------------------------
# Helper: human-readable reason for a rate decision
# ---------------------------------------------------------------------------


def _rate_reason(
    code: str,
    d: date,
    occ_pct: float,
    days_out: int,
    cfg: dict,
) -> str:
    """Return a short human-readable explanation for the chosen rate."""
    is_weekend = d.weekday() in WEEKEND_DAYS

    if is_peak_date(d):
        return "peak date"
    if occ_pct > 0.90:
        return "occ >90%"
    if is_weekend:
        if days_out < 7 and occ_pct > 0.80:
            return "weekend last-minute high occ"
        if days_out >= 7 and occ_pct > 0.70:
            return "weekend advance high occ"
        return "weekend base"
    # Midweek
    if days_out < 7 and occ_pct < 0.25:
        return "midweek last-minute low occ"
    if 7 <= days_out < 14 and occ_pct < 0.30:
        return "midweek advance low occ"
    return "midweek base"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PricingEngine().run()
