"""One test per row of the parsing-rules table in the README.

If a rule changes, exactly one case here should change with it.
"""

from __future__ import annotations

import pytest

from app.models.analysis import ParseReason
from app.services.analyzer import analyze, LogAnalyzer
from app.services.parser import parse_line, ParsedLine
from app.services.parser import is_valid_date, is_valid_time

WELL_FORMED = "2026-09-18 10:23:45 ERROR payment-service Connection timeout after 30s"


class TestWellFormed:
    def test_all_four_fields_are_captured(self) -> None:
        assert parse_line(WELL_FORMED) == ParsedLine(
            timestamp="2026-09-18 10:23:45",
            level="ERROR",
            service="payment-service",
            message="Connection timeout after 30s",
        )

    def test_counts_as_an_error(self) -> None:
        result = analyze([WELL_FORMED])
        assert result.services[0].error_count == 1
        assert result.top_offenders == ["payment-service"]

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"])
    def test_every_known_level_parses(self, level: str) -> None:
        parsed = parse_line(f"2026-09-18 10:23:45 {level} svc hello")
        assert isinstance(parsed, ParsedLine)
        assert parsed.level == level


class TestUnparseableReasons:
    @pytest.mark.parametrize(
        "line",
        [
            "ERROR billing-service No Auth token",
            "just some free text",
            "10:23:45 ERROR svc time but no date",
            "2026-09-18 ERROR svc date but no time",
            "26-09-18 10:23:45 ERROR svc two-digit year",
            "2026-9-18 10:23:45 ERROR svc unpadded month",
            " 2026-09-18 10:23:45 ERROR svc leading space",
        ],
        ids=[
            "brief-sample",
            "free-text",
            "time-only",
            "date-only",
            "short-year",
            "unpadded-month",
            "leading-space",
        ],
    )
    def test_missing_timestamp(self, line: str) -> None:
        assert parse_line(line) is ParseReason.MISSING_TIMESTAMP

    @pytest.mark.parametrize(
        "line",
        [
            "2026-02-30 25:10:00 ERROR payment-service impossible both",
            "2026-02-30 10:23:45 ERROR svc february thirtieth",
            "2026-13-01 10:23:45 ERROR svc month thirteen",
            "2026-09-00 10:23:45 ERROR svc day zero",
            "2026-09-18 25:10:00 ERROR svc hour twenty-five",
            "2026-09-18 10:60:00 ERROR svc minute sixty",
            "2026-09-18 10:23:60 ERROR svc leap second",
            "2026-02-29 10:23:45 ERROR svc not a leap year",
        ],
        ids=[
            "both-impossible",
            "feb-30",
            "month-13",
            "day-0",
            "hour-25",
            "minute-60",
            "second-60",
            "non-leap-feb-29",
        ],
    )
    def test_invalid_timestamp(self, line: str) -> None:
        assert parse_line(line) is ParseReason.INVALID_TIMESTAMP

    @pytest.mark.parametrize(
        "line",
        [
            "2026-09-18 10:23:45 WARNING auth-service spelled out",
            "2026-09-18 10:23:45 error auth-service lowercase",
            "2026-09-18 10:23:45 Error auth-service mixed case",
            "2026-09-18 10:23:45 TRACE auth-service unknown level",
            "2026-09-18 10:23:45 WARNS auth-service prefix of WARN",
            "2026-09-18 10:23:45",
        ],
        ids=["WARNING", "lowercase", "mixed-case", "TRACE", "warn-prefix", "nothing-after-time"],
    )
    def test_unknown_level(self, line: str) -> None:
        assert parse_line(line) is ParseReason.UNKNOWN_LEVEL

    def test_missing_service(self) -> None:
        assert parse_line("2026-09-18 10:23:45 ERROR") is ParseReason.MISSING_SERVICE

    def test_reason_order_is_stable_when_several_rules_break(self) -> None:
        """A line can break more than one rule; the first field wins.

        Timestamp, then level, then service -- the order the fields appear in,
        so the reason a client shows does not depend on check ordering.
        """
        assert (
            parse_line("2026-02-30 10:23:45 WARNING") is ParseReason.INVALID_TIMESTAMP
        )
        assert parse_line("2026-09-18 10:23:45 WARNING") is ParseReason.UNKNOWN_LEVEL


