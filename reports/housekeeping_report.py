"""
HSMI Housekeeping Report
========================
Posts a daily room-by-room housekeeping status to Slack #operations at 7am AEST
(9pm UTC, cron '0 21 * * *').

For each of the 18 physical rooms the report shows:
  - Clean?:    TURNOVER | CHECKOUT | STAYOVER | - (no action)
  - Check-in?: Guest surname + party size if arriving today, else -
  - Notes:     Special keywords found in reservation notes

Room statuses:
  TURNOVER  — checkout today AND checkin today (full service + prepare for new guest)
  CHECKOUT  — checking out today, no new arrival (clean for departure)
  STAYOVER  — guest in house, no movement today (room serviced on request)
  -         — vacant or CHECKIN-only (no outgoing guest; prepare but no full clean)

Environment variables:
  CLOUDBEDS_API_KEY          — Cloudbeds API key (required)
  CLOUDBEDS_PROPERTY_ID      — Cloudbeds property ID (required)
  SLACK_OPERATIONS_WEBHOOK_URL — Slack #operations incoming webhook (required)
"""

import logging
import os
import re
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

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError  # noqa: E402

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
# Room definitions — physical room number → room type code
# ---------------------------------------------------------------------------
ROOMS: dict[int, str] = {
    1: "TWI",  2: "TWI",  3: "SPA",  4: "SPA",  5: "ACC",
    6: "TWI",  7: "QUE",  8: "FAM",  9: "FAM", 10: "BAL",
    11: "BAL", 12: "TWI", 13: "TWI", 14: "TWI", 15: "TWI",
    16: "QUE", 17: "FAM", 18: "FAM",
}

# ---------------------------------------------------------------------------
# Special keywords to scan for in reservation notes
# ---------------------------------------------------------------------------
_KEYWORDS = [
    "cot",
    "foldout", "fold-out", "fold out",
    "pet", "dog",
    "late checkout", "late check-out",
    "early checkin", "early check-in",
    "accessible", "wheelchair",
]

_CANCELLED = {"cancelled", "canceled", "no_show", "no-show", "noshow"}


