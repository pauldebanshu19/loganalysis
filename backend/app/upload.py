"""Reading an upload without ever holding it in memory.

FastAPI's ``UploadFile`` spools the whole body to disk before a handler sees a
byte of it, which makes limits something you check after paying for them.  Here
the bytes are read a chunk at a time, split into lines, and handed straight to
the analyzer, so:

* a 5 GB upload is cut off at the configured limit rather than after it lands,
* memory stays flat regardless of file size,
* a slow client holds a socket, not a gigabyte,
* and the analysis is finished at roughly the moment the last byte arrives.

Reading is async so a slow client never blocks the event loop; parsing is CPU
work, so blocks of bytes are handed to a worker thread instead of being parsed
inline.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
from anyio import to_thread
from starlette.requests import ClientDisconnect, Request

from analyzer import LogAnalyzer, ParseReason

from .config import Settings
from .errors import EmptyFile, FileTooLarge, MissingFile, UnsupportedMediaType, UploadTimeout

log = logging.getLogger("app.upload")

#: Bytes buffered in the event loop before a block is handed to a worker
#: thread.  Small enough to stay flat in memory, large enough that the
#: hand-off cost is amortised over thousands of lines.
DISPATCH_BYTES = 256 * 1024

#: How much of the start of a file is checked for NUL bytes.  A text log has
#: none; anything that does is a binary file sent by mistake, and analysing it
#: would produce a meaningless wall of unparseable lines.
BINARY_PROBE_BYTES = 8 * 1024

#: Bytes of an over-long line kept for its sample.  The analyzer cuts the text
#: to its own 500-character limit; this only has to be comfortably more than
#: that even if every character is four bytes.
#:
#: The head is always taken at a fixed length rather than "whatever was in the
#: buffer when the limit tripped", which would make the reported sample depend
#: on where the network happened to split the stream.
OVERLONG_HEAD_BYTES = 4 * 1024

#: Slack above the file limit for a multipart envelope -- boundaries, a
#: Content-Disposition header, a filename.  Without it a file of exactly the
#: limit is refused over a couple of hundred bytes of framing the user never
#: chose and cannot see, and the error reads as an off-by-one.
MULTIPART_ALLOWANCE_BYTES = 64 * 1024

#: The form field the file must arrive in.
FILE_FIELD = "file"


@dataclass(slots=True)
class UploadOutcome:
    """What the reader learned that the analyzer could not."""

    filename: str | None
    size_bytes: int


class LineFeeder:
    """Splits a byte stream into lines and feeds them to an analyzer.

    Works on bytes rather than on decoded text so that the line-length limit is
    a byte limit, as configured, and so that a multi-byte character split
    across two chunks is reassembled before anything tries to decode it.
    """

    __slots__ = (
        "_analyzer",
        "_buffer",
        "_bytes_seen",
        "_head_bytes",
        "_max_line_bytes",
        "_probe_left",
        "_skipping",
    )

    def __init__(self, analyzer: LogAnalyzer, *, max_line_bytes: int) -> None:
        self._analyzer = analyzer
        self._max_line_bytes = max_line_bytes
        # Whenever an over-long line is reported, at least this many of its
        # bytes are in hand, whether it ended or merely ran on -- so the sample
        # is the same however the stream was chunked.
        self._head_bytes = min(OVERLONG_HEAD_BYTES, max_line_bytes)
        self._buffer = bytearray()
        self._bytes_seen = 0
        self._probe_left = BINARY_PROBE_BYTES
        #: True while the tail of an over-long line is being thrown away.
        self._skipping = False

    @property
    def bytes_seen(self) -> int:
        """Bytes of actual file content, excluding any multipart envelope."""
        return self._bytes_seen

    def push(self, chunk: bytes) -> None:
        """Take the next chunk of the file. Safe to call from a worker thread."""
        self._reject_binary(chunk)
        self._bytes_seen += len(chunk)

        buffer = self._buffer
        buffer += chunk

        analyzer = self._analyzer
        max_line_bytes = self._max_line_bytes
        lines: list[str] = []
        start = 0
        skipping = self._skipping

        while (newline := buffer.find(b"\n", start)) != -1:
            segment = buffer[start:newline]
            start = newline + 1
            if skipping:
                # The remainder of a line already reported as too long.
                skipping = False
                continue
            if len(segment) > max_line_bytes:
                # A complete but over-long line.  It is reported here rather
                # than left for the analyzer's own length check, because that
                # check counts characters while the configured limit is in
                # bytes -- and because whether a line arrives whole or in
                # pieces must not change the reason it is given.
                if lines:
                    analyzer.feed_many(lines)
                    lines = []
                analyzer.feed_unparseable(
                    str(bytes(segment[: self._head_bytes]), "utf-8", "replace"),
                    ParseReason.LINE_TOO_LONG,
                )
                continue
            lines.append(str(segment, "utf-8", "replace"))
        del buffer[:start]

        overlong_head: str | None = None
        if skipping:
            # Still inside an over-long line and no newline in sight.
            buffer.clear()
        elif len(buffer) > max_line_bytes:
            # A line has run past the limit and has not ended yet.  Keep its
            # head for the sample, drop the rest, and resume at the next
            # newline -- otherwise a single 5 GB line would be buffered whole,
            # which is the exact failure this module exists to prevent.
            overlong_head = str(bytes(buffer[: self._head_bytes]), "utf-8", "replace")
            buffer.clear()
            skipping = True

        self._skipping = skipping

        # Order matters: the unterminated over-long line comes after every
        # complete line in this chunk, and its line number has to reflect that.
        if lines:
            analyzer.feed_many(lines)
        if overlong_head is not None:
            analyzer.feed_unparseable(overlong_head, ParseReason.LINE_TOO_LONG)

    def finish(self) -> None:
        """Flush a final line that had no trailing newline."""
        if self._skipping:
            self._buffer.clear()
            self._skipping = False
            return
        if self._buffer:
            line = str(bytes(self._buffer), "utf-8", "replace")
            self._buffer.clear()
            self._analyzer.feed(line)

    def _reject_binary(self, chunk: bytes) -> None:
        if self._probe_left <= 0:
            return
        probe = chunk[: self._probe_left]
        if b"\x00" in probe:
            raise UnsupportedMediaType(
                "This looks like a binary file rather than a text log.",
                found="nul_byte",
            )
        self._probe_left -= len(probe)


class _AnalyzerTarget:
    """Bridges streaming-form-data's target protocol to a :class:`LineFeeder`.

    Deliberately not a subclass of ``BaseTarget``: the only behaviour needed is
    the four hook methods the parser calls, and duck-typing them keeps the
    coupling to one library's class hierarchy out of the way.
    """

    def __init__(self, feeder: LineFeeder) -> None:
        self._feeder = feeder
        self.multipart_filename: str | None = None
        self.multipart_content_type: str | None = None
        self._started = False
        self._finished = False

    @property
    def received_a_file(self) -> bool:
        return self._started

    def start(self) -> None:
        self._started = True

    def data_received(self, chunk: bytes) -> None:
        self._feeder.push(chunk)

    def finish(self) -> None:
        self._feeder.finish()
        self._finished = True

    def set_multipart_filename(self, filename: str) -> None:
        self.multipart_filename = filename

    def set_multipart_content_type(self, content_type: str) -> None:
        self.multipart_content_type = content_type


def media_type_of(request: Request) -> str:
    """The request's content type without its parameters, lowercased."""
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


