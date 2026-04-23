"""
HSMI Weekly Comp Report
=======================
Runs every Monday at 9am AEST (11pm UTC Sunday, cron '0 23 * * 0').
Makes 6 SerpApi calls — 2 queries × 3 nights (Friday, Saturday, Sunday) —
and posts a pricing table to Slack #growth.

SerpApi budget: 6 calls/week ≈ 24 calls/month.
Shared budget with competitor_signal.py (≈120/month) ≈ 144/month total.
Free tier limit: 250/month.

Environment variables:
  SERP_API_KEY      — SerpApi key (required)
  SLACK_WEBHOOK_URL — Slack incoming webhook (required)
"""

import logging
import os
import sys
from datetime import date, timedelta
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

SERP_ENDPOINT = "https://serpapi.com/search"

# Two complementary queries that together cover the full comp set.
# Q1 captures HSMI + Hepburn-area properties (Mineral Springs, premium refs).
# Q2 captures Daylesford-area comps (Central, Motor, Frangos, Albert, Royal, Hotel).
# Merged per night: Q1 takes priority on any duplicate property name.
_QUERY_HEPBURN    = "Hepburn Springs Victoria accommodation"
_QUERY_DAYLESFORD = "Daylesford Victoria accommodation motel"


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
# SerpApi query + parsing
# ---------------------------------------------------------------------------

def _query_serpapi(api_key: str, q: str, checkin: date) -> dict:
    """Single SerpApi call for one query string and night. Returns raw JSON."""
    params = {
        "engine": "google_hotels",
        "q": q,
        "check_in_date": checkin.strftime("%Y-%m-%d"),
        "check_out_date": (checkin + timedelta(days=1)).strftime("%Y-%m-%d"),
        "adults": "2",
        "currency": "AUD",
        "gl": "au",
        "hl": "en",
        "api_key": api_key,
    }
    logger.info("SerpApi [%s]: %s", q, checkin.strftime("%a %d %b"))
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


def _query_and_merge_night(api_key: str, checkin: date) -> dict:
    """Run both search queries for one night and return merged properties."""
    r1 = _query_serpapi(api_key, _QUERY_HEPBURN, checkin)
    r2 = _query_serpapi(api_key, _QUERY_DAYLESFORD, checkin)
    return _merge_responses(r1, r2)


def _process_night(data: dict) -> dict:
    """
    Process one night's SerpApi response into a structured result.

    Returns:
      regional_pct / regional_signal / available / total
      hsmi          — {price_str, price_num, sold} or None
      pricing_comps — {keyword: {name, price_str, price_num, sold}}
      reference     — {keyword: {name, price_str, price_num, sold}}
      comp_avg / comp_count / hsmi_vs_comp_pct
    """
    if "error" in data:
        raise ValueError(f"SerpApi error: {data['error']}")

    props = data.get("properties", [])
    total = len(props)
    sold_out_count = sum(1 for p in props if p.get("is_sold_out"))
    available = total - sold_out_count
    regional_pct = round(available / total * 100) if total > 0 else 100

    if regional_pct < 1:
        signal = "SOLD_OUT"
    elif regional_pct <= 4:
        signal = "CRITICAL"
    elif regional_pct <= 9:
        signal = "HIGH"
    else:
        signal = "NORMAL"

    hsmi: Optional[dict] = None
    pricing_comps: dict[str, dict] = {}
    reference: dict[str, dict] = {}

    for p in props:
        nl = p.get("name", "").lower()
        price_raw = p.get("rate_per_night", {})
        price_raw_str = price_raw.get("lowest") if isinstance(price_raw, dict) else None
        price_num = _parse_price(price_raw_str)
        sold = p.get("is_sold_out", False)
        price_str = "SOLD" if sold else (f"${price_num:.0f}" if price_num else "N/A")

        category, key = _classify(nl)
        src = p.get("_source", "?")
        entry = {
            "name": p.get("name", "?"),
            "price_str": price_str,
            "price_num": None if sold else price_num,
            "sold": sold,
        }

        if category == "hsmi":
            hsmi = entry
            logger.info("  HSMI  [%s]: %s — %s", src, p.get("name"), price_str)
        elif category == "pricing_comp":
            pricing_comps[key] = entry
            logger.info("  COMP  [%s]: %s — %s", src, p.get("name"), price_str)
        elif category == "reference":
            reference[key] = entry
            logger.info("  REF   [%s]: %s — %s", src, p.get("name"), price_str)
        elif category == "skip":
            logger.debug("  SKIP  [%s]: %s", src, p.get("name"))
        else:
            logger.debug("  other [%s]: %s — %s", src, p.get("name"), price_str)

    comp_prices = [v["price_num"] for v in pricing_comps.values() if v["price_num"]]
    comp_avg = sum(comp_prices) / len(comp_prices) if comp_prices else None

    hsmi_price = hsmi["price_num"] if hsmi else None
    hsmi_vs_comp_pct = (
        round(((hsmi_price - comp_avg) / comp_avg) * 100)
        if hsmi_price and comp_avg
        else None
    )

    return {
        "regional_pct":    regional_pct,
        "regional_signal": signal,
        "available":       available,
        "total":           total,
        "hsmi":            hsmi,
        "pricing_comps":   pricing_comps,
        "reference":       reference,
        "comp_avg":        comp_avg,
        "comp_count":      len(comp_prices),
        "hsmi_vs_comp_pct": hsmi_vs_comp_pct,
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
        f"*HSMI Weekly Comp Report — {date.today().strftime('%a %d %b %Y')}*",
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

    # Saturday regional availability signal
    sat_r = nights.get("sat", {})
    if sat_r and "error" not in sat_r:
        pct  = sat_r.get("regional_pct", "?")
        sig  = sat_r.get("regional_signal", "?")
        avail = sat_r.get("available", "?")
        total = sat_r.get("total", "?")
        signal_emoji = {"SOLD_OUT": "🔴", "CRITICAL": "🟠", "HIGH": "🟡", "NORMAL": "🟢"}.get(sig, "⚪")
        lines.append(f"*Saturday regional:* {avail}/{total} available ({pct}%) — {signal_emoji} {sig}")
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
    lines.append("_ℹ️ Rates sourced from Google Hotels search results (Booking.com and other OTA pricing). Direct bookings save guests nothing but save HSMI ~17% commission._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    api_key = os.environ.get("SERP_API_KEY", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    missing = [v for v, k in [("SERP_API_KEY", api_key), ("SLACK_WEBHOOK_URL", webhook)] if not k]
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    today  = date.today()
    friday = _next_friday(today)
    logger.info(
        "=== HSMI Weekly Comp Report — %s | Weekend: %s/%s/%s ===",
        today,
        friday,
        friday + timedelta(days=1),
        friday + timedelta(days=2),
    )

    nights: dict = {}
    for night_key, night_date in [
        ("fri", friday),
        ("sat", friday + timedelta(days=1)),
        ("sun", friday + timedelta(days=2)),
    ]:
        logger.info("--- %s ---", night_date.strftime("%a %d %b"))
        try:
            merged = _query_and_merge_night(api_key, night_date)
            nights[night_key] = _process_night(merged)
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
