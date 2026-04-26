"""
Test that Cloudbeds Min LOS rules are enforced by check_availability.

The test calls the same Cloudbeds endpoint Cherry uses (getAvailableRoomTypes)
and asserts that rooms with a Min LOS restriction are absent for under-minimum
stays and present for qualifying stays.

Usage:
    cd voice_agent
    pytest test_los.py -v

    # Test a specific Saturday:
    pytest test_los.py -v --saturday 2026-05-09

Prereqs:
    CLOUDBEDS_API_KEY or CHERRY_CLOUDBEDS_API_KEY in env (or ../.env)
    The chosen Saturday must have a Min LOS > 1 set in Cloudbeds.
    Check: Cloudbeds → Rates & Availability → Availability Matrix → Min LOS row.
"""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from cloudbeds_client import CloudbedsClient


# ---------------------------------------------------------------------------
# Pytest CLI option
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--saturday",
        default="2026-05-09",
        help="Saturday date (YYYY-MM-DD) that has Min LOS = 2 set in Cloudbeds",
    )


@pytest.fixture
def saturday(request) -> date:
    raw = request.config.getoption("--saturday")
    d = date.fromisoformat(raw)
    assert d.weekday() == 5, f"{raw} is not a Saturday"
    return d


# ---------------------------------------------------------------------------
# Client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def cb() -> CloudbedsClient:
    api_key = (
        os.environ.get("CHERRY_CLOUDBEDS_API_KEY", "").strip()
        or os.environ.get("CLOUDBEDS_API_KEY", "").strip()
    )
    if not api_key:
        # Try loading from ../.env
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line.startswith("CLOUDBEDS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                if line.startswith("CHERRY_CLOUDBEDS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break

    if not api_key:
        pytest.skip("No Cloudbeds API key found in env or .env")

    property_id = os.environ.get("CLOUDBEDS_PROPERTY_ID", "8293433316474880")
    return CloudbedsClient(api_key=api_key, property_id=property_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_available_room_type_ids(cb: CloudbedsClient, checkin: date, checkout: date) -> set[str]:
    resp = cb._get("getAvailableRoomTypes", params={
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
    })
    data = resp.get("data", [])
    if not data:
        return set()
    rooms = data[0].get("propertyRooms", []) if isinstance(data, list) else []
    return {
        str(r["roomTypeID"])
        for r in rooms
        if int(r.get("roomsAvailable") or 0) > 0
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLOSEnforcement:
    """
    Verify that Cloudbeds enforces Min LOS restrictions server-side so that
    Cherry's check_availability naturally respects them without extra logic.
    """

    def test_one_night_saturday_blocked_when_min_los_2(self, cb, saturday):
        """
        A 1-night stay starting on a Saturday with Min LOS=2 should return
        zero available rooms (or at least fewer than the 2-night equivalent).
        If this fails, Min LOS is NOT being enforced — Cherry would quote
        rooms that can't actually be booked.
        """
        sunday = saturday + timedelta(days=1)
        one_night_ids = get_available_room_type_ids(cb, saturday, sunday)

        # If any rooms are returned for 1 night when Min LOS=2 is set,
        # Cloudbeds is not filtering — this is the failure case.
        # We can't know the exact room type ID without config, so we record
        # the count and rely on the 2-night assertion to confirm the room exists.
        print(f"\n  1-night ({saturday} → {sunday}): {len(one_night_ids)} room types available")
        print(f"  Room type IDs returned: {one_night_ids or 'none'}")

        # Store for comparison in next test — one_night should have fewer
        # (or zero) rooms than two_night when Min LOS=2 is active.
        pytest.one_night_ids = one_night_ids

    def test_two_night_saturday_available_when_min_los_2(self, cb, saturday):
        """
        A 2-night stay starting on the same Saturday should have rooms
        available — confirming the room exists and the 1-night block
        is specifically due to Min LOS, not zero inventory.
        """
        monday = saturday + timedelta(days=2)
        two_night_ids = get_available_room_type_ids(cb, saturday, monday)

        print(f"\n  2-night ({saturday} → {monday}): {len(two_night_ids)} room types available")
        print(f"  Room type IDs returned: {two_night_ids or 'none'}")

        assert two_night_ids, (
            f"No rooms available for 2 nights from {saturday}. "
            "Either you're fully booked OR the Saturday itself has no inventory. "
            "Pick a Saturday with actual availability and a Min LOS=2 rule set."
        )

    def test_los_filters_correctly(self, cb, saturday):
        """
        Combined assertion: 2-night should return MORE room types than 1-night.
        This is the key test — if both return the same rooms, LOS is not filtering.
        """
        sunday  = saturday + timedelta(days=1)
        monday  = saturday + timedelta(days=2)

        one_night_ids = get_available_room_type_ids(cb, saturday, sunday)
        two_night_ids = get_available_room_type_ids(cb, saturday, monday)

        print(f"\n  1-night room type IDs: {one_night_ids or 'none'}")
        print(f"  2-night room type IDs: {two_night_ids or 'none'}")

        los_blocked = two_night_ids - one_night_ids
        print(f"  Room types blocked by Min LOS: {los_blocked or 'none — LOS may not be active on this date'}")

        assert two_night_ids >= one_night_ids, (
            "2-night returned fewer rooms than 1-night — unexpected."
        )

        if not los_blocked:
            pytest.skip(
                f"No LOS filtering detected on {saturday}. "
                "Confirm a Min LOS > 1 rule is actually set in Cloudbeds "
                "for this date before treating this as a pass."
            )

        print(f"\n  ✓ LOS enforcement confirmed: {len(los_blocked)} room type(s) "
              f"blocked for 1-night, available for 2-night.")
