"""Gunicorn configuration.

One Uvicorn worker per CPU core.  Threads inside a worker share a single core
for Python code because of the GIL, so the thread pool the upload reader uses
buys overlap between reading and parsing, not parallelism -- parallelism has to
come from processes.

Workers share nothing except Redis, so adding containers behind a load balancer
needs no code change.
"""

from __future__ import annotations

import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8000")

#: One worker per core, overridable because a container is often given a
#: fraction of a core and `cpu_count()` reports the host's, not the cgroup's.
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count()))

worker_class = "uvicorn.workers.UvicornWorker"

#: Long enough for a 100 MB upload over a slow connection. The per-request
#: idle timeout in `app/upload.py` is what actually catches a stalled client;
#: this is the backstop for a worker that has genuinely wedged.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 300))
graceful_timeout = 30
keepalive = 5

#: Uvicorn's own access log is disabled in `app/logging.py`; the JSON access
#: log the middleware writes carries the request id and the line counts.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()

#: Recycle workers periodically so a slow leak anywhere in the stack cannot
#: accumulate over days. The jitter stops every worker restarting at once.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 10_000))
max_requests_jitter = 500


def on_starting(server) -> None:  # noqa: ANN001 - gunicorn's hook signature
    server.log.info("starting %d worker(s) on %s", workers, bind)