async def read_upload(
    request: Request, analyzer: LogAnalyzer, settings: Settings
) -> UploadOutcome:
    """Stream the request body into ``analyzer``.

    Accepts ``multipart/form-data`` with a ``file`` field, as browsers and the
    CLI send, or a raw ``text/plain`` body with an optional ``X-Filename``
    header, as ``curl --data-binary`` sends.  Anything else is a 415 before a
    byte of body is read.
    """
    media_type = media_type_of(request)
    if media_type == "multipart/form-data":
        return await _read_multipart(request, analyzer, settings)
    if media_type == "text/plain":
        return await _read_raw_body(request, analyzer, settings)
    raise UnsupportedMediaType(
        f"Content-Type {media_type or '(none)'} is not supported."
        if media_type
        else UnsupportedMediaType.message,
        received=media_type or None,
        supported=["multipart/form-data", "text/plain"],
    )


async def _read_multipart(
    request: Request, analyzer: LogAnalyzer, settings: Settings
) -> UploadOutcome:
    from streaming_form_data import StreamingFormDataParser

    feeder = LineFeeder(analyzer, max_line_bytes=settings.max_line_bytes)
    target = _AnalyzerTarget(feeder)
    parser = StreamingFormDataParser(headers=request.headers)
    parser.register(FILE_FIELD, target)  # type: ignore[arg-type]

    await _pump(request, settings, parser.data_received)

    if not target.received_a_file:
        raise MissingFile(
            f"The request has no `{FILE_FIELD}` field.", expected_field=FILE_FIELD
        )
    if feeder.bytes_seen == 0:
        raise EmptyFile()
    _reject_oversize_file(feeder.bytes_seen, settings.max_upload_bytes)
    # A truncated body means the parser never reached the closing boundary and
    # so never called finish(); flushing here keeps a final line that had no
    # trailing newline.  finish() is idempotent, so the usual path is unharmed.
    feeder.finish()

    # The size reported is the file's own, not the socket's: the multipart
    # envelope is the transport's overhead, and `meta.bytes` is meant to line
    # up with what the user sees in a file listing.
    return UploadOutcome(
        filename=sanitize_filename(target.multipart_filename),
        size_bytes=feeder.bytes_seen,
    )


