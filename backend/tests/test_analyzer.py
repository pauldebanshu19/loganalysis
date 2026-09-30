"""Analyzer behaviour: the brief's sample, ties, empties, merging, samples."""

from __future__ import annotations

from datetime import datetime

import pytest

from analyzer import LogAnalyzer, ParseReason, analyze, iter_lines
from analyzer.models import SAMPLE_TEXT_LIMIT, is_analysis_id, new_analysis_id


class TestBriefSample:
    """The acceptance check: 7 processed, 1 unparseable, payment-service 2."""

    @pytest.fixture
    def result(self, brief_log):
        return analyze(iter_lines(str(brief_log)), filename="brief.log")

    def test_line_counts(self, result) -> None:
        assert result.lines_processed == 7
        assert result.unparseable_lines == 1
        assert result.blank_lines == 0

    def test_error_counts_per_service(self, result) -> None:
        assert [(s.service, s.error_count) for s in result.services] == [
            ("payment-service", 2),
            ("billing-service", 1),
            ("auth-service", 0),
        ]

    def test_top_offender(self, result) -> None:
        assert result.top_offenders == ["payment-service"]

    def test_the_unparseable_line_is_reported_with_its_real_line_number(
        self, result
    ) -> None:
        (sample,) = result.unparseable_samples
        assert sample.line_number == 6
        assert sample.reason is ParseReason.MISSING_TIMESTAMP
        assert sample.text == "ERROR billing-service No Auth token"

    def test_time_range_covers_the_file(self, result) -> None:
        assert result.time_range.first == datetime(2026, 9, 18, 10, 23, 45)
        assert result.time_range.last == datetime(2026, 9, 18, 10, 23, 50)

    def test_services_with_no_errors_are_still_listed(self, result) -> None:
        auth = next(s for s in result.services if s.service == "auth-service")
        assert auth.error_count == 0
        assert auth.levels.INFO == 2


class TestTopOffenders:
    def test_a_tie_lists_every_tied_service(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 ERROR payment-service a",
                "2026-09-18 10:00:01 ERROR billing-service b",
                "2026-09-18 10:00:02 INFO auth-service c",
            ]
        )
        assert result.top_offenders == ["billing-service", "payment-service"]

    def test_no_errors_anywhere_means_no_offender(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 INFO auth-service a",
                "2026-09-18 10:00:01 WARN billing-service b",
            ]
        )
        assert result.top_offenders == []
        assert all(s.error_count == 0 for s in result.services)

    def test_fatal_counts_towards_errors(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 FATAL payment-service died",
                "2026-09-18 10:00:01 ERROR payment-service broke",
            ]
        )
        service = result.services[0]
        assert service.error_count == 2
        assert service.levels.FATAL == 1
        assert service.levels.ERROR == 1

    def test_services_sort_by_errors_then_by_name(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 ERROR zeta x",
                "2026-09-18 10:00:01 ERROR alpha x",
                "2026-09-18 10:00:02 ERROR alpha x",
                "2026-09-18 10:00:03 INFO beta x",
            ]
        )
        assert [s.service for s in result.services] == ["alpha", "zeta", "beta"]


class TestErrorRate:
    def test_error_rate_is_errors_over_that_services_parsed_lines(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 ERROR payment-service a",
                "2026-09-18 10:00:01 INFO payment-service b",
                "2026-09-18 10:00:02 INFO payment-service c",
                "2026-09-18 10:00:03 INFO payment-service d",
                "2026-09-18 10:00:04 ERROR auth-service e",
            ]
        )
        by_name = {s.service: s for s in result.services}
        assert by_name["payment-service"].error_rate == 0.25
        assert by_name["auth-service"].error_rate == 1.0

    def test_unparseable_lines_do_not_dilute_any_services_rate(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 ERROR payment-service a",
                "garbage that belongs to nobody",
            ]
        )
        assert result.services[0].error_rate == 1.0


class TestDegenerateInputs:
    def test_no_lines_at_all(self) -> None:
        result = analyze([])
        assert result.lines_processed == 0
        assert result.services == []
        assert result.top_offenders == []
        assert result.time_range.first is None
        assert result.time_range.last is None

    def test_only_blank_lines(self) -> None:
        result = analyze(["", "   ", "\n", "\t\n"])
        assert result.blank_lines == 4
        assert result.lines_processed == 0
        assert result.unparseable_lines == 0
        assert result.services == []

    def test_every_line_unparseable(self) -> None:
        result = analyze(["nope", "still nope", "nope again"])
        assert result.lines_processed == 3
        assert result.unparseable_lines == 3
        assert result.services == []
        assert result.top_offenders == []
        assert len(result.unparseable_samples) == 3

    def test_line_numbers_count_blank_lines(self) -> None:
        """A sample's line number must match what an editor shows."""
        result = analyze(["", "", "bad line here", ""])
        assert result.unparseable_samples[0].line_number == 3


