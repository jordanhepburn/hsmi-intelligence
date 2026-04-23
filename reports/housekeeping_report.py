"""
HSMI Housekeeping Report
========================
Posts a daily room-by-room housekeeping status to Slack #operations at 7am AEST
(9pm UTC, cron '0 21 * * *').

Room statuses (derived from getHousekeepingStatus):
  🔴 TURNOVER  — check-out today AND check-in today (full clean + prepare for new guest)
  🟠 CHECKOUT  — check-out today, no new arrival (full clean for departure)
  🟡 DIRTY     — vacant but dirty (shows "dirty since <day>")
  🟢 CHECKIN   — guest arriving, room already clean
  ⚪ VACANT    — empty and clean, no action needed

Rows are sorted by priority (🔴 first, ⚪ last) so Dwayne scans top-to-bottom
and stops at the white rows.

Environment variables:
  CLOUDBEDS_API_KEY            — Cloudbeds API key (required)
  CLOUDBEDS_PROPERTY_ID        — Cloudbeds property ID (required)
  SLACK_OPERATIONS_WEBHOOK_URL — Slack #operations incoming webhook (required)
"""

import logging
import os
import re
import sys
from datetime import date, datetime, timezone
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

# Status priority for sort order (lower = higher priority)
_STATUS_PRIORITY = {
    "TURNOVER": 1,
    "CHECKOUT": 2,
    "DIRTY":    3,
    "CHECKIN":  4,
    "VACANT":   5,
}

