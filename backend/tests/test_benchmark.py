"""Throughput and memory guards for the analyzer.

Marked ``benchmark`` because timing tests belong in a run you asked for, not in
the one a pre-commit hook fires on a laptop that is busy doing something else::

    pytest -m benchmark          # just these
    pytest -m "not benchmark"    # everything else

The targets are the PRD's, set with headroom over what the parser actually does
so an ordinary machine under ordinary load still passes.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from analyzer import LogAnalyzer, ParseReason

pytestmark = pytest.mark.benchmark

BENCH_LINES = 1_000_000
TARGET_LINES_PER_SECOND = 300_000
GENERATOR = Path(__file__).resolve().parents[2] / "tools" / "gen_logs.py"


@pytest.fixture(scope="module")
def big_log(tmp_path_factory) -> Path:
    """A seeded 1M-line file, ~59 MB, with 2% malformed and 1% blank lines."""
    path = tmp_path_factory.mktemp("bench") / "bench.log"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--lines", str(BENCH_LINES), "--out", str(path)],
        check=True,
        capture_output=True,
    )
    # Warm the OS file cache so the first timed read is not also the first
    # trip to disk.
    path.read_bytes()
    return path


def _feed_file(analyzer: LogAnalyzer, path: Path) -> None:
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        analyzer.feed_many(handle)


def test_parser_throughput(big_log: Path) -> None:
    analyzer = LogAnalyzer()
    gc.collect()
    started = time.perf_counter()
    _feed_file(analyzer, big_log)
    elapsed = time.perf_counter() - started

    rate = analyzer.lines_processed / elapsed
    print(f"\n  {rate:,.0f} lines/s over {analyzer.lines_processed:,} lines")
    assert rate >= TARGET_LINES_PER_SECOND, (
        f"{rate:,.0f} lines/s is below the {TARGET_LINES_PER_SECOND:,} target"
    )


def test_memory_does_not_grow_with_file_size(big_log: Path) -> None:
    """The point of the streaming design: a 59 MB file costs kilobytes."""
    analyzer = LogAnalyzer()
    gc.collect()
    tracemalloc.start()
    try:
        _feed_file(analyzer, big_log)
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    print(f"\n  {retained / 1024:.1f} KB retained for a {big_log.stat().st_size / 1e6:.0f} MB file")
    # Counters for a handful of services plus 20 samples.  A megabyte is three
    # orders of magnitude of slack and still catches an accidental line buffer.
    assert retained < 1024 * 1024


def test_merging_two_halves_of_a_large_file_agrees_with_one_pass(big_log: Path) -> None:
    lines = big_log.read_text(encoding="utf-8", errors="replace").splitlines()
    midpoint = len(lines) // 2

    whole = LogAnalyzer()
    whole.feed_many(lines)

    left, right = LogAnalyzer(), LogAnalyzer()
    left.feed_many(lines[:midpoint])
    right.feed_many(lines[midpoint:])

    fixed_id = "an_0000000000000000"
    assert left.merge(right).result(id=fixed_id) == whole.result(id=fixed_id)


def test_a_pathological_single_line_file_stays_bounded() -> None:
    """One enormous line must cost its limit, not its length."""
    analyzer = LogAnalyzer(max_line_chars=64 * 1024)
    gc.collect()
    tracemalloc.start()
    try:
        analyzer.feed("x" * (4 * 1024 * 1024))
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    result = analyzer.result()
    assert result.unparseable_lines == 1
    assert result.unparseable_samples[0].reason is ParseReason.LINE_TOO_LONG
    # Only the 500-character sample survives; the 4 MB line itself was the
    # caller's, and the analyzer kept no reference to it.
    assert retained < 64 * 1024
