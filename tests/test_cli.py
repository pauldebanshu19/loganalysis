"""The command-line client: the printed summary, and what goes to which stream.

The CLI lives outside the ``app`` package, in ``client/logscan.py``, so it is
loaded from its file rather than imported.  The server is never started: the
client's one network call is replaced with a canned result.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

CLI_PATH = Path(__file__).resolve().parents[1] / "client" / "logscan.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("logscan", CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["logscan"] = module
    spec.loader.exec_module(module)
    return module


logscan = _load_cli()

#: What the API returns for the brief's expected-output example, trimmed to the
#: fields the CLI reads.  Services arrive worst first, ties by name.
SAMPLE_RESULT: dict[str, Any] = {
    "id": "an_01M3SRAS0894QTBP",
    "lines_processed": 11,
    "unparseable_lines": 1,
    "blank_lines": 0,
    "services": [
        {"service": "payment-service", "error_count": 2},
        {"service": "billing-service", "error_count": 1},
        {"service": "auth-service", "error_count": 0},
        {"service": "inventory-service", "error_count": 0},
    ],
    "top_offenders": ["payment-service"],
    "unparseable_samples": [
        {
            "line_number": 7,
            "reason": "missing_timestamp",
            "text": "error billing-service No Auth token",
        }
    ],
    "meta": {"filename": "sample.log", "bytes": 512, "duration_ms": 5.23},
}

EXPECTED_SUMMARY = """\
=================================================================
                    LOG ANALYSIS RESULT
=================================================================

Lines processed   : 11
Unparseable lines : 1

Service Name                         Error count
-----------------------------------------------------------------
payment-service                                2
billing-service                                1
auth-service                                   0
inventory-service                              0

-----------------------------------------------------------------

Top offender      : payment-service


Processing time   : 5.23 ms
=================================================================
"""


def render(result: dict[str, Any], **kwargs: Any) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True)
    logscan.render_summary(result, console, **kwargs)
    return buffer.getvalue()


def lines_of(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


class TestSummary:
    def test_matches_the_expected_layout(self) -> None:
        assert render(SAMPLE_RESULT) == EXPECTED_SUMMARY

    def test_a_tie_names_every_tied_service(self) -> None:
        result = {
            **SAMPLE_RESULT,
            "services": [
                {"service": "payment-service", "error_count": 2},
                {"service": "billing-service", "error_count": 2},
            ],
            "top_offenders": ["billing-service", "payment-service"],
        }
        assert lines_of(render(result), "Top offender") == [
            "Top offender      : billing-service, payment-service (tied, 2 errors each)"
        ]

    def test_no_errors_means_no_offender(self) -> None:
        result = {**SAMPLE_RESULT, "top_offenders": []}
        assert lines_of(render(result), "Top offender") == ["Top offender      : none"]

    def test_large_numbers_get_thousands_separators(self) -> None:
        result = {
            **SAMPLE_RESULT,
            "lines_processed": 1247,
            "services": [{"service": "payment-service", "error_count": 12345}],
            "meta": {"duration_ms": 1234.5},
        }
        text = render(result)
        assert "Lines processed   : 1,247" in text
        assert "Processing time   : 1,234.50 ms" in text
        assert lines_of(text, "payment-service")[0].endswith(" 12,345")

    def test_a_long_service_name_widens_the_table_and_keeps_counts_aligned(
        self,
    ) -> None:
        long_name = "notification-dispatch-service-eu-west-1-canary-pool-b"
        result = {
            **SAMPLE_RESULT,
            "services": [
                {"service": long_name, "error_count": 3},
                {"service": "auth-service", "error_count": 0},
            ],
        }
        text = render(result)
        heading = lines_of(text, "Service Name")[0]
        rows = [*lines_of(text, long_name), *lines_of(text, "auth-service")]
        assert all(len(row) == len(heading) for row in rows)
        assert lines_of(text, long_name)[0].startswith(long_name + "  ")
        rules = lines_of(text, "---")
        assert rules and all(len(rule) >= len(heading) for rule in rules)

    def test_a_file_with_no_parsable_lines_still_prints_the_block(self) -> None:
        result = {**SAMPLE_RESULT, "services": [], "top_offenders": []}
        text = render(result)
        assert "(no line could be parsed)" in text
        assert text.rstrip().endswith("=" * logscan.WIDTH)

    def test_markup_in_a_service_name_is_printed_literally(self) -> None:
        result = {
            **SAMPLE_RESULT,
            "services": [{"service": "[bold]svc[/bold]", "error_count": 1}],
            "top_offenders": ["[bold]svc[/bold]"],
        }
        assert "Top offender      : [bold]svc[/bold]" in render(result)

    def test_show_unparseable_lists_bad_lines_between_offender_and_timing(
        self,
    ) -> None:
        text = render(SAMPLE_RESULT, show_unparseable=True)
        tail = text.split("Top offender      : payment-service\n", 1)[1]
        assert tail == (
            "\n"
            "Unparseable lines (showing 1 of 1):\n"
            "  line 7  missing_timestamp: error billing-service No Auth token\n"
            "\n"
            "Processing time   : 5.23 ms\n"
            + "=" * logscan.WIDTH
            + "\n"
        )


class TestCommand:
    @pytest.fixture
    def run(self, monkeypatch, brief_log: Path):
        monkeypatch.setattr(
            logscan.LogscanClient, "analyze", lambda self, path, **kw: SAMPLE_RESULT
        )
        runner = CliRunner()

        def invoke(*args: str):
            return runner.invoke(logscan.app, [str(brief_log), *args])

        return invoke

    def test_summary_is_preceded_by_upload_status(self, run, brief_log: Path) -> None:
        result = run()
        assert result.exit_code == 0, result.output
        assert result.stdout.startswith(
            f"Uploading: {brief_log}\n\nJob completed.\n\n" + "=" * logscan.WIDTH
        )
        assert result.stdout.endswith(EXPECTED_SUMMARY)

    def test_json_keeps_stdout_parseable_and_status_on_stderr(self, run) -> None:
        result = run("--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == SAMPLE_RESULT
        assert "Uploading:" in result.stderr
        assert "Job completed." in result.stderr

    def test_fail_on_errors_exits_4_when_a_service_has_errors(self, run) -> None:
        assert run("--fail-on-errors").exit_code == logscan.EXIT_ERRORS_FOUND