async def _read_raw_body(
    request: Request, analyzer: LogAnalyzer, settings: Settings
) -> UploadOutcome:
    feeder = LineFeeder(analyzer, max_line_bytes=settings.max_line_bytes)
    size = await _pump(request, settings, feeder.push)
    if size == 0:
        raise EmptyFile()
    # The raw path has no envelope, so the body is the file; it still goes
    # through the same check, which is what the transport allowance permitted
    # through above.
    _reject_oversize_file(size, settings.max_upload_bytes)
    feeder.finish()
    return UploadOutcome(
        filename=sanitize_filename(request.headers.get("x-filename")), size_bytes=size
    )


async def _pump(request: Request, settings: Settings, consume) -> int:
    """Read the body and hand it to ``consume`` in blocks, off the event loop.

    Returns the number of body bytes read.  ``consume`` is called from a worker
    thread, one block at a time and never concurrently, so it can be ordinary
    synchronous code.

    The ceiling applied here is the *transport* one -- the file limit plus room
    for a multipart envelope -- and it exists to stop an unbounded upload, not
    to decide the answer.  Whether the file itself is too large is settled by
    the caller against the bytes the analyzer actually saw, so that a 100 MB
    file is accepted rather than refused over a few hundred bytes of framing.
    """
    limit = settings.max_upload_bytes + MULTIPART_ALLOWANCE_BYTES
    _reject_declared_oversize(request, limit, settings.max_upload_bytes)

    block = bytearray()
    total = 0
    try:
        async for chunk in _chunks(request, settings.UPLOAD_IDLE_TIMEOUT_S):
            total += len(chunk)
            if total > limit:
                raise FileTooLarge(
                    _too_large_message(total, settings.max_upload_bytes),
                    limit_bytes=settings.max_upload_bytes,
                    received_bytes=total,
                )
            block += chunk
            if len(block) >= DISPATCH_BYTES:
                payload = bytes(block)
                block.clear()
                await to_thread.run_sync(consume, payload)
        if block:
            await to_thread.run_sync(consume, bytes(block))
    except ClientDisconnect:
        # Nobody is listening for a response.  Let the caller unwind so the
        # analysis slot is released, and say so in the log rather than in a
        # reply that goes nowhere.
        log.info("client_disconnected", extra={"bytes_read": total})
        raise

    return total


async def _chunks(request: Request, idle_timeout: float) -> AsyncIterator[bytes]:
    """Yield body chunks, giving up if the client goes quiet.

    A client that opens a connection, sends a header and then stops costs a
    slot for as long as it is tolerated; ``UPLOAD_IDLE_TIMEOUT_S`` is how long
    that is.
    """
    stream = request.stream().__aiter__()
    while True:
        try:
            with anyio.fail_after(idle_timeout):
                chunk = await stream.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise UploadTimeout(
                f"No data received for {idle_timeout:g} seconds.",
                idle_timeout_s=idle_timeout,
            ) from None
        if chunk:
            yield chunk


def _reject_declared_oversize(
    request: Request, transport_limit: int, file_limit: int
) -> None:
    """Refuse an oversized upload from its ``Content-Length`` alone.

    Saves reading a gigabyte to learn what the header already said.  A client
    that lies, or uses chunked encoding and sends no length, is still caught by
    the running count in :func:`_pump`.

    The header covers the whole body, so it is compared against the transport
    ceiling; the limit reported back is the file one, because that is the
    number the client can act on.
    """
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        return
    if int(declared) > transport_limit:
        raise FileTooLarge(
            _too_large_message(int(declared), file_limit),
            limit_bytes=file_limit,
            received_bytes=int(declared),
        )


def _reject_oversize_file(size: int, limit: int) -> None:
    """The authoritative check: the file's own bytes against the file limit."""
    if size > limit:
        raise FileTooLarge(
            _too_large_message(size, limit),
            limit_bytes=limit,
            received_bytes=size,
        )


def _too_large_message(received: int, limit: int) -> str:
    return (
        f"File is {received / 1024 / 1024:.0f} MB; "
        f"the limit is {limit // 1024 // 1024} MB."
    )


def sanitize_filename(name: str | None) -> str | None:
    """Make a client-supplied filename safe to echo back.

    The name is never used to open anything, but it is rendered in a browser
    and printed in a terminal, so path separators, control characters and
    unbounded length all come off before it goes anywhere.
    """
    if not name:
        return None
    # Take the last path segment under either separator, so neither
    # "../../etc/passwd" nor a Windows path survives as one.
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in cleaned if char.isprintable()).strip()
    cleaned = cleaned.lstrip(".") or None
    return cleaned[:255] if cleaned else None


__all__ = [
    "BINARY_PROBE_BYTES",
    "DISPATCH_BYTES",
    "MULTIPART_ALLOWANCE_BYTES",
    "FILE_FIELD",
    "LineFeeder",
    "UploadOutcome",
    "media_type_of",
    "read_upload",
    "sanitize_filename",
]
