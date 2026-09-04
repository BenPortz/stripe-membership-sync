"""Sync engine: orchestrates the full webhook-to-membership flow.

Responsibilities:
  1. Check idempotency (skip already-processed events)
  2. Resolve customer email (from event or Stripe API)
  3. Find or create WordPress user
  4. Map Stripe price to PMPro level
  5. Apply the membership change
  6. Record the outcome in the event ledger
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_level_for_price, load_membership_map
from app.models.events import WebhookEvent
from app.services.stripe_handler import ParsedEvent, SubscriptionAction, resolve_customer_email
from app.services.wordpress_client import WordPressClient, WordPressAPIError
from app.utils.logging import mask_email

logger = structlog.get_logger()


class SyncEngine:
    """Coordinates Stripe event processing and WordPress membership updates."""

    def __init__(self, wp_client: WordPressClient):
        self.wp = wp_client
        self.config = load_membership_map()

    async def process_event(self, parsed: ParsedEvent, session: AsyncSession) -> WebhookEvent:
        """Process a parsed Stripe event end-to-end.

        Returns the WebhookEvent ledger record (committed).
        """
        # 1. Idempotency check
        existing = await self._find_existing_event(parsed.event_id, session)
        if existing and existing.status in ("completed", "skipped"):
            logger.info(
                "sync.event.duplicate",
                event_id=parsed.event_id,
                status=existing.status,
            )
            return existing

        # 2. Create or update ledger record
        event_record = existing or WebhookEvent(
            stripe_event_id=parsed.event_id,
            event_type=parsed.event_type,
            stripe_customer_id=parsed.customer_id,
            stripe_subscription_id=parsed.subscription_id,
            stripe_price_id=parsed.price_id,
            payload_summary=parsed.summary,
            status="processing",
        )

        if not existing:
            session.add(event_record)

        event_record.status = "processing"
        event_record.customer_email = parsed.customer_email
        await session.commit()

        try:
            # 3. Resolve email if missing
            email = parsed.customer_email
            if not email and parsed.customer_id:
                email = await resolve_customer_email(parsed.customer_id)
                event_record.customer_email = email

            if not email:
                event_record.status = "failed"
                event_record.error_message = "No customer email found in event or Stripe"
                await session.commit()
                logger.error("sync.no_email", event_id=parsed.event_id)
                return event_record

            # 4. Dispatch by action type
            if parsed.action == SubscriptionAction.GRANT:
                await self._handle_grant(email, parsed, event_record, session)

            elif parsed.action == SubscriptionAction.CANCEL:
                await self._handle_cancel(email, parsed, event_record, session)

            elif parsed.action == SubscriptionAction.UPDATE:
                await self._handle_update(email, parsed, event_record, session)

            elif parsed.action == SubscriptionAction.PAYMENT_FAILED:
                await self._handle_payment_failed(email, parsed, event_record, session)

            else:
                event_record.status = "skipped"
                event_record.error_message = f"Unhandled action: {parsed.action}"
                logger.info("sync.action.skipped", action=parsed.action.value)

        except WordPressAPIError as e:
            event_record.status = "failed"
            event_record.error_message = f"WordPress API error: {e} (HTTP {e.status_code})"
            event_record.retry_count += 1
            logger.error(
                "sync.wp_error",
                event_id=parsed.event_id,
                error=str(e),
                status_code=e.status_code,
            )

        except Exception as e:
            event_record.status = "failed"
            event_record.error_message = f"Unexpected error: {type(e).__name__}: {e}"
            event_record.retry_count += 1
            logger.exception("sync.unexpected_error", event_id=parsed.event_id)

        event_record.processed_at = datetime.now(timezone.utc)
        await session.commit()
        return event_record

    # ---- Action handlers ----

    async def _handle_grant(
        self,
        email: str,
        parsed: ParsedEvent,
        record: WebhookEvent,
        session: AsyncSession,
    ) -> None:
        """New subscription. Create the user if needed, then assign membership."""
        # Find or create WordPress user
        wp_user = await self.wp.get_or_create_user(email)
        record.wp_user_id = wp_user.id
        record.wp_user_created = wp_user.created

        # Map price to level
        level = get_level_for_price(parsed.price_id, self.config)
        record.pmpro_level_id = level["pmpro_level_id"]

        # Assign membership
        await self.wp.assign_membership_level(wp_user.id, level["pmpro_level_id"])
        record.pmpro_action = SubscriptionAction.GRANT.value
        record.status = "completed"

        logger.info(
            "sync.membership.granted",
            email=mask_email(email),
            wp_user_id=wp_user.id,
            level_id=level["pmpro_level_id"],
            level_name=level["level_name"],
            user_created=wp_user.created,
        )

    async def _handle_cancel(
        self,
        email: str,
        parsed: ParsedEvent,
        record: WebhookEvent,
        session: AsyncSession,
    ) -> None:
        """Subscription cancelled. Remove the membership."""
        wp_user = await self.wp.find_user_by_email(email)

        if not wp_user:
            record.status = "skipped"
            record.error_message = f"No WordPress user found for {email}, nothing to cancel"
            logger.warning("sync.cancel.no_user", email=mask_email(email))
            return

        record.wp_user_id = wp_user.id
        await self.wp.cancel_membership(wp_user.id)
        record.pmpro_action = SubscriptionAction.CANCEL.value
        record.status = "completed"

        logger.info("sync.membership.cancelled", email=mask_email(email), wp_user_id=wp_user.id)

    async def _handle_update(
        self,
        email: str,
        parsed: ParsedEvent,
        record: WebhookEvent,
        session: AsyncSession,
    ) -> None:
        """Subscription updated (plan change). Update the membership level."""
        wp_user = await self.wp.find_user_by_email(email)

        if not wp_user:
            # User doesn't exist yet, so treat this as a grant
            logger.info("sync.update.no_user_treating_as_grant", email=mask_email(email))
            return await self._handle_grant(email, parsed, record, session)

        record.wp_user_id = wp_user.id

        if parsed.price_id:
            level = get_level_for_price(parsed.price_id, self.config)
            record.pmpro_level_id = level["pmpro_level_id"]
            await self.wp.assign_membership_level(wp_user.id, level["pmpro_level_id"])
            record.pmpro_action = SubscriptionAction.UPDATE.value
            record.status = "completed"
            logger.info(
                "sync.membership.updated",
                email=mask_email(email),
                level_id=level["pmpro_level_id"],
            )
        else:
            record.status = "skipped"
            record.error_message = "No price_id in update event"

    async def _handle_payment_failed(
        self,
        email: str,
        parsed: ParsedEvent,
        record: WebhookEvent,
        session: AsyncSession,
    ) -> None:
        """Payment failed. Log the failure for alerting.

        We don't immediately cancel membership; Stripe will send a
        customer.subscription.deleted event if the subscription is
        ultimately cancelled after retry attempts.
        """
        wp_user = await self.wp.find_user_by_email(email)
        if wp_user:
            record.wp_user_id = wp_user.id

        record.pmpro_action = SubscriptionAction.PAYMENT_FAILED.value
        record.status = "completed"

        logger.warning(
            "sync.payment.failed",
            email=mask_email(email),
            subscription_id=parsed.subscription_id,
        )

    # ---- Helpers ----

    async def _find_existing_event(
        self, stripe_event_id: str, session: AsyncSession
    ) -> WebhookEvent | None:
        result = await session.execute(
            select(WebhookEvent).where(WebhookEvent.stripe_event_id == stripe_event_id)
        )
        return result.scalar_one_or_none()
