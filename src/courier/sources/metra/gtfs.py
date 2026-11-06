"""Metra static GTFS schedule parsing."""

from collections.abc import Iterator
import csv
import dataclasses
import datetime
import io
import re
import zipfile
import zoneinfo

METRA_TZ = zoneinfo.ZoneInfo("America/Chicago")

_TRIP_ID = re.compile(r"^(?P<line>[^_]+)_[A-Z]+(?P<number>\d+)_")


@dataclasses.dataclass(frozen=True, slots=True)
class Schedule:
    """Parsed static schedule.

    Attributes:
        trips: GTFS trip ids per (line, train number), for every train.
        lines: Route ids.
        stop_names: Stop name per stop id.
        arrivals: Arrival seconds since service-day start, per (trip id, stop sequence), for configured trains.
    """

    trips: dict[tuple[str, str], frozenset[str]]
    lines: frozenset[str]
    stop_names: dict[str, str]
    arrivals: dict[tuple[str, int], int]


def parse_trip_id(trip_id: str) -> tuple[str, str] | None:
    """Splits a trip id into (line, train number).

    Args:
        trip_id: GTFS trip id such as ``BNSF_BN1254_V2_A``.

    Returns:
        The (line, number) pair, or None if the id does not match.
    """
    m = _TRIP_ID.match(trip_id)
    return (m["line"], m["number"]) if m else None


def gtfs_seconds(value: str) -> int:
    """Converts a GTFS ``HH:MM:SS`` time (hours may exceed 24) to seconds."""
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def scheduled_at(start_date: datetime.date, seconds: int) -> datetime.datetime:
    """Resolves seconds into a service day to an aware datetime.

    GTFS times count from noon minus 12 hours, which keeps DST days correct.

    Args:
        start_date: Service day.
        seconds: Seconds since the service-day start.

    Returns:
        The aware datetime in Metra's timezone.
    """
    noon = datetime.datetime.combine(start_date, datetime.time(12), tzinfo=METRA_TZ).astimezone(datetime.UTC)
    return (noon - datetime.timedelta(hours=12) + datetime.timedelta(seconds=seconds)).astimezone(METRA_TZ)


def load_schedule(zip_bytes: bytes, wanted: set[tuple[str, str]]) -> Schedule:
    """Parses the static GTFS schedule zip.

    Args:
        zip_bytes: Contents of the schedule zip.
        wanted: (line, number) trains whose stop times are kept.

    Returns:
        The parsed schedule.

    Raises:
        ValueError: If the zip is invalid or a required file is missing.
    """

    def rows(name: str) -> Iterator[dict[str, str]]:
        with zf.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"), skipinitialspace=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            trips: dict[tuple[str, str], set[str]] = {}
            for row in rows("trips.txt"):
                if key := parse_trip_id(row["trip_id"]):
                    trips.setdefault(key, set()).add(row["trip_id"])
            lines = frozenset(row["route_id"] for row in rows("routes.txt"))
            stop_names = {row["stop_id"]: row["stop_name"] for row in rows("stops.txt")}
            wanted_ids = set().union(*(trips.get(k, set()) for k in wanted))
            arrivals = {
                (row["trip_id"], int(row["stop_sequence"])): gtfs_seconds(row["arrival_time"])
                for row in rows("stop_times.txt")
                if row["trip_id"] in wanted_ids
            }
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid GTFS schedule: {e}") from e
    return Schedule({k: frozenset(v) for k, v in trips.items()}, lines, stop_names, arrivals)
