from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, BinaryIO

import httpx
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

DEFAULT_SERVER = "http://localhost:8000"
ANALYSES_PATH = "/api/v1/analyses"

#: Waits between attempts. Also the cap on how many retries there can be.
BACKOFF_SECONDS = (0.5, 1.0, 2.0)

#: Statuses worth trying again. 502 and 504 are here because they mean a
#: service behind a reverse proxy was briefly unreachable, which is exactly the
#: kind of thing that resolves itself.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

#: A server can ask for a longer wait than we would choose, but not an absurd
#: one -- a misconfigured proxy should not park the CLI for an hour.
MAX_RETRY_AFTER = 60.0

#: The server enforces its own limit; checking here too means an obviously
#: oversized file fails in milliseconds instead of after a long upload.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

#: Files smaller than this upload too quickly for a progress bar to be
#: anything but a flicker.
PROGRESS_THRESHOLD_BYTES = 5 * 1024 * 1024

EXIT_OK = 0
EXIT_LOCAL = 1
EXIT_REJECTED = 2
EXIT_UNREACHABLE = 3
EXIT_ERRORS_FOUND = 4


# -- output ----------------------------------------------------------------

#: Width of the result block, and of the rules drawn across it.
WIDTH = 65

TITLE = "LOG ANALYSIS RESULT"
TITLE_INDENT = 20

#: Labels are padded to the longest one plus a space, so the colons line up.
LABEL_WIDTH = len("Unparseable lines") + 1

#: Column headings.
NAME_HEADING = "Service Name"
COUNT_HEADING = "Error count"

#: Width of the name column. Error counts are right-aligned to the end of the
#: "Error count" heading; a service name too long to fit widens the column.
NAME_WIDTH = 37


def render_summary(
    result: dict[str, Any], console: Console, *, show_unparseable: bool = False
) -> None:
    """Print the result block.

        =================================================================
                            LOG ANALYSIS RESULT
        =================================================================

        Lines processed   : 7
        Unparseable lines : 1

        Service Name                         Error count
        -----------------------------------------------------------------
        payment-service                                2
        billing-service                                1
        auth-service                                   0

        -----------------------------------------------------------------

        Top offender      : payment-service


        Processing time   : 1.87 ms
        =================================================================

    Services come worst first, as the API sorts them. Colour is decoration
    only: every number and label is in the text, so piping the output to a
    file loses nothing.
    """
    meta = result.get("meta") or {}
    lines = [
        "=" * WIDTH,
        " " * TITLE_INDENT + TITLE,
        "=" * WIDTH,
        "",
        _field("Lines processed", f"{result['lines_processed']:,}"),
        _field("Unparseable lines", f"{result['unparseable_lines']:,}"),
        "",
        *_service_table(result.get("services") or []),
        "",
        _field("Top offender", _top_offender(result)),
        "",
    ]
    if show_unparseable:
        lines.extend(_unparseable_lines(result))
    lines.extend(
        [
            "",
            _field("Processing time", f"{meta.get('duration_ms', 0):,.2f} ms"),
            "=" * WIDTH,
        ]
    )
    for line in lines:
        # Service names and log text are data: never let Rich read them as markup.
        console.print(line, highlight=False, markup=False)


def _field(label: str, value: str) -> str:
    return f"{label:<{LABEL_WIDTH}}: {value}"


def _service_table(services: list[dict[str, Any]]) -> list[str]:
    """The service/error-count table, rules included, as pre-formatted lines."""
    names = [str(service["service"]) for service in services]
    counts = [f"{service['error_count']:,}" for service in services]

    # Lists, not bare arguments: with no services, max(37) alone would raise.
    name_width = max([NAME_WIDTH, *(len(name) + 2 for name in names)])
    count_width = max([len(COUNT_HEADING), *(len(count) for count in counts)])
    rule = "-" * max(WIDTH, name_width + count_width)

    rows = [
        f"{name:<{name_width}}{count:>{count_width}}"
        for name, count in zip(names, counts, strict=True)
    ]
    return [
        f"{NAME_HEADING:<{name_width}}{COUNT_HEADING:>{count_width}}",
        rule,
        *(rows or ["(no line could be parsed)"]),
        "",
        rule,
    ]


