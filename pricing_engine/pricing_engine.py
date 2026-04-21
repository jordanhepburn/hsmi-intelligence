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
from config import BASE_RATE_IDS, IGNORED_ROOM_TYPE_IDS, LOOKAHEAD_DAYS, RATE_CHANGE_THRESHOLD, ROOM_TYPE_ID_MAP  # noqa: E402
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
        Assign rateIDs for patchRate calls.

        Primary source: BASE_RATE_IDS in config.py — the hardcoded Cloudbeds
        Base Rate (PlanID=BASE) rateIDs that drive public OTA pricing.

        Fallback: API discovery from getRatePlans for any code missing from
        BASE_RATE_IDS (should never be needed unless a room type is added).
        """
        logger.info("Step 2: Loading rate plans")
        logger.info(
            "Targeting BASE rate plan — changes will affect public rates across all channels"
        )

        # Start with hardcoded BASE rateIDs — these are authoritative
        for code, rt in self._room_type_map.items():
            rate_id = BASE_RATE_IDS.get(code, "")
            rt["rate_id"] = rate_id
            if rate_id:
                logger.info("  %s: rateID=%s (BASE plan, hardcoded)", code, rate_id)
            else:
                logger.warning("  %s: not in BASE_RATE_IDS — will attempt API discovery", code)

        # If any codes are missing, try to fill from getRatePlans API response
        missing_codes = [c for c, rt in self._room_type_map.items() if not rt.get("rate_id")]
        if missing_codes:
            logger.info("Attempting API discovery for missing codes: %s", missing_codes)
            try:
                response = self.client.get_rate_plans(self.today, self.end_date)
                entries = response.get("data", [])
                if isinstance(entries, dict):
                    entries = list(entries.values())

                # BASE plan entries have no ratePlanID field (PlanID=BASE)
                base_by_rt_id: dict[str, str] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("isDerived") or entry.get("ratePlanID"):
                        continue  # skip derived and non-base plans
                    rt_id  = str(entry.get("roomTypeID") or "")
                    rid    = str(entry.get("rateID") or "")
                    if rt_id and rid:
                        base_by_rt_id[rt_id] = rid

                for code in missing_codes:
                    rt = self._room_type_map[code]
                    rid = base_by_rt_id.get(rt["id"], "")
                    rt["rate_id"] = rid
                    if rid:
                        logger.info("  %s: rateID=%s (BASE plan, API discovered)", code, rid)
                    else:
                        logger.warning(
                            "  %s (roomTypeID=%s): no BASE rateID found — skipping rate updates",
                            code, rt["id"],
                        )
            except Exception as exc:
                logger.warning("API discovery failed: %s — missing codes will be skipped", exc)

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
        lookahead = (self.end_date - self.today).days
        logger.info("Step 3: Calculating occupancy over next %d days", lookahead)
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
                    continue

                occ_pct = self._occupancy.get(d_str, {}).get(code, 0.0)
                floor_ = cfg["floor"]
                ceiling_ = cfg["ceiling"]

                # ---- Priority 1: Peak date — always peak rate, ignore occ ----
                if is_peak_date(current):
                    rate = cfg["peak"]
                    reason = "peak date"

                # ---- Weekend (Fri/Sat/Sun) ----
                elif is_weekend:
                    if days_out >= 15:
                        rate = cfg["weekend"]
                        reason = "weekend 15+ days — hold base"
                    elif days_out >= 8:                        # 8–14 days out
                        if occ_pct > 0.50:
                            rate = cfg["weekend"] * 1.10
                            reason = "weekend 8-14d >50% occ +10%"
                        else:
                            rate = cfg["weekend"]
                            reason = "weekend 8-14d hold base"
                    elif days_out >= 2:                        # 2–7 days out
                        if occ_pct > 0.80:
                            rate = cfg["weekend"] * 1.25
                            reason = "weekend 2-7d >80% occ +25%"
                        elif occ_pct > 0.60:
                            rate = cfg["weekend"] * 1.15
                            reason = "weekend 2-7d >60% occ +15%"
                        elif occ_pct < 0.30:
                            rate = cfg["weekend"] * 0.92
                            reason = "weekend 2-7d <30% occ -8%"
                        else:
                            rate = cfg["weekend"]
                            reason = "weekend 2-7d hold base"
                    else:                                       # same day / next day
                        if occ_pct > 0.70:
                            rate = cfg["weekend"] * 1.20
                            reason = "weekend last-minute >70% occ +20%"
                        elif occ_pct < 0.40:
                            rate = cfg["weekend"] * 0.88
                            reason = "weekend last-minute <40% occ -12%"
                        else:
                            rate = cfg["weekend"]
                            reason = "weekend last-minute hold base"

                # ---- Midweek (Mon–Thu) ----
                else:
                    if days_out >= 14:
                        rate = cfg["midweek"]
                        reason = "midweek 14+ days — hold base"
                    elif days_out >= 7:                        # 7–13 days out
                        if occ_pct < 0.25:
                            rate = cfg["midweek"] * 0.90
                            reason = "midweek 7-14d <25% occ -10%"
                        else:
                            rate = cfg["midweek"]
                            reason = "midweek 7-14d hold base"
                    elif days_out >= 2:                        # 2–6 days out
                        if occ_pct < 0.20:
                            rate = cfg["midweek"] * 0.85
                            reason = "midweek 2-7d <20% occ -15%"
                        else:
                            rate = cfg["midweek"]
                            reason = "midweek 2-7d hold base"
                    else:                                       # same day / next day
                        if occ_pct < 0.30:
                            rate = cfg["midweek"] * 0.82
                            reason = "midweek same-day <30% occ -18%"
                        else:
                            rate = cfg["midweek"]
                            reason = "midweek same-day hold base"

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
    if is_weekend:
        if days_out >= 15:
            return "weekend 15+ days base"
        if days_out >= 8:
            return "weekend 8-14d >50% occ +10%" if occ_pct > 0.50 else "weekend 8-14d base"
        if days_out >= 2:
            if occ_pct > 0.80: return "weekend 2-7d >80% occ +25%"
            if occ_pct > 0.60: return "weekend 2-7d >60% occ +15%"
            if occ_pct < 0.30: return "weekend 2-7d <30% occ -8%"
            return "weekend 2-7d base"
        if occ_pct > 0.70: return "weekend last-minute >70% occ +20%"
        if occ_pct < 0.40: return "weekend last-minute <40% occ -12%"
        return "weekend last-minute base"
    # Midweek
    if days_out >= 14:
        return "midweek 14+ days base"
    if days_out >= 7:
        return "midweek 7-14d <25% occ -10%" if occ_pct < 0.25 else "midweek 7-14d base"
    if days_out >= 2:
        return "midweek 2-7d <20% occ -15%" if occ_pct < 0.20 else "midweek 2-7d base"
    return "midweek same-day <30% occ -18%" if occ_pct < 0.30 else "midweek same-day base"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PricingEngine().run()
