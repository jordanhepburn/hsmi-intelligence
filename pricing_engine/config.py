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

# Hardcoded Cloudbeds room type IDs for this property.
# These are authoritative — no name or short-code matching is needed.
# Update here if room types are ever reconfigured in Cloudbeds.
#
#   Code  Cloudbeds ID        Name                  Units
#   TWI   8444747503112281    Twin Room             7
#   QUE   8444807581536336    Queen Room            2
#   SPA   8444866768408617    King Spa Room         2
#   FAM   8444603143032894    Family Room           4
#   BAL   53164553982152      Upstairs Twin Room    2
#   ACC   8444882454052890    Accessible Twin Room  1
ROOM_TYPE_ID_MAP: dict[str, dict] = {
    "TWI": {"id": "8444747503112281",  "name": "Twin Room",             "total_rooms": 7},
    "QUE": {"id": "8444807581536336",  "name": "Queen Room",            "total_rooms": 2},
    "SPA": {"id": "8444866768408617",  "name": "King Spa Room",         "total_rooms": 2},
    "FAM": {"id": "8444603143032894",  "name": "Family Room",           "total_rooms": 4},
    "BAL": {"id": "53164553982152",    "name": "Upstairs Twin Room",    "total_rooms": 2},
    "ACC": {"id": "8444882454052890",  "name": "Accessible Twin Room",  "total_rooms": 1},
}

# Cloudbeds room type IDs to silently ignore (e.g. whole-property bookings).
IGNORED_ROOM_TYPE_IDS: set[str] = {"88154598678728"}  # Motel Takeover
