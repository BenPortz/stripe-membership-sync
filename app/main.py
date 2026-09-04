"""FastAPI application: webhook receiver, health check, and admin API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from secrets import compare_digest

import stripe
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, Settings
from app.models.events import WebhookEvent, get_session_factory, init_db
from app.models.schemas import (
    EventListResponse,
    EventResponse,
    HealthResponse,
    ManualSyncRequest,
    ManualSyncResponse,
)
from app.services.stripe_handler import ParsedEvent, SubscriptionAction, parse_event, verify_webhook
from app.services.sync_engine import SyncEngine
from app.services.wordpress_client import WordPressClient
from app.utils.logging import mask_email, setup_logging

logger = structlog.get_logger()


# ---- Lifespan ----


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and logging on startup."""
    settings = get_settings()
    setup_logging(settings.log_level)
    await init_db(settings.database_url)
    logger.info(
        "app.started",
        environment=settings.environment,
        wordpress_url=settings.wordpress_url,
    )
    yield
    logger.info("app.shutdown")


app = FastAPI(
    title="Stripe to WordPress Membership Sync",
    description="Webhook service that syncs Stripe subscriptions to PMPro membership levels.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---- Dependencies ----

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_wp_client(settings: Settings = Depends(get_settings)) -> WordPressClient:
    return WordPressClient(
        base_url=settings.wordpress_url,
        username=settings.wordpress_username,
        app_password=settings.wordpress_app_password,
    )


def get_sync_engine(wp_client: WordPressClient = Depends(get_wp_client)) -> SyncEngine:
    return SyncEngine(wp_client)


async def get_db_session():
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def verify_api_key(
    api_key: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    if not api_key or not compare_digest(api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


# ---- Webhook endpoint ----

HANDLED_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
    "customer.updated",
}


@app.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
    settings: Settings = Depends(get_settings),
    sync_engine: SyncEngine = Depends(get_sync_engine),
    session: AsyncSession = Depends(get_db_session),
):
    """Receive and process Stripe webhook events.

    1. Verify the webhook signature
    2. Parse the event into a normalized form
    3. Process it through the sync engine (idempotent)
    4. Return 200 to acknowledge receipt
    """
    # Read raw body for signature verification
    payload = await request.body()

    # Verify signature
    try:
        event = verify_webhook(payload, stripe_signature, settings.stripe_webhook_secret)
    except stripe.error.SignatureVerificationError:
        logger.warning("webhook.signature_invalid")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Skip events we don't handle
    if event["type"] not in HANDLED_EVENTS:
        logger.info("webhook.event_skipped", event_type=event["type"])
        return {"status": "skipped", "reason": f"Event type {event['type']} not handled"}

    # Parse and process
    parsed = parse_event(event)
    event_record = await sync_engine.process_event(parsed, session)

    return {
        "status": event_record.status,
        "event_id": parsed.event_id,
        "action": event_record.pmpro_action,
    }


# ---- Health check ----


@app.get("/health", response_model=HealthResponse)
async def health_check(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
):
    """Health check with last event timestamp and processed count."""
    # Get the most recent event
    result = await session.execute(
        select(WebhookEvent.received_at)
        .order_by(WebhookEvent.received_at.desc())
        .limit(1)
    )
    last_event = result.scalar_one_or_none()

    # Count total processed
    count_result = await session.execute(
        select(func.count(WebhookEvent.id)).where(WebhookEvent.status == "completed")
    )
    total = count_result.scalar_one()

    return HealthResponse(
        status="healthy",
        environment=settings.environment,
        last_event_at=last_event,
        total_events_processed=total,
    )


# ---- Admin API (requires API key) ----


@app.get("/events", response_model=EventListResponse, dependencies=[Depends(verify_api_key)])
async def list_events(
    limit: int = 50,
    status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """List recent webhook events (most recent first)."""
    query = select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(limit)

    if status:
        query = query.where(WebhookEvent.status == status)

    result = await session.execute(query)
    events = result.scalars().all()

    count_result = await session.execute(select(func.count(WebhookEvent.id)))
    total = count_result.scalar_one()

    return EventListResponse(
        events=[EventResponse.model_validate(e) for e in events],
        total=total,
    )


@app.get("/events/{event_id}", response_model=EventResponse, dependencies=[Depends(verify_api_key)])
async def get_event(
    event_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single event by ID."""
    result = await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
    event = result.scalar_one_or_none()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return EventResponse.model_validate(event)


@app.post("/sync/manual", response_model=ManualSyncResponse, dependencies=[Depends(verify_api_key)])
async def manual_sync(
    req: ManualSyncRequest,
    wp_client: WordPressClient = Depends(get_wp_client),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
):
    """Manually grant membership to an email address.

    Useful for one-off additions or when a webhook was missed.
    """
    try:
        wp_user = await wp_client.get_or_create_user(req.email)
        level_id = req.pmpro_level_id or settings.pmpro_default_level_id
        await wp_client.assign_membership_level(wp_user.id, level_id)

        # Record it
        record = WebhookEvent(
            stripe_event_id=f"manual_{datetime.now(timezone.utc).isoformat()}",
            event_type="manual_sync",
            customer_email=req.email,
            stripe_customer_id=req.stripe_customer_id,
            status="completed",
            wp_user_id=wp_user.id,
            wp_user_created=wp_user.created,
            pmpro_level_id=level_id,
            pmpro_action="granted",
            processed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        await session.commit()

        return ManualSyncResponse(
            success=True,
            message=f"Membership level {level_id} granted to {req.email}",
            wp_user_id=wp_user.id,
            pmpro_level_id=level_id,
        )

    except Exception:
        # Traceback to the logs, generic message to the caller.
        logger.exception("manual_sync.failed", email=mask_email(req.email))
        return ManualSyncResponse(
            success=False,
            message="Sync failed. Check the service logs for details.",
        )
