"""
HSMI Competitor Signal
======================
Queries SerpApi for regional accommodation availability and competitor pricing
around Hepburn Springs / Daylesford. Runs once per day at 12pm AEST via
GitHub Actions cron '0 2 * * *'.

Makes exactly 2 SerpApi calls per run (next Saturday + the Saturday after),
writing the results to competitor_cache.json. The pricing engine reads this
cache to apply a competitor-aware multiplier on top of its occupancy brackets.

Budget: 2 calls/day × 30 days = 60 calls/month (free tier limit: 100/month).

DO NOT import or call this from hourly pricing runs — the cache is shared with
the pricing engine via a local file and GitHub Actions runners are ephemeral.
The cache exists only within the same workflow job run.

Environment variables:
  SERP_API_KEY      — SerpApi key (required)
  SLACK_WEBHOOK_URL — Slack incoming webhook for signal summary (optional)
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
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

def _next_saturday(today: date) -> date:
    """Return the next Saturday strictly after today."""
    days_ahead = (5 - today.weekday()) % 7  # Saturday = weekday 5
    if days_ahead == 0:
        days_ahead = 7  # if today IS Saturday, go to next week
    return today + timedelta(days=days_ahead)


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

def _query_serpapi(api_key: str, checkin: date, checkout: date) -> dict:
    """Single SerpApi call. Returns raw JSON."""
    params = {
        "engine": "google_hotels",
        "q": "Hepburn Springs Daylesford Victoria accommodation",
        "check_in_date": checkin.strftime("%Y-%m-%d"),
        "check_out_date": checkout.strftime("%Y-%m-%d"),
        "adults": "2",
        "currency": "AUD",
        "gl": "au",
        "hl": "en",
        "api_key": api_key,
    }
    logger.info("SerpApi query: %s → %s", checkin, checkout)
    resp = requests.get(SERP_ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


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
    reference_props: list[dict] = []

    for p in props:
        nl = p.get("name", "").lower()
        price_raw = p.get("rate_per_night", {})
        price_str = price_raw.get("lowest") if isinstance(price_raw, dict) else None
        price_num = _parse_price(price_str)
        sold_tag = " [SOLD OUT]" if p.get("is_sold_out") else ""
        kind = _classify(nl)

        if kind == "hsmi":
            hsmi_price = price_num
            logger.info("  HSMI:  %s — %s%s", p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "pricing_comp":
            if price_num:
                comp_prices.append(price_num)
            logger.info("  COMP:  %s — %s%s", p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "reference":
            reference_props.append({"name": p.get("name", "?"), "price_str": price_str or "N/A"})
            logger.info("  REF:   %s — %s%s", p.get("name"), price_str or "N/A", sold_tag)
        elif kind == "skip":
            logger.debug("  SKIP:  %s (alias suppressed)", p.get("name"))
        else:
            logger.debug("  other: %s — %s", p.get("name"), price_str or "N/A")

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
        "regional_pct": regional_pct,
        "regional_signal": regional_signal,
        "hsmi_price": hsmi_price,
        "comp_avg": round(comp_avg, 2) if comp_avg else None,
        "comp_min": min(comp_prices) if comp_prices else None,
        "comp_max": max(comp_prices) if comp_prices else None,
        "comp_count": len(comp_prices),
        "hsmi_vs_comp_pct": hsmi_vs_comp_pct,
        "reference_props": reference_props,
    }


# ---------------------------------------------------------------------------
# Cache write
# ---------------------------------------------------------------------------

def build_and_write_cache(api_key: str) -> dict:
    """
    Run both SerpApi queries (next 2 Saturdays), build the cache payload,
    write it to CACHE_PATH, and return the payload.
    """
    today = date.today()
    sat1 = _next_saturday(today)
    sat2 = sat1 + timedelta(weeks=1)

    cache: dict = {
        "updated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query_dates": [sat1.strftime("%Y-%m-%d"), sat2.strftime("%Y-%m-%d")],
        "signals": {},
    }

    for d in (sat1, sat2):
        d_str = d.strftime("%Y-%m-%d")
        logger.info("--- Querying %s (%s) ---", d_str, d.strftime("%a %d %b"))
        try:
            raw = _query_serpapi(api_key, d, d + timedelta(days=1))
            cache["signals"][d_str] = _process_response(raw)
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
    today = date.today()
    lines = [
        f"*HSMI Competitor Signal — {today.strftime('%a %d %b %Y')}*",
        f"_12pm market snapshot — rate push triggered_",
        "",
    ]

    for d_str, sig in cache.get("signals", {}).items():
        d = date.fromisoformat(d_str)
        day_label = d.strftime("%a %d %b")

        if "error" in sig:
            lines.append(f"*{day_label}*: ❌ query failed — {sig['error']}")
            continue

        rs = sig.get("regional_signal", "?")
        pct = sig.get("regional_pct", "?")
        hsmi = f"A${sig['hsmi_price']:.0f}" if sig.get("hsmi_price") else "not listed"
        avg = f"A${sig['comp_avg']:.0f}" if sig.get("comp_avg") else "N/A"
        diff = sig.get("hsmi_vs_comp_pct")
        diff_str = f" ({diff:+d}% vs mid-tier avg)" if diff is not None else ""

        lines += [
            f"*{day_label}*",
            f"  Regional: {pct}% available — *{rs}*",
            f"  HSMI: {hsmi}{diff_str} | Mid-tier comp avg: {avg}",
            f"  → {_engine_recommendation(sig)}",
        ]

        # Premium benchmarks (reference only — not used in pricing)
        ref = sig.get("reference_props", [])
        if ref:
            ref_parts = ", ".join(
                f"{r['name'].split(',')[0]} {r['price_str']}" for r in ref if r["price_str"] != "N/A"
            )
            if ref_parts:
                lines.append(f"  _Premium benchmarks: {ref_parts}_")

        lines.append("")

    lines.append("_Pricing engine running now — rate changes will follow if needed_")

    try:
        resp = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)
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

    logger.info("=== HSMI Competitor Signal — %s ===", date.today())
    cache = build_and_write_cache(api_key)

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        post_slack_summary(cache, webhook)
    else:
        logger.info("SLACK_WEBHOOK_URL not set — skipping Slack notification")

    logger.info("=== Competitor Signal complete — cache ready for pricing engine ===")


if __name__ == "__main__":
    run()
