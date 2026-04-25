"""
HSMI Competitor Signal
======================
Queries SerpApi for regional accommodation availability and competitor pricing
around Hepburn Springs / Daylesford. Runs once per day at 9am AEST via
GitHub Actions cron '0 23 * * *'.

Makes exactly 4 SerpApi calls per run (2 queries × 2 dates: the coming
Friday and Saturday), writing the results to competitor_cache.json. The
pricing engine reads this cache to apply a competitor-aware multiplier
on top of its occupancy brackets.

If HSMI does not appear in Google Hotels results, the script falls back to
reading the TWI base rate from Cloudbeds (CLOUDBEDS_API_KEY / _PROPERTY_ID).

Budget: 4 calls/day × 30 days ≈ 120 calls/month (free tier limit: 250/month).

DO NOT import or call this from hourly pricing runs — the cache is shared with
the pricing engine via a local file and GitHub Actions runners are ephemeral.
The cache exists only within the same workflow job run.

Environment variables:
  SERP_API_KEY              — SerpApi key (required)
  SLACK_PRICING_WEBHOOK_URL — Slack incoming webhook for #api-pricing-engine (optional)
  CLOUDBEDS_API_KEY         — Cloudbeds API key (optional; enables HSMI price fallback)
  CLOUDBEDS_PROPERTY_ID     — Cloudbeds property ID (optional; required with above)
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
SERP_ENDPOINT = "https://serpapi.com/search"

# Two complementary queries that together cover the full comp set.
# Q1 captures HSMI + Hepburn-area properties (Mineral Springs, premium refs).
# Q2 captures Daylesford-area comps (Central, Motor, Frangos, Albert, Royal, Hotel).
# Merged per date: Q1 takes priority on any duplicate property name.
_QUERY_HEPBURN    = "Hepburn Springs Victoria accommodation"
_QUERY_DAYLESFORD = "Daylesford Victoria accommodation motel"

# Mid-tier comps — same tier as HSMI. Prices from this set are averaged
# and used for the competitor multiplier calculation in the pricing engine.
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

# Premium / reference-only properties — tracked and logged for market context
# but NOT included in the comp avg used for pricing decisions.
REFERENCE_ONLY = [
    "hepburn at hepburn",
    "lake house",
    "hotel bellinzona",
    "bellinzona",
    "hepburn spa",
    "shizuka",
]

# Noise aliases to suppress — same physical property as an entry above,
# avoids double-counting if Google returns multiple listings.
_SKIP_ALIASES = ["wyndham", "albert motel"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _target_dates(today: date) -> tuple[date, date]:
    """
    Return (date1, date2) for the coming weekend to check.

    Normal case: the upcoming Friday and Saturday of the same weekend.
      If today IS Friday, Friday = today (check tonight + tomorrow).
    Saturday special case: use today as Saturday + next Friday (next weekend).
    """
    weekday = today.weekday()  # Mon=0 … Fri=4, Sat=5, Sun=6

    if weekday == 5:  # Today is Saturday — check today + next Friday
        return today, today + timedelta(days=6)

    # For all other days: find the upcoming Friday (today counts if it's Friday)
    days_to_fri = (4 - weekday) % 7
    friday = today + timedelta(days=days_to_fri)
    saturday = friday + timedelta(days=1)
    return friday, saturday


def _parse_price(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace("A$", "").replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SerpApi query + parsing
# ---------------------------------------------------------------------------

def _query_serpapi(api_key: str, q: str, checkin: date, checkout: date) -> dict:
    """Single SerpApi call for one query string. Returns raw JSON."""
    params = {
        "engine": "google_hotels",
        "q": q,
        "check_in_date": checkin.strftime("%Y-%m-%d"),
        "check_out_date": checkout.strftime("%Y-%m-%d"),
        "adults": "2",
        "currency": "AUD",
        "gl": "au",
        "hl": "en",
        "api_key": api_key,
    }
    logger.info("SerpApi [%s]: %s → %s", q, checkin, checkout)
    resp = requests.get(SERP_ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _merge_responses(r1: dict, r2: dict) -> dict:
    """
    Merge two SerpApi responses into one synthetic response.

    Properties are deduped by lowercase name; r1 takes priority on conflicts.
    Each property gains a '_source' field ('Q1' or 'Q2') used for logging.
    r1's metadata is kept as the base; only 'properties' is replaced.
    """
    if "error" in r1:
        raise ValueError(f"SerpApi error (Q1): {r1['error']}")
    if "error" in r2:
        raise ValueError(f"SerpApi error (Q2): {r2['error']}")

    seen: dict[str, dict] = {}
    for p in r1.get("properties", []):
        nl = p.get("name", "").lower()
        if nl not in seen:
            seen[nl] = {**p, "_source": "Q1"}
    for p in r2.get("properties", []):
        nl = p.get("name", "").lower()
        if nl not in seen:
            seen[nl] = {**p, "_source": "Q2"}

    return {**r1, "properties": list(seen.values())}


def _classify(nl: str) -> str:
    """
    Classify a lowercased property name.

    Returns: 'hsmi' | 'pricing_comp' | 'reference' | 'skip' | 'other'
    """
    if "hepburn springs motor" in nl:
        return "hsmi"
    if any(alias in nl for alias in _SKIP_ALIASES):
        return "skip"
    if any(kw in nl for kw in PRICING_COMPS):
        return "pricing_comp"
    if any(kw in nl for kw in REFERENCE_ONLY):
        return "reference"
    return "other"


def _process_response(data: dict) -> dict:
    """
    Extract competitor signal from a SerpApi response.

    Returns a dict with:
      regional_pct      — % of listed properties that are available
      regional_signal   — SOLD_OUT / CRITICAL / HIGH / NORMAL
      hsmi_price        — HSMI nightly rate shown on Google Hotels (or None)
      comp_avg          — mid-tier PRICING_COMPS average (used for multiplier)
      comp_min / max    — range of PRICING_COMPS prices
      comp_count        — number of PRICING_COMPS with visible pricing
      hsmi_vs_comp_pct  — HSMI price vs PRICING_COMPS avg (signed %)
      reference_props   — list of {name, price_str} for REFERENCE_ONLY props
    """
    if "error" in data:
        raise ValueError(f"SerpApi error: {data['error']}")

    props = data.get("properties", [])
    total = len(props)
    sold_out_count = sum(1 for p in props if p.get("is_sold_out"))
    available = total - sold_out_count
    regional_pct = round(available / total * 100) if total > 0 else 100

    if regional_pct < 1:
        regional_signal = "SOLD_OUT"
    elif regional_pct <= 4:
        regional_signal = "CRITICAL"
    elif regional_pct <= 9:
        regional_signal = "HIGH"
    else:
        regional_signal = "NORMAL"

    hsmi_price: Optional[float] = None
    comp_prices: list[float] = []
    comp_props: list[dict] = []   # [{name, price}] for each found PRICING_COMP
    reference_props: list[dict] = []
    found_comp_keywords: set[str] = set()

    for p in props:
        nl = p.get("name", "").lower()
        price_raw = p.get("rate_per_night", {})
        price_str = price_raw.get("lowest") if isinstance(price_raw, dict) else None
        price_num = _parse_price(price_str)
        sold_tag = " [SOLD OUT]" if p.get("is_sold_out") else ""
        kind = _classify(nl)
        src = p.get("_source", "?")

        if kind == "hsmi":
            hsmi_price = price_num
            logger.info("  HSMI  [%s]: %s — %s%s", src, p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "pricing_comp":
            for kw in PRICING_COMPS:
                if kw in nl:
                    found_comp_keywords.add(kw)
                    break
            if price_num:
                comp_prices.append(price_num)
                comp_props.append({"name": p.get("name", "?"), "price": price_num})
            logger.info("  COMP  [%s]: %s — %s%s", src, p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "reference":
            reference_props.append({"name": p.get("name", "?"), "price_str": price_str or "N/A"})
            logger.info("  REF   [%s]: %s — %s%s", src, p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "skip":
            logger.debug("  SKIP  [%s]: %s (alias suppressed)", src, p.get("name"))
        else:
            logger.debug("  other [%s]: %s — %s", src, p.get("name"), price_str or "N/A")

    missing_comp_keywords = [kw for kw in PRICING_COMPS if kw not in found_comp_keywords]
    if missing_comp_keywords:
        logger.info("  Comps NOT found in results: %s", missing_comp_keywords)
    else:
        logger.info("  All %d PRICING_COMPS found in results", len(PRICING_COMPS))

    comp_avg = sum(comp_prices) / len(comp_prices) if comp_prices else None
    hsmi_vs_comp_pct = (
        round(((hsmi_price - comp_avg) / comp_avg) * 100)
        if hsmi_price and comp_avg
        else None
    )

    logger.info(
        "  Regional: %d/%d available (%d%%) — %s | HSMI: %s | Mid-tier avg: %s",
        available, total, regional_pct, regional_signal,
        f"A${hsmi_price:.0f}" if hsmi_price else "not found",
        f"A${comp_avg:.0f}" if comp_avg else "N/A",
    )

    return {
        "regional_pct":    regional_pct,
        "regional_signal": regional_signal,
        "hsmi_price":      hsmi_price,
        "hsmi_source":     "google_hotels",
        "comp_avg":        round(comp_avg, 2) if comp_avg else None,
        "comp_min":        min(comp_prices) if comp_prices else None,
        "comp_max":        max(comp_prices) if comp_prices else None,
        "comp_count":      len(comp_prices),
        "comp_props":      comp_props,
        "missing_comps":   missing_comp_keywords,
        "hsmi_vs_comp_pct": hsmi_vs_comp_pct,
        "reference_props": reference_props,
    }


# ---------------------------------------------------------------------------
# Cloudbeds fallback — used when HSMI is not listed on Google Hotels
# ---------------------------------------------------------------------------

def _fetch_cloudbeds_rate(cb_api_key: str, cb_property_id: str, d: date) -> Optional[float]:
    """
    Fetch the simple average of all 6 BASE room-type rates from Cloudbeds for
    date d, using the room type IDs from ROOM_TYPE_ID_MAP (keyed via
    BASE_RATE_IDS so we only query rate plans that are publicly listed).

    Returns the average as a float, or None if no rates could be retrieved.
    Silently catches all errors so a Cloudbeds outage never blocks the signal.
    """
    try:
        from cloudbeds_client import CloudbedsClient  # noqa: PLC0415
        from config import BASE_RATE_IDS, ROOM_TYPE_ID_MAP  # noqa: PLC0415

        client = CloudbedsClient(api_key=cb_api_key, property_id=cb_property_id)
        d_str = d.strftime("%Y-%m-%d")
        checkout = d + timedelta(days=1)

        fetched: list[float] = []
        failed: list[str] = []
        for code in BASE_RATE_IDS:
            room_type_id = ROOM_TYPE_ID_MAP[code]["id"]
            try:
                rates = client.get_rate(room_type_id, d, checkout)
                val = rates.get(d_str)
                if val is not None:
                    fetched.append(val)
                    logger.info("  Cloudbeds %s: A$%.0f", code, val)
                else:
                    failed.append(f"{code}(no rate)")
                    logger.warning("  Cloudbeds %s: no rate returned for %s", code, d_str)
            except Exception as exc:
                failed.append(f"{code}(error)")
                logger.warning("  Cloudbeds %s rate fetch failed: %s", code, exc)

        if not fetched:
            logger.warning("  Cloudbeds fallback: no rates retrieved (failed: %s)", failed)
            return None
        avg = sum(fetched) / len(fetched)
        logger.info(
            "  Cloudbeds fallback: %d/%d room types → avg A$%.0f (failed: %s)",
            len(fetched), len(BASE_RATE_IDS), avg, failed or "none",
        )
        return avg
    except Exception as exc:
        logger.warning("Cloudbeds rate fallback failed for %s: %s", d, exc)
        return None


# ---------------------------------------------------------------------------
# Cache write
# ---------------------------------------------------------------------------

def build_and_write_cache(api_key: str) -> dict:
    """
    Run SerpApi queries for the coming Friday + Saturday (2 queries each = 4
    calls total), build the cache payload, write it to CACHE_PATH, and return
    the payload.

    If CLOUDBEDS_API_KEY / CLOUDBEDS_PROPERTY_ID are set and HSMI is absent
    from Google Hotels results, the TWI base rate is fetched from Cloudbeds
    as a fallback so the competitor comparison always has an HSMI price.
    """
    today = datetime.now(_MELB).date()
    d1, d2 = _target_dates(today)
    logger.info(
        "Target dates: %s (%s) and %s (%s)",
        d1, d1.strftime("%a"), d2, d2.strftime("%a"),
    )

    cb_api_key   = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
    cb_property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
    use_cb_fallback = bool(cb_api_key and cb_property_id)

    cache: dict = {
        "updated_at":  datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query_dates": [d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d")],
        "signals":     {},
    }

    for d in (d1, d2):
        d_str = d.strftime("%Y-%m-%d")
        checkout = d + timedelta(days=1)
        logger.info("--- Querying %s (%s) ---", d_str, d.strftime("%a %d %b"))
        try:
            raw1 = _query_serpapi(api_key, _QUERY_HEPBURN, d, checkout)
            raw2 = _query_serpapi(api_key, _QUERY_DAYLESFORD, d, checkout)
            merged = _merge_responses(raw1, raw2)
            signal = _process_response(merged)

            # HSMI fallback: if Google Hotels didn't list HSMI, use Cloudbeds TWI rate
            if signal.get("hsmi_price") is None and use_cb_fallback:
                cb_rate = _fetch_cloudbeds_rate(cb_api_key, cb_property_id, d)
                if cb_rate is not None:
                    logger.info(
                        "HSMI not on Google Hotels — using Cloudbeds avg rate across all room types: A$%.0f",
                        cb_rate,
                    )
                    signal["hsmi_price"]  = cb_rate
                    signal["hsmi_source"] = "cloudbeds_fallback"
                    comp_avg = signal.get("comp_avg")
                    if comp_avg:
                        signal["hsmi_vs_comp_pct"] = round(
                            ((cb_rate - comp_avg) / comp_avg) * 100
                        )

            cache["signals"][d_str] = signal
        except Exception as exc:
            logger.error("Query failed for %s: %s", d_str, exc)
            cache["signals"][d_str] = {"error": str(exc)}

    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    logger.info("Cache written → %s", CACHE_PATH)
    return cache


# ---------------------------------------------------------------------------
# Slack summary
# ---------------------------------------------------------------------------

def _engine_recommendation(sig: dict) -> str:
    """One-line recommendation string from a signal dict."""
    rs = sig.get("regional_signal", "NORMAL")
    if rs == "SOLD_OUT":
        return "⚠️ Push all room types to ceiling"
    elif rs == "CRITICAL":
        return "📈 Apply +3 bracket uplift"
    elif rs == "HIGH":
        return "📈 Apply +2 bracket uplift"
    else:
        hsmi = sig.get("hsmi_price")
        avg = sig.get("comp_avg")
        diff = sig.get("hsmi_vs_comp_pct")
        if hsmi and avg and diff is not None:
            if diff < -15:
                return f"HSMI is {abs(diff)}% below mid-tier avg — nudge up 8%"
            elif diff < 0:
                return f"HSMI is {abs(diff)}% below mid-tier avg — nudge up 5%"
            else:
                return "HSMI competitively priced vs mid-tier — hold"
        return "NORMAL demand — hold"


def post_slack_summary(cache: dict, webhook_url: str) -> None:
    today = datetime.now(_MELB).date()
    lines = [
        f"*HSMI Competitor Signal — {today.strftime('%a %d %b %Y')}*",
        f"_9am market snapshot — rate push triggered_",
        "",
    ]

    for d_str, sig in cache.get("signals", {}).items():
        d = date.fromisoformat(d_str)
        day_label = d.strftime("%a %d %b")

        if "error" in sig:
            lines.append(f"*{day_label}*: ❌ query failed — {sig['error']}")
            continue

        rs         = sig.get("regional_signal", "NORMAL")
        hsmi_price = sig.get("hsmi_price")
        hsmi_source = sig.get("hsmi_source", "google_hotels")
        comp_avg   = sig.get("comp_avg")
        diff       = sig.get("hsmi_vs_comp_pct")

        # Build compact main line
        hsmi_part = f"HSMI ${hsmi_price:.0f}" if hsmi_price else "HSMI —"
        if hsmi_source == "cloudbeds_fallback":
            hsmi_part += "†"            # dagger indicates Cloudbeds fallback
        avg_part  = f"Comp avg ${comp_avg:.0f}" if comp_avg else "Comp avg N/A"
        diff_part = f"({diff:+d}%)" if diff is not None else ""
        rec       = _engine_recommendation(sig)
        lines.append(
            f"*{day_label}:* {hsmi_part} | {avg_part} {diff_part} | {rs} → {rec}"
        )

        # Comp prices line
        comp_props = sig.get("comp_props", [])
        missing_comps = sig.get("missing_comps", [])
        if comp_props:
            comp_parts = ", ".join(
                f"{c['name'].split(',')[0]} ${c['price']:.0f}" for c in comp_props
            )
            lines.append(f"  _Comps: {comp_parts}_")
        if missing_comps:
            lines.append(f"  _Not found: {', '.join(missing_comps)}_")

        # Premium benchmarks — only show if at least one has a price
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
        lines.append("_† HSMI not listed on Google Hotels — Cloudbeds avg rate used_")
    lines.append("_Pricing engine running now — rate changes will follow if needed_")

    try:
        resp = requests.post(webhook_url, json={"text": "\n".join(lines), "username": "Ops Agent", "icon_emoji": ":chart_with_upwards_trend:"}, timeout=10)
        resp.raise_for_status()
        logger.info("Slack summary posted")
    except requests.RequestException as exc:
        logger.warning("Slack post failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    api_key = os.environ.get("SERP_API_KEY", "").strip()
    if not api_key:
        logger.critical("SERP_API_KEY environment variable not set")
        sys.exit(1)

    logger.info("=== HSMI Competitor Signal — %s ===", datetime.now(_MELB).date())
    cache = build_and_write_cache(api_key)

    webhook = os.environ.get("SLACK_PRICING_WEBHOOK_URL", "").strip()
    if webhook:
        post_slack_summary(cache, webhook)
    else:
        logger.info("SLACK_PRICING_WEBHOOK_URL not set — skipping Slack notification")

    logger.info("=== Competitor Signal complete — cache ready for pricing engine ===")


if __name__ == "__main__":
    run()
