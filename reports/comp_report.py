"""
HSMI Weekly Comp Report
=======================
Runs every Monday at 9am AEST (11pm UTC Sunday, cron '0 23 * * 0').
Makes 3 Booking.com API calls (one per night: Friday, Saturday, Sunday) via
RapidAPI and posts a pricing table to Slack #growth.

Single coordinates-based search centred on Hepburn Springs (lat -37.311,
lng 144.138, 15 km radius) covers both Hepburn Springs and Daylesford.

Environment variables:
  BOOKING_COM_API_KEY — RapidAPI key for Booking.com Hotels API (required)
  SLACK_WEBHOOK_URL   — Slack incoming webhook (required)
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), os.path.join(_REPO_ROOT, "pricing_engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
# Comp set definitions (mirrors competitor_signal.py)
# ---------------------------------------------------------------------------

# Mid-tier — same tier as HSMI. Prices from this set drive comp avg.
PRICING_COMPS = [
    "daylesford central",
    "daylesford motor",
    "mineral springs hotel",
    "central springs inn",
    "royal daylesford",
    "hotel frangos",
    "albert hotel",
    "daylesford hotel",
]

# Premium / reference — logged separately, excluded from comp avg.
REFERENCE_ONLY = [
    "hepburn at hepburn",
    "lake house",
    "hotel bellinzona",
    "bellinzona",
    "hepburn spa",
    "shizuka",
]

# Aliases that resolve to an already-listed property — suppress to avoid duplication.
_SKIP_ALIASES = ["wyndham", "albert motel"]

# Clean display names for the Slack table
_COMP_NAMES = {
    "daylesford central":    "Daylesford Central",
    "daylesford motor":      "Daylesford Motor Inn",
    "mineral springs hotel": "Mineral Springs Hotel",
    "central springs inn":   "Central Springs Inn",
    "royal daylesford":      "Royal Hotel Daylesford",
    "hotel frangos":         "Hotel Frangos",
    "albert hotel":          "Albert Hotel",
    "daylesford hotel":      "Daylesford Hotel",
}

_REF_NAMES = {
    "hepburn at hepburn": "Hepburn at Hepburn",
    "lake house":         "Lake House",
    "hotel bellinzona":   "Hotel Bellinzona",
    "bellinzona":         "Hotel Bellinzona",   # same property — deduped in output
    "hepburn spa":        "Hepburn Spa",
    "shizuka":            "Shizuka Ryokan",
}

# Booking.com Hotels API via RapidAPI
_RAPIDAPI_HOST   = "booking-com15.p.rapidapi.com"
_SEARCH_ENDPOINT = f"https://{_RAPIDAPI_HOST}/api/v1/hotels/searchHotels"
_DEST_ENDPOINT   = f"https://{_RAPIDAPI_HOST}/api/v1/hotels/searchDestination"

# Single coordinates search centred on Hepburn Springs.
# 15 km radius captures both Hepburn Springs and central Daylesford.
_SEARCH_LAT    = -37.311
_SEARCH_LNG    = 144.138
_SEARCH_RADIUS = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_friday(today: date) -> date:
    """Return the next Friday strictly after today."""
    days_ahead = (4 - today.weekday()) % 7  # Friday = weekday 4
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _parse_price(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace("A$", "").replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _classify(nl: str) -> tuple[str, str]:
    """Return (category, matched_keyword) for a lowercased property name."""
    if "hepburn springs motor" in nl:
        return "hsmi", "hsmi"
    if any(alias in nl for alias in _SKIP_ALIASES):
        return "skip", ""
    for kw in PRICING_COMPS:
        if kw in nl:
            return "pricing_comp", kw
    for kw in REFERENCE_ONLY:
        if kw in nl:
            return "reference", kw
    return "other", ""


# ---------------------------------------------------------------------------
# Booking.com API via RapidAPI
# ---------------------------------------------------------------------------

def _booking_headers(api_key: str) -> dict:
    return {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": _RAPIDAPI_HOST}


def _resolve_dest_id(api_key: str) -> tuple[Optional[str], str]:
    """Resolve Hepburn Springs / Daylesford region to a Booking.com dest_id."""
    try:
        resp = requests.get(
            _DEST_ENDPOINT,
            headers=_booking_headers(api_key),
            params={"query": "Hepburn Springs Daylesford Victoria Australia", "languagecode": "en-us"},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])
        if results:
            dest_id   = results[0].get("dest_id")
            dest_type = results[0].get("dest_type", "city")
            logger.info("Resolved region → dest_id=%s type=%s", dest_id, dest_type)
            return dest_id, dest_type
    except Exception as exc:
        logger.warning("dest_id resolution failed: %s", exc)
    return None, "city"


def _query_booking_night(
    api_key: str,
    checkin: date,
    dest_id: Optional[str],
    dest_type: str,
) -> dict:
    """Single Booking.com search for one night. Returns raw API response."""
    params = {
        "arrival_date":       checkin.strftime("%Y-%m-%d"),
        "departure_date":     (checkin + timedelta(days=1)).strftime("%Y-%m-%d"),
        "adults":             "2",
        "room_qty":           "1",
        "currency_code":      "AUD",
        "languagecode":       "en-us",
        "units":              "metric",
        "page_number":        "1",
        "filter_by_currency": "AUD",
        "locale":             "en-gb",
        "search_type":        "CITY",
    }
    if dest_id:
        params["dest_id"]   = dest_id
        params["dest_type"] = dest_type
    else:
        params["latitude"]  = str(_SEARCH_LAT)
        params["longitude"] = str(_SEARCH_LNG)
        params["radius"]    = str(_SEARCH_RADIUS)

    logger.info("Booking.com: %s", checkin.strftime("%a %d %b"))
    resp = requests.get(
        _SEARCH_ENDPOINT,
        headers=_booking_headers(api_key),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _process_night(data: dict) -> dict:
    """
    Process one night's Booking.com API response into a structured result.

    Parses the sold_out_percentage banner (mirrors the "X% of rooms sold in
    Daylesford" message shown at the top of Booking.com search results).
    99% sold = SOLD_OUT signal.

    Returns:
      regional_pct / regional_sold_pct / regional_signal / available / total
      sold_out_banner — raw % from Booking.com banner (or None)
      hsmi            — {price_str, price_num, sold} or None
      pricing_comps   — {keyword: {name, price_str, price_num, sold}}
      reference       — {keyword: {name, price_str, price_num, sold}}
      comp_avg / comp_count / hsmi_vs_comp_pct
    """
    # Top-level structure: {status, message, timestamp, data}
    # data structure: {hotels: [...], meta: [...], appear: [...]}
    # Each hotel: {hotel_id, accessibilityLabel, property: {...}}
    payload        = data.get("data", data)
    props          = payload.get("hotels", []) if isinstance(payload, dict) else []
    meta_raw   = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta_raw, dict):
        search_meta = meta_raw
    elif isinstance(meta_raw, list):
        search_meta = {k: v for d in meta_raw if isinstance(d, dict) for k, v in d.items()}
    else:
        search_meta = {}

    logger.info("  API returned %d hotel records", len(props))

    # Booking.com sold-out banner — 99% = SOLD_OUT
    banner_pct: Optional[float] = None
    raw_banner = search_meta.get("sold_out_percentage")
    if raw_banner is not None:
        try:
            banner_pct = float(raw_banner)
            logger.info("  Booking.com banner: %.0f%% sold", banner_pct)
        except (ValueError, TypeError):
            pass

    total = len(props)
    sold_out_count = 0
    for p in props:
        prop = p.get("property", p)
        if prop.get("soldout"):
            sold_out_count += 1

    available = total - sold_out_count

    if banner_pct is not None:
        sold_pct  = banner_pct
        avail_pct = round(100 - sold_pct)
    elif total > 0:
        avail_pct = round(available / total * 100)
        sold_pct  = 100 - avail_pct
    else:
        avail_pct = 100
        sold_pct  = 0

    if sold_pct >= 99:
        signal = "SOLD_OUT"
    elif sold_pct >= 96:
        signal = "CRITICAL"
    elif sold_pct >= 90:
        signal = "HIGH"
    else:
        signal = "NORMAL"

    hsmi: Optional[dict] = None
    pricing_comps: dict[str, dict] = {}
    reference: dict[str, dict] = {}

    for p in props:
        prop = p.get("property", p)  # all useful fields live in the "property" sub-object
        name = prop.get("name", "")
        nl   = name.lower()

        pb        = prop.get("priceBreakdown", {})
        price_raw = (
            pb.get("grossPrice", {}).get("value")
            or pb.get("allInclusivePrice")
            or prop.get("min_total_price")
            or prop.get("price")
        )
        price_num = _parse_price(price_raw)
        sold      = bool(prop.get("soldout"))
        price_str = "SOLD" if sold else (f"${price_num:.0f}" if price_num else "N/A")

        category, key = _classify(nl)
        entry = {
            "name":      name,
            "price_str": price_str,
            "price_num": None if sold else price_num,
            "sold":      sold,
        }

        if category == "hsmi":
            hsmi = entry
            logger.info("  HSMI : %s — %s", name, price_str)
        elif category == "pricing_comp":
            pricing_comps[key] = entry
            logger.info("  COMP : %s — %s", name, price_str)
        elif category == "reference":
            reference[key] = entry
            logger.info("  REF  : %s — %s", name, price_str)
        elif category == "skip":
            logger.debug("  SKIP : %s", name)
        else:
            logger.debug("  other: %s — %s", name, price_str)

    comp_prices = [v["price_num"] for v in pricing_comps.values() if v["price_num"]]
    comp_avg    = sum(comp_prices) / len(comp_prices) if comp_prices else None

    hsmi_price = hsmi["price_num"] if hsmi else None
    hsmi_vs_comp_pct = (
        round(((hsmi_price - comp_avg) / comp_avg) * 100)
        if hsmi_price and comp_avg
        else None
    )

    return {
        "regional_pct":      avail_pct,
        "regional_sold_pct": round(sold_pct),
        "regional_signal":   signal,
        "sold_out_banner":   banner_pct,
        "available":         available,
        "total":             total,
        "hsmi":              hsmi,
        "pricing_comps":     pricing_comps,
        "reference":         reference,
        "comp_avg":          comp_avg,
        "comp_count":        len(comp_prices),
        "hsmi_vs_comp_pct":  hsmi_vs_comp_pct,
    }


# ---------------------------------------------------------------------------
# Slack message builder
# ---------------------------------------------------------------------------

_NAME_COL  = 24
_PRICE_COL = 8   # each night column width


def _pval(nights: dict, night_key: str, comp_kw: str = "", ref_kw: str = "") -> str:
    """
    Safely retrieve a price string from a night result.

    Pass comp_kw for PRICING_COMPS, ref_kw for REFERENCE_ONLY,
    or neither for HSMI.
    """
    r = nights.get(night_key)
    if r is None:
        return "—"
    if "error" in r:
        return "ERR"
    if comp_kw:
        return r.get("pricing_comps", {}).get(comp_kw, {}).get("price_str", "—")
    if ref_kw:
        return r.get("reference", {}).get(ref_kw, {}).get("price_str", "—")
    h = r.get("hsmi")
    return h["price_str"] if h else "—"


def _row(name: str, fri: str, sat: str, sun: str) -> str:
    return f"{name:<{_NAME_COL}}{fri:>{_PRICE_COL}}{sat:>{_PRICE_COL}}{sun:>{_PRICE_COL}}"


def _build_message(friday: date, nights: dict) -> str:
    saturday = friday + timedelta(days=1)
    sunday   = friday + timedelta(days=2)

    fri_hdr = friday.strftime("%a %-d")
    sat_hdr = saturday.strftime("%a %-d")
    sun_hdr = sunday.strftime("%a %-d")

    sep = "─" * (_NAME_COL + _PRICE_COL * 3)

    lines: list[str] = [
        f"*HSMI Weekly Comp Report — {datetime.now(_MELB).strftime('%a %d %b %Y')}*",
        f"_Coming weekend: {friday.strftime('%a %d %b')} · {saturday.strftime('%a %d %b')} · {sunday.strftime('%a %d %b')}_",
        "",
        "```",
        _row("Property", fri_hdr, sat_hdr, sun_hdr),
        sep,
    ]

    # HSMI row
    lines.append(_row(
        "HSMI (ours)",
        _pval(nights, "fri"),
        _pval(nights, "sat"),
        _pval(nights, "sun"),
    ))
    lines.append(sep)

    # Mid-tier comp rows (all PRICING_COMPS in defined order)
    for kw in PRICING_COMPS:
        display = _COMP_NAMES.get(kw, kw.title())
        fri_v = _pval(nights, "fri", comp_kw=kw)
        sat_v = _pval(nights, "sat", comp_kw=kw)
        sun_v = _pval(nights, "sun", comp_kw=kw)
        lines.append(_row(display, fri_v, sat_v, sun_v))

    lines.append(sep)

    # Summary rows
    def avg_str(nk: str) -> str:
        avg = nights.get(nk, {}).get("comp_avg") if "error" not in nights.get(nk, {}) else None
        return f"${avg:.0f}" if avg else "N/A"

    def diff_str(nk: str) -> str:
        d = nights.get(nk, {}).get("hsmi_vs_comp_pct") if "error" not in nights.get(nk, {}) else None
        return f"{d:+d}%" if d is not None else "N/A"

    lines.append(_row("Mid-tier avg", avg_str("fri"), avg_str("sat"), avg_str("sun")))
    lines.append(_row("HSMI vs avg",  diff_str("fri"), diff_str("sat"), diff_str("sun")))
    lines.append("```")
    lines.append("")

    # Saturday regional availability signal (from Booking.com banner)
    sat_r = nights.get("sat", {})
    if sat_r and "error" not in sat_r:
        sold_pct    = sat_r.get("regional_sold_pct", "?")
        sig         = sat_r.get("regional_signal", "?")
        banner      = sat_r.get("sold_out_banner")
        signal_emoji = {"SOLD_OUT": "🔴", "CRITICAL": "🟠", "HIGH": "🟡", "NORMAL": "🟢"}.get(sig, "⚪")
        banner_note  = f" _(Booking.com: {banner:.0f}% sold)_" if banner is not None else ""
        sold_flag    = " 🔥" if isinstance(sold_pct, (int, float)) and sold_pct >= 99 else ""
        lines.append(f"*Saturday regional:* {sold_pct}% rooms sold{sold_flag}{banner_note} — {signal_emoji} {sig}")
        lines.append("")

    # Premium benchmarks (reference only)
    seen_ref: set[str] = set()
    ref_lines: list[str] = []
    for kw in REFERENCE_ONLY:
        display = _REF_NAMES.get(kw, kw.title())
        if display in seen_ref:
            continue  # deduplicate bellinzona aliases
        fri_v = _pval(nights, "fri", ref_kw=kw)
        sat_v = _pval(nights, "sat", ref_kw=kw)
        sun_v = _pval(nights, "sun", ref_kw=kw)
        if any(v not in ("—", "ERR") for v in (fri_v, sat_v, sun_v)):
            ref_lines.append(f"  {display}: Fri {fri_v} · Sat {sat_v} · Sun {sun_v}")
            seen_ref.add(display)

    if ref_lines:
        lines.append("*Premium benchmarks (reference only — not used in pricing)*")
        lines.extend(ref_lines)

    lines.append("")
    lines.append("_ℹ️ Rates sourced from Booking.com. Direct bookings save guests nothing but save HSMI ~17% commission._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    api_key = os.environ.get("BOOKING_COM_API_KEY", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    missing = [v for v, k in [("BOOKING_COM_API_KEY", api_key), ("SLACK_WEBHOOK_URL", webhook)] if not k]
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    today  = datetime.now(_MELB).date()
    friday = _next_friday(today)
    logger.info(
        "=== HSMI Weekly Comp Report (Booking.com) — %s | Weekend: %s/%s/%s ===",
        today,
        friday,
        friday + timedelta(days=1),
        friday + timedelta(days=2),
    )

    dest_id, dest_type = _resolve_dest_id(api_key)

    nights: dict = {}
    for night_key, night_date in [
        ("fri", friday),
        ("sat", friday + timedelta(days=1)),
        ("sun", friday + timedelta(days=2)),
    ]:
        logger.info("--- %s ---", night_date.strftime("%a %d %b"))
        try:
            raw = _query_booking_night(api_key, night_date, dest_id, dest_type)
            nights[night_key] = _process_night(raw)
        except Exception as exc:
            logger.error("Query failed for %s: %s", night_date, exc)
            nights[night_key] = {"error": str(exc)}

    message = _build_message(friday, nights)

    try:
        resp = requests.post(webhook, json={"text": message, "username": "Ops Agent", "icon_emoji": ":bar_chart:"}, timeout=15)
        resp.raise_for_status()
        logger.info("Weekly comp report posted to Slack")
    except requests.RequestException as exc:
        logger.error("Slack post failed: %s — printing report to stdout", exc)
        print(message)
        sys.exit(1)

    logger.info("=== Weekly Comp Report complete ===")


if __name__ == "__main__":
    run()