def _extract_keywords(text: str) -> list[str]:
    """Return deduplicated list of special keywords found in text (case-insensitive)."""
    if not text:
        return []
    tl = text.lower()
    seen: set[str] = set()
    found: list[str] = []
    for kw in _KEYWORDS:
        if kw in tl and kw not in seen:
            seen.add(kw)
            found.append(kw)
    return found


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class HousekeepingReport:
    """Fetches today's reservation activity and posts a housekeeping table to Slack."""

    def __init__(self) -> None:
        api_key     = os.environ.get("CLOUDBEDS_API_KEY", "").strip()
        property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()
        missing = [v for v, k in [("CLOUDBEDS_API_KEY", api_key), ("CLOUDBEDS_PROPERTY_ID", property_id)] if not k]
        if missing:
            logger.critical("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        self.client  = CloudbedsClient(api_key=api_key, property_id=property_id)
        self.webhook = os.environ.get("SLACK_OPERATIONS_WEBHOOK_URL", "").strip()
        if not self.webhook:
            logger.warning("SLACK_OPERATIONS_WEBHOOK_URL not set — report will print to stdout only")
        self.today = date.today()

        # Populated in _load_room_map()
        self._room_id_map: dict[str, int] = {}  # Cloudbeds roomID → physical room number

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("=== HSMI Housekeeping Report — %s ===", self.today)

        self._load_room_map()

        today_str     = self.today.strftime("%Y-%m-%d")
        tomorrow_str  = (self.today + timedelta(days=1)).strftime("%Y-%m-%d")
        cutoff_str    = (self.today + timedelta(days=90)).strftime("%Y-%m-%d")

        # Fetch reservation summaries for each category
        logger.info("Fetching checkouts today")
        checkout_ids = self._fetch_res_ids(checkOutFrom=today_str, checkOutTo=today_str)

        logger.info("Fetching checkins today")
        checkin_ids  = self._fetch_res_ids(checkInFrom=today_str, checkInTo=today_str)

        logger.info("Fetching stayovers (in house, checking out tomorrow or later)")
        # Pull all upcoming checkouts, then filter in Python for those who
        # checked in before today (excluding today's new arrivals).
        staying_raw  = self._fetch_res_ids(
            checkOutFrom=tomorrow_str, checkOutTo=cutoff_str
        )
        stayover_ids = staying_raw - checkin_ids  # exclude today's arrivals

        logger.info(
            "Counts — checkouts: %d  checkins: %d  stayovers: %d",
            len(checkout_ids), len(checkin_ids), len(stayover_ids),
        )

        # Enrich all unique reservations with full detail (room assignment, guest, notes)
        all_ids = checkout_ids | checkin_ids | stayover_ids
        details: dict[str, dict] = {}
        for res_id in all_ids:
            details[res_id] = self._fetch_detail(res_id)

        # Build per-room data
        room_data: dict[int, dict] = {
            n: {"checkout": None, "checkin": None, "stayover": None}
            for n in ROOMS
        }

        for res_id in checkout_ids:
            d = details.get(res_id, {})
            for rn in self._room_numbers_for(d):
                if rn in room_data:
                    room_data[rn]["checkout"] = d

        for res_id in checkin_ids:
            d = details.get(res_id, {})
            for rn in self._room_numbers_for(d):
                if rn in room_data:
                    room_data[rn]["checkin"] = d

        for res_id in stayover_ids:
            d = details.get(res_id, {})
            for rn in self._room_numbers_for(d):
                if rn in room_data:
                    room_data[rn]["stayover"] = d

        message = self._build_message(room_data)
        logger.info("Posting housekeeping report")
        self._post(message)
        logger.info("=== Housekeeping Report complete ===")

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _load_room_map(self) -> None:
        """
        Call getRooms to build Cloudbeds roomID → physical room number mapping.
        Falls back gracefully if the endpoint is unavailable.
        """
        try:
            resp = self.client._get("getRooms")
            data = resp.get("data", resp)
            items: list[dict] = (
                list(data.values()) if isinstance(data, dict)
                else data if isinstance(data, list)
                else []
            )
            for room in items:
                room_id = str(
                    room.get("roomID") or room.get("physicalRoomID") or
                    room.get("id") or ""
                )
                room_name = str(room.get("roomName") or room.get("name") or "")
                if room_id:
                    m = re.search(r"\b(\d+)\b", room_name)
                    if m:
                        self._room_id_map[room_id] = int(m.group(1))
            logger.info("Room map: %d entries loaded", len(self._room_id_map))
        except Exception as exc:
            logger.warning("getRooms failed: %s — room numbers may be missing from report", exc)

    def _fetch_res_ids(self, **params) -> set[str]:
        """
        Paginated getReservations fetch with arbitrary filter params.
        Returns a set of reservationID strings (cancelled/no-show excluded).
        """
        ids: set[str] = set()
        page = 1
        while True:
            try:
                resp = self.client._get("getReservations", params={**params, "pageNum": page})
            except CloudbedsAPIError as exc:
                logger.warning("getReservations failed (params=%s, page=%d): %s", params, page, exc)
                break

            data  = resp.get("data", [])
            items: list[dict] = (
                data if isinstance(data, list)
                else data.get("reservations", []) if isinstance(data, dict)
                else []
            )
            if not items:
                break

            for item in items:
                status = (
                    item.get("status") or item.get("reservationStatus") or ""
                ).lower().replace(" ", "_")
                if status in _CANCELLED:
                    continue
                res_id = str(item.get("reservationID") or "")
                if res_id:
                    ids.add(res_id)

            count = int(resp.get("count") or len(items))
            total = int(resp.get("total") or 0)
            if not items or (total and page * count >= total):
                break
            page += 1

        return ids

    def _fetch_detail(self, res_id: str) -> dict:
        """Return full reservation detail dict from getReservation (singular)."""
        try:
            resp = self.client._get("getReservation", params={"reservationID": res_id})
            return resp.get("data", resp)
        except CloudbedsAPIError as exc:
            logger.warning("getReservation %s failed: %s", res_id, exc)
            return {}

    # ------------------------------------------------------------------
    # Data extraction helpers
    # ------------------------------------------------------------------

    def _room_numbers_for(self, detail: dict) -> list[int]:
        """
        Extract physical room numbers from the 'assigned' array in a reservation
        detail. Tries the room ID map first, falls back to parsing room name.
        """
        nums: list[int] = []
        for assignment in detail.get("assigned", []):
            room_id = str(
                assignment.get("roomID") or
                assignment.get("physicalRoomID") or ""
            )
            if room_id and room_id in self._room_id_map:
                nums.append(self._room_id_map[room_id])
                continue
            # Fallback: parse room number from name/number field
            room_name = str(
                assignment.get("roomName") or
                assignment.get("roomNumber") or ""
            )
            m = re.search(r"\b(\d+)\b", room_name)
            if m:
                n = int(m.group(1))
                if n in ROOMS:
                    nums.append(n)
        return nums

    def _guest_info(self, detail: dict) -> tuple[str, int]:
        """Return (surname, party_size) from a reservation detail dict."""
        full_name = (
            detail.get("guestName") or
            detail.get("fullName") or
            (detail.get("firstName", "") + " " + detail.get("lastName", "")).strip()
        ).strip()
        surname = full_name.split()[-1].title() if full_name else "Guest"

        try:
            adults   = int(detail.get("adults", 1) or 1)
            children = int(detail.get("children", 0) or detail.get("kids", 0) or 0)
            party    = max(adults + children, 1)
        except (TypeError, ValueError):
            party = 1

        return surname, party

    def _keywords_for(self, detail: dict) -> list[str]:
        """Scan all note-like fields in a reservation for special keywords."""
        text = " ".join(
            str(detail.get(f) or "")
            for f in (
                "guestNotes", "internalNotes", "notes",
                "specialRequests", "housekeepingNotes", "comments",
            )
        )
        return _extract_keywords(text)

    # ------------------------------------------------------------------
    # Message builder
    # ------------------------------------------------------------------

    def _build_message(self, room_data: dict[int, dict]) -> str:
        today_label = self.today.strftime("%a %d %b %Y")

        C_ROOM    = 10   # "18 - FAM" = 8 chars
        C_CLEAN   = 12   # "TURNOVER" = 8 chars
        C_CHECKIN = 20   # "Smithington × 10" ≈ 17 chars

        sep    = "─" * (C_ROOM + C_CLEAN + C_CHECKIN + 20)
        header = (
            f"{'Room':<{C_ROOM}}"
            f"{'Clean?':<{C_CLEAN}}"
            f"{'Check-in?':<{C_CHECKIN}}"
            f"Notes"
        )

        n_checkouts = n_turnovers = n_stayovers = n_checkins = 0
        special_notes: list[str] = []
        rows: list[str] = []

        for room_num in sorted(ROOMS):
            room_type = ROOMS[room_num]
            rd  = room_data[room_num]
            co  = rd["checkout"]
            ci  = rd["checkin"]
            so  = rd["stayover"]

            # --- Determine clean status ---
            if co and ci:
                clean = "TURNOVER"
                n_turnovers += 1
            elif co:
                clean = "CHECKOUT"
                n_checkouts += 1
            elif so:
                clean = "STAYOVER"
                n_stayovers += 1
            elif ci:
                clean = "-"         # arriving into vacant room — prepare but no full clean
                n_checkins += 1
            else:
                clean = "-"         # vacant

            # --- Check-in column ---
            if ci:
                surname, party = self._guest_info(ci)
                checkin_col = f"{surname} × {party}"
            else:
                checkin_col = "-"

            # --- Notes: keywords from all active reservation(s) for this room ---
            keywords: list[str] = []
            for res in (co, ci, so):
                if res:
                    keywords.extend(self._keywords_for(res))
            # Deduplicate while preserving order
            seen_kw: set[str] = set()
            unique_kw = [k for k in keywords if not (k in seen_kw or seen_kw.add(k))]  # type: ignore[func-returns-value]
            notes_col = ", ".join(unique_kw) if unique_kw else "-"

            if unique_kw:
                special_notes.append(f"  Room {room_num} ({room_type}): {', '.join(unique_kw)}")

            room_label = f"{room_num} - {room_type}"
            rows.append(
                f"{room_label:<{C_ROOM}}"
                f"{clean:<{C_CLEAN}}"
                f"{checkin_col:<{C_CHECKIN}}"
                f"{notes_col}"
            )

        lines: list[str] = [
            f"*HSMI Housekeeping — {today_label}*",
            "",
            "```",
            header,
            sep,
        ]
        lines.extend(rows)
        lines.append("```")
        lines.append("")
        lines.append(
            f"_{n_checkouts} checkout{'s' if n_checkouts != 1 else ''}"
            f" | {n_turnovers} turnover{'s' if n_turnovers != 1 else ''}"
            f" | {n_stayovers} stayover{'s' if n_stayovers != 1 else ''}"
            f" | {n_checkins} checkin{'s' if n_checkins != 1 else ''}_"
        )

        if special_notes:
            lines.append("")
            lines.append("*Special notes:*")
            lines.extend(special_notes)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Slack post
    # ------------------------------------------------------------------

    def _post(self, message: str) -> None:
        if not self.webhook:
            print(message)
            return
        try:
            resp = requests.post(self.webhook, json={"text": message}, timeout=15)
            resp.raise_for_status()
            logger.info("Housekeeping report posted to Slack #operations")
        except requests.RequestException as exc:
            logger.error("Slack post failed: %s — printing to stdout", exc)
            print(message)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    HousekeepingReport().run()
