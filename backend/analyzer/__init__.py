"""Part 1: the analyzer.

A pure-Python package that turns log lines into a summary.  It never imports
FastAPI or touches the network, so it can be driven by the HTTP API, a test, a
script or a future background worker without change.
"""

from .analyzer import (
    DEFAULT_MAX_LINE_CHARS,
    DEFAULT_MAX_SAMPLES,
    LogAnalyzer,
    analyze,
    iter_lines,
)
from .models import (
    ANALYZER_VERSION,
    AnalysisMeta,
    AnalysisResult,
    LevelCounts,
    LogLevel,
    ParseReason,
    ServiceStats,
    TimeRange,
    UnparseableSample,
    is_analysis_id,
    new_analysis_id,
)
from .parser import ParsedLine, parse_line

__all__ = [
    "ANALYZER_VERSION",
    "DEFAULT_MAX_LINE_CHARS",
    "DEFAULT_MAX_SAMPLES",
    "AnalysisMeta",
    "AnalysisResult",
    "LevelCounts",
    "LogAnalyzer",
    "LogLevel",
    "ParseReason",
    "ParsedLine",
    "ServiceStats",
    "TimeRange",
    "UnparseableSample",
    "analyze",
    "is_analysis_id",
    "iter_lines",
    "new_analysis_id",
    "parse_line",
]
