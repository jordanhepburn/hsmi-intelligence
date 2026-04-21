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
        self.end_date = self.today + timedelta(days=LOOKAHEAD_DAYS)

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
        Fetch rate plans, log all of them, select the best match, and extract
        per-room-type rateIDs needed for patchRate calls.
        """
        import json as _json

        logger.info("Step 2: Loading rate plans")
        response = self.client.get_rate_plans(self.today, self.end_date)
        data = response.get("data", response)

        # getRatePlans may return a list or a dict keyed by ratePlanID
        if isinstance(data, list):
            plans = data
        elif isinstance(data, dict):
            raw = data.get("ratePlans", data)
            plans = list(raw.values()) if isinstance(raw, dict) else raw
        else:
            plans = []

        if not plans:
            logger.critical("getRatePlans returned no plans — cannot continue")
            sys.exit(1)

        # Log every plan name/ID so Jordan can confirm which is the base rate
        logger.info("getRatePlans returned %d plan(s):", len(plans))
        for p in plans:
            p_id   = str(p.get("ratePlanID") or p.get("id") or "?")
            p_name = p.get("ratePlanNamePrivate") or p.get("ratePlanName") or p.get("name") or "?"
            p_pub  = p.get("ratePlanNamePublic") or ""
            p_stat = p.get("status") or "?"
            logger.info(
                "  id=%-20s  private='%s'  public='%s'  status=%s",
                p_id, p_name, p_pub, p_stat,
            )

        # Select rate plan: prefer BAR / standard / best-available, else first active
        selected_plan: dict | None = None
        for p in plans:
            name_str = " ".join([
                p.get("ratePlanNamePrivate") or "",
                p.get("ratePlanNamePublic") or "",
                p.get("ratePlanName") or "",
            ]).lower()
            status = str(p.get("status") or "active").lower()
            if status not in ("active", "1", "true", "enabled"):
                continue
            if any(kw in name_str for kw in ("bar", "standard", "best available", "rack")):
                selected_plan = p
                break

        if selected_plan is None:
            # Fallback: first active plan
            for p in plans:
                status = str(p.get("status") or "active").lower()
                if status in ("active", "1", "true", "enabled"):
                    selected_plan = p
                    logger.warning("No BAR/standard plan found — using first active plan")
                    break

        if selected_plan is None:
            logger.critical("No active rate plans found — cannot continue")
            sys.exit(1)

        self._rate_plan_id = str(
            selected_plan.get("ratePlanID") or selected_plan.get("id") or ""
        )
        plan_name = (
            selected_plan.get("ratePlanNamePrivate")
            or selected_plan.get("ratePlanName")
            or selected_plan.get("name")
            or "?"
        )
        logger.info("Selected rate plan: '%s' (id=%s)", plan_name, self._rate_plan_id)

        # Dump the full selected plan JSON so we can see exactly how rateIDs
        # are nested — this is the diagnostic that will tell us the real structure.
        logger.info(
            "Selected plan full structure:\n%s",
            _json.dumps(selected_plan, indent=2, default=str),
        )

        # ----------------------------------------------------------------
        # Extract rateID per room type.
        # Cloudbeds nests rateIDs differently depending on API version.
        # We try every known shape and log each attempt.
        # ----------------------------------------------------------------
        rate_id_by_room_type_id: dict[str, str] = {}

        def _record(rt_id: str, rate_id: str, source: str) -> None:
            if rt_id and rate_id and rt_id not in rate_id_by_room_type_id:
                rate_id_by_room_type_id[rt_id] = rate_id
                logger.info("  rateID [%s]: roomTypeID=%s → rateID=%s", source, rt_id, rate_id)

        # Shape A: plan.rooms = {roomTypeID: {rates: {rateID: {...}}}}
        rooms = selected_plan.get("rooms")
        if isinstance(rooms, dict):
            logger.info("  Trying shape A (plan.rooms dict)…")
            for rt_id, room_data in rooms.items():
                if not isinstance(room_data, dict):
                    continue
                rates = room_data.get("rates", {})
                if isinstance(rates, dict):
                    for rate_key, rate_val in rates.items():
                        # The key itself is often the rateID; the value may also carry it
                        rid = (
                            (rate_val.get("rateID") if isinstance(rate_val, dict) else None)
                            or rate_key
                        )
                        _record(str(rt_id), str(rid), "A-dict")
                        break  # one rateID per room type is enough
                elif isinstance(rates, list):
                    for rate_item in rates:
                        if isinstance(rate_item, dict):
                            rid = rate_item.get("rateID") or rate_item.get("id")
                            if rid:
                                _record(str(rt_id), str(rid), "A-list")
                                break

        # Shape B: plan.rooms = [{roomTypeID, rates: [{rateID, ...}]}]
        if isinstance(rooms, list):
            logger.info("  Trying shape B (plan.rooms list)…")
            for room in rooms:
                if not isinstance(room, dict):
                    continue
                rt_id = str(room.get("roomTypeID") or room.get("roomType") or "")
                rates = room.get("rates", [])
                if isinstance(rates, list):
                    for r in rates:
                        if isinstance(r, dict):
                            rid = r.get("rateID") or r.get("id")
                            if rid:
                                _record(rt_id, str(rid), "B-list")
                                break
                elif isinstance(rates, dict):
                    for rate_key, rate_val in rates.items():
                        rid = (
                            (rate_val.get("rateID") if isinstance(rate_val, dict) else None)
                            or rate_key
                        )
                        _record(rt_id, str(rid), "B-dict")
                        break

        # Shape C: plan.roomTypes or plan.roomRates = [{roomTypeID, rateID}]
        for field in ("roomTypes", "roomRates", "rates"):
            items = selected_plan.get(field)
            if not items:
                continue
            if isinstance(items, dict):
                items = list(items.values())
            logger.info("  Trying shape C (plan.%s)…", field)
            for item in items:
                if not isinstance(item, dict):
                    continue
                rt_id = str(item.get("roomTypeID") or item.get("roomType") or "")
                rid = str(item.get("rateID") or item.get("id") or "")
                _record(rt_id, rid, f"C-{field}")

        # Shape D: plan.roomTypes = {roomTypeID: {rateID, rates: [...]}}
        room_types_dict = selected_plan.get("roomTypes")
        if isinstance(room_types_dict, dict):
            logger.info("  Trying shape D (plan.roomTypes dict keyed by roomTypeID)…")
            for rt_id, rt_data in room_types_dict.items():
                if not isinstance(rt_data, dict):
                    continue
                # rateID may be directly on the room type entry
                rid = rt_data.get("rateID") or rt_data.get("id")
                if rid:
                    _record(str(rt_id), str(rid), "D-direct")
                    continue
                # or inside a nested rates structure
                rates = rt_data.get("rates", {})
                if isinstance(rates, dict):
                    for rate_key in rates:
                        _record(str(rt_id), str(rate_key), "D-rates-key")
                        break
                elif isinstance(rates, list):
                    for r in rates:
                        if isinstance(r, dict):
                            rid2 = r.get("rateID") or r.get("id")
                            if rid2:
                                _record(str(rt_id), str(rid2), "D-rates-list")
                                break

        if not rate_id_by_room_type_id:
            logger.warning(
                "Could not extract any rateID from the selected rate plan. "
                "See the full plan structure logged above to diagnose."
            )

        # Store rateID on each room type entry
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
            summary_lines = "_No rate changes required today._"
        else:
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
