"""``/api/v1/analyses`` -- an analysis is a resource.

``POST`` creates one and returns it; ``GET`` fetches it again by id.  Only the
summary is stored, for an hour, which is what gives the web UI a shareable
result link without the service keeping anyone's log contents.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request, Response

from analyzer import AnalysisResult, LogAnalyzer, is_analysis_id

from ..config import Settings, get_settings
from ..errors import AnalysisNotFound, ErrorResponse
from ..metrics import record_analysis
from ..middleware import note
from ..security import require_api_key
from ..upload import read_upload

router = APIRouter(
    prefix="/api/v1/analyses",
    tags=["analyses"],
    dependencies=[Depends(require_api_key)],
)

#: Documented failure responses, so the OpenAPI schema (and the TypeScript
#: types generated from it) knows every shape a client can receive.
_POST_ERRORS = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        400: "No file in the request, or the file is empty.",
        401: "An API key is required and was missing or wrong.",
        408: "The upload stalled.",
        413: "The file is over the size limit.",
        415: "Unsupported content type, or the content is binary.",
        422: "A query parameter is invalid.",
        429: "Too many requests from this client.",
        503: "Every analysis slot is busy.",
    }.items()
}

_GET_ERRORS = {
    404: {"model": ErrorResponse, "description": "No such analysis, or it expired."},
    401: {"model": ErrorResponse, "description": "An API key is required."},
}


@router.post(
    "",
    status_code=201,
    response_model=AnalysisResult,
    responses=_POST_ERRORS,
    summary="Analyse a log file",
    description=(
        "Send the log either as `multipart/form-data` with the file in a `file` "
        "field, or as a raw `text/plain` body with an optional `X-Filename` "
        "header. The body is read, parsed and discarded as it arrives; only the "
        "summary is kept."
    ),
)
async def create_analysis(
    request: Request,
    response: Response,
    samples: int | None = Query(
        default=None,
        ge=0,
        le=100,
        description="How many unparseable lines to return as examples. Default 20.",
    ),
    settings: Settings = Depends(get_settings),
) -> AnalysisResult:
    max_samples = min(
        settings.DEFAULT_SAMPLES if samples is None else samples, settings.MAX_SAMPLES
    )

    # The slot is held for the whole read, not just the parse: what needs
    # bounding is the number of uploads in flight, not the CPU alone.
    async with request.app.state.slots.acquire():
        started = time.perf_counter()
        analyzer = LogAnalyzer(
            max_samples=max_samples, max_line_chars=settings.max_line_bytes
        )
        outcome = await read_upload(request, analyzer, settings)
        elapsed = time.perf_counter() - started

        result = analyzer.result(
            filename=outcome.filename,
            size_bytes=outcome.size_bytes,
            duration_ms=round(elapsed * 1000),
        )

    await request.app.state.store.put(result)

    record_analysis(
        lines=result.lines_processed,
        unparseable=result.unparseable_lines,
        size_bytes=result.meta.bytes,
        seconds=elapsed,
    )
    note(
        request,
        analysis_id=result.id,
        bytes=result.meta.bytes,
        lines_processed=result.lines_processed,
        unparseable_lines=result.unparseable_lines,
        services=len(result.services),
    )

    response.headers["Location"] = f"{router.prefix}/{result.id}"
    return result


@router.get(
    "/{analysis_id}",
    response_model=AnalysisResult,
    responses=_GET_ERRORS,
    summary="Fetch a stored analysis",
)
async def get_analysis(analysis_id: str, request: Request) -> AnalysisResult:
    # Junk that cannot be an id is rejected before the store is asked, so a
    # scan for `/analyses/../../etc` costs nothing downstream.
    if not is_analysis_id(analysis_id):
        raise AnalysisNotFound(analysis_id=analysis_id)

    result = await request.app.state.store.get(analysis_id)
    if result is None:
        raise AnalysisNotFound(analysis_id=analysis_id)

    note(request, analysis_id=analysis_id)
    return result


__all__ = ["router"]
