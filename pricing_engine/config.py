"""
Pricing configuration for Hepburn Springs Motor Inn (HSMI).

Each room type has five price tiers:
  floor    — never go below this
  midweek  — Mon–Thu base rate
  weekend  — Fri–Sun base rate
  peak     — public holidays & school holiday periods
  ceiling  — never go above this

Note: BAL and ACC ceiling values sit below their peak tier — the ceiling
constraint is always applied last so rates can never exceed ceiling regardless
of which tier or modifier fires.
"""

ROOM_TYPES: dict[str, dict[str, float]] = {
    "TWI": {"floor": 105, "midweek": 160, "weekend": 190, "peak": 225, "ceiling": 260},
    "QUE": {"floor": 140, "midweek": 185, "weekend": 220, "peak": 260, "ceiling": 280},
    "SPA": {"floor": 160, "midweek": 195, "weekend": 235, "peak": 275, "ceiling": 310},
    "FAM": {"floor": 150, "midweek": 220, "weekend": 265, "peak": 310, "ceiling": 435},
    "BAL": {"floor": 175, "midweek": 195, "weekend": 235, "peak": 275, "ceiling": 260},
    "ACC": {"floor": 175, "midweek": 205, "weekend": 245, "peak": 285, "ceiling": 240},
}

# Total physical rooms across the property (used as fallback if API returns 0)
TOTAL_ROOMS: int = 18

# How far ahead the engine prices, in calendar days
LOOKAHEAD_DAYS: int = 60

# Only push a rate update to Cloudbeds if the difference exceeds this threshold
RATE_CHANGE_THRESHOLD: float = 5.0  # AUD

# Keywords used to match Cloudbeds room type names to our pricing tier codes.
# Matching is case-insensitive substring search on roomTypeName + roomTypeShortName.
# Add synonyms here if Cloudbeds uses different naming for this property.
NAME_KEYWORDS: dict[str, list[str]] = {
    "TWI": ["twin", "twi"],
    "QUE": ["queen", "que"],
    "SPA": ["spa"],
    "FAM": ["family", "fam"],
    "BAL": ["balcony", "bal"],
    "ACC": ["accessible", "acc", "access", "disability", "disabled"],
}