def _top_offender(result: dict[str, Any]) -> str:
    """Who produced the most errors: one service, several tied, or none."""
    offenders = result.get("top_offenders") or []
    if not offenders:
        return "none"
    if len(offenders) == 1:
        return str(offenders[0])

    by_name = {service["service"]: service for service in result.get("services", [])}
    errors = by_name[offenders[0]]["error_count"] if offenders[0] in by_name else 0
    joined = ", ".join(sorted(offenders))
    return f"{joined} (tied, {errors:,} errors each)"


def _unparseable_lines(result: dict[str, Any]) -> list[str]:
    """The optional list of bad lines, shown between the offender and the timing."""
    samples = result.get("unparseable_samples") or []
    total = result.get("unparseable_lines", 0)

    if not samples:
        return [
            "No unparseable lines."
            if not total
            else f"{total:,} unparseable lines, no samples requested."
        ]

    width = max(len(str(sample["line_number"])) for sample in samples)
    return [
        f"Unparseable lines (showing {len(samples):,} of {total:,}):",
        *(
            f"  line {str(sample['line_number']).rjust(width)}  "
            f"{sample['reason']}: {sample['text']}"
            for sample in samples
        ),
    ]


def render_json(result: dict[str, Any], console: Console) -> None:
    """Print the result exactly as the server sent it."""
    console.print(json.dumps(result, indent=2, ensure_ascii=False), soft_wrap=True)


# -- talking to the server -------------------------------------------------


class LocalError(Exception):
    """Something was wrong before the request was made."""


class ApiError(Exception):
    """The server refused the request and said why."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details or {}
        super().__init__(message)


class TransportError(Exception):
    """The server could not be reached, or did not answer in time."""


class _ProgressReader:
    """A file wrapper that reports how much has been read.

    httpx asks the file object for chunks as it writes them to the socket, so
    counting here counts bytes actually sent rather than bytes queued.
    """

    def __init__(self, handle: BinaryIO, on_progress: Callable[[int], None]) -> None:
        self._handle = handle
        self._on_progress = on_progress

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._on_progress(len(chunk))
        return chunk

    def __getattr__(self, name: str) -> Any:
        # httpx also reaches for `name`, `seek` and `tell`; pass them through
        # to the real handle rather than reimplementing a file.
        return getattr(self._handle, name)


class LogscanClient:
    """A thin, retrying client for the one endpoint the CLI needs.

    The file is streamed from disk rather than read into memory, so `logscan`
    on a 100 MB log costs a buffer, not 100 MB of RSS -- the same property the
    server side is built around.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_SERVER,
        *,
        timeout: float = 120.0,
        retries: int = 3,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, min(retries, len(BACKOFF_SECONDS)))
        self.api_key = api_key

    def analyze(
        self,
        path: Path,
        *,
        samples: int | None = None,
        on_progress: Callable[[int], None] | None = None,
        on_retry: Callable[[int, float, str], None] | None = None,
    ) -> dict[str, Any]:
        """Upload ``path`` and return the summary.

        Raises :class:`ApiError` if the server refused it, or
        :class:`TransportError` if it could not be reached.
        """
        url = f"{self.base_url}{ANALYSES_PATH}"
        params = {} if samples is None else {"samples": samples}
        headers = {"X-API-Key": self.api_key} if self.api_key else {}

        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    with path.open("rb") as handle:
                        stream: Any = handle
                        if on_progress is not None:
                            stream = _ProgressReader(handle, on_progress)
                        response = client.post(
                            url,
                            params=params,
                            headers=headers,
                            files={"file": (path.name, stream, "text/plain")},
                        )
            except httpx.TimeoutException as exc:
                reason = f"timed out after {self.timeout:g}s"
                if attempt >= self.retries:
                    raise TransportError(f"{self.base_url}: {reason}") from exc
                attempt = self._wait(attempt, None, reason, on_retry)
                continue
            except httpx.HTTPError as exc:
                reason = f"could not connect ({type(exc).__name__})"
                if attempt >= self.retries:
                    raise TransportError(f"{self.base_url}: {reason}") from exc
                attempt = self._wait(attempt, None, reason, on_retry)
                continue

            if response.status_code < 400:
                return response.json()

            error = _parse_error(response)
            retryable = (
                response.status_code in RETRYABLE_STATUSES
                or response.status_code >= 500
            )
            if not retryable or attempt >= self.retries:
                raise error

            attempt = self._wait(
                attempt, _retry_after(response), f"server said {error.code}", on_retry
            )

    def _wait(
        self,
        attempt: int,
        retry_after: float | None,
        reason: str,
        on_retry: Callable[[int, float, str], None] | None,
    ) -> int:
        """Sleep before the next attempt; return the new attempt number."""
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        if retry_after is not None:
            # The server knows when it will be ready; our backoff is only a
            # guess, so prefer its answer when it gave one.
            delay = min(retry_after, MAX_RETRY_AFTER)
        if on_retry is not None:
            on_retry(attempt + 1, delay, reason)
        time.sleep(delay)
        return attempt + 1


