from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.models.analysis import ANALYZER_VERSION

router = APIRouter(prefix="/api/v1", tags=["health"])


class SlotStatus(BaseModel):
    in_use: int
    capacity: int


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"] = Field(
        description="`degraded` means the API is serving but the result store is "
        "unreachable, so new analyses cannot be stored or fetched by id."
    )
    store_reachable: bool
    slots: SlotStatus
    analyzer_version: str


@router.get("/health", response_model=HealthStatus, summary="Liveness and store check")
async def health(request: Request) -> HealthStatus:
    store_reachable = await request.app.state.store.ping()
    slots = request.app.state.slots
    return HealthStatus(
        status="ok" if store_reachable else "degraded",
        store_reachable=store_reachable,
        slots=SlotStatus(in_use=slots.in_use, capacity=slots.capacity),
        analyzer_version=ANALYZER_VERSION,
    )


__all__ = ["router"]
