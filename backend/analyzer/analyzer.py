"""The analyzer: log lines in, a summary out.

This module does no I/O and imports nothing from FastAPI.  The server, the
tests and any future background worker all drive the same class, which is what
keeps "what the CLI printed" and "what the API returned" the same answer.

Memory is constant in the size of the input.  What is kept is a handful of
counters per service, two timestamps, and the first few unparseable lines --
so a 100 MB file costs the same as a 100 KB one.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from datetime import datetime

from .models import (
    ANALYZER_VERSION,
    SAMPLE_TEXT_LIMIT,
    AnalysisMeta,
    AnalysisResult,
    LevelCounts,
    LogLevel,
    ParseReason,
    ServiceStats,
    TimeRange,
    UnparseableSample,
    new_analysis_id,
)
from .parser import LINE_RE, ParsedLine, date_cache, is_valid_time, parse_line

#: Level name to its slot in a service's counter array.  Doubling as the
#: "is this a known level" test saves a second lookup on the hot path.
_LEVEL_INDEX: dict[str, int] = {level.value: i for i, level in enumerate(LogLevel)}
_LEVEL_NAMES: tuple[str, ...] = tuple(level.value for level in LogLevel)
_ERROR_SLOT = _LEVEL_INDEX[LogLevel.ERROR.value]
_FATAL_SLOT = _LEVEL_INDEX[LogLevel.FATAL.value]

#: Default ceiling on a single line, in characters.  The streaming reader
#: applies the same number in bytes before it ever builds the string; this is
#: the backstop for callers that hand over whole strings.
DEFAULT_MAX_LINE_CHARS = 64 * 1024

#: Default number of unparseable lines kept as examples.
DEFAULT_MAX_SAMPLES = 20


class LogAnalyzer:
    """Accumulates counts over a stream of log lines.

    Feed it lines from anywhere -- a file handle, an upload stream, a list in a
    test -- then call :meth:`result`.  Two analyzers covering two halves of a
    file can be merged into the same answer one analyzer over the whole file
    would have given, which is what would let a very large file be split across
    workers without changing the output.
    """

    __slots__ = (
        "_blank_lines",
        "_first_ts",
        "_last_ts",
        "_lines_seen",
        "_max_line_chars",
        "_max_samples",
        "_samples",
        "_services",
        "_unparseable_lines",
    )

    def __init__(
        self,
        *,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
    ) -> None:
        self._max_samples = max_samples
        self._max_line_chars = max_line_chars
        #: service name -> counts per level, indexed by ``_LEVEL_INDEX``.
        self._services: dict[str, list[int]] = {}
        self._samples: list[tuple[int, ParseReason, str]] = []
        self._lines_seen = 0
        self._blank_lines = 0
        self._unparseable_lines = 0
        self._first_ts: str | None = None
        self._last_ts: str | None = None

    # -- reading ----------------------------------------------------------

    def feed(self, line: str) -> None:
        """Feed a single line, with or without its line ending."""
        self.feed_many((line,))

    def feed_many(self, lines: Iterable[str]) -> None:
        """Feed many lines.

        This is the hot path, so the parse is inlined rather than delegated to
        :func:`~analyzer.parser.parse_line`: the common case -- a well-formed
        line -- costs one regex match, one cached date lookup and two dict
        lookups, and the slower work of deciding *why* a line failed only
        happens for lines that actually failed.
        """
        services = self._services
        samples = self._samples
        max_samples = self._max_samples
        max_line_chars = self._max_line_chars
        match_line = LINE_RE.match
        level_index = _LEVEL_INDEX
        date_valid = date_cache
        first_ts = self._first_ts
        last_ts = self._last_ts
        line_no = self._lines_seen
        blank = 0
        unparseable = 0

        for raw in lines:
            line_no += 1
            # Strips the line ending (LF or CRLF) and any trailing whitespace
            # in one pass, which is also what leaves the message group below
            # free of trailing blanks.
            line = raw.rstrip()
            if not line:
                blank += 1
                continue

            match = match_line(line) if len(line) <= max_line_chars else None
            if match is not None:
                date, time_str, level, service, message = match.group(1, 2, 3, 4, 5)
                slot = level_index.get(level)
                if (
                    slot is not None
                    and service is not None
                    and date_valid[date]
                    and is_valid_time(time_str)
                ):
                    counts = services.get(service)
                    if counts is None:
                        counts = services[service] = [0, 0, 0, 0, 0]
                    counts[slot] += 1

                    # Fixed-width timestamps, so string order is time order and
                    # no datetime objects need building per line.
                    ts = date + " " + time_str
                    if first_ts is None:
                        first_ts = last_ts = ts
                    elif ts < first_ts:
                        first_ts = ts
                    elif last_ts is None or ts > last_ts:
                        last_ts = ts
                    continue

            # Something is wrong with this line; only now is it worth spending
            # time working out which rule it broke.
            unparseable += 1
            if len(samples) < max_samples:
                samples.append(
                    (line_no, _reason_for(line, max_line_chars), line[:SAMPLE_TEXT_LIMIT])
                )

        self._lines_seen = line_no
        self._blank_lines += blank
        self._unparseable_lines += unparseable
        self._first_ts = first_ts
        self._last_ts = last_ts

    def feed_unparseable(self, text: str, reason: ParseReason) -> None:
        """Count a line the caller already knows cannot be parsed.

        The streaming upload reader uses this for a line that ran past the byte
        limit: it has to truncate the line to keep memory bounded, and the
        truncated text no longer carries the very length that disqualified it.
        """
        self._lines_seen += 1
        self._unparseable_lines += 1
        if len(self._samples) < self._max_samples:
            self._samples.append((self._lines_seen, reason, text[:SAMPLE_TEXT_LIMIT]))

    # -- combining --------------------------------------------------------

    def merge(self, other: LogAnalyzer) -> LogAnalyzer:
        """Fold ``other`` into this analyzer and return it.

        ``other``'s lines are treated as following this analyzer's, so
        ``a.merge(b).result()`` matches analysing a's lines followed by b's --
        the line numbers in b's samples included.
        """
        services = self._services
        for service, counts in other._services.items():
            mine = services.get(service)
            if mine is None:
                services[service] = counts.copy()
            else:
                for i, n in enumerate(counts):
                    mine[i] += n

        offset = self._lines_seen
        room = self._max_samples - len(self._samples)
        if room > 0:
            self._samples.extend(
                (line_no + offset, reason, text)
                for line_no, reason, text in other._samples[:room]
            )

        self._lines_seen += other._lines_seen
        self._blank_lines += other._blank_lines
        self._unparseable_lines += other._unparseable_lines

        if other._first_ts is not None and (
            self._first_ts is None or other._first_ts < self._first_ts
        ):
            self._first_ts = other._first_ts
        if other._last_ts is not None and (
            self._last_ts is None or other._last_ts > self._last_ts
        ):
            self._last_ts = other._last_ts
        return self

    # -- reporting --------------------------------------------------------

    @property
    def lines_processed(self) -> int:
        """Every non-blank line read, parsed or not."""
        return self._lines_seen - self._blank_lines

    @property
    def parsed_lines(self) -> int:
        """Lines that had all four required fields."""
        return self.lines_processed - self._unparseable_lines

    @property
    def unparseable_lines(self) -> int:
        return self._unparseable_lines

    @property
    def blank_lines(self) -> int:
        return self._blank_lines

    def service_stats(self) -> list[ServiceStats]:
        """Per-service totals, worst offender first, then by name."""
        stats = []
        for service, counts in self._services.items():
            errors = counts[_ERROR_SLOT] + counts[_FATAL_SLOT]
            parsed = sum(counts)
            stats.append(
                ServiceStats(
                    service=service,
                    error_count=errors,
                    # A service is only in the dict because a line parsed for
                    # it, so `parsed` is never zero.
                    error_rate=round(errors / parsed, 4),
                    levels=LevelCounts(**dict(zip(_LEVEL_NAMES, counts, strict=True))),
                )
            )
        stats.sort(key=lambda s: (-s.error_count, s.service))
        return stats

    def result(
        self,
        *,
        id: str | None = None,
        filename: str | None = None,
        size_bytes: int = 0,
        duration_ms: int = 0,
    ) -> AnalysisResult:
        """Build the summary.

        The keyword arguments are the things the analyzer cannot know, because
        it never touched a file or a socket; they default to values that are
        honest about not having been measured.
        """
        services = self.service_stats()
        top = services[0].error_count if services else 0
        return AnalysisResult(
            id=id or new_analysis_id(),
            lines_processed=self.lines_processed,
            unparseable_lines=self._unparseable_lines,
            blank_lines=self._blank_lines,
            services=services,
            # `services` is sorted by error count, so the tied leaders are the
            # run at the front.  No errors anywhere means no offender at all.
            top_offenders=(
                [s.service for s in services if s.error_count == top] if top else []
            ),
            unparseable_samples=[
                UnparseableSample(line_number=n, reason=reason, text=text)
                for n, reason, text in self._samples
            ],
            time_range=TimeRange(
                first=_to_datetime(self._first_ts),
                last=_to_datetime(self._last_ts),
            ),
            meta=AnalysisMeta(
                filename=filename,
                bytes=size_bytes,
                duration_ms=duration_ms,
                analyzer_version=ANALYZER_VERSION,
            ),
        )


def _reason_for(line: str, max_line_chars: int) -> ParseReason:
    """Work out why ``line`` did not parse.

    Only called for lines that already failed the fast path, so the cost of a
    second, more careful pass is paid on bad lines alone.
    """
    if len(line) > max_line_chars:
        return ParseReason.LINE_TOO_LONG
    reason = parse_line(line)
    if isinstance(reason, ParsedLine):  # pragma: no cover - defensive
        # Unreachable unless the inlined fast path and parse_line disagree.
        # Counting the line beats dropping it, so pick the neutral reason.
        return ParseReason.MISSING_TIMESTAMP
    return reason


def _to_datetime(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts is not None else None


def analyze(
    lines: Iterable[str],
    *,
    filename: str | None = None,
    size_bytes: int = 0,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> AnalysisResult:
    """Analyse an iterable of lines in one call.

    The convenience entry point for tests, scripts and anything reading a local
    file; the API drives :class:`LogAnalyzer` directly so it can interleave
    parsing with reading the upload.
    """
    analyzer = LogAnalyzer(max_samples=max_samples)
    started = time.perf_counter()
    analyzer.feed_many(lines)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return analyzer.result(
        filename=filename, size_bytes=size_bytes, duration_ms=elapsed_ms
    )


def iter_lines(path: str) -> Iterator[str]:
    """Yield the lines of a file on disk, decoded the way the API decodes them.

    Invalid UTF-8 becomes U+FFFD rather than raising, so a log with a few bad
    bytes is still analysed instead of rejected.
    """
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        yield from handle


__all__ = [
    "DEFAULT_MAX_LINE_CHARS",
    "DEFAULT_MAX_SAMPLES",
    "LogAnalyzer",
    "analyze",
    "iter_lines",
]
