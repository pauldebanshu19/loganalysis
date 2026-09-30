from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

#: Attributes ``logging`` puts on every record.  Anything outside this set came
#: from an ``extra=`` at the call site and belongs in the JSON output.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(
                record.created, tz=dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Send everything through one JSON handler on stdout.

    Uvicorn installs its own handlers; replacing them keeps the output one
    format rather than a mix of JSON and Uvicorn's colourised text.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # Uvicorn's own access log duplicates ours without the request id or the
    # line counts, so only one of the two is worth keeping.
    logging.getLogger("uvicorn.access").disabled = True


__all__ = ["JsonFormatter", "configure_logging"]
