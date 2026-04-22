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
  SLACK_PRICING_WEBHOOK_URL — Incoming webhook for #api-pricing-engine (optional)
  ANTHROPIC_API_KEY       — Reserved for future AI-assisted pricing (optional)

Usage:
  python pricing_engine/pricing_engine.py
"""

import json
import logging
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytz
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
AEST = pytz.timezone("Australia/Melbourne")

# Occupancy bracket multipliers applied on top of the day-of-week base rate.
# Each tuple: (upper_bound_inclusive, multiplier, label)
# Iterated from lowest to highest; first bracket where occ_pct <= upper is used.
_OCC_BRACKETS: list[tuple[float, float, str]] = [
    (0.10, 0.85, "0-10%"),
    (0.25, 0.92, "11-25%"),
    (0.40, 1.00, "26-40%"),
    (0.55, 1.08, "41-55%"),
    (0.70, 1.15, "56-70%"),
    (0.85, 1.25, "71-85%"),
    (1.01, 1.35, "86-100%"),
]

# AFTERNOON window (12pm–5pm AEST): discount urgency +5pp, rate increases capped at +20%.
_OCC_BRACKETS_AFTERNOON: list[tuple[float, float, str]] = [
    (0.10, 0.80, "0-10%"),
    (0.25, 0.87, "11-25%"),
    (0.40, 0.95, "26-40%"),
    (0.55, 1.08, "41-55%"),
    (0.70, 1.15, "56-70%"),
    (0.85, 1.20, "71-85%"),   # capped at +20%
    (1.01, 1.20, "86-100%"),  # capped at +20%
]


def _get_time_window() -> tuple[str, str]:
    """
    Return (window_name, time_str) based on current Australia/Melbourne time.

    Windows:
      MORNING   — before 12:00 AEST (full brackets active)
      AFTERNOON — 12:00–16:59 AEST (capped increases, extra discount urgency)
      EVENING   — 17:00+ AEST (floor or hold only for same-day pricing)
    """
    now_aest = datetime.now(tz=AEST)
    time_str = now_aest.strftime("%H:%M")
    hour = now_aest.hour
    if hour < 12:
        return "MORNING", time_str
    elif hour < 17:
        return "AFTERNOON", time_str
    else:
        return "EVENING", time_str


def _occ_bracket(occ_pct: float, brackets: list[tuple[float, float, str]]) -> tuple[float, str]:
    """Return (multiplier, label) for the given occupancy percentage."""
    for upper, mult, label in brackets:
        if occ_pct <= upper:
            return mult, label
    return brackets[-1][1], brackets[-1][2]


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
        self.slack_webhook = os.environ.get("SLACK_PRICING_WEBHOOK_URL", "").strip()
        self.today = date.today()

        # Daily base run at 20:00 UTC (6am AEST) uses full 60-day window.
        # All other runs (hourly) use a 14-day window to stay fast.
        utc_hour = datetime.utcnow().hour
        self.is_base_run = (utc_hour == 20)
        lookahead = LOOKAHEAD_DAYS if self.is_base_run else 14
        self.end_date = self.today + timedelta(days=lookahead)
        logger.info(
            "Run type: %s (UTC hour=%d) — lookahead=%d days",
            "DAILY BASE" if self.is_base_run else "HOURLY",
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
        self._rate_reasons: dict[str, dict[str, str]] = {}    # date_str → {code: bracket_label}
        self._competitor_signals: dict[str, dict] = {}         # date_str → signal dict from cache
        # Each entry: {code, date, old_rate, new_rate, direction, bracket}
        self._updates_pushed: list[dict] = []

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
            self._fetch_current_rates()   # must precede _calculate_rates (EVENING window needs live rates)
            self._load_competitor_cache()
            self._calculate_rates()
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
    # Step 4: Fetch current rates from Cloudbeds
    # (must run before _calculate_rates so EVENING window can hold live rates)
    # ------------------------------------------------------------------

    def _fetch_current_rates(self) -> None:
        """
        Pull the rates currently live in Cloudbeds for comparison.
        Populates ``self._current_rates``.
        """
        logger.info("Step 4: Fetching current rates from Cloudbeds")
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
    # Competitor cache
    # ------------------------------------------------------------------

    def _load_competitor_cache(self) -> None:
        """
        Load competitor signal cache written by competitor_signal.py.

        Cache is valid for 24 hours. If the file is missing (hourly runs on
        ephemeral runners never have it) or stale, log a warning and leave
        ``self._competitor_signals`` empty — occupancy brackets apply unchanged.
        """
        cache_path = Path(_HERE) / "competitor_cache.json"

        if not cache_path.exists():
            logger.info("Competitor cache not found — competitor adjustment disabled for this run")
            return

        try:
            data = json.loads(cache_path.read_text())
            updated_at_str = data.get("updated_at", "")
            updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            age_hours = (datetime.now(tz=timezone.utc) - updated_at).total_seconds() / 3600

            if age_hours > 24:
                logger.warning(
                    "Competitor cache is %.1fh old (limit 24h) — skipping competitor adjustment",
                    age_hours,
                )
                return

            self._competitor_signals = data.get("signals", {})
            logger.info(
                "Competitor cache loaded: %.1fh old, signals for %s",
                age_hours,
                ", ".join(self._competitor_signals.keys()) or "none",
            )
            for d_str, sig in self._competitor_signals.items():
                if "error" in sig:
                    logger.warning("  %s: cached error — %s", d_str, sig["error"])
                else:
                    logger.info(
                        "  %s: %s | HSMI %s | comp avg %s | HSMI vs comp %s%%",
                        d_str,
                        sig.get("regional_signal", "?"),
                        f"A${sig['hsmi_price']:.0f}" if sig.get("hsmi_price") else "N/A",
                        f"A${sig['comp_avg']:.0f}" if sig.get("comp_avg") else "N/A",
                        sig.get("hsmi_vs_comp_pct", "N/A"),
                    )
        except Exception as exc:
            logger.warning("Failed to load competitor cache: %s — skipping competitor adjustment", exc)

    def _competitor_multiplier_for(self, d_str: str, occ_rate: float) -> tuple[float, str]:
        """
        Return (multiplier, label) from the competitor cache for a specific date.

        Comparison is between ``occ_rate`` (the occupancy-bracket rate we plan
        to set) and the competitor average visible on Google Hotels.

        Returns (1.00, "") if no signal is available for this date.
        """
        sig = self._competitor_signals.get(d_str)
        if not sig or "error" in sig:
            return 1.00, ""

        rs = sig.get("regional_signal", "NORMAL")

        if rs == "SOLD_OUT":
            return 1.35, "regional SOLD_OUT"
        elif rs == "CRITICAL":
            return 1.20, "regional CRITICAL"
        elif rs == "HIGH":
            return 1.10, "regional HIGH"
        else:
            # NORMAL: compare our planned rate vs comp average
            comp_avg = sig.get("comp_avg")
            if comp_avg and occ_rate > 0:
                ratio = occ_rate / comp_avg
                if ratio < 0.85:
                    return 1.08, f"underpriced vs comp avg A${comp_avg:.0f}"
                elif ratio < 1.00:
                    return 1.05, f"below comp avg A${comp_avg:.0f}"
            return 1.00, ""

    # ------------------------------------------------------------------
    # Step 5: Calculate target rates
    # ------------------------------------------------------------------

    def _calculate_rates(self) -> dict[str, dict[str, float]]:
        """
        Apply the occupancy bracket multiplier system for every room type × date.

        Bracket multipliers are applied on top of the day-of-week base rate
        (midweek / weekend / peak from Notion tiers).

        Special cases:
          - Peak dates: use peak base rate, then apply occupancy bracket.
          - Weekend 15+ days out: hold weekend base rate unchanged (too far
            out to react meaningfully to current occupancy).
          - Same-day EVENING (17:00+ AEST):
              occ > 85% → hold current live rate
              occ ≤ 85% → set to floor

        Returns
        -------
        dict
            ``{date_str: {room_type_code: target_rate}}``
        """
        logger.info("Step 5: Calculating target rates")

        time_window, time_str = _get_time_window()
        window_desc = {
            "MORNING":   "full brackets active",
            "AFTERNOON": "capped increases (+20% max), discount urgency +5pp",
            "EVENING":   "floor or hold only for same-day",
        }[time_window]
        logger.info("Time window: %s (%s AEST) — %s", time_window, time_str, window_desc)

        current = self.today
        while current < self.end_date:
            d_str = current.strftime("%Y-%m-%d")
            days_out = (current - self.today).days
            is_weekend = current.weekday() in WEEKEND_DAYS
            is_same_day = (days_out == 0)
            self._target_rates[d_str] = {}

            for code, cfg in self.room_types.items():
                if code not in self._room_type_map:
                    continue

                occ_pct = self._occupancy.get(d_str, {}).get(code, 0.0)
                floor_  = cfg["floor"]
                ceiling_ = cfg["ceiling"]

                # ── Special case: weekend 15+ days — hold base, skip brackets ──
                if is_weekend and not is_peak_date(current) and days_out >= 15:
                    rate = max(floor_, min(ceiling_, round(cfg["weekend"])))
                    self._target_rates[d_str][code] = rate
                    self._rate_reasons.setdefault(d_str, {})[code] = "weekend hold"
                    logger.debug(
                        "%s %s: $%.0f weekend base (15+ days out — hold, no occ bracket)",
                        code, d_str, rate,
                    )
                    continue

                # ── Special case: same-day EVENING ──
                if is_same_day and time_window == "EVENING":
                    if occ_pct > 0.85:
                        live = self._current_rates.get(d_str, {}).get(code)
                        if live is not None:
                            rate = max(floor_, min(ceiling_, round(live)))
                            reason = f"EVENING: occ >85% — hold live rate ${live:.0f}"
                        else:
                            # No live rate available — hold base as safe fallback
                            base = cfg["peak"] if is_peak_date(current) else (cfg["weekend"] if is_weekend else cfg["midweek"])
                            rate = max(floor_, min(ceiling_, round(base)))
                            reason = "EVENING: occ >85% — hold base (live rate unavailable)"
                        self._rate_reasons.setdefault(d_str, {})[code] = "EVENING hold"
                    else:
                        rate = floor_
                        reason = "EVENING: occ ≤85% — floor"
                        self._rate_reasons.setdefault(d_str, {})[code] = "EVENING floor"
                    self._target_rates[d_str][code] = rate
                    logger.info(
                        "%s %s: $%.0f (%.0f%% occ | %s)",
                        code, d_str, rate, occ_pct * 100, reason,
                    )
                    continue

                # ── Determine base rate ──
                if is_peak_date(current):
                    base_rate = cfg["peak"]
                    base_label = "peak"
                elif is_weekend:
                    base_rate = cfg["weekend"]
                    base_label = "weekend"
                else:
                    base_rate = cfg["midweek"]
                    base_label = "midweek"

                # ── Select bracket table ──
                if is_same_day and time_window == "AFTERNOON":
                    brackets = _OCC_BRACKETS_AFTERNOON
                else:
                    brackets = _OCC_BRACKETS

                multiplier, bracket_label = _occ_bracket(occ_pct, brackets)

                raw_rate   = base_rate * multiplier
                rounded    = round(raw_rate)
                final_rate = max(floor_, min(ceiling_, rounded))

                # Build log suffix describing any clamp
                if final_rate < rounded:
                    clamp_note = f"→ capped at ${final_rate:.0f} ceiling"
                elif final_rate > rounded:
                    clamp_note = f"→ floored at ${final_rate:.0f} floor"
                else:
                    clamp_note = f"→ ${final_rate:.0f}"

                logger.info(
                    "%s %s: $%.0f %s × %.2f (%s occ) = $%.0f %s",
                    code, d_str,
                    base_rate, base_label,
                    multiplier, bracket_label,
                    raw_rate,
                    clamp_note,
                )

                # ── Apply competitor signal (if cache is loaded for this date) ──
                comp_mult, comp_label = self._competitor_multiplier_for(d_str, final_rate)
                if comp_mult != 1.00:
                    comp_raw = final_rate * comp_mult
                    comp_rounded = round(comp_raw)
                    comp_final = max(floor_, min(ceiling_, comp_rounded))
                    if comp_final < comp_rounded:
                        comp_note = f"→ capped at ${comp_final:.0f} ceiling"
                    elif comp_final > comp_rounded:
                        comp_note = f"→ floored at ${comp_final:.0f} floor"
                    else:
                        comp_note = f"→ ${comp_final:.0f}"
                    logger.info(
                        "%s %s: competitor × %.2f (%s) = $%.0f %s",
                        code, d_str, comp_mult, comp_label, comp_raw, comp_note,
                    )
                    final_rate = comp_final

                reason = f"{bracket_label} occ"
                if comp_mult != 1.00:
                    reason += f" + comp {comp_label}"
                self._rate_reasons.setdefault(d_str, {})[code] = reason
                self._target_rates[d_str][code] = final_rate

            current += timedelta(days=1)

        return self._target_rates

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
                    logger.info(
                        "%s %s: %s to $%.0f (%.0f%% occ, %dd out)",
                        code, d_str, direction, new_rate, occ_pct * 100, days_out,
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
                            self._updates_pushed.append({
                                "code":      code,
                                "date":      current,
                                "old_rate":  current_rate,
                                "new_rate":  new_rate,
                                "direction": direction,
                                "bracket":   self._rate_reasons.get(d_str, {}).get(code, ""),
                            })
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
        """
        Post a Slack message after each run.

        Base runs (6am AEST): always post the health check.  If rates were also
        updated, a brief count is appended so the operator knows without burying
        the snapshot under a long change list.
        Hourly runs: silent unless at least one rate was pushed, then post the
        arrow-format change list.
        """
        if not self.slack_webhook:
            logger.warning("SLACK_PRICING_WEBHOOK_URL not set — skipping Slack notification")
            return

        n = len(self._updates_pushed)

        if not self.is_base_run and n == 0:
            logger.info("No rate changes — skipping Slack notification (hourly run)")
            return

        if self.is_base_run:
            text = self._build_health_check_message()
            if n > 0:
                text += f"\n_Also pushed {n} rate update{'s' if n != 1 else ''} — see run logs_"
            label = f"health check (+{n} updates)" if n > 0 else "health check"
        else:
            text  = self._build_rate_changes_message(n)
            label = f"{n} updates"

        try:
            response = requests.post(self.slack_webhook, json={"text": text}, timeout=10)
            response.raise_for_status()
            logger.info("Slack notification sent (%s)", label)
        except requests.RequestException as exc:
            logger.warning("Failed to send Slack notification: %s", exc)

    def _build_health_check_message(self) -> str:
        """Daily health check posted when the base run finds no rate changes needed."""
        n_dates = (self.end_date - self.today).days
        n_rooms = len(self._room_type_map)
        total_capacity = sum(rt["total_rooms"] for rt in self._room_type_map.values())

        lines = [
            f"*HSMI Pricing Engine — {self.today.strftime('%a %d %b %Y')}*",
            f"✅ Daily base run complete — all rates already optimal",
            f"_{n_dates} dates checked · {n_rooms} room types_",
            f"",
            f"*Occupancy snapshot — next 7 days*",
        ]

        alerts: list[str] = []

        for i in range(7):
            d = self.today + timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            day_label = d.strftime("%a %d %b")
            occ_data = self._occupancy.get(d_str, {})

            # Property-wide occupancy for this date
            booked = sum(
                round(occ_data.get(code, 0.0) * rt["total_rooms"])
                for code, rt in self._room_type_map.items()
            )
            overall_pct = booked / total_capacity * 100 if total_capacity else 0

            # Room types at ≥70% (entering the +25% / +35% bracket territory)
            high = [
                f"{code} {occ_data.get(code, 0.0) * 100:.0f}%"
                for code in sorted(self._room_type_map)
                if occ_data.get(code, 0.0) >= 0.70
            ]
            high_str = "  ⚠️ " + ", ".join(high) if high else ""
            lines.append(f"  {day_label}   {overall_pct:3.0f}% full{high_str}")

            # Collect dates with any room type ≥70% for the alert block
            if high:
                alerts.append(f"  {day_label}: {', '.join(high)}")

        if alerts:
            lines += ["", "*High occupancy (≥70%) in next 7 days*"]
            lines.extend(alerts)

        lines += ["", f"📋 https://www.notion.so/349c905ced6b81d1be30d33aa3cf15eb"]
        return "\n".join(lines)

    def _build_rate_changes_message(self, n: int) -> str:
        """Rate-change summary posted whenever updates were pushed."""
        sorted_updates = sorted(self._updates_pushed, key=lambda u: (u["date"], u["code"]))
        shown = sorted_updates[:20]

        lines = []
        for u in shown:
            arrow  = "↑" if u["direction"] in ("raised", "set") else "↓"
            new    = u["new_rate"]
            old    = u["old_rate"]
            date_s = u["date"].strftime("%a %d %b")

            if old is not None and old > 0:
                pct    = (new - old) / old * 100
                change = f"${old:.0f} → ${new:.0f} ({pct:+.0f}%)"
            else:
                # Old rate not available from Cloudbeds — show new rate only
                change = f"→ ${new:.0f}"

            suffix = f" — {u['bracket']}" if u["bracket"] else ""
            lines.append(f"{arrow} {u['code']} {date_s}: {change}{suffix}")

        summary = "\n".join(lines)
        if n > 20:
            summary += f"\n… and {n - 20} more"

        enabled = sum(1 for rt in self._room_type_map.values() if rt.get("rate_id"))
        total   = len(self._room_type_map)
        push_status = (
            f"✅ {enabled}/{total} room types active"
            if enabled
            else "⛔ No rateIDs — no updates pushed"
        )

        return (
            f"*HSMI Pricing Engine — {self.today}*\n"
            f"_{n} rate update{'s' if n != 1 else ''} pushed — {push_status}_\n\n"
            f"{summary}\n\n"
            f"📋 https://www.notion.so/349c905ced6b81d1be30d33aa3cf15eb"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PricingEngine().run()
