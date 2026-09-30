from __future__ import annotations

import re
from typing import Final, NamedTuple

from app.models.analysis import LEVEL_NAMES, ParseReason

#: Fields are separated by runs of spaces or tabs.  Matching those two
#: characters explicitly rather than ``\s`` keeps the hot path off Python's
#: Unicode whitespace tables, which is worth roughly a third of parse time.
#:
#: The level and service are captured as bare tokens instead of being spelled
#: out as an alternation: it lets one regex serve both the success path and the
#: diagnosis path, and it stops ``WARNING`` from matching as ``WARN`` plus a
#: stray ``ING``.  Both trailing groups are optional so that a line which stops
#: early still matches and can be told apart from one with no timestamp at all.
#:
#: ``re.ASCII`` is here for correctness as much as speed: without it ``\d`` also
#: matches other scripts' digit characters, so a year written in Arabic-Indic
#: digits would pass the shape check and ``int()`` would happily convert it.
#: The format's digits are ASCII.  The flag does not narrow ``\S``, so service
#: names in any script still parse.
LINE_RE: Final = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ \t]+(\d{2}:\d{2}:\d{2})"
    r"(?:[ \t]+(\S+)(?:[ \t]+(\S+)(?:[ \t]+(.*))?)?)?$",
    re.ASCII,
)

_DAYS_IN_MONTH: Final = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class ParsedLine(NamedTuple):
    """A line that had all four required fields."""

    timestamp: str
    """``YYYY-MM-DD HH:MM:SS``.  Fixed width, so string order is time order."""

    level: str
    service: str
    message: str


def _is_valid_date(date: str) -> bool:
    """Whether ``YYYY-MM-DD`` names a day that exists.

    Called through :func:`is_valid_date`, which caches it -- a log file usually
    spans one or two distinct dates, so the check runs a couple of times rather
    than once per line.
    """
    year = int(date[0:4])
    month = int(date[5:7])
    day = int(date[8:10])
    if not 1 <= month <= 12 or day < 1:
        return False
    limit = _DAYS_IN_MONTH[month - 1]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        limit = 29
    return day <= limit


class _DateCache(dict):
    """A dict that fills itself, used as the date-validity memo.

    ``dict.__missing__`` is the cheapest cache Python offers: a hit is a plain
    subscript with no function call, unlike ``functools.lru_cache``.
    """

    def __missing__(self, date: str) -> bool:
        # Bounded because a crafted file could otherwise mint a new key per
        # line.  Real logs never come close to the cap.
        if len(self) > 4096:
            self.clear()
        valid = _is_valid_date(date)
        self[date] = valid
        return valid


#: Shared memo, exposed so the analyzer's inlined hot path can subscript it
#: directly instead of paying for a function call per line.
date_cache: dict[str, bool] = _DateCache()


def is_valid_date(date: str) -> bool:
    """Cached ``YYYY-MM-DD`` validity check."""
    return date_cache[date]


def is_valid_time(time_str: str) -> bool:
    """Whether ``HH:MM:SS`` names a time that exists.

    Leap seconds are rejected: ``23:59:60`` is far more likely to be a broken
    line than a real one.
    """
    return (
        time_str[0:2] <= "23" and time_str[3:5] <= "59" and time_str[6:8] <= "59"
    )


def parse_line(line: str) -> ParsedLine | ParseReason:
    """Parse one line, or say why it could not be parsed.

    ``line`` must already have its line ending and trailing whitespace removed
    and must not be blank -- both are the caller's job, since the analyzer has
    to count blank lines separately anyway.

    Returns a :class:`ParsedLine` on success, or the :class:`ParseReason` that
    applies.  When a line breaks more than one rule the reason is the first one
    in field order (timestamp, then level, then service), so the answer is
    stable rather than depending on which check happened to run first.
    """
    match = LINE_RE.match(line)
    if match is None:
        # Nothing that looks like ``<date> <time>`` at the start of the line.
        return ParseReason.MISSING_TIMESTAMP

    date, time_str, level, service, message = match.group(1, 2, 3, 4, 5)

    if not date_cache[date] or not is_valid_time(time_str):
        return ParseReason.INVALID_TIMESTAMP
    if level not in LEVEL_NAMES:
        # Covers both an unrecognised level such as ``WARNING`` or ``error``,
        # and a line that ends after the timestamp with no level at all.
        return ParseReason.UNKNOWN_LEVEL
    if service is None:
        return ParseReason.MISSING_SERVICE

    return ParsedLine(f"{date} {time_str}", level, service, message or "")


__all__ = [
    "LINE_RE",
    "ParsedLine",
    "date_cache",
    "is_valid_date",
    "is_valid_time",
    "parse_line",
]
