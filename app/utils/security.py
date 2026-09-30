from __future__ import annotations

import hmac

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings
from app.utils.errors import Unauthorized

API_KEY_HEADER = "X-API-Key"

#: ``auto_error=False`` so a missing key reaches our own check and comes back
#: in the shared error shape rather than as FastAPI's ``{"detail": ...}``.
_header_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


async def require_api_key(
    request: Request,
    presented: str | None = Depends(_header_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """Allow the request through, or raise :class:`Unauthorized`."""
    if not settings.auth_required:
        return

    if presented and any(
        # Constant-time compare, so a wrong key cannot be narrowed down by
        # timing how long the rejection took.
        hmac.compare_digest(presented, known)
        for known in settings.API_KEYS
    ):
        return

    raise Unauthorized(
        "Missing or invalid API key." if presented else "This server requires an API key.",
        header=API_KEY_HEADER,
    )


__all__ = ["API_KEY_HEADER", "require_api_key"]
