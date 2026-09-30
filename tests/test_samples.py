"""Every file in samples/ gives the result the README documents.

The samples are there to be tried by hand and shown in a demo, so a change that
quietly alters what one of them produces should fail here first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from app.services.analyzer import analyze, iter_lines
from app.utils.errors import ErrorCode

#: file -> (lines processed, unparseable, blank, top offenders, errors per service)
EXPECTED = {
    "sample.log": (
        7, 1, 0, ["payment-service"],
        {"payment-service": 2, "billing-service": 1, "auth-service": 0},
    ),
    "four-services.log": (
        11, 1, 0, ["payment-service"],
        {"payment-service": 2, "billing-service": 1, "auth-service": 0, "inventory-service": 0},
    ),
    "incident.log": (
        48, 5, 0, ["payment-service"],
        {
            "payment-service": 5,
            "api-gateway": 4,
            "billing-service": 3,
            "notification-service": 1,
            "auth-service": 0,
            "inventory-service": 0,
        },
    ),
    "tie.log": (
        8, 0, 0, ["billing-service", "payment-service"],
        {"billing-service": 3, "payment-service": 3, "auth-service": 0},
    ),
    "healthy.log": (
        12, 0, 0, [],
        {"api-gateway": 0, "auth-service": 0, "inventory-service": 0, "payment-service": 0},
    ),
    "edge-cases.log": (
        10, 5, 1, ["payment-service"],
        {"payment-service": 2, "Payment-Service": 0, "auth-service": 0},
    ),
    "crlf.log": (2, 0, 0, ["payment-service"], {"payment-service": 1, "auth-service": 0}),
    "invalid-utf8.log": (1, 0, 0, ["payment-service"], {"payment-service": 1}),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_sample_gives_its_documented_result(samples_dir: Path, name: str) -> None:
    processed, unparseable, blank, top, errors = EXPECTED[name]
    result = analyze(iter_lines(str(samples_dir / name)))

    assert result.lines_processed == processed
    assert result.unparseable_lines == unparseable
    assert result.blank_lines == blank
    assert result.top_offenders == top
    # Compared as a list, so the worst-first order is checked too.
    assert [(s.service, s.error_count) for s in result.services] == list(errors.items())


def test_the_incident_logs_bad_lines_are_the_ones_real_logs_have(samples_dir: Path) -> None:
    """A stack trace, a line from another logging library, a half-written line."""
    result = analyze(iter_lines(str(samples_dir / "incident.log")))
    assert [(s.line_number, s.reason.value) for s in result.unparseable_samples] == [
        (17, "missing_timestamp"),
        (18, "missing_timestamp"),
        (19, "missing_timestamp"),
        (28, "unknown_level"),
        (33, "missing_timestamp"),
    ]


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("empty.log", 400, ErrorCode.EMPTY_FILE),
        ("binary.log", 415, ErrorCode.UNSUPPORTED_MEDIA_TYPE),
    ],
)
async def test_the_samples_the_server_rejects(
    client: httpx.AsyncClient, samples_dir: Path, error_of, name: str, status: int, code
) -> None:
    response = await client.post(
        "/api/v1/analyses", files={"file": (name, (samples_dir / name).read_bytes())}
    )
    assert response.status_code == status
    assert error_of(response)["code"] == code


def test_the_generator_writes_a_realistic_log(samples_dir: Path, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_large_log", samples_dir / "generate_large_log.py"
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    out = tmp_path / "generated.log"
    generator.generate(out, lines=20_000, seed=7)
    result = analyze(iter_lines(str(out)))

    assert result.lines_processed == 20_000
    # A few malformed lines, as real logs have, but only a few.
    assert 0 < result.unparseable_lines < 200
    assert {s.service for s in result.services} == set(generator.SERVICES)
    assert result.top_offenders == ["payment-service"]
