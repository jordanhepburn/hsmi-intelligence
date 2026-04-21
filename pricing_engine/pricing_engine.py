"""
HSMI Dynamic Pricing Engine
============================

Calculates optimal nightly rates for Hepburn Springs Motor Inn across a
rolling 60-day window and pushes updates to Cloudbeds when a rate changes
by more than the configured threshold ($5).

Environment variables (required unless marked optional):
  CLOUDBEDS_API_KEY       — Cloudbeds x-api-key credential
  CLOUDBEDS_PROPERTY_ID   — Cloudbeds property ID
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
from config import IGNORED_SHORT_CODES, LOOKAHEAD_DAYS, NAME_KEYWORDS, RATE_CHANGE_THRESHOLD, ROOM_TYPES, SHORT_CODE_MAP  # noqa: E402
from holidays import is_peak_date  # noqa: E402

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

        # Populated during run()
        self._room_type_map: dict[str, dict] = {}   # code → {id, total_rooms}
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
        Fetch room types from Cloudbeds and map them to our pricing tier codes.

        Cloudbeds returns room types with their own IDs and names, which may not
        match our short codes (TWI/QUE/SPA/FAM/BAL/ACC) directly.  We perform
        case-insensitive keyword matching on roomTypeName and roomTypeShortName.
        The full API response is logged at INFO level so the mapping can be
        reviewed and NAME_KEYWORDS adjusted if needed.
        """
        logger.info("Step 1: Loading and mapping room types from Cloudbeds")
        raw_room_types = self.client.get_room_types()  # full response already logged by client

        if not raw_room_types:
            logger.critical("getRoomTypes returned no data — cannot continue")
            sys.exit(1)

        # ----------------------------------------------------------------
        # Log a human-readable summary of every room type Cloudbeds returned
        # ----------------------------------------------------------------
        logger.info("Cloudbeds returned %d room type(s):", len(raw_room_types))
        for rt in raw_room_types:
            rt_id   = str(rt.get("roomTypeID") or rt.get("id") or "?")
            name    = str(rt.get("roomTypeName") or rt.get("name") or "?")
            short   = str(rt.get("roomTypeShortName") or rt.get("shortName") or rt.get("code") or "")
            total   = rt.get("totalRooms") or rt.get("roomsCount") or rt.get("count") or "?"
            logger.info("  id=%-12s  name=%-30s  short=%-6s  total=%s", rt_id, name, short, total)

        # ----------------------------------------------------------------
        # Build a reverse index: cloudbeds_short_code → record
        # ----------------------------------------------------------------
        short_to_rt: dict[str, dict] = {}
        for rt in raw_room_types:
            sc = (
                rt.get("roomTypeShortName") or rt.get("shortName") or rt.get("code") or ""
            ).strip().upper()
            if sc:
                short_to_rt[sc] = rt

        # ----------------------------------------------------------------
        # Match each Cloudbeds room type to our pricing tier codes.
        # Priority order:
        #   1. Exact roomTypeShortName match via SHORT_CODE_MAP  (most reliable)
        #   2. Case-insensitive keyword match on roomTypeName     (fallback)
        # Room types whose short code is in IGNORED_SHORT_CODES are skipped.
        # ----------------------------------------------------------------
        unmatched_api_ids = {
            str(rt.get("roomTypeID") or rt.get("id") or "")
            for rt in raw_room_types
        }
        ignored_ids: set[str] = set()

        # Pre-log any ignored room types
        for sc in IGNORED_SHORT_CODES:
            if sc in short_to_rt:
                rt = short_to_rt[sc]
                rt_id = str(rt.get("roomTypeID") or rt.get("id") or "")
                logger.info(
                    "IGNORED: short=%s id=%s ('%s') — listed in IGNORED_SHORT_CODES",
                    sc, rt_id, rt.get("roomTypeName") or rt.get("name"),
                )
                ignored_ids.add(rt_id)

        for code in ROOM_TYPES:
            rt: dict | None = None
            match_method = ""

            # --- Pass 1: exact short code ---
            expected_sc = SHORT_CODE_MAP.get(code, code)
            if expected_sc in short_to_rt:
                candidate = short_to_rt[expected_sc]
                cid = str(candidate.get("roomTypeID") or candidate.get("id") or "")
                if cid not in ignored_ids:
                    rt = candidate
                    match_method = f"exact short code '{expected_sc}'"

            # --- Pass 2: keyword match on roomTypeName ---
            if rt is None:
                keywords = NAME_KEYWORDS.get(code, [])
                keyword_matches = []
                for candidate in raw_room_types:
                    cid = str(candidate.get("roomTypeID") or candidate.get("id") or "")
                    if cid in ignored_ids:
                        continue
                    name_haystack = (candidate.get("roomTypeName") or "").lower()
                    if any(kw.lower() in name_haystack for kw in keywords):
                        keyword_matches.append(candidate)

                if len(keyword_matches) > 1:
                    names = [m.get("roomTypeName") for m in keyword_matches]
                    logger.warning(
                        "Ambiguous keyword match for tier '%s': matched %d room types %s. "
                        "Using first. Set SHORT_CODE_MAP['%s'] to the exact short code to fix.",
                        code, len(keyword_matches), names, code,
                    )
                if keyword_matches:
                    rt = keyword_matches[0]
                    match_method = f"keyword fallback {NAME_KEYWORDS.get(code)}"

            if rt is None:
                logger.warning(
                    "No Cloudbeds room type matched pricing tier '%s' "
                    "(tried short code '%s', keywords %s) — tier skipped",
                    code, SHORT_CODE_MAP.get(code, code), NAME_KEYWORDS.get(code),
                )
                continue

            rt_id = str(rt.get("roomTypeID") or rt.get("id") or "")
            total = int(rt.get("totalRooms") or rt.get("roomsCount") or rt.get("count") or 0)
            total = total if total > 0 else 1
            self._room_type_map[code] = {"id": rt_id, "total_rooms": total}
            unmatched_api_ids.discard(rt_id)
            logger.info(
                "MAPPED: %s → id=%s ('%s') via %s, %d room(s)",
                code, rt_id, rt.get("roomTypeName") or rt.get("name"), match_method, total,
            )

        # Warn about Cloudbeds room types that weren't mapped or ignored
        leftover = unmatched_api_ids - ignored_ids
        if leftover:
            logger.warning(
                "Cloudbeds room type IDs not mapped to any pricing tier: %s — "
                "add to IGNORED_SHORT_CODES or NAME_KEYWORDS in config.py if needed",
                sorted(leftover),
            )

        if not self._room_type_map:
            logger.critical(
                "No room types could be mapped. Review the getRoomTypes response above "
                "and update NAME_KEYWORDS in pricing_engine/config.py."
            )
            sys.exit(1)

        logger.info(
            "Room type mapping complete: %d/%d tiers mapped",
            len(self._room_type_map), len(ROOM_TYPES),
        )

    # ------------------------------------------------------------------
    # Step 2: Load rate plans
    # ------------------------------------------------------------------

    def _load_rate_plans(self) -> None:
        """
        Fetch rate plans and select the first active standard / BAR plan.
        """
        logger.info("Step 2: Loading rate plans")
        response = self.client.get_rate_plans()
        data = response.get("data", response)
        plans = data if isinstance(data, list) else data.get("ratePlans", [])

        for plan in plans:
            name: str = (plan.get("ratePlanName") or plan.get("name") or "").lower()
            status: str = (plan.get("status") or "active").lower()
            plan_id: str = str(plan.get("ratePlanID") or plan.get("id") or "")

            if status not in ("active", "1", "true", "enabled"):
                continue

            if any(kw in name for kw in ("bar", "standard", "best available", "rack")):
                self._rate_plan_id = plan_id
                logger.info("Selected rate plan: '%s' (id=%s)", name, plan_id)
                return

        # Fallback: just pick the first active plan
        for plan in plans:
            status = (plan.get("status") or "active").lower()
            plan_id = str(plan.get("ratePlanID") or plan.get("id") or "")
            if status in ("active", "1", "true", "enabled") and plan_id:
                self._rate_plan_id = plan_id
                logger.warning(
                    "No BAR/standard plan found — falling back to first active plan id=%s", plan_id
                )
                return

        logger.critical("No active rate plans found — cannot continue")
        sys.exit(1)

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

            for code, cfg in ROOM_TYPES.items():
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
                rates = self.client.get_rates(
                    room_type_id=rt["id"],
                    rate_plan_id=self._rate_plan_id,
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
                    current += timedelta(days=1)
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
                    reason = _rate_reason(code, current, occ_pct, days_out, ROOM_TYPES[code])
                    logger.info(
                        "%s %s: %s to $%.0f — %s (%.0f%% occ, %dd out)",
                        code, d_str, direction, new_rate, reason, occ_pct * 100, days_out,
                    )
                    try:
                        self.client.put_room_rate(
                            room_type_id=rt["id"],
                            rate_plan_id=self._rate_plan_id,
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

        payload = {
            "text": (
                f"*HSMI Pricing Engine — {self.today}*\n"
                f"{n} rate update{'s' if n != 1 else ''} pushed\n"
                f"{summary_lines}"
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