_STATUS_EMOJI = {
    "TURNOVER": "🔴",
    "CHECKOUT": "🟠",
    "DIRTY":    "🟡",
    "CHECKIN":  "🟢",
    "VACANT":   "⚪",
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
    """Fetches housekeeping status and today's arrivals, posts room table to Slack."""

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

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("=== HSMI Housekeeping Report — %s ===", self.today)

        today_str = self.today.strftime("%Y-%m-%d")

        # Step 1: Get live room status from housekeeping endpoint
        logger.info("Fetching housekeeping status")
        hs_rooms = self._fetch_housekeeping_status()
        logger.info("Housekeeping status: %d rooms returned", len(hs_rooms))

        # Step 2: Get today's check-in reservations with full detail
        logger.info("Fetching check-in reservations today")
        checkin_ids = self._fetch_res_ids(checkInFrom=today_str, checkInTo=today_str)
        logger.info("Check-ins today: %d reservations", len(checkin_ids))

        checkin_details: list[dict] = []
        for res_id in checkin_ids:
            d = self._fetch_detail(res_id)
            if d:
                checkin_details.append(d)

        # Build roomID → checkin detail map for Arriving Guest / Notes columns
        room_id_to_checkin: dict[str, dict] = {}
        for detail in checkin_details:
            for assignment in (detail.get("rooms") or detail.get("assigned") or []):
                if isinstance(assignment, dict):
                    rid = str(
                        assignment.get("roomID") or
                        assignment.get("physicalRoomID") or ""
                    )
                    if rid:
                        room_id_to_checkin[rid] = detail
        logger.info("Checkin room assignments resolved: %d rooms", len(room_id_to_checkin))

        # Step 3: Derive status for each room and build row data
        rows: list[dict] = []
        for room in hs_rooms:
            room_id   = str(room.get("roomID") or room.get("id") or "")
            room_name = str(room.get("roomName") or room.get("name") or "")
            fd_status = (room.get("frontdeskStatus") or "").lower().strip()
            condition = (room.get("roomCondition") or "").lower().strip()
            date_str  = str(room.get("date") or "")

            # Extract room number from name
            m = re.search(r"\b(\d+)\b", room_name)
            room_num  = int(m.group(1)) if m else 0
            room_type = ROOMS.get(room_num, "?")

            # Look up arriving guest for this room
            checkin_detail = room_id_to_checkin.get(room_id)

            # Determine status
            if fd_status == "check-out" and checkin_detail:
                status = "TURNOVER"
            elif fd_status == "check-out":
                status = "CHECKOUT"
            elif fd_status == "check-in":
                status = "CHECKIN"
            elif condition == "dirty":
                status = "DIRTY"
            else:
                status = "VACANT"

            # Arriving guest info
            if checkin_detail:
                surname, party = self._guest_info(checkin_detail)
                arriving_guest = f"{surname} × {party}"
                keywords = self._keywords_for(checkin_detail)
            else:
                arriving_guest = "-"
                keywords = []

            # "Dirty since <day>" label for DIRTY rooms
            dirty_label = ""
            if status == "DIRTY" and date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    dirty_label = dt.strftime("%a")
                except (ValueError, TypeError):
                    dirty_label = date_str[:3]

            rows.append({
                "room_num":      room_num,
                "room_type":     room_type,
                "status":        status,
                "priority":      _STATUS_PRIORITY.get(status, 9),
                "arriving_guest": arriving_guest,
                "keywords":      keywords,
                "dirty_label":   dirty_label,
            })

        # Sort: priority first, then room number
        rows.sort(key=lambda r: (r["priority"], r["room_num"]))

        logger.info(
            "Status summary — %s",
            ", ".join(
                f"{s}: {sum(1 for r in rows if r['status'] == s)}"
                for s in _STATUS_PRIORITY
            ),
        )

        message = self._build_message(rows)
        logger.info("Posting housekeeping report")
        self._post(message)
        logger.info("=== Housekeeping Report complete ===")

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _fetch_housekeeping_status(self) -> list[dict]:
        """Call getHousekeepingStatus and return the list of room objects."""
        try:
            resp = self.client._get("getHousekeepingStatus")
            data = resp.get("data", resp)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # May be keyed by roomID or wrapped in a sub-key
                for key in ("rooms", "housekeeping", "result"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
                return list(data.values())
            return []
        except Exception as exc:
            logger.error("getHousekeepingStatus failed: %s", exc)
            return []

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
        parts: list[str] = [
            str(detail.get(f) or "")
            for f in (
                "guestNotes", "guestNote", "internalNotes", "notes", "note",
                "specialRequests", "specialRequest", "housekeepingNotes",
                "comments", "comment", "message", "remarks",
            )
        ]
        # Also scan notes nested inside room assignment objects
        for room in (detail.get("rooms") or detail.get("assigned") or []):
            if isinstance(room, dict):
                parts.extend(
                    str(room.get(f) or "")
                    for f in ("notes", "note", "housekeepingNotes", "specialRequests", "comments")
                )
        return _extract_keywords(" ".join(parts))

    # ------------------------------------------------------------------
    # Message builder
    # ------------------------------------------------------------------

    def _build_message(self, rows: list[dict]) -> str:
        today_label = self.today.strftime("%a %d %b %Y")

        # Column widths (plain ASCII — emoji prefix sits outside these widths)
        C_ROOM   = 9    # "18 FAM" = 6 chars
        C_STATUS = 14   # "DIRTY (Mon)" = 11 chars
        C_GUEST  = 20   # "Smithington × 10" ≈ 17 chars

        # Header: 3 spaces to align with "🔴 " prefix (emoji=2 + space=1)
        sep    = "─" * (3 + C_ROOM + C_STATUS + C_GUEST + 16)
        header = (
            f"   "
            f"{'Room':<{C_ROOM}}"
            f"{'Status':<{C_STATUS}}"
            f"{'Arriving Guest':<{C_GUEST}}"
            f"Notes"
        )

        n_turnovers = n_checkouts = n_dirty = n_checkins = n_vacant = 0
        special_notes: list[str] = []
        table_rows: list[str] = []

        for r in rows:
            status    = r["status"]
            emoji     = _STATUS_EMOJI[status]
            room_num  = r["room_num"]
            room_type = r["room_type"]

            if status == "TURNOVER": n_turnovers += 1
            elif status == "CHECKOUT": n_checkouts += 1
            elif status == "DIRTY": n_dirty += 1
            elif status == "CHECKIN": n_checkins += 1
            else: n_vacant += 1

            # Status display — DIRTY shows how long it's been dirty
            if status == "DIRTY" and r["dirty_label"]:
                status_col = f"DIRTY ({r['dirty_label']})"
            else:
                status_col = status

            notes_col  = ", ".join(r["keywords"]) if r["keywords"] else "-"
            room_label = f"{room_num} {room_type}"

            if r["keywords"]:
                special_notes.append(
                    f"  {emoji} Room {room_num} ({room_type}): {notes_col}"
                )

            # Emoji sits before the fixed-width columns so padding is unaffected
            table_rows.append(
                f"{emoji} "
                f"{room_label:<{C_ROOM}}"
                f"{status_col:<{C_STATUS}}"
                f"{r['arriving_guest']:<{C_GUEST}}"
                f"{notes_col}"
            )

        lines: list[str] = [
            f"*HSMI Housekeeping — {today_label}*",
            "",
            "```",
            header,
            sep,
        ]
        lines.extend(table_rows)
        lines.append("```")
        lines.append("")
        lines.append(
            f"_{n_turnovers} turnover{'s' if n_turnovers != 1 else ''}"
            f" | {n_checkouts} checkout{'s' if n_checkouts != 1 else ''}"
            f" | {n_dirty} dirty"
            f" | {n_checkins} checkin{'s' if n_checkins != 1 else ''}"
            f" | {n_vacant} vacant_"
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
            resp = requests.post(
                self.webhook,
                json={"text": message, "username": "Ops Agent", "icon_emoji": ":clipboard:"},
                timeout=15,
            )
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