class TestSamples:
    def test_only_the_first_n_bad_lines_are_kept(self) -> None:
        result = analyze([f"bad {i}" for i in range(100)], max_samples=20)
        assert result.unparseable_lines == 100
        assert len(result.unparseable_samples) == 20
        assert result.unparseable_samples[0].text == "bad 0"
        assert result.unparseable_samples[-1].text == "bad 19"

    def test_samples_can_be_turned_off(self) -> None:
        result = analyze(["bad"], max_samples=0)
        assert result.unparseable_lines == 1
        assert result.unparseable_samples == []

    def test_sample_text_is_cut_at_the_limit(self) -> None:
        result = analyze(["z" * 5_000])
        assert len(result.unparseable_samples[0].text) == SAMPLE_TEXT_LIMIT

    def test_feed_unparseable_records_a_caller_supplied_reason(self) -> None:
        analyzer = LogAnalyzer()
        analyzer.feed("2026-09-18 10:00:00 INFO svc fine")
        analyzer.feed_unparseable("truncated...", ParseReason.LINE_TOO_LONG)
        result = analyzer.result()
        assert result.lines_processed == 2
        assert result.unparseable_lines == 1
        assert result.unparseable_samples[0].line_number == 2
        assert result.unparseable_samples[0].reason is ParseReason.LINE_TOO_LONG


class TestTimeRange:
    def test_uses_the_extremes_not_the_first_and_last_line(self) -> None:
        """Logs from several sources are not always in chronological order."""
        result = analyze(
            [
                "2026-09-18 12:00:00 INFO svc middle",
                "2026-09-18 08:00:00 INFO svc earliest",
                "2026-09-18 23:59:59 INFO svc latest",
                "2026-09-18 09:00:00 INFO svc middle",
            ]
        )
        assert result.time_range.first == datetime(2026, 9, 18, 8, 0, 0)
        assert result.time_range.last == datetime(2026, 9, 18, 23, 59, 59)

    def test_spans_days(self) -> None:
        result = analyze(
            [
                "2026-09-18 23:00:00 INFO svc a",
                "2026-09-19 01:00:00 INFO svc b",
                "2026-09-17 22:00:00 INFO svc c",
            ]
        )
        assert result.time_range.first == datetime(2026, 9, 17, 22, 0, 0)
        assert result.time_range.last == datetime(2026, 9, 19, 1, 0, 0)

    def test_unparseable_lines_do_not_move_the_range(self) -> None:
        result = analyze(
            [
                "2026-09-18 10:00:00 INFO svc good",
                "1999-01-01 00:00:00 WARNING svc bad level",
            ]
        )
        assert result.time_range.first == datetime(2026, 9, 18, 10, 0, 0)


class TestMerge:
    LINES = [
        "2026-09-18 10:00:00 ERROR payment-service a",
        "",
        "2026-09-18 10:00:01 INFO auth-service b",
        "nonsense",
        "2026-09-18 10:00:02 FATAL payment-service c",
        "2026-09-18 09:00:00 WARN billing-service d",
    ]

    @pytest.mark.parametrize("split", range(len(LINES) + 1))
    def test_merging_halves_equals_analysing_the_whole(self, split: int) -> None:
        whole = LogAnalyzer()
        whole.feed_many(self.LINES)

        left, right = LogAnalyzer(), LogAnalyzer()
        left.feed_many(self.LINES[:split])
        right.feed_many(self.LINES[split:])
        merged = left.merge(right)

        fixed_id = "an_0000000000000000"
        assert merged.result(id=fixed_id) == whole.result(id=fixed_id)

    def test_merge_returns_the_left_analyzer(self) -> None:
        left = LogAnalyzer()
        assert left.merge(LogAnalyzer()) is left

    def test_merge_shifts_the_right_sides_line_numbers(self) -> None:
        left, right = LogAnalyzer(), LogAnalyzer()
        left.feed_many(["2026-09-18 10:00:00 INFO svc a", "", "bad one"])
        right.feed_many(["also bad"])
        samples = left.merge(right).result().unparseable_samples
        assert [s.line_number for s in samples] == [3, 4]

    def test_merging_respects_the_sample_cap(self) -> None:
        left = LogAnalyzer(max_samples=3)
        right = LogAnalyzer(max_samples=3)
        left.feed_many(["bad", "bad"])
        right.feed_many(["bad", "bad"])
        assert len(left.merge(right).result().unparseable_samples) == 3

    def test_merging_into_an_empty_analyzer(self) -> None:
        right = LogAnalyzer()
        right.feed_many(self.LINES)
        merged = LogAnalyzer().merge(right)
        fixed_id = "an_0000000000000000"
        assert merged.result(id=fixed_id) == right.result(id=fixed_id)


class TestIds:
    def test_generated_ids_have_the_documented_shape(self) -> None:
        analysis_id = new_analysis_id()
        assert analysis_id.startswith("an_")
        assert is_analysis_id(analysis_id)

    def test_ids_are_unique(self) -> None:
        assert len({new_analysis_id() for _ in range(2_000)}) == 2_000

    def test_ids_sort_chronologically(self) -> None:
        first = new_analysis_id()
        second = new_analysis_id()
        # Same millisecond is possible, hence >= rather than >.
        assert second[3:13] >= first[3:13]

    @pytest.mark.parametrize(
        "value",
        ["", "an_", "nope", "an_TOOSHORT", "an_0000000000000000X", "an_lowercase000000"],
    )
    def test_junk_ids_are_rejected(self, value: str) -> None:
        assert is_analysis_id(value) is False


class TestMeta:
    def test_meta_defaults_are_honest_about_not_being_measured(self) -> None:
        meta = LogAnalyzer().result().meta
        assert meta.filename is None
        assert meta.bytes == 0
        assert meta.duration_ms == 0
        assert meta.analyzer_version == "1.0.0"

    def test_meta_is_passed_through(self) -> None:
        meta = LogAnalyzer().result(
            filename="app.log", size_bytes=81_234, duration_ms=38
        ).meta
        assert (meta.filename, meta.bytes, meta.duration_ms) == ("app.log", 81_234, 38)
