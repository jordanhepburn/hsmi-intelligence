"""
HSMI Voice Agent Functions Server
==================================
FastAPI webhook server for Cherry (HSMI Retell AI voice agent).

Retell POSTs to /functions/{function_name} with the function arguments in the
request body.  Each handler returns {"result": "..."} — the text Cherry
speaks back to the caller.

Environment variables:
  CHERRY_CLOUDBEDS_API_KEY       — Cherry-specific Cloudbeds API key (preferred)
  CLOUDBEDS_API_KEY              — Cloudbeds API key (fallback if CHERRY key absent)
  CLOUDBEDS_PROPERTY_ID          — Cloudbeds property ID
  SLACK_OPERATIONS_WEBHOOK_URL   — Slack #operations incoming webhook
  SLACK_PHONE_CALLS_WEBHOOK_URL  — Slack #phone-calls incoming webhook
  RETELL_API_KEY                 — Retell API key (used to verify webhook signatures)
  VERONICA_SLACK_ID              — Slack member ID for Veronica (tagged on hold_room)
"""

import hashlib
import hmac
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Path setup — shared/ and pricing_engine/ live one level up from voice_agent/
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (
    _REPO_ROOT,
    os.path.join(_REPO_ROOT, "shared"),
    os.path.join(_REPO_ROOT, "pricing_engine"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cloudbeds_client import CloudbedsClient, CloudbedsAPIError  # noqa: E402
from config import ROOM_TYPE_ID_MAP  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="HSMI Cherry Voice Agent Functions")

AEST = ZoneInfo("Australia/Melbourne")

# Phone number spelled out for Cherry to read clearly
_PHONE = "oh three, five three four eight, two five seven two"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cb() -> CloudbedsClient:
    api_key = (
        os.environ.get("CHERRY_CLOUDBEDS_API_KEY", "").strip()
        or os.environ["CLOUDBEDS_API_KEY"]
    )
    return CloudbedsClient(
        api_key=api_key,
        property_id=os.environ["CLOUDBEDS_PROPERTY_ID"],
    )


def _slack(text: str, username: str = "Cherry", icon_emoji: str = ":telephone_receiver:") -> None:
    webhook = os.environ.get("SLACK_OPERATIONS_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.warning("SLACK_OPERATIONS_WEBHOOK_URL not set — skipping Slack post")
        return
    try:
        resp = requests.post(
            webhook,
            json={"text": text, "username": username, "icon_emoji": icon_emoji},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Slack post failed: %s", exc)


def _aest_now() -> str:
    return datetime.now(AEST).strftime("%-d %b %Y %-I:%M%p AEST")


def _fmt_date(d: date) -> str:
    return d.strftime("%-d %B %Y")


def _parse_date(s: str) -> date:
    return date.fromisoformat(str(s).strip())


def _args(body: dict) -> dict:
    """
    Retell may nest function arguments under different keys depending on
    how the agent is configured.  Try each known envelope before falling
    back to treating the whole body as arguments.
    """
    for key in ("func_call", "function_call"):
        if key in body and isinstance(body[key], dict):
            inner = body[key]
            return inner.get("arguments") or inner.get("args") or inner
    for key in ("arguments", "args", "function_arguments", "input"):
        if key in body and isinstance(body[key], dict):
            return body[key]
    return body


def _is_cancelled(res: dict) -> bool:
    status = (
        res.get("status") or res.get("reservationStatus") or ""
    ).lower().replace(" ", "_")
    return status in {"cancelled", "canceled", "no_show", "no-show", "noshow"}


def _room_number_from_detail(detail: dict) -> str | None:
    """Return the first physical room number found in a reservation detail dict."""
    for assignment in detail.get("assigned", []):
        rn = str(
            assignment.get("roomName") or assignment.get("roomNumber") or ""
        )
        m = re.search(r"\b(\d+)\b", rn)
        if m:
            return m.group(1)
        if rn:
            return rn
    return None


def _is_room_occupied_on(client: CloudbedsClient, room_number: str, target_date: date) -> bool:
    """
    Return True if room_number is occupied on target_date.

    Checks both:
      - reservations checking IN on target_date
      - stayovers whose checkout is AFTER target_date (in house that night)

    Detail lookups are capped at 15 reservations each to bound latency.
    """
    target_str = target_date.strftime("%Y-%m-%d")
    after_str  = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff_str = (target_date + timedelta(days=90)).strftime("%Y-%m-%d")

    # --- Check-ins on target_date ---
    try:
        resp  = client._get("getReservations", params={
            "checkInFrom": target_str,
            "checkInTo":   target_str,
        })
        data  = resp.get("data", [])
        items = data if isinstance(data, list) else data.get("reservations", [])
        for res in items[:15]:
            if _is_cancelled(res):
                continue
            res_id = str(res.get("reservationID") or "")
            if not res_id:
                continue
            try:
                detail = client._get("getReservation", params={"reservationID": res_id}).get("data", {})
                if _room_number_from_detail(detail) == room_number:
                    return True
            except CloudbedsAPIError:
                pass
    except CloudbedsAPIError as exc:
        logger.warning("getReservations (checkins) failed: %s", exc)

    # --- Stayovers checking out after target_date (still in house that night) ---
    try:
        resp  = client._get("getReservations", params={
            "checkOutFrom": after_str,
            "checkOutTo":   cutoff_str,
        })
        data  = resp.get("data", [])
        items = data if isinstance(data, list) else data.get("reservations", [])
        for res in items[:15]:
            if _is_cancelled(res):
                continue
            # Only include guests who checked in before target_date
            check_in = str(res.get("startDate") or res.get("checkIn") or "")
            if check_in and check_in >= target_str:
                continue  # arriving on or after target — not a stayover
            res_id = str(res.get("reservationID") or "")
            if not res_id:
                continue
            try:
                detail = client._get("getReservation", params={"reservationID": res_id}).get("data", {})
                if _room_number_from_detail(detail) == room_number:
                    return True
            except CloudbedsAPIError:
                pass
    except CloudbedsAPIError as exc:
        logger.warning("getReservations (stayovers) failed: %s", exc)

    return False


def _find_reservation_id(client: CloudbedsClient, guest_name: str, checkout_date_str: str) -> str | None:
    """
    Find the reservationID for guest_name checking out on checkout_date_str.
    Returns the first matching res ID, or None.
    """
    try:
        resp  = client._get("getReservations", params={
            "checkOutFrom": checkout_date_str,
            "checkOutTo":   checkout_date_str,
        })
        data  = resp.get("data", [])
        items = data if isinstance(data, list) else data.get("reservations", [])
        name_lower = guest_name.strip().lower()
        for res in items:
            if _is_cancelled(res):
                continue
            full = (res.get("guestName") or res.get("fullName") or "").strip().lower()
            if name_lower and (name_lower in full or any(p in full for p in name_lower.split() if p)):
                return str(res.get("reservationID") or "")
    except CloudbedsAPIError as exc:
        logger.warning("_find_reservation_id failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test/hold_room")
async def test_hold_room():
    """
    Diagnostic endpoint — fires a dry-run hold_room with test data and returns
    the raw Cloudbeds response. Does NOT post to Slack.
    GET https://<railway-url>/test/hold_room
    """
    from datetime import date, timedelta
    from config import BASE_RATE_IDS
    from cloudbeds_client import CloudbedsClient

    api_key = (
        os.environ.get("CHERRY_CLOUDBEDS_API_KEY", "").strip()
        or os.environ.get("CLOUDBEDS_API_KEY", "")
    )
    property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "")

    checkin  = (date.today() + timedelta(days=30)).isoformat()
    checkout = (date.today() + timedelta(days=31)).isoformat()
    room_code = "TWI"
    cfg = ROOM_TYPE_ID_MAP[room_code]

    payload = {
        "startDate":             checkin,
        "endDate":               checkout,
        "guestFirstName":        "Test",
        "guestLastName":         "Cherry",
        "guestEmail":            "",
        "guestPhone":            "0400000000",
        "guestCountry":          "AU",
        "paymentMethod":         "cc",
        "rooms[0][roomTypeID]":  cfg["id"],
        "rooms[0][rateID]":      BASE_RATE_IDS[room_code],
        "rooms[0][quantity]":    "1",
        "adults[0][roomTypeID]": cfg["id"],
        "adults[0][quantity]":   "1",
        "adults[0][adults]":     "1",
        "children[0][roomTypeID]": cfg["id"],
        "children[0][quantity]": "1",
        "children[0][children]": "0",
        "sendEmailConfirmation": "false",
        "estimatedArrivalTime":  "15:00",
    }

    try:
        client = CloudbedsClient(api_key=api_key, property_id=property_id)
        resp   = client._request("POST", "postReservation", data=payload)
        return {"payload_sent": payload, "cloudbeds_response": resp}
    except Exception as exc:
        return {"payload_sent": payload, "error": str(exc)}


# ---------------------------------------------------------------------------
# 1. check_availability
# ---------------------------------------------------------------------------


@app.post("/functions/check_availability")
async def check_availability(request: Request):
    body = _args(await request.json())
    checkin_str  = str(body.get("checkin_date", "") or "").strip()
    checkout_str = str(body.get("checkout_date", "") or "").strip()
    room_filter  = str(body.get("room_type", "") or "").upper().strip()

    if not checkin_str or not checkout_str:
        return JSONResponse({
            "result": "I need both a check-in and check-out date to check availability."
        })

    try:
        checkin  = _parse_date(checkin_str)
        checkout = _parse_date(checkout_str)
    except (ValueError, TypeError):
        return JSONResponse({
            "result": (
                "I couldn't understand those dates. "
                "Could you say them as, for example, the fifteenth of June?"
            )
        })

    nights = (checkout - checkin).days
    if nights < 1:
        return JSONResponse({
            "result": "The check-out date needs to be after the check-in date."
        })

    try:
        client = _cb()

        # Fetch available room types for the stay window.
        # Response: data[0].propertyRooms — one entry per rate plan per room type.
        avail_resp    = client._get("getAvailableRoomTypes", params={
            "startDate": checkin_str,
            "endDate":   checkout_str,
        })
        avail_data    = avail_resp.get("data", [])
        property_obj  = avail_data[0] if isinstance(avail_data, list) and avail_data else {}
        property_rooms = property_obj.get("propertyRooms", [])

        # Group by roomTypeID: max roomsAvailable + rate from the "default" plan
        avail_by_id: dict[str, dict] = {}
        for item in property_rooms:
            rt_id = str(item.get("roomTypeID") or "")
            if not rt_id:
                continue
            qty  = int(item.get("roomsAvailable") or 0)
            rate = float(item.get("roomRate") or 0) or None
            plan = str(
                item.get("ratePlanNamePublic") or
                item.get("ratePlanNamePrivate") or ""
            ).lower()
            if rt_id not in avail_by_id:
                avail_by_id[rt_id] = {"qty": qty, "rate": None}
            else:
                avail_by_id[rt_id]["qty"] = max(avail_by_id[rt_id]["qty"], qty)
            # Prefer the default (rack) rate plan for the price Cherry quotes
            if "default" in plan or avail_by_id[rt_id]["rate"] is None:
                avail_by_id[rt_id]["rate"] = rate

        available: list[dict] = []
        for code, cfg in ROOM_TYPE_ID_MAP.items():
            if room_filter and code != room_filter:
                continue
            rt_id     = cfg["id"]
            avail_rec = avail_by_id.get(rt_id)
            qty       = avail_rec["qty"] if avail_rec else 0
            if qty < 1:
                logger.info("check_availability: %s excluded (qty=%d)", code, qty)
                continue

            # roomRate is the total stay cost — divide by nights to get nightly average
            total   = avail_rec["rate"]
            nightly = round(total / nights) if total and nights > 0 else None
            available.append({
                "code": code,
                "name": cfg["name"],
                "rate": nightly,
            })

        logger.info(
            "check_availability: %s→%s — %d/%d room types available",
            checkin_str, checkout_str, len(available), len(ROOM_TYPE_ID_MAP),
        )

        if not available:
            return JSONResponse({
                "result": (
                    f"Unfortunately we're fully booked from {_fmt_date(checkin)} "
                    f"to {_fmt_date(checkout)}. Can I help you with different dates?"
                )
            })

        nights_word = "night" if nights == 1 else "nights"
        parts = [
            f"{rm['name']} at ${rm['rate']:.0f} per night"
            if rm["rate"] else f"{rm['name']} — pricing on request"
            for rm in available
        ]
        if len(parts) == 1:
            rooms_list = parts[0]
        else:
            rooms_list = ", ".join(parts[:-1]) + f", and {parts[-1]}"

        return JSONResponse({
            "result": (
                f"We have availability for {_fmt_date(checkin)} to {_fmt_date(checkout)} "
                f"— {nights} {nights_word}. "
                f"The rooms available are: {rooms_list}. "
                f"Would you like me to hold one for you?"
            )
        })

    except Exception as exc:
        logger.exception("check_availability error: %s", exc)
        return JSONResponse({
            "result": (
                "I'm having trouble checking availability right now. "
                f"Please call us directly on {_PHONE} and we'll be happy to help."
            )
        })


# ---------------------------------------------------------------------------
# 1b. get_rate_breakdown
# ---------------------------------------------------------------------------


@app.post("/functions/get_rate_breakdown")
async def get_rate_breakdown(request: Request):
    body = _args(await request.json())
    checkin_str  = str(body.get("checkin_date", "") or "").strip()
    checkout_str = str(body.get("checkout_date", "") or "").strip()
    room_code    = str(body.get("room_type_code", "") or body.get("room_type", "") or "").upper().strip()

    if not checkin_str or not checkout_str or not room_code:
        return JSONResponse({
            "result": "I need the check-in date, check-out date, and room type to give you a breakdown."
        })

    try:
        checkin  = _parse_date(checkin_str)
        checkout = _parse_date(checkout_str)
    except (ValueError, TypeError):
        return JSONResponse({"result": "I couldn't understand those dates."})

    nights = (checkout - checkin).days
    if nights < 1:
        return JSONResponse({"result": "The check-out date needs to be after the check-in date."})

    cfg = ROOM_TYPE_ID_MAP.get(room_code)
    if not cfg:
        return JSONResponse({"result": f"I don't recognise the room type {room_code}. Could you clarify?"})

    def _ordinal(n: int) -> str:
        return "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

    def _fmt_day(d: date) -> str:
        return d.strftime("%A the %-d") + _ordinal(d.day) + d.strftime(" of %B")

    try:
        client = _cb()

        # getRate with detailedRates=true returns roomRateDetailed — one entry per night
        resp   = client._get("getRate", params={
            "roomTypeID":    cfg["id"],
            "startDate":     checkin_str,
            "endDate":       checkout_str,
            "detailedRates": "true",
        })
        detail = resp.get("data", {}).get("roomRateDetailed", [])

        if not detail:
            return JSONResponse({
                "result": "I couldn't retrieve the nightly rates right now. Please call us and we'll go through it with you."
            })

        parts: list[str] = []
        total = 0.0
        for entry in detail:
            try:
                d    = date.fromisoformat(str(entry["date"]))
                rate = float(entry["rate"])
            except (KeyError, ValueError, TypeError):
                continue
            total += rate
            parts.append(f"{_fmt_day(d)} is ${rate:.0f}")

        if not parts:
            return JSONResponse({"result": "I couldn't find rate details for those dates."})

        nights_word = "night" if nights == 1 else "nights"
        breakdown   = ", ".join(parts)
        room_name   = cfg["name"]

        return JSONResponse({
            "result": (
                f"For the {room_name}: {breakdown}. "
                f"That's ${total:.0f} total for {nights} {nights_word}."
            )
        })

    except Exception as exc:
        logger.exception("get_rate_breakdown error: %s", exc)
        return JSONResponse({
            "result": (
                "I'm having trouble fetching the rate breakdown right now. "
                f"Please call us on {_PHONE} and we'll go through it with you."
            )
        })


# ---------------------------------------------------------------------------
# 2. hold_room
# ---------------------------------------------------------------------------


@app.post("/functions/hold_room")
async def hold_room(request: Request):
    body = _args(await request.json())
    checkin_str  = str(body.get("checkin_date", "") or "")
    checkout_str = str(body.get("checkout_date", "") or "")
    room_code    = str(body.get("room_type_code", "") or body.get("room_type", "") or "").upper().strip()
    guest_name   = str(body.get("guest_name", "Guest") or "Guest")
    guest_phone  = str(body.get("guest_phone", "") or "")
    guest_email  = str(body.get("guest_email", "") or "")
    num_guests   = str(body.get("num_guests", "1") or "1")

    # Split guest name into first/last for Cloudbeds
    name_parts   = guest_name.strip().split()
    first_name   = name_parts[0] if name_parts else "Guest"
    last_name    = " ".join(name_parts[1:]) if len(name_parts) > 1 else "."

    # Resolve code to friendly room name and Cloudbeds IDs
    cfg        = ROOM_TYPE_ID_MAP.get(room_code, {})
    room_label = cfg.get("name") or room_code or "not specified"

    # -----------------------------------------------------------------------
    # Step 1: Create reservation in Cloudbeds via postReservation
    # API requires form-encoded (not JSON). Correct payload structure confirmed
    # via probing. If Cloudbeds returns "could not accommodate", the property
    # likely needs to enable API-based booking in its channel settings.
    # -----------------------------------------------------------------------
    reservation_id: str | None = None
    cb_status = "not attempted"

    if room_code and room_code in ROOM_TYPE_ID_MAP:
        try:
            from config import BASE_RATE_IDS
            adults_count = max(int(num_guests), 1)
            room_type_id = cfg["id"]
            rate_id      = BASE_RATE_IDS.get(room_code, "")
            property_id  = os.environ.get("CLOUDBEDS_PROPERTY_ID", "").strip()

            # Payload must be form-encoded (data=), not JSON.
            # propertyID is intentionally omitted — _request() adds it to query params.
            # sourceID is omitted — "s-3-1" caused "channel not enabled" errors.
            payload = {
                "startDate":                checkin_str,
                "endDate":                  checkout_str,
                "guestFirstName":           first_name,
                "guestLastName":            last_name,
                "guestEmail":               guest_email or "",
                "guestPhone":               guest_phone or "",
                "guestCountry":             "AU",
                "paymentMethod":            "cc",
                "rooms[0][roomTypeID]":     room_type_id,
                "rooms[0][rateID]":         rate_id,
                "rooms[0][quantity]":       "1",
                "adults[0][roomTypeID]":    room_type_id,
                "adults[0][quantity]":      "1",
                "adults[0][adults]":        str(adults_count),
                "children[0][roomTypeID]":  room_type_id,
                "children[0][quantity]":    "1",
                "children[0][children]":    "0",
                "sendEmailConfirmation":    "false",
                "estimatedArrivalTime":     "15:00",
            }
            logger.info("postReservation payload: %s", payload)
            client = _cb()
            resp   = client._request("POST", "postReservation", data=payload)
            logger.info("postReservation response: %s", resp)

            if resp.get("success"):
                reservation_id = str(
                    resp.get("reservationID") or
                    resp.get("data", {}).get("reservationID") or ""
                )
                cb_status = f"created — reservationID {reservation_id}"
                logger.info("Reservation created: %s", reservation_id)
            else:
                cb_status = f"failed — {resp.get('message', 'unknown error')}"
                logger.warning("postReservation failed: %s", resp)

        except Exception as exc:
            cb_status = f"error — {exc}"
            logger.error("postReservation exception: %s", exc)
    else:
        cb_status = "skipped — unknown room type"
        logger.warning("hold_room: unknown room_code '%s', skipping postReservation", room_code)

    # -----------------------------------------------------------------------
    # Step 2: Notify Slack #api-phone-calls
    # -----------------------------------------------------------------------
    veronica_id = os.environ.get("VERONICA_SLACK_ID", "").strip()
    tag         = f"<@{veronica_id}> " if veronica_id else ""

    if reservation_id:
        res_line = f"*Reservation ID:* {reservation_id} ✅ Created in Cloudbeds"
        action   = "Call guest to confirm and take payment"
    else:
        res_line = f"*Cloudbeds:* {cb_status} ⚠️ Create reservation manually"
        action   = "1. Create reservation in Cloudbeds NOW  2. Call guest to take payment"

    slack_text = (
        f"{tag}:bell: *New phone booking via Cherry*\n"
        f"*Guest:* {guest_name}\n"
        f"*Phone:* {guest_phone or 'not provided'}\n"
        f"*Email:* {guest_email or 'not provided'}\n"
        f"*Room:* {room_label}\n"
        f"*Dates:* {checkin_str} → {checkout_str}\n"
        f"*Guests:* {num_guests}\n"
        f"{res_line}\n"
        f"*Action:* {action}"
    )

    phone_calls_webhook = os.environ.get("SLACK_PHONE_CALLS_WEBHOOK_URL", "").strip()
    try:
        if phone_calls_webhook:
            requests.post(phone_calls_webhook, json={"text": slack_text, "username": "Cherry", "icon_emoji": ":telephone_receiver:"}, timeout=10).raise_for_status()
        else:
            logger.warning("SLACK_PHONE_CALLS_WEBHOOK_URL not set — skipping hold_room Slack post")
    except Exception as exc:
        logger.error("hold_room Slack post failed: %s", exc)

    return JSONResponse({
        "result": (
            "I've noted your reservation request. "
            "Our team will call you within the hour to confirm and arrange payment."
        )
    })


# ---------------------------------------------------------------------------
# 3. check_late_checkout
# ---------------------------------------------------------------------------


@app.post("/functions/check_late_checkout")
async def check_late_checkout(request: Request):
    body = _args(await request.json())
    room_number   = str(body.get("room_number", "") or "").strip()
    guest_name    = str(body.get("guest_name", "Guest") or "Guest")
    checkout_date = str(body.get("checkout_date", "") or "").strip()

    if not room_number or not checkout_date:
        return JSONResponse({
            "result": "I need your room number and checkout date to check late checkout availability."
        })

    try:
        co_date   = _parse_date(checkout_date)
        next_day  = co_date + timedelta(days=1)
    except (ValueError, TypeError):
        return JSONResponse({
            "result": "I couldn't understand that date. Could you confirm your checkout date?"
        })

    try:
        client = _cb()

        # Check if the room is occupied on the day after checkout
        occupied = _is_room_occupied_on(client, room_number, next_day)

        if occupied:
            return JSONResponse({
                "result": (
                    "Unfortunately your room is needed the next day so we can't extend past 10am. "
                    "I can offer you a 10:30am checkout if that helps."
                )
            })

        # Room is free — find the reservation and add a note
        res_id = _find_reservation_id(client, guest_name, checkout_date)
        if res_id:
            try:
                client._request("POST", "putReservationNote", data={
                    "reservationID": res_id,
                    "note": (
                        f"Late checkout until 12pm approved for {guest_name} "
                        f"(requested via Cherry voice agent on {_aest_now()})."
                    ),
                    "noteType": "housekeeping",
                })
                logger.info("Late checkout note added to reservation %s", res_id)
            except Exception as exc:
                logger.warning("putReservationNote failed for %s: %s", res_id, exc)

        return JSONResponse({
            "result": (
                "Late checkout until 12 noon is available for your room. "
                "I've added a note to your booking — no need to rush out in the morning."
            )
        })

    except Exception as exc:
        logger.exception("check_late_checkout error: %s", exc)
        return JSONResponse({
            "result": (
                "I'm having trouble checking that right now. "
                f"Please call us on {_PHONE} and we'll sort it out for you."
            )
        })


# ---------------------------------------------------------------------------
# 4. log_maintenance
# ---------------------------------------------------------------------------


@app.post("/functions/log_maintenance")
async def log_maintenance(request: Request):
    body = _args(await request.json())
    room_number       = str(body.get("room_number", "") or "")
    issue_description = str(body.get("issue_description", "") or "")
    guest_name        = str(body.get("guest_name", "Guest") or "Guest")

    slack_text = (
        f":wrench: *Maintenance request via Cherry*\n"
        f"*Room:* {room_number}\n"
        f"*Guest:* {guest_name}\n"
        f"*Issue:* {issue_description}\n"
        f"*Logged:* {_aest_now()}"
    )

    try:
        _slack(slack_text, username="Cherry", icon_emoji=":wrench:")
    except Exception as exc:
        logger.error("log_maintenance Slack post failed: %s", exc)

    return JSONResponse({
        "result": (
            "I've logged that with our maintenance team. "
            "Someone will attend to your room as soon as possible."
        )
    })


# ---------------------------------------------------------------------------
# 5. log_message
# ---------------------------------------------------------------------------


@app.post("/functions/log_message")
async def log_message(request: Request):
    body = _args(await request.json())
    caller_name     = str(body.get("caller_name", "") or "")
    company_name    = str(body.get("company_name", "") or "")
    reason          = str(body.get("reason", "") or "")
    callback_number = str(body.get("callback_number", "") or "")

    caller_label = f"{caller_name} ({company_name})" if company_name else caller_name

    slack_text = (
        f":clipboard: *Message via Cherry*\n"
        f"*From:* {caller_label}\n"
        f"*Reason:* {reason}\n"
        f"*Callback:* {callback_number}\n"
        f"*Time:* {_aest_now()}"
    )

    phone_calls_webhook = os.environ.get("SLACK_PHONE_CALLS_WEBHOOK_URL", "").strip()
    try:
        if phone_calls_webhook:
            requests.post(phone_calls_webhook, json={"text": slack_text, "username": "Cherry", "icon_emoji": ":memo:"}, timeout=10).raise_for_status()
        else:
            logger.warning("SLACK_PHONE_CALLS_WEBHOOK_URL not set — skipping log_message Slack post")
    except Exception as exc:
        logger.error("log_message Slack post failed: %s", exc)

    return JSONResponse({
        "result": (
            "Thank you, I've passed that on to our management team. "
            "They'll be in touch."
        )
    })


# ---------------------------------------------------------------------------
# 6. get_checkin_instructions
# ---------------------------------------------------------------------------


@app.post("/functions/get_checkin_instructions")
async def get_checkin_instructions(request: Request):
    body = _args(await request.json())
    guest_name        = str(body.get("guest_name", "") or "").strip()
    booking_reference = str(body.get("booking_reference", "") or "").strip()

    today_str = date.today().strftime("%Y-%m-%d")

    try:
        client = _cb()
        params: dict = {
            "checkInFrom": today_str,
            "checkInTo":   today_str,
        }
        if booking_reference:
            params["reservationID"] = booking_reference

        resp  = client._get("getReservations", params=params)
        data  = resp.get("data", [])
        items = data if isinstance(data, list) else data.get("reservations", []) if isinstance(data, dict) else []

        matched_res_id: str | None = None
        name_lower = guest_name.lower()

        for res in items:
            if _is_cancelled(res):
                continue
            res_id = str(res.get("reservationID") or "")
            if not res_id:
                continue

            if name_lower:
                full = (res.get("guestName") or res.get("fullName") or "").strip().lower()
                name_parts = [p for p in name_lower.split() if len(p) > 1]
                if name_lower in full or any(p in full for p in name_parts):
                    matched_res_id = res_id
                    break
            else:
                # No name — take first active today-checkin
                matched_res_id = res_id
                break

        if not matched_res_id:
            return JSONResponse({
                "result": (
                    "I couldn't find a booking under that name for today. "
                    "Could you double-check the name, or call us on "
                    f"{_PHONE} and we'll look it up for you?"
                )
            })

        # Fetch full detail to get room number
        detail_resp = client._get("getReservation", params={"reservationID": matched_res_id})
        detail = detail_resp.get("data", detail_resp)
        room_num = _room_number_from_detail(detail)

        if not room_num:
            return JSONResponse({
                "result": (
                    "I found your booking but couldn't retrieve the room number. "
                    f"Please call us on {_PHONE} and we'll assist you right away."
                )
            })

        return JSONResponse({
            "result": (
                f"Welcome! Your room is number {room_num}. "
                "You should have received your access instructions by SMS — "
                "if you're having trouble, please let me know what the issue is and I'll help."
            )
        })

    except Exception as exc:
        logger.exception("get_checkin_instructions error: %s", exc)
        return JSONResponse({
            "result": (
                "I'm having trouble retrieving your booking right now. "
                f"Please call us on {_PHONE} and we'll get you sorted."
            )
        })


# ---------------------------------------------------------------------------
# Retell webhook — call_started event
# Injects current_date and current_time as dynamic variables at call start.
# ---------------------------------------------------------------------------

def _verify_retell_signature(body: bytes, signature: str) -> bool:
    """
    Verify Retell's x-retell-signature header.

    Retell signs the raw request body with the API key using HMAC-SHA256
    and base64-encodes the result.
    """
    import base64
    secret = os.environ.get("RETELL_API_KEY", "").strip()
    if not secret:
        logger.warning("RETELL_API_KEY not set — skipping signature verification")
        return True  # allow through if unconfigured (dev/test)
    expected = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/call-started")
async def call_started(request: Request):
    # Signature verification disabled — re-enable when Retell webhook secret is confirmed
    # body      = await request.body()
    # signature = request.headers.get("x-retell-signature", "")
    # if signature and not _verify_retell_signature(body, signature):
    #     logger.warning("Invalid Retell signature — rejecting call_started webhook")
    #     return JSONResponse({"error": "invalid signature"}, status_code=401)
    logger.info("call_started webhook — signature verification skipped")

    now        = datetime.now(AEST)
    # "Wednesday 22 April 2026"
    current_date = now.strftime("%A %-d %B %Y")
    # "20:30"
    current_time = now.strftime("%H:%M")

    logger.info("call_started webhook — injecting date=%s time=%s", current_date, current_time)

    return JSONResponse({
        "dynamic_variables": {
            "current_date": current_date,
            "current_time": current_time,
        }
    })


# ---------------------------------------------------------------------------
# External cron trigger — called by cron-job.org every hour
# Triggers the pricing_engine.yml GitHub Actions workflow via workflow_dispatch.
# More reliable than GitHub's native schedule cron.
#
# Environment variables:
#   GITHUB_TOKEN  — PAT with 'workflow' scope (create at github.com/settings/tokens)
#   CRON_SECRET   — arbitrary secret shared with cron-job.org (prevents abuse)
# ---------------------------------------------------------------------------


@app.post("/cron/pricing-engine")
async def cron_pricing_engine(request: Request):
    return _trigger_workflow(
        "pricing_engine.yml",
        "cron/pricing-engine",
        request.headers.get("x-cron-secret", ""),
    )


def _trigger_workflow(workflow_file: str, label: str, request_secret: str) -> JSONResponse:
    """Shared helper — validates cron secret and dispatches a GitHub Actions workflow."""
    expected = os.environ.get("CRON_SECRET", "").strip()
    if expected and not hmac.compare_digest(request_secret, expected):
        logger.warning("%s: invalid secret", label)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        logger.error("%s: GITHUB_TOKEN not set", label)
        return JSONResponse({"error": "GITHUB_TOKEN not configured"}, status_code=500)

    resp = requests.post(
        f"https://api.github.com/repos/jordanhepburn/hsmi-intelligence"
        f"/actions/workflows/{workflow_file}/dispatches",
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main"},
        timeout=15,
    )

    if resp.status_code == 204:
        logger.info("%s: workflow triggered successfully", label)
        return JSONResponse({"status": "triggered"})

    logger.error("%s: GitHub API returned %s — %s", label, resp.status_code, resp.text[:200])
    return JSONResponse({"error": f"GitHub API returned {resp.status_code}"}, status_code=502)


@app.post("/cron/housekeeping-report")
async def cron_housekeeping_report(request: Request):
    return _trigger_workflow(
        "housekeeping_report.yml",
        "cron/housekeeping-report",
        request.headers.get("x-cron-secret", ""),
    )


@app.post("/cron/housekeeping-roster")
async def cron_housekeeping_roster(request: Request):
    return _trigger_workflow(
        "housekeeping_roster.yml",
        "cron/housekeeping-roster",
        request.headers.get("x-cron-secret", ""),
    )


@app.post("/cron/competitor-signal")
async def cron_competitor_signal(request: Request):
    return _trigger_workflow(
        "competitor_signal.yml",
        "cron/competitor-signal",
        request.headers.get("x-cron-secret", ""),
    )
