"""
Victorian public holidays and school holidays for 2026–2027.

Covers a rolling 60-day window beyond today for the foreseeable future.

To add future years:
  1. Add dates to VICTORIAN_PUBLIC_HOLIDAYS list.
  2. Add (start, end) tuples to SCHOOL_HOLIDAY_PERIODS list.
     Dates are inclusive on both ends.
"""

from datetime import date
from typing import Union

# ---------------------------------------------------------------------------
# Victorian Public Holidays
# ---------------------------------------------------------------------------

VICTORIAN_PUBLIC_HOLIDAYS: list[date] = [
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 26),  # Australia Day
    date(2026, 3, 9),   # Labour Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 4),   # Easter Saturday
    date(2026, 4, 5),   # Easter Sunday
    date(2026, 4, 6),   # Easter Monday
    date(2026, 4, 25),  # ANZAC Day
    date(2026, 6, 8),   # King's Birthday
    date(2026, 9, 25),  # AFL Grand Final Friday (last Friday of September 2026)
    date(2026, 11, 3),  # Melbourne Cup
    date(2026, 12, 25), # Christmas Day
    date(2026, 12, 28), # Boxing Day (observed — 26th falls on Saturday)

    # 2027 (partial — covers the rolling 60-day window into early 2027)
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 26),  # Australia Day
    date(2027, 3, 8),   # Labour Day (second Monday of March)
    date(2027, 3, 26),  # Good Friday
    date(2027, 3, 27),  # Easter Saturday
    date(2027, 3, 28),  # Easter Sunday
    date(2027, 3, 29),  # Easter Monday
    date(2027, 4, 25),  # ANZAC Day
    date(2027, 6, 14),  # King's Birthday (second Monday of June)
    date(2027, 9, 24),  # AFL Grand Final Friday (last Friday of September 2027)
    date(2027, 11, 2),  # Melbourne Cup (first Tuesday of November)
    date(2027, 12, 25), # Christmas Day
    date(2027, 12, 27), # Boxing Day (observed)
]

_PUBLIC_HOLIDAY_SET: frozenset[date] = frozenset(VICTORIAN_PUBLIC_HOLIDAYS)

# ---------------------------------------------------------------------------
# Victorian School Holidays
# ---------------------------------------------------------------------------
# Each tuple is (first day of holidays, last day of holidays) — both inclusive.

SCHOOL_HOLIDAY_PERIODS: list[tuple[date, date]] = [
    # Summer 2025–2026
    (date(2025, 12, 19), date(2026, 1, 28)),
    # Autumn 2026
    (date(2026, 4, 4),  date(2026, 4, 17)),
    # Winter 2026
    (date(2026, 6, 27), date(2026, 7, 10)),
    # Spring 2026
    (date(2026, 9, 19), date(2026, 10, 2)),
    # Summer 2026–2027
    (date(2026, 12, 19), date(2027, 1, 27)),
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_public_holiday(d: Union[date, str]) -> bool:
    """Return True if *d* is a Victorian public holiday.

    Parameters
    ----------
    d : date | str
        A :class:`datetime.date` instance or an ISO-format string
        ``"YYYY-MM-DD"``.
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d in _PUBLIC_HOLIDAY_SET


def is_school_holiday(d: Union[date, str]) -> bool:
    """Return True if *d* falls within a Victorian school holiday period.

    Parameters
    ----------
    d : date | str
        A :class:`datetime.date` instance or an ISO-format string
        ``"YYYY-MM-DD"``.
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return any(start <= d <= end for start, end in SCHOOL_HOLIDAY_PERIODS)


def is_peak_date(d: Union[date, str]) -> bool:
    """Return True if *d* is either a public holiday or a school holiday.

    Peak dates trigger the highest pricing tier in the engine.

    Parameters
    ----------
    d : date | str
        A :class:`datetime.date` instance or an ISO-format string
        ``"YYYY-MM-DD"``.
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return is_public_holiday(d) or is_school_holiday(d)
