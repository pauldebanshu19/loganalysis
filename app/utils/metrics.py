from __future__ import annotations

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

#: Latency buckets chosen around the service's targets -- a 10 MB file under 2 s and
#: a 100 MB file under 10 s -- so the histogram has resolution where the
#: promises are, not just near zero.
_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

requests_total = Counter(
    "api_requests_total",
    "Requests by route, method and status.",
    ["route", "method", "status"],
)

request_duration_seconds = Histogram(
    "api_request_duration_seconds",
    "Request duration by route and method.",
    ["route", "method"],
    buckets=_LATENCY_BUCKETS,
)

errors_total = Counter(
    "api_errors_total",
    "Errors by code, counted wherever they are raised.",
    ["code"],
)

lines_processed_total = Counter(
    "analysis_lines_processed_total",
    "Non-blank log lines analysed.",
)

unparseable_lines_total = Counter(
    "analysis_unparseable_lines_total",
    "Log lines that broke a parsing rule.",
)

bytes_processed_total = Counter(
    "analysis_bytes_processed_total",
    "Uploaded bytes analysed.",
)

analysis_duration_seconds = Histogram(
    "analysis_duration_seconds",
    "Time from first byte to finished summary.",
    buckets=_LATENCY_BUCKETS,
)

slots_in_use = Gauge(
    "analysis_slots_in_use",
    "Analysis slots currently held.",
)

slots_total = Gauge(
    "analysis_slots_total",
    "Analysis slots this worker has.",
)

slot_waits_total = Counter(
    "analysis_slot_waits_total",
    "Requests that had to wait for a slot, by outcome.",
    ["outcome"],
)


def build_registry() -> CollectorRegistry:
    """The registry ``/metrics`` should serve: this process's own counters.

    With ``uvicorn --workers N`` each worker keeps its own counters, so a
    scrape reports whichever worker answered.
    """
    return REGISTRY


def record_analysis(*, lines: int, unparseable: int, size_bytes: int, seconds: float) -> None:
    """Record one finished analysis."""
    lines_processed_total.inc(lines)
    unparseable_lines_total.inc(unparseable)
    bytes_processed_total.inc(size_bytes)
    analysis_duration_seconds.observe(seconds)


__all__ = [
    "analysis_duration_seconds",
    "build_registry",
    "bytes_processed_total",
    "errors_total",
    "lines_processed_total",
    "record_analysis",
    "request_duration_seconds",
    "requests_total",
    "slot_waits_total",
    "slots_in_use",
    "slots_total",
    "unparseable_lines_total",
]
