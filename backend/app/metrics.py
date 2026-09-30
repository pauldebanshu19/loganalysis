"""Prometheus metrics.

The same Counter-and-Histogram-per-request pattern the reference repo uses in
Flask ``after_request`` hooks, moved into FastAPI middleware so it covers every
route including the ones FastAPI generates.

Routes are labelled by their template (``/api/v1/analyses/{id}``) and never by
the resolved path, or every analysis id would mint a new time series.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess

#: Latency buckets chosen around the PRD's targets -- a 10 MB file under 2 s and
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
    multiprocess_mode="livesum",
)

slots_total = Gauge(
    "analysis_slots_total",
    "Analysis slots this worker has.",
    multiprocess_mode="livesum",
)

slot_waits_total = Counter(
    "analysis_slot_waits_total",
    "Requests that had to wait for a slot, by outcome.",
    ["outcome"],
)


def build_registry() -> CollectorRegistry:
    """The registry ``/metrics`` should serve.

    Under Gunicorn each worker is its own process with its own counters, so
    scraping one of them would report one worker's view.  With
    ``PROMETHEUS_MULTIPROC_DIR`` set, prometheus-client collects every worker's
    counters from that directory instead; without it, this is the plain
    in-process registry.
    """
    import os

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry

    from prometheus_client import REGISTRY

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
