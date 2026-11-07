"""Minimal async client for the Fi (tryfi.com) API."""

import dataclasses
import datetime
import json
import logging
from typing import Any

import httpx

from courier import message

log = logging.getLogger(__name__)

BASE_URL = "https://api.tryfi.com"
# Apollo's CSRF guard rejects GETs without a non-simple content-type
GRAPHQL_HEADERS = {"content-type": "application/json"}
STATUS_QUERY = (
    "query { currentUser { userHouseholds { household { pets { id name device { info "
    "operationParams { mode } lastConnectionState { __typename date "
    "... on ConnectedToBase { chargingBase { id } } } } } } } } }"
)


@dataclasses.dataclass(frozen=True, slots=True)
class PetStatus:
    """A collared pet's current status.

    Attributes:
        pet_id: Fi pet id.
        name: Pet name as shown in the Fi app.
        battery_percent: Battery level, 0-100.
        is_charging: Charging flag; only V1 collars report it, otherwise None.
        connection_type: GraphQL type of the last connection, e.g. "ConnectedToBase".
        last_connection: When the collar last connected (aware, UTC).
        mode: "NORMAL" or "LOST_DOG".
    """

    pet_id: str
    name: str
    battery_percent: int
    is_charging: bool | None
    connection_type: str
    last_connection: datetime.datetime
    mode: str

    @property
    def charging(self) -> bool:
        """Whether the collar is charging or sitting on its base."""
        return self.is_charging is True or self.connection_type == "ConnectedToBase"


def parse_fi_date(value: str) -> datetime.datetime:
    """Parses a Fi ISO-8601 timestamp.

    Args:
        value: Timestamp such as "2026-10-09T14:02:00.000Z".

    Returns:
        An aware datetime.

    Raises:
        ValueError: The value is malformed or has no timezone.
    """
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"date without timezone: {value!r}")
    return parsed


def _parse_pet(pet: dict[str, Any]) -> PetStatus:
    device = pet["device"]
    info = device["info"]
    if isinstance(info, str):
        info = json.loads(info)

    conn = device["lastConnectionState"]
    charging = info.get("isCharging")
    if charging is not None:
        charging = bool(charging)

    return PetStatus(
        pet_id=pet["id"],
        name=pet["name"],
        battery_percent=round(float(info["batteryPercent"])),
        is_charging=charging,
        connection_type=conn["__typename"],
        last_connection=parse_fi_date(conn["date"]),
        mode=device["operationParams"]["mode"],
    )


def _collared(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        pet
        for household in data["currentUser"]["userHouseholds"]
        for pet in household["household"]["pets"]
        if pet.get("device")
    ]


def pet_names(data: dict[str, Any]) -> set[str]:
    """Returns names of all collared pets, including ones whose status could not be parsed.

    Args:
        data: The GraphQL `data` object from the status query.
    """
    return {pet["name"] for pet in _collared(data)}


def parse_pets(data: dict[str, Any]) -> list[PetStatus]:
    """Parses collared pets from a status query, skipping (and logging) malformed ones.

    Args:
        data: The GraphQL `data` object from the status query.

    Returns:
        Status of every collared pet whose device data parsed.
    """
    pets: list[PetStatus] = []

    for pet in _collared(data):
        try:
            pets.append(_parse_pet(pet))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("skipping pet %r: unexpected device data (%r)", pet.get("name"), exc)

    return pets


def _is_auth_failure(resp: httpx.Response) -> bool:
    if not resp.is_success:
        return resp.status_code in {401, 403}

    try:
        errors = resp.json().get("errors") or []
        messages = [str(error.get("message", "")).lower() for error in errors]
        return any("unauth" in error_message for error_message in messages)
    except ValueError, AttributeError:
        return False


class FiClient:
    """Session-based Fi API client.

    Args:
        email: Account email.
        password: Account password.
        client: HTTP client to use; the caller owns its lifecycle.
    """

    def __init__(self, email: str, password: str, client: httpx.AsyncClient) -> None:
        self._email = email
        self._password = password
        self._http = client
        self._logged_in = False

    async def login(self) -> None:
        """Logs in; the session cookie is kept by the HTTP client.

        Raises:
            SourceAuthError: Fi rejected the credentials.
        """
        resp = await self._http.post(f"{BASE_URL}/auth/login", data={"email": self._email, "password": self._password})

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.is_error or not isinstance(body, dict) or "error" in body:
            raise message.SourceAuthError(f"Fi login failed (HTTP {resp.status_code})")

        self._logged_in = True
        log.info("logged in to Fi")

    async def _query(self) -> httpx.Response:
        return await self._http.get(f"{BASE_URL}/graphql", params={"query": STATUS_QUERY}, headers=GRAPHQL_HEADERS)

    async def fetch_pets(self) -> tuple[list[PetStatus], set[str]]:
        """Fetches status of all collared pets, logging in again once if the session expired.

        Returns:
            Status of every collared pet whose device data parsed, and the names of all
            collared pets, including ones whose data did not parse.

        Raises:
            SourceAuthError: Login failed or the session was rejected after re-login.
            SourceError: Fi returned an error or an unexpected response shape.
            httpx.HTTPError: The request failed.
        """
        if not self._logged_in:
            await self.login()

        resp = await self._query()
        if _is_auth_failure(resp):
            log.info("Fi session expired; logging in again")
            await self.login()
            resp = await self._query()
            if _is_auth_failure(resp):
                raise message.SourceAuthError("Fi rejected the session after re-login")

        resp.raise_for_status()
        try:
            body = resp.json()
            if body.get("errors"):
                raise message.SourceError(f"Fi GraphQL error: {body['errors'][0].get('message')}")
            pets = parse_pets(body["data"])
            names = pet_names(body["data"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise message.SourceError(f"unexpected Fi response shape: {exc!r}") from exc

        return pets, names
