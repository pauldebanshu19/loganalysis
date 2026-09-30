"""The streaming reader.

The reader sees the file as arbitrary blocks of bytes, not as lines, so the
interesting failures all live on chunk boundaries: a line split in two, a CRLF
split between its two bytes, a multi-byte character split down the middle.  The
property at the bottom is the real guarantee -- however the bytes are chopped
up, the answer is the one a single pass over the whole file would give.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from analyzer import LogAnalyzer, ParseReason, analyze
from app.errors import UnsupportedMediaType
from app.upload import BINARY_PROBE_BYTES, LineFeeder, sanitize_filename

SAMPLE = (
    b"2026-09-18 10:23:45 ERROR payment-service Connection timeout after 30s\n"
    b"2026-09-18 10:23:46 INFO auth-service User login successful\n"
    b"\n"
    b"ERROR billing-service No Auth token\n"
    b"2026-09-18 10:23:50 FATAL payment-service died\n"
)


def feed_in_chunks(data: bytes, size: int, **kwargs) -> LogAnalyzer:
    analyzer = LogAnalyzer(**kwargs)
    feeder = LineFeeder(analyzer, max_line_bytes=kwargs.pop("max_line_bytes", 64 * 1024))
    for start in range(0, len(data), size):
        feeder.push(data[start : start + size])
    feeder.finish()
    return analyzer


class TestChunkBoundaries:
    @pytest.mark.parametrize("size", [1, 2, 3, 7, 16, 64, 1000, len(SAMPLE)])
    def test_any_chunk_size_gives_the_same_answer(self, size: int) -> None:
        analyzer = feed_in_chunks(SAMPLE, size)
        expected = analyze(SAMPLE.decode().splitlines())
        fixed = "an_0000000000000000"
        assert analyzer.result(id=fixed) == expected.model_copy(update={"id": fixed})

    def test_crlf_split_across_two_chunks(self) -> None:
        """The \\r ends one chunk and the \\n starts the next."""
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=64 * 1024)
        feeder.push(b"2026-09-18 10:23:45 ERROR payment-service timeout\r")
        feeder.push(b"\n2026-09-18 10:23:46 INFO auth-service ok\r\n")
        feeder.finish()
        result = analyzer.result()
        assert result.lines_processed == 2
        assert result.unparseable_lines == 0

    def test_a_multibyte_character_split_across_chunks(self) -> None:
        """Splitting on bytes means a character can arrive in two pieces."""
        line = "2026-09-18 10:23:45 INFO café-service naïve résumé\n".encode()
        midpoint = line.index("é".encode()) + 1  # between the two bytes of é

        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=64 * 1024)
        feeder.push(line[:midpoint])
        feeder.push(line[midpoint:])
        feeder.finish()

        result = analyzer.result()
        assert result.unparseable_lines == 0
        assert result.services[0].service == "café-service"

    def test_a_final_line_without_a_newline_is_not_lost(self) -> None:
        analyzer = feed_in_chunks(b"2026-09-18 10:23:45 ERROR svc no trailing newline", 8)
        result = analyzer.result()
        assert result.lines_processed == 1
        assert result.services[0].error_count == 1

    def test_an_empty_stream_produces_an_empty_result(self) -> None:
        analyzer = feed_in_chunks(b"", 8)
        result = analyzer.result()
        assert result.lines_processed == 0
        assert result.blank_lines == 0

    def test_bytes_seen_counts_the_file_not_the_chunking(self) -> None:
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=64 * 1024)
        for start in range(0, len(SAMPLE), 5):
            feeder.push(SAMPLE[start : start + 5])
        assert feeder.bytes_seen == len(SAMPLE)


class TestOverLongLines:
    def test_an_over_long_line_is_reported_once(self) -> None:
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=100)
        feeder.push(b"x" * 5000 + b"\n")
        feeder.finish()
        result = analyzer.result()
        assert result.unparseable_lines == 1
        assert result.unparseable_samples[0].reason is ParseReason.LINE_TOO_LONG

    def test_an_over_long_line_spanning_many_chunks_is_reported_once(self) -> None:
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=100)
        for _ in range(50):
            feeder.push(b"x" * 200)
        feeder.push(b"\n2026-09-18 10:23:45 ERROR svc after\n")
        feeder.finish()

        result = analyzer.result()
        assert result.lines_processed == 2
        assert result.unparseable_lines == 1
        assert result.services[0].error_count == 1

    def test_the_lines_around_an_over_long_one_keep_their_numbers(self) -> None:
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=100)
        feeder.push(b"first bad line\n")
        feeder.push(b"z" * 500 + b"\n")
        feeder.push(b"third bad line\n")
        feeder.finish()

        samples = analyzer.result().unparseable_samples
        assert [s.line_number for s in samples] == [1, 2, 3]
        assert [s.reason for s in samples] == [
            ParseReason.MISSING_TIMESTAMP,
            ParseReason.LINE_TOO_LONG,
            ParseReason.MISSING_TIMESTAMP,
        ]

    def test_an_over_long_final_line_without_a_newline(self) -> None:
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=100)
        feeder.push(b"q" * 4000)
        feeder.finish()
        result = analyzer.result()
        assert result.unparseable_lines == 1
        assert result.unparseable_samples[0].reason is ParseReason.LINE_TOO_LONG

    def test_memory_stays_bounded_through_an_enormous_line(self) -> None:
        """The whole point: a 20 MB line must not be buffered whole."""
        analyzer = LogAnalyzer()
        feeder = LineFeeder(analyzer, max_line_bytes=1024)
        for _ in range(20):
            feeder.push(b"w" * (1024 * 1024))
            # The internal buffer never holds more than one chunk's worth of a
            # line it has already given up on.
            assert len(feeder._buffer) <= 1024 + 1
        feeder.push(b"\n")
        feeder.finish()
        assert analyzer.result().unparseable_lines == 1


class TestBinaryDetection:
    def test_a_nul_byte_in_the_first_chunk_is_rejected(self) -> None:
        feeder = LineFeeder(LogAnalyzer(), max_line_bytes=64 * 1024)
        with pytest.raises(UnsupportedMediaType) as caught:
            feeder.push(b"2026-09-18 10:23:45 INFO svc ok\n\x00\x01\x02")
        assert caught.value.details["found"] == "nul_byte"

    def test_the_probe_stops_after_the_first_8kb(self) -> None:
        feeder = LineFeeder(LogAnalyzer(), max_line_bytes=64 * 1024)
        feeder.push(b"2026-09-18 10:23:45 INFO svc padding\n" * 300)
        assert len(b"2026-09-18 10:23:45 INFO svc padding\n" * 300) > BINARY_PROBE_BYTES
        # Past the probe window, a NUL is just a character in a line that will
        # not parse -- not grounds for rejecting the whole upload.
        feeder.push(b"\x00 late nul\n")
        feeder.finish()

    def test_the_probe_spans_chunks(self) -> None:
        feeder = LineFeeder(LogAnalyzer(), max_line_bytes=64 * 1024)
        feeder.push(b"a" * 100)
        with pytest.raises(UnsupportedMediaType):
            feeder.push(b"b" * 100 + b"\x00")


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("app.log", "app.log"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Windows\\System32\\config", "config"),
            ("/var/log/syslog", "syslog"),
            ("with space.log", "with space.log"),
            ("...hidden", "hidden"),
            ("", None),
            (None, None),
            ("   ", None),
            ("..", None),
            ("/", None),
            ("tab\there.log", "tabhere.log"),
            ("newline\nhere.log", "newlinehere.log"),
        ],
    )
    def test_names_are_made_safe_to_echo_back(
        self, supplied: str | None, expected: str | None
    ) -> None:
        assert sanitize_filename(supplied) == expected

    def test_a_very_long_name_is_cut(self) -> None:
        assert len(sanitize_filename("n" * 1000)) == 255


def _stream(data: bytes, chunk_sizes: list[int], max_line_bytes: int) -> LogAnalyzer:
    analyzer = LogAnalyzer(max_line_chars=max_line_bytes)
    feeder = LineFeeder(analyzer, max_line_bytes=max_line_bytes)
    position = index = 0
    while position < len(data):
        size = chunk_sizes[index % len(chunk_sizes)]
        feeder.push(data[position : position + size])
        position += size
        index += 1
    feeder.finish()
    return analyzer


#: NUL bytes are excluded because they are rejected as binary before any of
#: this runs; that path has its own tests.
log_bytes = st.binary(max_size=400).filter(lambda b: b"\x00" not in b)


@settings(max_examples=200)
@given(
    data=log_bytes,
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=30),
    max_line_bytes=st.sampled_from([8, 64, 1024, 64 * 1024]),
)
def test_chunking_never_changes_the_answer(
    data: bytes, chunk_sizes: list[int], max_line_bytes: int
) -> None:
    """However the stream is split, the summary is the same.

    This is what makes the streaming reader safe to trust: the client, the
    network and the event loop between them decide where the chunk boundaries
    fall, and none of them may change what the analyzer concludes -- not the
    counts, not the line numbers, and not the reason a line was rejected.
    """
    chunked = _stream(data, chunk_sizes, max_line_bytes)
    at_once = _stream(data, [max(len(data), 1)], max_line_bytes)

    fixed = "an_0000000000000000"
    assert chunked.result(id=fixed) == at_once.result(id=fixed)


@settings(max_examples=100)
@given(
    data=log_bytes.filter(lambda b: b"\r" not in b),
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=20),
)
def test_the_streamed_reader_agrees_with_reading_the_file_from_disk(
    data: bytes, chunk_sizes: list[int]
) -> None:
    """An upload and a local file must give the same answer.

    The CLI streams a file to the server while `analyze(iter_lines(...))` reads
    it directly; if these two disagreed, the acceptance check would depend on
    which route the bytes took.
    """
    streamed = _stream(data, chunk_sizes, 64 * 1024)

    # `newline=""` is what `iter_lines` uses: split on \n alone, so that a lone
    # \r or a \v inside a message is content rather than a line break.
    from io import StringIO

    text = data.decode("utf-8", "replace")
    on_disk = LogAnalyzer()
    on_disk.feed_many(StringIO(text, newline=""))

    fixed = "an_0000000000000000"
    assert streamed.result(id=fixed) == on_disk.result(id=fixed)
