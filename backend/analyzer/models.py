"""Pydantic models for the analysis result.

These are the contract shared by the analyzer, the HTTP API, the CLI and the web
UI.  The API serialises :class:`AnalysisResult` as-is, and the frontend's
TypeScript types are generated from the OpenAPI schema these models produce, so
a change here propagates everywhere by design.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

ANALYZER_VERSION = "1.0.0"

#: Unparseable sample text is cut at this many characters before being stored or
#: returned, so a pathological line can never bloat a result document.
SAMPLE_TEXT_LIMIT = 500


class LogLevel(str, Enum):
    """The five levels the brief's format defines.

    Anything else on a line makes that line unparseable; see
    :class:`ParseReason.UNKNOWN_LEVEL`.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


#: Levels that count towards a service's error count.  FATAL is a worse ERROR,
#: not a separate category, so it is counted as an error as well.
ERROR_LEVELS: frozenset[str] = frozenset({LogLevel.ERROR.value, LogLevel.FATAL.value})

#: Fast membership test used on the parser's hot path.
LEVEL_NAMES: frozenset[str] = frozenset(level.value for level in LogLevel)


class ParseReason(str, Enum):
    """Why a line could not be parsed.

    Clients switch on these codes to explain a bad line to a human, so they are
    part of the published schema and are never reworded in place.
    """

    MISSING_TIMESTAMP = "missing_timestamp"
    """The line does not start with ``YYYY-MM-DD HH:MM:SS``."""

    INVALID_TIMESTAMP = "invalid_timestamp"
    """The timestamp has the right shape but is not a real date or time."""

    UNKNOWN_LEVEL = "unknown_level"
    """The level field is absent or is not one of the five known levels."""

    MISSING_SERVICE = "missing_service"
    """A timestamp and level are present but no service name follows."""

    LINE_TOO_LONG = "line_too_long"
    """The line is longer than the configured maximum and was truncated."""


class LevelCounts(BaseModel):
    """How many lines of each level a single service produced."""

    DEBUG: int = 0
    INFO: int = 0
    WARN: int = 0
    ERROR: int = 0
    FATAL: int = 0


class ServiceStats(BaseModel):
    """Per-service totals.

    Every service seen in the file appears here, including ones with no errors
    at all -- "which component is failing" is only answerable if the quiet
    components are listed too.
    """

    service: str = Field(description="Service name, exactly as it appeared in the log.")
    error_count: int = Field(description="ERROR plus FATAL lines for this service.")
    error_rate: float = Field(
        description="error_count divided by this service's parsed lines, 0.0 to 1.0."
    )
    levels: LevelCounts = Field(description="Parsed line counts per level.")


class UnparseableSample(BaseModel):
    """One example of a line that could not be parsed."""

    line_number: int = Field(
        description="1-based line number in the uploaded file, counting blank lines."
    )
    reason: ParseReason = Field(description="Which parsing rule the line broke.")
    text: str = Field(
        description=f"The line itself, cut at {SAMPLE_TEXT_LIMIT} characters."
    )


class TimeRange(BaseModel):
    """Earliest and latest timestamp seen.

    These are the extremes over all parsed lines, not the first and last line of
    the file, because logs from several sources are not always in order.  Both
    are null when nothing parsed.
    """

    first: datetime | None = None
    last: datetime | None = None


class AnalysisMeta(BaseModel):
    """Everything about the request rather than the log's contents."""

    filename: str | None = Field(
        default=None, description="Sanitised upload filename, if the client sent one."
    )
    bytes: int = Field(default=0, description="Size of the analysed input in bytes.")
    duration_ms: int = Field(default=0, description="Wall-clock time spent analysing.")
    analyzer_version: str = Field(default=ANALYZER_VERSION)


class AnalysisResult(BaseModel):
    """The complete answer to "which service is failing and how badly"."""

    id: str = Field(description="Opaque id; fetch this result again at /api/v1/analyses/{id}.")
    lines_processed: int = Field(
        description="Every non-blank line read, whether it parsed or not."
    )
    unparseable_lines: int = Field(description="Lines that broke a parsing rule.")
    blank_lines: int = Field(description="Empty or whitespace-only lines, not processed.")
    services: list[ServiceStats] = Field(
        description="Sorted by error_count descending, then by name."
    )
    top_offenders: list[str] = Field(
        description="Service(s) with the most errors. Empty when there were no errors."
    )
    unparseable_samples: list[UnparseableSample] = Field(
        description="The first N bad lines, oldest first."
    )
    time_range: TimeRange
    meta: AnalysisMeta


#: Crockford base32, minus the letters that look like digits.  Ids are copied
#: into URLs and read aloud, so the ambiguous characters are worth losing.
_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_analysis_id() -> str:
    """Return a fresh, sortable analysis id such as ``an_01J9Z4K7Q2F3KD9M``.

    The first ten characters encode the millisecond timestamp, so ids sort
    chronologically; the last six are random, which is far more than enough to
    stay unique within the one hour a result lives.
    """
    ms = int(time.time() * 1000) & ((1 << 50) - 1)
    stamp = "".join(_ID_ALPHABET[(ms >> shift) & 0x1F] for shift in range(45, -1, -5))
    rand = "".join(_ID_ALPHABET[b & 0x1F] for b in os.urandom(6))
    return f"an_{stamp}{rand}"


def is_analysis_id(value: str) -> bool:
    """Whether ``value`` has the shape :func:`new_analysis_id` produces.

    Used to reject obvious junk before it reaches the result store, so a
    malformed id costs a 404 rather than a round trip to Redis.
    """
    if len(value) != 19 or not value.startswith("an_"):
        return False
    return all(char in _ID_ALPHABET for char in value[3:])


__all__ = [
    "ANALYZER_VERSION",
    "ERROR_LEVELS",
    "LEVEL_NAMES",
    "SAMPLE_TEXT_LIMIT",
    "AnalysisMeta",
    "AnalysisResult",
    "LevelCounts",
    "LogLevel",
    "ParseReason",
    "ServiceStats",
    "TimeRange",
    "UnparseableSample",
    "is_analysis_id",
    "new_analysis_id",
]