class TestAcceptedVariations:
    def test_no_message_still_parses(self) -> None:
        parsed = parse_line("2026-09-18 10:23:45 ERROR payment-service")
        assert isinstance(parsed, ParsedLine)
        assert parsed.message == ""

    def test_tabs_between_fields(self) -> None:
        parsed = parse_line("2026-09-18\t10:23:46\tINFO\tauth-service\ttab separated")
        assert isinstance(parsed, ParsedLine)
        assert parsed.service == "auth-service"
        assert parsed.message == "tab separated"

    def test_repeated_spaces_between_fields(self) -> None:
        parsed = parse_line("2026-09-18    10:23:47    WARN    auth-service    spaced")
        assert isinstance(parsed, ParsedLine)
        assert parsed.message == "spaced"

    def test_crlf_and_trailing_spaces(self) -> None:
        result = analyze(["2026-09-18 10:23:45 ERROR payment-service crlf line\r\n"])
        assert result.lines_processed == 1
        assert result.unparseable_lines == 0
        assert result.services[0].service == "payment-service"

    def test_trailing_whitespace_is_not_part_of_the_message(self) -> None:
        result = analyze(["2026-09-18 10:23:45 INFO svc hello   \r\n"])
        assert result.unparseable_lines == 0

    def test_message_keeps_its_internal_spacing(self) -> None:
        parsed = parse_line("2026-09-18 10:23:45 INFO svc a  b\tc")
        assert isinstance(parsed, ParsedLine)
        assert parsed.message == "a  b\tc"

    def test_service_names_are_case_sensitive(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:23:45 ERROR payment-service one",
                "2026-09-18 10:23:46 ERROR Payment-Service two",
            ]
        )
        assert [s.service for s in result.services] == [
            "Payment-Service",
            "payment-service",
        ]

    def test_invalid_utf8_becomes_a_replacement_char_and_still_parses(
        self, samples_dir
    ) -> None:
        raw = (samples_dir / "invalid-utf8.log").read_bytes()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        result = analyze(lines)
        assert result.unparseable_lines == 0
        assert result.services[0].service == "payment-service"
        assert "�" in lines[0]


class TestBlankAndOversizedLines:
    @pytest.mark.parametrize("line", ["", "   ", "\t", "\r\n", "  \t  \r\n"])
    def test_blank_lines_are_counted_separately(self, line: str) -> None:
        result = analyze([line])
        assert result.blank_lines == 1
        assert result.lines_processed == 0
        assert result.unparseable_lines == 0

    def test_line_over_the_limit_is_too_long(self) -> None:
        analyzer = LogAnalyzer(max_line_chars=64)
        analyzer.feed("2026-09-18 10:23:45 ERROR payment-service " + "x" * 100)
        result = analyzer.result()
        assert result.unparseable_lines == 1
        assert result.unparseable_samples[0].reason is ParseReason.LINE_TOO_LONG

    def test_a_line_exactly_at_the_limit_is_fine(self) -> None:
        prefix = "2026-09-18 10:23:45 ERROR payment-service "
        analyzer = LogAnalyzer(max_line_chars=64)
        analyzer.feed(prefix + "x" * (64 - len(prefix)))
        assert analyzer.result().unparseable_lines == 0


class TestValidators:
    @pytest.mark.parametrize(
        ("date", "valid"),
        [
            ("2026-09-18", True),
            ("2024-02-29", True),
            ("2000-02-29", True),
            ("1900-02-29", False),
            ("2026-02-29", False),
            ("2026-04-31", False),
            ("2026-12-31", True),
            ("2026-00-10", False),
        ],
    )
    def test_is_valid_date(self, date: str, valid: bool) -> None:
        assert is_valid_date(date) is valid

    @pytest.mark.parametrize(
        ("value", "valid"),
        [("00:00:00", True), ("23:59:59", True), ("24:00:00", False), ("23:60:00", False)],
    )
    def test_is_valid_time(self, value: str, valid: bool) -> None:
        assert is_valid_time(value) is valid

    def test_the_date_cache_does_not_grow_without_bound(self) -> None:
        """A crafted file must not be able to mint a cache entry per line."""
        from app.services.parser import date_cache

        date_cache.clear()
        for year in range(1000, 6000):
            is_valid_date(f"{year}-01-01")
        assert len(date_cache) <= 4097
