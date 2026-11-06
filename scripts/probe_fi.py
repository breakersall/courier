"""One-shot, read-only Fi API probe: 1 login + 1 GraphQL query, no retries.

Usage: uv run scripts/probe_fi.py [CREDS_FILE]   (line 1 email, line 2 password)
Prints the redacted response to stdout.
"""

import asyncio
import json
import pathlib
import sys
from typing import Any

import httpx

from courier.sources.fi import client as fi

QUERY = """query { currentUser { userHouseholds { household { pets {
  __typename id name
  device {
    __typename id moduleId info nextLocationUpdateExpectedBy
    operationParams { __typename mode ledEnabled ledOffAt }
    lastConnectionState {
      __typename date
      ... on ConnectedToUser { user { id } }
      ... on ConnectedToBase { chargingBase { id } }
      ... on ConnectedToCellular { signalStrengthPercent }
      ... on UnknownConnectivity { unknownConnectivity }
    }
  }
} } } } }"""

REDACT_KEYS = {
    "id", "moduleId", "email", "firstName", "lastName", "phoneNumber", "serialNumber",
    "ssid", "credentialPackHash", "bootSessionKey",
}  # fmt: skip


def redact(node: Any, names: dict[str, str]) -> Any:
    """Returns a copy of a JSON value with identifying fields replaced.

    Args:
        node: JSON value to redact.
        names: Real-to-placeholder pet name map, filled in as pets are seen.
    """
    if isinstance(node, list):
        return [redact(n, names) for n in node]

    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}

    for key, value in node.items():
        if key == "wifiNetworkNames":
            out[key] = ["REDACTED"] * len(value)
        elif key in REDACT_KEYS and isinstance(value, str):
            out[key] = "REDACTED"
        elif key == "name" and node.get("__typename") == "Pet" and isinstance(value, str):
            out[key] = names.setdefault(value, f"Pet{len(names) + 1}")
        else:
            out[key] = redact(value, names)

    return out


async def probe(email: str, password: str) -> None:
    """Logs in once, runs the query once, and prints the redacted response."""
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            await fi.FiClient(email, password, http).login()
        except fi.FiAuthError as exc:
            sys.exit(str(exc))  # No retry to avoid account lockout

        resp = await http.get(f"{fi.BASE_URL}/graphql", params={"query": QUERY}, headers=fi.GRAPHQL_HEADERS)
        print(f"graphql HTTP {resp.status_code}", file=sys.stderr)
        print(json.dumps(redact(resp.json(), {}), indent=2))


def main() -> None:
    """Reads the credentials file and runs the probe."""
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "~/.config/courier/fi-creds")
    email, password = path.expanduser().read_text().splitlines()[:2]

    asyncio.run(probe(email, password))


if __name__ == "__main__":
    main()
