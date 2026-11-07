"""Metra event evaluators: pure functions over GTFS-realtime feeds."""

import datetime
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

from courier import message
from courier.sources.metra import gtfs
from courier.sources.metra import settings

FeedMessage = gtfs_realtime_pb2.FeedMessage
TripUpdate = gtfs_realtime_pb2.TripUpdate
SKIPPED = TripUpdate.StopTimeUpdate.SKIPPED


def _start_date(tu: TripUpdate) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(tu.trip.start_date)
    except ValueError:  # start_date is optional in GTFS-realtime
        return None


def train_trips(feed: FeedMessage, schedule: gtfs.Schedule, line: str, number: str) -> list[TripUpdate]:
    """Finds a train's trip updates in a feed.

    Args:
        feed: Trip updates feed.
        schedule: Static schedule.
        line: Line id.
        number: Train number.

    Returns:
        Matching trip updates, by schedule trip id or by parsed trip id.
    """
    ids = schedule.trips.get((line, number), frozenset())
    return [
        e.trip_update
        for e in feed.entity
        if e.HasField("trip_update")
        and (e.trip_update.trip.trip_id in ids or gtfs.parse_trip_id(e.trip_update.trip.trip_id) == (line, number))
    ]


def _upcoming(
    tu: TripUpdate, schedule: gtfs.Schedule, start: datetime.date, tz: ZoneInfo
) -> tuple[str, datetime.datetime, int] | None:
    """Returns the next stop's name, expected time and minutes late, or None if there is no next stop.

    The next stop is the first update that is not skipped, has a time, and is in the schedule.
    """
    # the next stop, not the worst one: a train can make up time it lost earlier
    next_stop = next(
        (
            update
            for update in tu.stop_time_update
            if update.schedule_relationship != SKIPPED
            and (update.arrival.time or update.departure.time)
            and (tu.trip.trip_id, update.stop_sequence) in schedule.arrivals
        ),
        None,
    )
    if next_stop is None:
        return None

    scheduled_seconds = schedule.arrivals[(tu.trip.trip_id, next_stop.stop_sequence)]
    expected = next_stop.arrival.time or next_stop.departure.time
    minutes_late = int((expected - gtfs.scheduled_at(start, scheduled_seconds).timestamp()) // 60)

    stop_name = schedule.stop_names.get(next_stop.stop_id, next_stop.stop_id)
    return stop_name, datetime.datetime.fromtimestamp(expected, tz), minutes_late


def _is_cancelled(tu: TripUpdate) -> bool:
    return tu.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED or (
        bool(tu.stop_time_update) and all(s.schedule_relationship == SKIPPED for s in tu.stop_time_update)
    )


def delay_message(  # noqa: PLR0913, PLR0917 - pure function, takes every input explicitly
    tu: TripUpdate,
    schedule: gtfs.Schedule,
    line: str,
    number: str,
    event: settings.TrainDelayed,
    today: datetime.date,
    tz: ZoneInfo,
) -> message.Decision:
    """Builds a delay message when today's trip is late enough at its next stop.

    Args:
        tu: The trip update.
        schedule: Static schedule.
        line: Line id.
        number: Train number.
        event: The delay event config.
        today: Today in Metra time.
        tz: Zone for the expected time in the body.

    Returns:
        The message or None, and a one-line reason either way.
    """
    start = _start_date(tu)
    if start is None:
        return None, "trip has no start date"
    if start != today:
        return None, f"trip runs {start:%a %b %d}, not today"

    upcoming = _upcoming(tu, schedule, start, tz)
    if upcoming is None:
        return None, "no upcoming stop in schedule"

    stop_name, expected, minutes = upcoming
    if minutes < event.minutes:
        return None, f"{minutes} min late < {event.minutes}"

    delay = message.Message(
        title=f"{line} {number}: {minutes} min late",
        body=f"Next stop {stop_name}, expected {expected:%H:%M}.",
        priority="default",
        tags="train",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-delay",
    )
    return delay, f"{minutes} min late ≥ {event.minutes}"


def cancel_message(tu: TripUpdate, line: str, number: str, today: datetime.date) -> message.Decision:
    """Builds a cancellation message for a trip today or later.

    Args:
        tu: The trip update.
        line: Line id.
        number: Train number.
        today: Today in Metra time.

    Returns:
        The message or None, and a one-line reason either way.
    """
    start = _start_date(tu)
    if start is None:
        return None, "trip has no start date"
    if start < today:
        return None, f"trip ran {start:%a %b %d}"
    if not _is_cancelled(tu):
        return None, "running"

    cancellation = message.Message(
        title=f"{line} {number} cancelled",
        body=f"{start:%a %b %d} {tu.trip.start_time[:5]} departure is cancelled.",
        priority="high",
        tags="x",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-cancel",
    )
    return cancellation, "cancelled"


def alert_messages(feed: FeedMessage, line_id: str) -> list[tuple[str, message.Message]]:
    """Builds messages for alerts that affect a line.

    Args:
        feed: Alerts feed.
        line_id: Line id.

    Returns:
        (alert_id, message) pairs.
    """
    out = []

    for e in feed.entity:
        if not e.HasField("alert") or not any(i.route_id == line_id for i in e.alert.informed_entity):
            continue

        a = e.alert
        title = "Metra alert"
        if a.header_text.translation:
            title = a.header_text.translation[0].text

        body = ""
        if a.description_text.translation:
            body = a.description_text.translation[0].text
        if a.url.translation:
            body += "\n\n" + a.url.translation[0].text

        priority = "default"
        if a.severity_level == gtfs_realtime_pb2.Alert.SEVERE:
            priority = "high"

        out.append(
            (
                e.id,
                message.Message(
                    title=title,
                    body=body,
                    priority=priority,
                    tags="warning",
                    sid=e.id,
                ),
            )
        )

    return out