def _parse_error(response: httpx.Response) -> ApiError:
    """Turn a failure response into an :class:`ApiError`.

    The server always sends the same body, but a proxy in front of it might
    not, so a response that is not the expected shape still produces a usable
    error rather than a parse failure.
    """
    request_id = response.headers.get("x-request-id")
    try:
        error = response.json()["error"]
        return ApiError(
            status=response.status_code,
            code=str(error["code"]),
            message=str(error["message"]),
            request_id=error.get("request_id") or request_id,
            details=error.get("details") or {},
        )
    except (ValueError, KeyError, TypeError):
        return ApiError(
            status=response.status_code,
            code=f"http_{response.status_code}",
            message=(response.text or response.reason_phrase or "Unknown error")[:500],
            request_id=request_id,
        )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The header also permits an HTTP date; the backoff is a fine fallback.
        return None


# -- the command -----------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=__doc__,
)


@app.command()
def main(
    path: Annotated[
        Path,
        typer.Argument(
            metavar="LOGFILE", help="The log file to analyse.", show_default=False
        ),
    ],
    server: Annotated[
        str,
        typer.Option(
            "--server", envvar="LOGSCAN_SERVER", help="Base URL of the log analyzer API."
        ),
    ] = DEFAULT_SERVER,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the raw JSON result.")
    ] = False,
    show_unparseable: Annotated[
        bool,
        typer.Option(
            "--show-unparseable",
            help="Also print sample bad lines with line number and reason.",
        ),
    ] = False,
    fail_on_errors: Annotated[
        bool,
        typer.Option(
            "--fail-on-errors", help="Exit 4 if any service has errors, for use in CI."
        ),
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", metavar="SEC", help="Whole-request timeout.")
    ] = 120.0,
    retries: Annotated[
        int,
        typer.Option(
            "--retries",
            metavar="N",
            help="Retries on connection errors, 429, 502, 503 and 504.",
        ),
    ] = 3,
    samples: Annotated[
        int | None,
        typer.Option(
            "--samples",
            metavar="N",
            help="How many unparseable examples to request (0-100).",
        ),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="LOGSCAN_API_KEY",
            help="Sent as X-API-Key, for a server that requires one.",
        ),
    ] = None,
    no_color: Annotated[
        bool, typer.Option("--no-color", help="Plain output; also off when not a TTY.")
    ] = False,
) -> None:
    """Analyse LOGFILE and print a summary."""
    console = _console(no_color)
    errors = Console(stderr=True, no_color=_no_color(no_color), highlight=False)

    try:
        size = _check_local(path)
    except LocalError as exc:
        errors.print(f"logscan: {exc}")
        raise typer.Exit(EXIT_LOCAL)

    if retries < 0:
        errors.print("logscan: --retries cannot be negative")
        raise typer.Exit(EXIT_LOCAL)
    if timeout <= 0:
        errors.print("logscan: --timeout must be greater than zero")
        raise typer.Exit(EXIT_LOCAL)

    client = LogscanClient(server, timeout=timeout, retries=retries, api_key=api_key)

    def announce_retry(attempt: int, delay: float, reason: str) -> None:
        errors.print(f"logscan: {reason}; retrying in {delay:g}s ({attempt})")

    # Progress lines go to stderr under --json, so stdout stays parseable JSON.
    status = errors if as_json else console
    status.print(f"Uploading: {path}", highlight=False, markup=False)

    try:
        result = _upload(client, path, size, samples, console, announce_retry)
    except ApiError as exc:
        _report_api_error(exc, errors)
        raise typer.Exit(EXIT_UNREACHABLE if exc.status >= 500 else EXIT_REJECTED)
    except TransportError as exc:
        errors.print(f"logscan: {exc}")
        raise typer.Exit(EXIT_UNREACHABLE)

    status.print()
    status.print("Job completed.")
    status.print()

    if as_json:
        render_json(result, console)
    else:
        render_summary(result, console, show_unparseable=show_unparseable)

    if fail_on_errors and any(
        service["error_count"] > 0 for service in result.get("services", [])
    ):
        raise typer.Exit(EXIT_ERRORS_FOUND)


