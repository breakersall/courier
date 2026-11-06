import datetime
import json
import pathlib
from typing import Any

import httpx
import pytest

from courier.sources.fi import client as fi

FIXTURE = json.loads((pathlib.Path(__file__).parents[2] / "fixtures" / "households.json").read_text())
LOGIN_OK = {"userId": "u1", "sessionId": "s1"}


def by_name(pets: list[fi.PetStatus]) -> dict[str, fi.PetStatus]:
    return {p.name: p for p in pets}


def test_parse_pets_v2() -> None:
    rex = by_name(fi.parse_pets(FIXTURE["data"]))["Rex"]

    assert rex.battery_percent == 37
    assert rex.is_charging is None
    assert rex.charging is True
    assert rex.connection_type == "ConnectedToBase"
    assert rex.last_connection == datetime.datetime(2026, 10, 9, 14, 2, tzinfo=datetime.UTC)


def test_parse_pets_v1() -> None:
    bella = by_name(fi.parse_pets(FIXTURE["data"]))["Bella"]
    assert bella.is_charging is False
    assert bella.charging is False
    assert bella.mode == "LOST_DOG"


def test_parse_float_battery_and_string_info() -> None:
    data = _with_rex(info=json.dumps({"batteryPercent": 40.6}))
    assert by_name(fi.parse_pets(data))["Rex"].battery_percent == 41


def test_parse_skips_missing_device_and_bad_info(caplog: pytest.LogCaptureFixture) -> None:
    assert [p.name for p in fi.parse_pets(FIXTURE["data"])] == ["Rex", "Bella"]
    assert "Broken" in caplog.text


class FakeFi:
    """Scripted Fi API: `graphql` is a list of responses served in order."""

    def __init__(self, graphql: list[httpx.Response], login: httpx.Response | None = None) -> None:
        self.graphql = graphql
        self.login = login or httpx.Response(200, json=LOGIN_OK)
        self.logins = 0
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/auth/login":
            self.logins += 1
            return self.login
        return self.graphql.pop(0)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_login_failure_raises() -> None:
    fake = FakeFi([], login=httpx.Response(200, json={"error": {"message": "bad"}}))
    async with fake.client() as http:
        with pytest.raises(fi.FiAuthError) as exc:
            await fi.FiClient("me@example.com", "s3cret", http).login()
    assert "s3cret" not in str(exc.value)


async def test_fetch_sends_csrf_safe_content_type() -> None:
    fake = FakeFi([httpx.Response(200, json=FIXTURE)])
    async with fake.client() as http:
        pets = await fi.FiClient("me@example.com", "pw", http).fetch_pets()

    assert len(pets) == 2

    gql = fake.requests[-1]
    assert gql.method == "GET"
    assert gql.headers["content-type"] == "application/json"
    assert "batteryPercent" not in gql.url.params["query"]  # The info blob is fetched whole
    assert "lastConnectionState" in gql.url.params["query"]


async def test_fetch_relogin_on_401() -> None:
    fake = FakeFi([httpx.Response(401), httpx.Response(200, json=FIXTURE)])
    async with fake.client() as http:
        pets = await fi.FiClient("me@example.com", "pw", http).fetch_pets()
    assert len(pets) == 2
    assert fake.logins == 2


async def test_fetch_second_401_raises() -> None:
    fake = FakeFi([httpx.Response(401), httpx.Response(401)])
    async with fake.client() as http:
        with pytest.raises(fi.FiAuthError):
            await fi.FiClient("me@example.com", "pw", http).fetch_pets()
    assert fake.logins == 2


async def test_fetch_relogin_on_graphql_auth_error() -> None:
    unauth = httpx.Response(200, json={"errors": [{"message": "Unauthorized"}]})
    fake = FakeFi([unauth, httpx.Response(200, json=FIXTURE)])
    async with fake.client() as http:
        pets = await fi.FiClient("me@example.com", "pw", http).fetch_pets()

    assert len(pets) == 2
    assert fake.logins == 2


def _with_rex(**device: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(FIXTURE["data"]))
    data["currentUser"]["userHouseholds"][0]["household"]["pets"][0]["device"].update(device)
    return data


@pytest.mark.parametrize(
    "device",
    [
        {"info": None},
        {"lastConnectionState": {"__typename": "ConnectedToBase", "date": None}},
        {"lastConnectionState": {"__typename": "ConnectedToBase", "date": "2026-10-09T14:02:00"}},
    ],
    ids=["info-null", "date-null", "date-naive"],
)
def test_parse_skips_malformed_pet(device: dict[str, Any]) -> None:
    assert [p.name for p in fi.parse_pets(_with_rex(**device))] == ["Bella"]


def test_pet_names_include_skipped_pets() -> None:
    assert fi.pet_names(FIXTURE["data"]) == {"Rex", "Bella", "Broken"}


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": {"currentUser": None}}, ["x"]], ids=str)
async def test_fetch_bad_shape_raises_fi_error(body: Any) -> None:
    fake = FakeFi([httpx.Response(200, json=body)])
    async with fake.client() as http:
        with pytest.raises(fi.FiError):
            await fi.FiClient("me@example.com", "pw", http).fetch_pets()
