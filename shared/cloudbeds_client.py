"""
Reusable Cloudbeds API client for HSMI Intelligence.

Authentication: x-api-key header.
Base URL: https://api.cloudbeds.com/api/v1.3/

All requests automatically attach the propertyID query parameter.
Retry logic: 3 attempts with exponential backoff on 429 / 5xx responses.
"""

import logging
import time
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cloudbeds.com/api/v1.3/"

CANCELLED_STATUSES = {"cancelled", "canceled", "no_show", "no-show", "noshow"}

MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds; doubles each retry


class CloudbedsAPIError(Exception):
    """Raised when the Cloudbeds API returns an unrecoverable error."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class CloudbedsClient:
    """
    Thin wrapper around the Cloudbeds REST API v1.3.

    Parameters
    ----------
    api_key : str
        The x-api-key credential for the property.
    property_id : str
        The Cloudbeds property ID; attached to every request as ``propertyID``.
    """

    def __init__(self, api_key: str, property_id: str) -> None:
        self._api_key = api_key
        self._property_id = str(property_id)
        self._session = requests.Session()
        self._session.headers.update({"x-api-key": self._api_key})
        logger.debug("CloudbedsClient initialised for property %s", self._property_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """
        Execute an HTTP request with retry logic.

        Retries on HTTP 429 (rate limit) and 5xx (server error) up to
        MAX_RETRIES times using exponential backoff.

        Raises
        ------
        CloudbedsAPIError
            On non-retryable errors or if retries are exhausted.
        """
        url = BASE_URL.rstrip("/") + "/" + endpoint.lstrip("/")

        # Merge propertyID into params
        params = kwargs.pop("params", {})
        params["propertyID"] = self._property_id

        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(
                    "%s %s params=%s (attempt %d/%d)",
                    method.upper(),
                    url,
                    params,
                    attempt,
                    MAX_RETRIES,
                )
                response = self._session.request(
                    method, url, params=params, timeout=30, **kwargs
                )

                if response.status_code == 200:
                    return response.json()

                if response.status_code in (429, 500, 502, 503, 504):
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        "HTTP %s from %s — retrying in %ss (attempt %d/%d)",
                        response.status_code,
                        endpoint,
                        wait,
                        attempt,
                        MAX_RETRIES,
                    )
                    last_exc = CloudbedsAPIError(
                        f"HTTP {response.status_code} from {endpoint}",
                        status_code=response.status_code,
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable HTTP error
                raise CloudbedsAPIError(
                    f"HTTP {response.status_code} from {endpoint}: {response.text[:200]}",
                    status_code=response.status_code,
                )

            except requests.RequestException as exc:
                wait = BACKOFF_BASE ** attempt
                logger.warning(
                    "Request error on %s: %s — retrying in %ss (attempt %d/%d)",
                    endpoint,
                    exc,
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                last_exc = exc
                time.sleep(wait)

        raise CloudbedsAPIError(
            f"Failed after {MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", endpoint, params=params or {})

    def _post(self, endpoint: str, data: Optional[dict] = None) -> dict:
        return self._request("POST", endpoint, json=data or {})

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_rate_plans(self, start_date: date, end_date: date) -> dict:
        """
        Fetch all rate plans for the property over the given date window.

        Parameters
        ----------
        start_date : date
        end_date : date

        Returns
        -------
        dict
            Raw API response from GET /getRatePlans.
        """
        params = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "detailedRates": "true",
        }
        logger.info(
            "Fetching rate plans startDate=%s endDate=%s detailedRates=true",
            params["startDate"], params["endDate"],
        )
        response = self._get("getRatePlans", params=params)

        import json as _json
        logger.info("getRatePlans raw response:\n%s", _json.dumps(response, indent=2, default=str))

        return response

    def get_rate(
        self,
        room_type_id: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, float]:
        """
        Fetch current nightly rates for a room type (GET /getRate).

        Parameters
        ----------
        room_type_id : str
        start_date : date
        end_date : date

        Returns
        -------
        dict[str, float]
            Mapping of ``"YYYY-MM-DD"`` → rate (float).
        """
        logger.info(
            "Fetching current rates via getRate: room_type=%s %s→%s",
            room_type_id, start_date, end_date,
        )
        params = {
            "roomTypeID": room_type_id,
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "detailedRates": "true",
        }
        response = self._get("getRate", params=params)

        import json as _json
        logger.debug("getRate raw response:\n%s", _json.dumps(response, indent=2, default=str))

        rates: dict[str, float] = {}
        data = response.get("data", response)
        if isinstance(data, dict):
            for date_str, rate_info in data.items():
                if isinstance(rate_info, dict):
                    val = rate_info.get("roomRate") or rate_info.get("rate")
                    if val is not None:
                        try:
                            rates[date_str] = float(val)
                        except (TypeError, ValueError):
                            pass
                else:
                    try:
                        rates[date_str] = float(rate_info)
                    except (TypeError, ValueError):
                        pass
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    d = item.get("date") or item.get("startDate")
                    r = item.get("roomRate") or item.get("rate")
                    if d and r is not None:
                        try:
                            rates[str(d)] = float(r)
                        except (TypeError, ValueError):
                            pass

        logger.debug("getRate returned %d rate entries for room_type=%s", len(rates), room_type_id)
        return rates

    def get_room_types(self) -> list[dict]:
        """
        Fetch all room types from Cloudbeds and return raw records.

        Logs the full API response at INFO level so the caller can inspect
        field names and values returned by this specific property.

        Returns
        -------
        list[dict]
            Raw room type records, each containing all fields returned by the
            API.  Common fields: roomTypeID, roomTypeName, roomTypeShortName,
            totalRooms (but these may differ per property/API version).
        """
        logger.info("Fetching room types from Cloudbeds")
        response = self._get("getRoomTypes")

        # Log the full raw response so callers can inspect the actual field names
        import json as _json
        logger.info("getRoomTypes raw response:\n%s", _json.dumps(response, indent=2, default=str))

        data = response.get("data", response)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Cloudbeds sometimes nests under 'roomTypes' as a list or dict
            raw = data.get("roomTypes", data)
            items = list(raw.values()) if isinstance(raw, dict) else raw
        else:
            items = []

        return items

    def get_reservations(self, start_date: date, end_date: date) -> list[dict]:
        """
        Fetch all active reservations overlapping the date range.

        Handles pagination automatically. Filters out cancelled and no-show
        reservations.

        Parameters
        ----------
        start_date : date
        end_date : date

        Returns
        -------
        list[dict]
            Each dict has keys: reservationID, startDate, endDate,
            roomTypeID, status.
        """
        logger.info("Fetching reservations %s→%s", start_date, end_date)
        reservations: list[dict] = []
        page_num = 1

        while True:
            params = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "pageNum": page_num,
            }
            response = self._get("getReservations", params=params)
            data = response.get("data", response)
            items = data if isinstance(data, list) else data.get("reservations", [])

            if not items:
                break

            for res in items:
                status = (
                    res.get("status") or res.get("reservationStatus") or ""
                ).lower().replace(" ", "_")

                if status in CANCELLED_STATUSES:
                    logger.debug(
                        "Skipping reservation %s with status '%s'",
                        res.get("reservationID"),
                        status,
                    )
                    continue

                reservations.append(
                    {
                        "reservationID": str(res.get("reservationID") or res.get("id") or ""),
                        "startDate": str(res.get("startDate") or res.get("checkIn") or ""),
                        "endDate": str(res.get("endDate") or res.get("checkOut") or ""),
                        "roomTypeID": str(res.get("roomTypeID") or ""),
                        "status": status,
                    }
                )

            # Determine whether there are more pages
            total_results = response.get("total") or response.get("totalResults") or 0
            page_size = response.get("pageSize") or response.get("resultsPerPage") or len(items)
            if not items or (page_size and page_num * page_size >= int(total_results)):
                break

            page_num += 1

        logger.info("Loaded %d active reservations", len(reservations))
        return reservations

    def patch_rate(
        self,
        rate_id: str,
        date_str: str,
        rate: float,
    ) -> dict:
        """
        Push a single nightly rate update to Cloudbeds (POST /patchRate).

        Uses the rateID obtained from getRatePlans, not the roomTypeID.
        Cloudbeds processes this asynchronously and returns a jobReferenceID.

        Parameters
        ----------
        rate_id : str
            The rateID for this room type / rate plan combination,
            obtained from getRatePlans.
        date_str : str
            Date in ``"YYYY-MM-DD"`` format.
        rate : float
            Target rate; rounded to nearest dollar before submission.

        Returns
        -------
        dict
            Raw API response (contains jobReferenceID for async tracking).
        """
        rounded_rate = round(rate)
        payload = {
            "rates": [
                {
                    "rateID": rate_id,
                    "interval": [
                        {
                            "startDate": date_str,
                            "endDate": date_str,
                            "rate": rounded_rate,
                        }
                    ],
                }
            ]
        }
        import json as _json
        logger.info(
            "patchRate request body: %s",
            _json.dumps(payload),
        )
        return self._post("patchRate", data=payload)
