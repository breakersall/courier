"""Synthetic Metra GTFS zip fixtures."""

import io
import zipfile

import pytest

LINES = ["BNSF", "HC", "ME", "MD-N", "MD-W", "NCS", "RI", "SWS", "UP-N", "UP-NW", "UP-W"]
STOP_TIMES_HEADER = (
    "trip_id, arrival_time, departure_time, stop_id, stop_sequence, pickup_type, drop_off_type, "
    "center_boarding, south_boarding, bikes_allowed, notice"
)


def make_gtfs_zip(trips: list[str], stop_times: list[str]) -> bytes:
    """Build a GTFS zip using the real Metra header and spacing style.

    Args:
        trips: Trip ids.
        stop_times: Complete stop_times.txt data rows.

    Returns:
        The zip bytes.
    """
    files = {
        "trips.txt": "\n".join(
            ["route_id, service_id, trip_id, trip_headsign, block_id, shape_id, direction_id"]
            + [f"{t.split('_')[0]},  SUN,  {t},  CHICAGO,  b,  s,  0" for t in trips]
        ),
        "routes.txt": "\n".join(
            [
                "route_id, route_short_name, route_long_name, route_desc, agency_id, route_type, "
                "route_color, route_text_color, route_url"
            ]
            + [f"{x},  {x},  {x},  d,  METRA,  2,  000000,  FFFFFF,  u" for x in LINES]
        ),
        "stops.txt": "\n".join(
            ["stop_id, stop_name, stop_desc, stop_lat, stop_lon, zone_id, stop_url, wheelchair_boarding"]
            + [
                f"{i},  {n},  d,  41.0,  -87.0,  1,  u,  1"
                for i, n in [
                    ("CICERO", "Cicero"),
                    ("BNWESTERN", "Western Ave"),
                    ("HALSTED", "Halsted Street"),
                    ("CUS", "Chicago Union Station"),
                ]
            ]
        ),
        "stop_times.txt": "\n".join([STOP_TIMES_HEADER, *stop_times]),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text + "\n")

    return buf.getvalue()


@pytest.fixture
def bn1204_zip() -> bytes:
    """Zip with BN1204_V2_AA stop times plus two BN1254 trips."""
    rows = [
        f"BNSF_BN1204_V2_AA,  {t}:00,  {t}:00,  {s},  {n},  0,  0,  0,  0,  1,  0"
        for s, n, t in [
            ("CICERO", 25, "05:58"),
            ("BNWESTERN", 26, "06:03"),
            ("HALSTED", 27, "06:06"),
            ("CUS", 28, "06:15"),
        ]
    ]

    return make_gtfs_zip(["BNSF_BN1204_V2_AA", "BNSF_BN1254_V2_A", "BNSF_BN1254_V2_AA"], rows)
