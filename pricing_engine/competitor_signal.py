"""
HSMI Competitor Signal
======================
Queries Booking.com (via RapidAPI) for regional accommodation pricing and
availability around Hepburn Springs / Daylesford. Runs once per day at 9am
AEST via GitHub Actions cron.

Uses a single coordinates-based search per date, centred on Hepburn Springs
(lat -37.311, lng 144.138, radius 15 km). This covers both Hepburn Springs
and Daylesford in one call without geography bleed from two separate queries.

Availability signal
-------------------
Booking.com shows a "X% of rooms sold in Daylesford" banner at the top of
search results when demand is high. The API response includes a
`sold_out_percentage` metadata field mirroring this banner. When it reaches
99% that is treated here as SOLD_OUT — the whole mid-tier market is gone.
We also infer it from available vs total properties as a fallback.

Signal thresholds:
  sold_out_pct >= 99%  → SOLD_OUT  (push all room types to ceiling)
  sold_out_pct >= 96%  → CRITICAL  (×1.20 competitor multiplier)
  sold_out_pct >= 90%  → HIGH      (×1.10 competitor multiplier)
  otherwise            → NORMAL    (check HSMI vs comp avg nudge)

Output: competitor_cache.json — same schema as prior SerpApi version so the
pricing engine reads it without changes.

Environment variables:
  BOOKING_COM_API_KEY       — RapidAPI key for Booking.com Hotels API (required)
  SLACK_PRICING_WEBHOOK_URL — Slack webhook for #api-pricing-engine (optional)
  CLOUDBEDS_API_KEY         — Cloudbeds API key (optional; enables HSMI price fallback)
  CLOUDBEDS_PROPERTY_ID     — Cloudbeds property ID (optional)
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "shared"), _HERE):
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
# Constants
# ---------------------------------------------------------------------------
CACHE_PATH = Path(_HERE) / "competitor_cache.json"

# Booking.com Hotels API via RapidAPI
_RAPIDAPI_HOST = "booking-com15.p.rapidapi.com"
_SEARCH_ENDPOINT = f"https://{_RAPIDAPI_HOST}/api/v1/hotels/searchHotels"
_DEST_ENDPOINT   = f"https://{_RAPIDAPI_HOST}/api/v1/hotels/searchDestination"

# Single coordinates search centred on Hepburn Springs.
# 15 km radius captures all of Hepburn Springs proper + central Daylesford.
_SEARCH_LAT    = -37.311
_SEARCH_LNG    = 144.138
_SEARCH_RADIUS = 15  # km

# Mid-tier comps — same tier as HSMI. Used for comp avg + multiplier logic.
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

# Premium / reference-only — tracked and logged but NOT in pricing calc.
REFERENCE_ONLY = [
    "hepburn at hepburn",
    "lake house",
    "hotel bellinzona",
    "bellinzona",
    "hepburn spa",
    "shizuka",
    "dudley boutique",
    "daylesford art motel",
]

_SKIP_ALIASES = ["wyndham", "albert motel"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _target_dates(today: date) -> tuple[date, date]:
    """
    Return (date1, date2) for the coming weekend.
    Normal: upcoming Friday + Saturday of same weekend.
    If today IS Friday: Friday = today.
    If today IS Saturday: today + next Friday (next weekend).
    """
    weekday = today.weekday()  # Mon=0 … Sat=5, Sun=6
    if weekday == 5:
        return today, today + timedelta(days=6)
    days_to_fri = (4 - weekday) % 7
    friday = today + timedelta(days=days_to_fri)
    return friday, friday + timedelta(days=1)


def _parse_price(val) -> Optional[float]:
    if val is None:
        return None
    try:
        s = str(val).replace("A$", "").replace("$", "").replace(",", "").strip()
        return float(s)
    except (ValueError, TypeError):
        return None


def _classify(nl: str) -> str:
    """Classify a lowercased property name."""
    if "hepburn springs motor" in nl:
        return "hsmi"
    if any(alias in nl for alias in _SKIP_ALIASES):
        return "skip"
    if any(kw in nl for kw in PRICING_COMPS):
        return "pricing_comp"
    if any(kw in nl for kw in REFERENCE_ONLY):
        return "reference"
    return "other"


# ---------------------------------------------------------------------------
# Booking.com API via RapidAPI
# ---------------------------------------------------------------------------

def _booking_headers(api_key: str) -> dict:
    return {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": _RAPIDAPI_HOST,
    }


def _resolve_dest_id(api_key: str, query: str) -> Optional[str]:
    """
    Resolve a location string to a Booking.com dest_id.
    Returns the first result's dest_id, or None on failure.
    """
    try:
        resp = requests.get(
            _DEST_ENDPOINT,
            headers=_booking_headers(api_key),
            params={"query": query, "languagecode": "en-us"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", [])
        if results:
            dest_id = results[0].get("dest_id")
            dest_type = results[0].get("dest_type", "city")
            logger.info("Resolved '%s' → dest_id=%s type=%s", query, dest_id, dest_type)
            return dest_id, dest_type
    except Exception as exc:
        logger.warning("dest_id resolution failed for '%s': %s", query, exc)
    return None, None


def _search_booking(
    api_key: str,
    checkin: date,
    checkout: date,
    dest_id: Optional[str] = None,
    dest_type: str = "city",
) -> dict:
    """
    Search Booking.com for 2-adult, 1-room stays in the Hepburn/Daylesford
    region. Uses dest_id if provided, otherwise falls back to lat/lng.

    Returns the raw API response dict.
    """
    params = {
        "arrival_date":    checkin.strftime("%Y-%m-%d"),
        "departure_date":  checkout.strftime("%Y-%m-%d"),
        "adults":          "2",
        "room_qty":        "1",
        "currency_code":   "AUD",
        "languagecode":    "en-us",
        "units":           "metric",
        "page_number":     "0",
        "filter_by_currency": "AUD",
        "locale":          "en-gb",
        "search_type":     "CITY",
    }

    if dest_id:
        params["dest_id"]   = dest_id
        params["dest_type"] = dest_type
    else:
        # Coordinate fallback — centred on Hepburn Springs
        params["latitude"]  = str(_SEARCH_LAT)
        params["longitude"] = str(_SEARCH_LNG)
        params["radius"]    = str(_SEARCH_RADIUS)

    logger.info(
        "Booking.com search: %s → %s (dest_id=%s)",
        checkin, checkout, dest_id or "coords",
    )
    resp = requests.get(
        _SEARCH_ENDPOINT,
        headers=_booking_headers(api_key),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Parse Booking.com response → competitor signal
# ---------------------------------------------------------------------------

def _process_booking_response(data: dict) -> dict:
    """
    Extract competitor signal from a Booking.com API response.

    Booking.com returns a `sold_out_percentage` field in the search metadata
    that mirrors the "X% of rooms sold in Daylesford" banner shown on their
    website. When this hits 99% it's the strongest possible demand signal for
    the region.

    Falls back to computing availability % from individual property records
    if the metadata field is absent.

    Returns a dict matching the existing competitor_cache.json schema so the
    pricing engine needs no changes.
    """
    # Top-level structure: {status, message, timestamp, data}
    # data structure: {hotels: [...], meta: {...}, appear: [...]}
    # Each hotel: {hotel_id, accessibilityLabel, property: {...}}
    payload        = data.get("data", data)
    search_meta    = payload.get("meta", {}) if isinstance(payload, dict) else {}
    properties_raw = payload.get("hotels", []) if isinstance(payload, dict) else []

    logger.info("  Raw response top keys: %s", list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
    if isinstance(payload, dict):
        logger.info("  meta keys: %s", list(search_meta.keys()) if search_meta else "empty")
        logger.info("  hotels count: %d | appear count: %d", len(properties_raw), len(payload.get("appear", [])))
        if properties_raw:
            logger.info("  First hotel keys: %s", list(properties_raw[0].keys()))
            if "property" in properties_raw[0]:
                logger.info("  First property keys: %s", list(properties_raw[0]["property"].keys())[:10])
    logger.info("  API returned %d hotel records", len(properties_raw))

    # --- Booking.com sold-out banner ---
    # `sold_out_percentage` in data.meta mirrors the top-of-page banner
    # e.g. "98% of rooms sold in Daylesford". 99% → SOLD_OUT signal.
    banner_pct: Optional[float] = None
    raw_banner = search_meta.get("sold_out_percentage")
    if raw_banner is not None:
        try:
            banner_pct = float(raw_banner)
            logger.info("  Booking.com banner: %.0f%% of rooms sold in region", banner_pct)
        except (ValueError, TypeError):
            pass

    # --- Property-level availability ---
    # Each item has a "property" sub-object with name, price, soldout flag.
    total = len(properties_raw)
    sold_out_count = 0
    for p in properties_raw:
        prop = p.get("property", p)  # fall back to p itself if no sub-object
        if prop.get("soldout"):
            sold_out_count += 1

    available = total - sold_out_count

    # Prefer Booking.com banner pct; fall back to computed
    if banner_pct is not None:
        sold_pct    = banner_pct
        avail_pct   = round(100 - sold_pct)
        source_note = "booking_banner"
    elif total > 0:
        avail_pct   = round(available / total * 100)
        sold_pct    = 100 - avail_pct
        source_note = "computed"
    else:
        avail_pct   = 100
        sold_pct    = 0
        source_note = "no_data"

    # --- Signal classification ---
    # 99% sold (Booking.com banner hitting 99%) = SOLD_OUT
    if sold_pct >= 99:
        regional_signal = "SOLD_OUT"
    elif sold_pct >= 96:
        regional_signal = "CRITICAL"
    elif sold_pct >= 90:
        regional_signal = "HIGH"
    else:
        regional_signal = "NORMAL"

    logger.info(
        "  Regional: %d%% sold (%s) → %s | %d/%d properties available",
        sold_pct, source_note, regional_signal, available, total,
    )

    # --- Per-property classification ---
    hsmi_price:     Optional[float] = None
    comp_prices:    list[float]     = []
    comp_props:     list[dict]      = []
    reference_props: list[dict]     = []
    found_comp_kws: set[str]        = set()

    for p in properties_raw:
        prop = p.get("property", p)  # all useful fields live in the "property" sub-object
        name = prop.get("name", "")
        nl   = name.lower()

        # Price: try priceBreakdown.grossPrice first, then common fallbacks
        pb        = prop.get("priceBreakdown", {})
        price_raw = (
            pb.get("grossPrice", {}).get("value")
            or pb.get("allInclusivePrice")
            or prop.get("min_total_price")
            or prop.get("price")
        )
        price_num = _parse_price(price_raw)

        sold_tag = " [SOLD OUT]" if prop.get("soldout") else ""

        kind = _classify(nl)

        if kind == "hsmi":
            hsmi_price = price_num
            logger.info("  HSMI : %s — A$%s%s", name, f"{price_num:.0f}" if price_num else "N/A", sold_tag)

        elif kind == "pricing_comp":
            for kw in PRICING_COMPS:
                if kw in nl:
                    found_comp_kws.add(kw)
                    break
            if price_num:
                comp_prices.append(price_num)
                comp_props.append({"name": name, "price": price_num})
            logger.info("  COMP : %s — A$%s%s", name, f"{price_num:.0f}" if price_num else "N/A", sold_tag)

        elif kind == "reference":
            reference_props.append({
                "name":      name,
                "price_str": f"A${price_num:.0f}" if price_num else "N/A",
            })
            logger.info("  REF  : %s — A$%s%s", name, f"{price_num:.0f}" if price_num else "N/A", sold_tag)

        elif kind == "skip":
            logger.debug("  SKIP : %s (alias suppressed)", name)
        else:
            logger.debug("  other: %s — A$%s", name, f"{price_num:.0f}" if price_num else "N/A")

    missing_comp_kws = [kw for kw in PRICING_COMPS if kw not in found_comp_kws]
    if missing_comp_kws:
        logger.info("  Comps NOT found: %s", missing_comp_kws)
    else:
        logger.info("  All %d PRICING_COMPS found", len(PRICING_COMPS))

    comp_avg = sum(comp_prices) / len(comp_prices) if comp_prices else None
    hsmi_vs_comp_pct = (
        round(((hsmi_price - comp_avg) / comp_avg) * 100)
        if hsmi_price and comp_avg
        else None
    )

    logger.info(
        "  HSMI: %s | Mid-tier avg: %s",
        f"A${hsmi_price:.0f}" if hsmi_price else "not found",
        f"A${comp_avg:.0f}" if comp_avg else "N/A",
    )

    return {
        "regional_pct":      avail_pct,
        "regional_sold_pct": round(sold_pct),
        "regional_signal":   regional_signal,
        "signal_source":     source_note,
        "hsmi_price":        hsmi_price,
        "hsmi_source":       "booking_com",
        "comp_avg":          round(comp_avg, 2) if comp_avg else None,
        "comp_min":          min(comp_prices) if comp_prices else None,
        "comp_max":          max(comp_prices) if comp_prices else None,
        "comp_count":        len(comp_prices),
        "comp_props":        comp_props,
        "missing_comps":     missing_comp_kws,
        "hsmi_vs_comp_pct":  hsmi_vs_comp_pct,
        "reference_props":   reference_props,
    }


# ---------------------------------------------------------------------------
# Cloudbeds HSMI rate fallback
# ---------------------------------------------------------------------------

def _fetch_cloudbeds_rate(cb_api_key: str, cb_property_id: str, d: date) -> Optional[float]:
    """Average of all 6 BASE room-type rates from Cloudbeds for date d."""
    try:
        from cloudbeds_client import CloudbedsClient  # noqa: PLC0415
        from config import BASE_RATE_IDS, ROOM_TYPE_ID_MAP  # noqa: PLC0415

        client   = CloudbedsClient(api_key=cb_api_key, property_id=cb_property_id)
        d_str    = d.strftime("%Y-%m-%d")
        checkout = d + timedelta(days=1)

        fetched: list[float] = []
        failed:  list[str]   = []
        for code in BASE_RATE_IDS:
            room_type_id = ROOM_TYPE_ID_MAP[code]["id"]
            try:
                rates = client.get_rate(room_type_id, d, checkout)
                val   = rates.get(d_str)
                if val is not None:
                    fetched.append(val)
                else:
                    failed.append(f"{code}(no rate)")
            except Exception as exc:
                failed.append(f"{code}(error)")
                logger.warning("  Cloudbeds %s rate fetch failed: %s", code, exc)

        if not fetched:
            logger.warning("  Cloudbeds fallback: no rates retrieved (failed: %s)", failed)
            return None
        avg = sum(fetched) / len(fetched)
        logger.info(
            "  Cloudbeds fallback: %d/%d room types → avg A$%.0f",
            len(fetched), len(BASE_RATE_IDS), avg,
        )
        return avg
    except Exception as exc:
        logger.warning("Cloudbeds rate fallback failed for %s: %s", d, exc)
        return None


# ---------------------------------------------------------------------------
# Cache build
# ---------------------------------------------------------------------------

def build_and_write_cache(api_key: str) -> dict:
    """
    Run Booking.com searches for the coming Friday + Saturday, build the
    competitor_cache.json payload, write it, and return it.
    """
    today = datetime.now(_MELB).date()
    d1, d2 = _target_dates(today)
    logger.info(
        "Target dates: %s (%s) and %s (%s)",
        d1, d1.strftime("%a"), d2, d2.strftime("%a"),
    )

    # Resolve Booking.com dest_id for the region once (shared across both dates)
    dest_id, dest_type = _resolve_dest_id(api_key, "Hepburn Springs Daylesford Victoria Australia")
    if not dest_id:
        logger.warning("Could not resolve dest_id — will use coordinate fallback")

    cb_api_key      = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
    cb_property_id  = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
    use_cb_fallback = bool(cb_api_key and cb_property_id)

    cache: dict = {
        "updated_at":  datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":      "booking_com",
        "query_dates": [d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d")],
        "signals":     {},
    }

    for d in (d1, d2):
        d_str    = d.strftime("%Y-%m-%d")
        checkout = d + timedelta(days=1)
        logger.info("--- Querying %s (%s) ---", d_str, d.strftime("%a %d %b"))
        try:
            raw    = _search_booking(api_key, d, checkout, dest_id, dest_type or "city")
            signal = _process_booking_response(raw)

            # HSMI fallback: use Cloudbeds avg if HSMI not on Booking.com
            if signal.get("hsmi_price") is None and use_cb_fallback:
                cb_rate = _fetch_cloudbeds_rate(cb_api_key, cb_property_id, d)
                if cb_rate is not None:
                    logger.info("HSMI not on Booking.com — Cloudbeds avg: A$%.0f", cb_rate)
                    signal["hsmi_price"]  = cb_rate
                    signal["hsmi_source"] = "cloudbeds_fallback"
                    comp_avg = signal.get("comp_avg")
                    if comp_avg:
                        signal["hsmi_vs_comp_pct"] = round(
                            ((cb_rate - comp_avg) / comp_avg) * 100
                        )

            cache["signals"][d_str] = signal
        except Exception as exc:
            logger.error("Query failed for %s: %s", d_str, exc, exc_info=True)
            cache["signals"][d_str] = {"error": str(exc)}

    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    logger.info("Cache written → %s", CACHE_PATH)
    return cache


# ---------------------------------------------------------------------------
# Slack summary
# ---------------------------------------------------------------------------

def _engine_recommendation(sig: dict) -> str:
    rs = sig.get("regional_signal", "NORMAL")
    if rs == "SOLD_OUT":
        return "⚠️ Push all room types to ceiling"
    elif rs == "CRITICAL":
        return "📈 Apply +3 bracket uplift"
    elif rs == "HIGH":
        return "📈 Apply +2 bracket uplift"
    else:
        diff = sig.get("hsmi_vs_comp_pct")
        if diff is not None:
            if diff < -15:
                return f"HSMI {abs(diff)}% below mid-tier avg — nudge up 8%"
            elif diff < 0:
                return f"HSMI {abs(diff)}% below mid-tier avg — nudge up 5%"
            else:
                return "HSMI competitively priced vs mid-tier — hold"
        return "NORMAL demand — hold"


def post_slack_summary(cache: dict, webhook_url: str) -> None:
    today = datetime.now(_MELB).date()
    lines = [
        f"*HSMI Competitor Signal — {today.strftime('%a %d %b %Y')}*",
        "_9am Booking.com snapshot — rate push triggered_",
        "",
    ]

    for d_str, sig in cache.get("signals", {}).items():
        d         = date.fromisoformat(d_str)
        day_label = d.strftime("%a %d %b")

        if "error" in sig:
            lines.append(f"*{day_label}*: ❌ query failed — {sig['error']}")
            continue

        rs          = sig.get("regional_signal", "NORMAL")
        sold_pct    = sig.get("regional_sold_pct")
        src         = sig.get("signal_source", "")
        hsmi_price  = sig.get("hsmi_price")
        hsmi_source = sig.get("hsmi_source", "booking_com")
        comp_avg    = sig.get("comp_avg")
        diff        = sig.get("hsmi_vs_comp_pct")

        # Banner line — highlight when hitting critical sold-out territory
        if sold_pct is not None:
            banner_flag = " 🔥" if sold_pct >= 99 else (" ⚠️" if sold_pct >= 96 else "")
            banner_note = f"{sold_pct}% rooms sold{banner_flag}"
            if src == "booking_banner":
                banner_note += " _(Booking.com banner)_"
        else:
            banner_note = "availability unknown"

        hsmi_part = f"HSMI A${hsmi_price:.0f}" if hsmi_price else "HSMI —"
        if hsmi_source == "cloudbeds_fallback":
            hsmi_part += "†"
        avg_part  = f"Comp avg A${comp_avg:.0f}" if comp_avg else "Comp avg N/A"
        diff_part = f"({diff:+d}%)" if diff is not None else ""
        rec       = _engine_recommendation(sig)

        lines.append(
            f"*{day_label}:* {banner_note} | {hsmi_part} | {avg_part} {diff_part} | {rs} → {rec}"
        )

        # Individual comp prices
        comp_props = sig.get("comp_props", [])
        if comp_props:
            comp_parts = ", ".join(
                f"{c['name'].split(',')[0]} A${c['price']:.0f}" for c in comp_props
            )
            lines.append(f"  _Comps: {comp_parts}_")
        missing = sig.get("missing_comps", [])
        if missing:
            lines.append(f"  _Not found on Booking.com: {', '.join(missing)}_")

        # Premium benchmarks
        ref = sig.get("reference_props", [])
        ref_parts = ", ".join(
            f"{r['name'].split(',')[0]} {r['price_str']}"
            for r in ref
            if r.get("price_str") and r["price_str"] != "N/A"
        )
        if ref_parts:
            lines.append(f"  _Premium: {ref_parts}_")

        lines.append("")

    if any("†" in l for l in lines):
        lines.append("_† HSMI not on Booking.com — Cloudbeds avg rate used_")
    lines.append("_Pricing engine running now — rate changes will follow if needed_")

    try:
        resp = requests.post(
            webhook_url,
            json={"text": "\n".join(lines), "username": "Ops Agent", "icon_emoji": ":chart_with_upwards_trend:"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Slack summary posted")
    except requests.RequestException as exc:
        logger.warning("Slack post failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    api_key = os.environ.get("BOOKING_COM_API_KEY", "").strip()
    if not api_key:
        logger.critical("BOOKING_COM_API_KEY environment variable not set")
        sys.exit(1)

    logger.info("=== HSMI Competitor Signal (Booking.com) — %s ===", datetime.now(_MELB).date())
    cache = build_and_write_cache(api_key)

    webhook = os.environ.get("SLACK_PRICING_WEBHOOK_URL", "").strip()
    if webhook:
        post_slack_summary(cache, webhook)
    else:
        logger.info("SLACK_PRICING_WEBHOOK_URL not set — skipping Slack")

    logger.info("=== Competitor Signal complete — cache ready for pricing engine ===")


if __name__ == "__main__":
    run()
