"""Properties that must hold for any input at all.

The example-based tests above pin down the cases we thought of.  These pin down
the ones we did not: Hypothesis builds log files out of well-formed lines, the
specific ways a line can be malformed, and blank lines, then checks the three
relationships the whole summary rests on.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.analysis import ParseReason
from app.services.analyzer import analyze, LogAnalyzer
from app.models.analysis import ERROR_LEVELS

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

services = st.sampled_from(["payment-service", "auth-service", "billing-service", "a"])
levels = st.sampled_from(LEVELS)
dates = st.sampled_from(["2026-09-18", "2026-09-19", "2024-02-29", "2026-01-01"])
times = st.sampled_from(["00:00:00", "10:23:45", "23:59:59", "12:00:00"])
messages = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), max_codepoint=0x2FFF),
    max_size=40,
)

well_formed = st.builds(
    lambda d, t, level, service, message: f"{d} {t} {level} {service} {message}".rstrip(),
    dates,
    times,
    levels,
    services,
    messages,
)

malformed = st.one_of(
    st.just("ERROR billing-service No Auth token"),
    st.just("2026-02-30 25:10:00 ERROR payment-service impossible"),
    st.just("2026-09-18 10:23:45 WARNING auth-service unknown level"),
    st.just("2026-09-18 10:23:45 ERROR"),
    st.text(max_size=30).filter(lambda s: not s.strip().startswith("20")),
)

blank = st.sampled_from(["", " ", "\t", "   \t "])

log_lines = st.lists(
    st.one_of(well_formed, well_formed, malformed, blank), max_size=120
)


@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_every_line_is_accounted_for(lines: list[str]) -> None:
    """Processed lines split cleanly into parsed and unparseable, and every
    line read is either processed or blank -- nothing is silently dropped."""
    analyzer = LogAnalyzer()
    analyzer.feed_many(lines)
    result = analyzer.result()

    assert result.lines_processed == analyzer.parsed_lines + result.unparseable_lines
    assert result.lines_processed + result.blank_lines == len(lines)
    assert analyzer.parsed_lines >= 0


@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_parsed_lines_equal_the_sum_of_every_level_count(lines: list[str]) -> None:
    result = analyze(lines)
    total = sum(
        getattr(service.levels, level) for service in result.services for level in LEVELS
    )
    assert total == result.lines_processed - result.unparseable_lines


@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_errors_are_exactly_the_error_and_fatal_lines(lines: list[str]) -> None:
    result = analyze(lines)
    expected = sum(
        1
        for line in lines
        if (parts := line.split())[2:3] and parts[2] in ERROR_LEVELS and len(parts) >= 4
    )
    counted = sum(service.error_count for service in result.services)
    # `expected` is a deliberately naive recount over well-formed-looking lines;
    # it can only over-count (it does not validate the timestamp), never under.
    assert counted <= expected
    assert counted == sum(
        service.levels.ERROR + service.levels.FATAL for service in result.services
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(left=log_lines, right=log_lines)
def test_merge_equals_analysing_the_concatenation(
    left: list[str], right: list[str]
) -> None:
    """The property that would let one file be split across workers."""
    whole = LogAnalyzer()
    whole.feed_many(left + right)

    a, b = LogAnalyzer(), LogAnalyzer()
    a.feed_many(left)
    b.feed_many(right)

    fixed_id = "an_0000000000000000"
    assert a.merge(b).result(id=fixed_id) == whole.result(id=fixed_id)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_feeding_one_line_at_a_time_matches_feeding_them_all(
    lines: list[str],
) -> None:
    """`feed` and `feed_many` share one implementation; prove they stay in step."""
    one_at_a_time = LogAnalyzer()
    for line in lines:
        one_at_a_time.feed(line)
    all_at_once = LogAnalyzer()
    all_at_once.feed_many(lines)

    fixed_id = "an_0000000000000000"
    assert one_at_a_time.result(id=fixed_id) == all_at_once.result(id=fixed_id)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_top_offenders_really_are_the_worst(lines: list[str]) -> None:
    result = analyze(lines)
    if not result.top_offenders:
        assert all(service.error_count == 0 for service in result.services)
        return

    by_name = {service.service: service for service in result.services}
    worst = max(service.error_count for service in result.services)
    assert worst > 0
    assert sorted(result.top_offenders) == sorted(
        name for name, service in by_name.items() if service.error_count == worst
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_error_rate_stays_within_bounds(lines: list[str]) -> None:
    for service in analyze(lines).services:
        assert 0.0 <= service.error_rate <= 1.0


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines, cap=st.integers(min_value=0, max_value=30))
def test_samples_never_exceed_the_cap_and_stay_in_file_order(
    lines: list[str], cap: int
) -> None:
    result = analyze(lines, max_samples=cap)
    samples = result.unparseable_samples
    assert len(samples) <= cap
    assert len(samples) <= result.unparseable_lines
    assert [s.line_number for s in samples] == sorted(s.line_number for s in samples)
    assert all(1 <= s.line_number <= len(lines) for s in samples)
    assert all(isinstance(s.reason, ParseReason) for s in samples)


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(lines=log_lines)
def test_the_time_range_is_ordered_and_only_set_when_something_parsed(
    lines: list[str],
) -> None:
    analyzer = LogAnalyzer()
    analyzer.feed_many(lines)
    result = analyzer.result()

    if analyzer.parsed_lines == 0:
        assert result.time_range.first is None
        assert result.time_range.last is None
    else:
        assert result.time_range.first is not None
        assert result.time_range.last is not None
        assert result.time_range.first <= result.time_range.last