def _upload(
    client: LogscanClient,
    path: Path,
    size: int,
    samples: int | None,
    console: Console,
    on_retry: Callable[[int, float, str], None],
) -> dict[str, Any]:
    """Run the upload, with a progress bar when one is worth showing."""
    if size < PROGRESS_THRESHOLD_BYTES or not console.is_terminal:
        return client.analyze(path, samples=samples, on_retry=on_retry)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Uploading {path.name}", total=size)

        def advance(sent: int) -> None:
            progress.advance(task, sent)

        def retry(attempt: int, delay: float, reason: str) -> None:
            # Starting over means the bytes already counted were not delivered.
            progress.reset(task)
            on_retry(attempt, delay, reason)

        return client.analyze(
            path, samples=samples, on_progress=advance, on_retry=retry
        )


def _check_local(path: Path) -> int:
    """Fail fast on a file the server would certainly reject.

    Returns the file's size. Every one of these would otherwise cost a full
    round trip to discover -- and for the size check, a full upload.
    """
    if not path.exists():
        raise LocalError(f"{path}: no such file")
    if path.is_dir():
        raise LocalError(f"{path}: is a directory, not a file")
    if not path.is_file():
        raise LocalError(f"{path}: not a regular file")
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise LocalError(f"{path}: cannot read ({exc.strerror or exc})") from exc

    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise LocalError(
            f"{path}: {size / 1024 / 1024:.0f} MB exceeds the "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit"
        )
    return size


def _report_api_error(exc: ApiError, errors: Console) -> None:
    """Print what the server said, plus the id that identifies it in the logs."""
    errors.print(f"logscan: {exc.message}")
    errors.print(f"  code: {exc.code}  status: {exc.status}")
    if exc.request_id:
        errors.print(f"  request id: {exc.request_id}")


def _no_color(flag: bool) -> bool:
    """Colour is off when asked, and when the usual environment says so.

    ``NO_COLOR`` is the cross-tool convention; honouring it means `logscan`
    behaves like the rest of a user's terminal without a second flag.
    """
    return flag or bool(os.environ.get("NO_COLOR"))


def _console(no_color: bool) -> Console:
    # Rich turns colour off by itself when stdout is not a terminal, so piping
    # to a file is already plain without the flag.
    return Console(no_color=_no_color(no_color), highlight=False, soft_wrap=True)


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    sys.exit(app())
