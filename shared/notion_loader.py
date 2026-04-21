"""
Notion loader for HSMI pricing tiers.

Fetches the pricing tier table from the HSMI Intelligence Stack Notion page
and returns it in the same shape as ROOM_TYPES in config.py:

    {
        "TWI": {"floor": 105, "midweek": 160, "weekend": 240, "peak": 280, "ceiling": 300},
        ...
    }

Falls back to the static values in config.py with a warning if Notion is
unavailable or the NOTION_API_KEY environment variable is not set.

Environment variables:
  NOTION_API_KEY  — Notion internal integration token (required for live fetch)

Usage:
    from notion_loader import load_pricing_tiers
    tiers = load_pricing_tiers()
"""

import logging
import os
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# The Notion page that owns the pricing tier table
PAGE_ID = "349c905ced6b81d1be30d33aa3cf15eb"

# Block ID of the 6-column pricing tier table (columns: Type, Floor, Midweek, Weekend, Peak, Ceiling)
PRICING_TABLE_BLOCK_ID = "4dfc66c1-46af-495e-981c-a8b3e910a5d2"

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Expected column order (0-indexed); header row is row 0
COL_TYPE = 0
COL_FLOOR = 1
COL_MIDWEEK = 2
COL_WEEKEND = 3
COL_PEAK = 4
COL_CEILING = 5

# Maps the leading code word in the Type cell to the canonical room type code.
# Handles both exact matches and parenthetical forms like "TWI (Twin)".
KNOWN_CODES = {"TWI", "QUE", "SPA", "FAM", "BAL", "ACC"}


def _cell_text(cell: list[dict]) -> str:
    """Extract plain text from a Notion table_row cell (list of rich_text objects)."""
    return "".join(rt.get("plain_text", "") for rt in cell).strip()


def _parse_price(value: str) -> Optional[float]:
    """Strip currency symbols/commas and parse to float. Returns None on failure."""
    cleaned = re.sub(r"[^\d.]", "", value)
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _extract_code(type_cell: str) -> Optional[str]:
    """
    Extract the room type code from a cell like 'TWI (Twin)' or 'TWI'.
    Returns None if no known code is found.
    """
    first_word = type_cell.split()[0].upper() if type_cell else ""
    if first_word in KNOWN_CODES:
        return first_word
    # Fallback: scan all tokens
    for token in re.split(r"[\s()/]+", type_cell.upper()):
        if token in KNOWN_CODES:
            return token
    return None


def _fetch_table_rows(api_key: str) -> list[list[str]]:
    """
    Fetch all rows from the pricing table block.

    Returns a list of rows, each row being a list of cell text strings.
    Row 0 is the header row.

    Raises
    ------
    requests.RequestException
        On network or HTTP errors.
    """
    url = f"{NOTION_BASE_URL}/blocks/{PRICING_TABLE_BLOCK_ID}/children"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_API_VERSION,
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()

    rows: list[list[str]] = []
    for block in data.get("results", []):
        if block.get("type") != "table_row":
            continue
        cells = block["table_row"]["cells"]
        rows.append([_cell_text(cell) for cell in cells])

    return rows


def load_pricing_tiers() -> dict[str, dict[str, float]]:
    """
    Fetch the pricing tier table from Notion and return it as a dict.

    Falls back to static config.py values with a warning if:
    - NOTION_API_KEY is not set
    - The Notion API request fails
    - The table cannot be parsed

    Returns
    -------
    dict[str, dict[str, float]]
        ``{code: {floor, midweek, weekend, peak, ceiling}}``
    """
    # Import here to avoid circular dependency at module load time
    from config import ROOM_TYPES as _FALLBACK  # noqa: PLC0415

    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "NOTION_API_KEY not set — using static pricing tiers from config.py"
        )
        return dict(_FALLBACK)

    try:
        rows = _fetch_table_rows(api_key)
    except requests.RequestException as exc:
        logger.warning(
            "Failed to fetch pricing tiers from Notion (%s) — falling back to config.py",
            exc,
        )
        return dict(_FALLBACK)

    if not rows:
        logger.warning("Notion pricing table returned no rows — falling back to config.py")
        return dict(_FALLBACK)

    # Row 0 is the header; data starts at row 1
    tiers: dict[str, dict[str, float]] = {}
    skipped = 0
    for row in rows[1:]:
        if len(row) < 6:
            skipped += 1
            continue

        code = _extract_code(row[COL_TYPE])
        if code is None:
            skipped += 1
            continue

        values = {
            "floor":   _parse_price(row[COL_FLOOR]),
            "midweek": _parse_price(row[COL_MIDWEEK]),
            "weekend": _parse_price(row[COL_WEEKEND]),
            "peak":    _parse_price(row[COL_PEAK]),
            "ceiling": _parse_price(row[COL_CEILING]),
        }

        missing = [k for k, v in values.items() if v is None]
        if missing:
            logger.warning(
                "Notion row for %s has unparseable values for: %s — skipping row",
                code, ", ".join(missing),
            )
            skipped += 1
            continue

        tiers[code] = {k: float(v) for k, v in values.items()}  # type: ignore[arg-type]

    if skipped:
        logger.warning("Skipped %d unparseable row(s) in Notion pricing table", skipped)

    if not tiers:
        logger.warning("No valid pricing tiers parsed from Notion — falling back to config.py")
        return dict(_FALLBACK)

    # Warn about any codes present in config but missing from Notion
    missing_codes = set(_FALLBACK) - set(tiers)
    if missing_codes:
        logger.warning(
            "Notion table missing room types %s — using config.py values for those",
            sorted(missing_codes),
        )
        for code in missing_codes:
            tiers[code] = dict(_FALLBACK[code])

    return tiers
