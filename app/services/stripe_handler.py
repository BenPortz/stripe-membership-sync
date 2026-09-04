"""Stripe webhook event parsing, verification, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import stripe
import structlog

from app.utils.logging import mask_email

logger = structlog.get_logger()


class SubscriptionAction(str, Enum):
    """Actions derived from Stripe subscription events."""

    GRANT = "granted"
    CANCEL = "cancelled"
    UPDATE = "updated"
    PAYMENT_FAILED = "payment_failed"
    UNKNOWN = "unknown"


@dataclass
class ParsedEvent:
    """Normalized representation of a Stripe subscription event."""

    event_id: str
    event_type: str
    customer_id: str
    customer_email: str | None
    subscription_id: str | None
    price_id: str | None
    action: SubscriptionAction
    raw_event: stripe.Event

    @property
    def summary(self) -> str:
        return (
            f"{self.action.value} for {self.customer_email or self.customer_id} "
            f"(sub: {self.subscription_id}, price: {self.price_id})"
        )


def verify_webhook(payload: bytes, sig_header: str, webhook_secret: str) -> stripe.Event:
    """Verify the Stripe webhook signature and construct the event.

    Raises stripe.error.SignatureVerificationError on invalid signatures.
    """
    event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    logger.info("stripe.webhook.verified", event_id=event["id"], event_type=event["type"])
    return event


def parse_event(event: stripe.Event) -> ParsedEvent:
    """Extract subscription-relevant fields from a Stripe event.

    Handles:
      - checkout.session.completed
      - customer.subscription.updated
      - customer.subscription.deleted
      - invoice.payment_failed
      - customer.updated
    """
    event_type = event["type"]
    data_obj = event["data"]["object"]

    # Defaults
    customer_id = ""
    customer_email = None
    subscription_id = None
    price_id = None
    action = SubscriptionAction.UNKNOWN

    if event_type == "checkout.session.completed":
        customer_id = data_obj.get("customer", "")
        customer_email = data_obj.get("customer_email") or data_obj.get("customer_details", {}).get(
            "email"
        )
        subscription_id = data_obj.get("subscription")
        # Extract price from line items if available
        line_items = data_obj.get("line_items", {}).get("data", [])
        if line_items:
            price_id = line_items[0].get("price", {}).get("id")
        action = SubscriptionAction.GRANT

    elif event_type == "customer.subscription.updated":
        customer_id = data_obj.get("customer", "")
        subscription_id = data_obj.get("id")
        items = data_obj.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")

        # Check if this is a cancellation (cancel_at_period_end set to true)
        if data_obj.get("cancel_at_period_end"):
            action = SubscriptionAction.CANCEL
        else:
            action = SubscriptionAction.UPDATE

    elif event_type == "customer.subscription.deleted":
        customer_id = data_obj.get("customer", "")
        subscription_id = data_obj.get("id")
        items = data_obj.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")
        action = SubscriptionAction.CANCEL

    elif event_type == "invoice.payment_failed":
        customer_id = data_obj.get("customer", "")
        customer_email = data_obj.get("customer_email")
        subscription_id = data_obj.get("subscription")
        lines = data_obj.get("lines", {}).get("data", [])
        if lines:
            price_id = lines[0].get("price", {}).get("id")
        action = SubscriptionAction.PAYMENT_FAILED

    elif event_type == "customer.updated":
        customer_id = data_obj.get("id", "")
        customer_email = data_obj.get("email")
        action = SubscriptionAction.UPDATE

    parsed = ParsedEvent(
        event_id=event["id"],
        event_type=event_type,
        customer_id=customer_id,
        customer_email=customer_email,
        subscription_id=subscription_id,
        price_id=price_id,
        action=action,
        raw_event=event,
    )

    logger.info(
        "stripe.event.parsed",
        event_id=parsed.event_id,
        action=parsed.action.value,
        email=mask_email(parsed.customer_email),
        price_id=parsed.price_id,
    )

    return parsed


async def resolve_customer_email(customer_id: str, api_key: str | None = None) -> str | None:
    """Fetch the customer email from Stripe if not present in the event payload."""
    try:
        customer = stripe.Customer.retrieve(customer_id, api_key=api_key)
        return customer.get("email")
    except stripe.error.StripeError as e:
        logger.warning("stripe.customer.lookup_failed", customer_id=customer_id, error=str(e))
        return None
