"""Pydantic schemas for API request/response validation."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, EmailStr


class HealthResponse(BaseModel):
    status: str
    environment: str
    last_event_at: datetime | None = None
    total_events_processed: int = 0


class EventResponse(BaseModel):
    id: int
    stripe_event_id: str
    event_type: str
    customer_email: str | None
    status: str
    pmpro_action: str | None
    pmpro_level_id: int | None
    wp_user_created: bool
    error_message: str | None
    retry_count: int
    received_at: datetime
    processed_at: datetime | None

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int


class ManualSyncRequest(BaseModel):
    email: str
    stripe_customer_id: str | None = None
    pmpro_level_id: int | None = None


class ManualSyncResponse(BaseModel):
    success: bool
    message: str
    wp_user_id: int | None = None
    pmpro_level_id: int | None = None
