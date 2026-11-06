"""Prometheus metrics, served at /metrics by the health server."""

import prometheus_client

# own registry: leaves out the default python_* and process_* collectors
REGISTRY = prometheus_client.CollectorRegistry()

POLLS = prometheus_client.Counter(
    "courier_polls", "Source polls by result (ok, error).", ["source", "result"], registry=REGISTRY
)
NOTIFICATIONS = prometheus_client.Counter(
    "courier_notifications",
    "Matched alerts by result (sent, failed, limited).",
    ["source", "target", "result"],
    registry=REGISTRY,
)
LAST_SUCCESS = prometheus_client.Gauge(
    "courier_last_success_timestamp_seconds", "Unix time of the last successful poll.", ["source"], registry=REGISTRY
)


def render() -> bytes:
    """Returns every metric in the Prometheus text format."""
    return prometheus_client.generate_latest(REGISTRY)
