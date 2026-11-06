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


def delay_message(  # noqa: PLR0913, PLR0917 - pure function, takes every input explicitly
    tu: TripUpdate,
    schedule: gtfs.Schedule,
    line: str,
    number: str,
    event: settings.TrainDelayed,
    today: datetime.date,
    tz: ZoneInfo,
) -> message.Message | None:
    """Builds a delay message when today's trip is late enough.

    Args:
        tu: The trip update.
        schedule: Static schedule.
        line: Line id.
        number: Train number.
        event: The delay event config.
        today: Today in Metra time.
        tz: Zone for the expected time in the body.

    Returns:
        The message, or None.
    """
    start = _start_date(tu)
    if start is None or start != today:
        return None

    delays = [
        stu.arrival.time - gtfs.scheduled_at(start, secs).timestamp()
        for stu in tu.stop_time_update
        if stu.arrival.time and (secs := schedule.arrivals.get((tu.trip.trip_id, stu.stop_sequence))) is not None
    ]
    if not delays:
        return None

    minutes = int(max(delays) // 60)
    if minutes < event.minutes:
        return None

    nxt = next(
        (s for s in tu.stop_time_update if s.schedule_relationship != SKIPPED and (s.arrival.time or s.departure.time)),
        None,
    )
    # Every stop is skipped or untimed (delay came from earlier stops): name the first update, no time.
    stop = nxt or tu.stop_time_update[0]
    body = f"Next stop {schedule.stop_names.get(stop.stop_id, stop.stop_id)}"
    if nxt is not None:
        body += f", expected {datetime.datetime.fromtimestamp(nxt.arrival.time or nxt.departure.time, tz):%H:%M}"
    body += "."

    return message.Message(
        title=f"{line} {number}: {minutes} min late",
        body=body,
        priority="default",
        tags="train",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-delay",
    )


def cancel_message(tu: TripUpdate, line: str, number: str, today: datetime.date) -> message.Message | None:
    """Builds a cancellation message for a trip today or later.

    Args:
        tu: The trip update.
        line: Line id.
        number: Train number.
        today: Today in Metra time.

    Returns:
        The message, or None.
    """
    start = _start_date(tu)
    cancelled = tu.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED or (
        bool(tu.stop_time_update) and all(s.schedule_relationship == SKIPPED for s in tu.stop_time_update)
    )

    if start is None or start < today or not cancelled:
        return None

    return message.Message(
        title=f"{line} {number} cancelled",
        body=f"{start:%a %b %d} {tu.trip.start_time[:5]} departure is cancelled.",
        priority="high",
        tags="x",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-cancel",
    )


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
        title = a.header_text.translation[0].text if a.header_text.translation else "Metra alert"
        body = a.description_text.translation[0].text if a.description_text.translation else ""
        if a.url.translation:
            body += "\n\n" + a.url.translation[0].text

        out.append(
            (
                e.id,
                message.Message(
                    title=title,
                    body=body,
                    priority="high" if a.severity_level == gtfs_realtime_pb2.Alert.SEVERE else "default",
                    tags="warning",
                    sid=e.id,
                ),
            )
        )

    return out
