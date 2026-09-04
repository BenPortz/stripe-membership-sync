"""Tests for Stripe event parsing logic."""

from __future__ import annotations

import pytest

from app.services.stripe_handler import parse_event, SubscriptionAction


def _make_stripe_event(event_type: str, data_object: dict, event_id: str = "evt_test") -> dict:
    """Helper to create a mock Stripe event dict."""

    class FakeEvent(dict):
        pass

    event = FakeEvent(
        {
            "id": event_id,
            "type": event_type,
            "data": {"object": data_object},
        }
    )
    return event


class TestCheckoutSessionCompleted:
    def test_extracts_email_and_subscription(self):
        event = _make_stripe_event(
            "checkout.session.completed",
            {
                "customer": "cus_abc",
                "customer_email": "buyer@example.com",
                "subscription": "sub_xyz",
                "line_items": {
                    "data": [{"price": {"id": "price_monthly_10"}}]
                },
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.GRANT
        assert parsed.customer_email == "buyer@example.com"
        assert parsed.customer_id == "cus_abc"
        assert parsed.subscription_id == "sub_xyz"
        assert parsed.price_id == "price_monthly_10"

    def test_falls_back_to_customer_details_email(self):
        event = _make_stripe_event(
            "checkout.session.completed",
            {
                "customer": "cus_abc",
                "customer_email": None,
                "customer_details": {"email": "fallback@example.com"},
                "subscription": "sub_xyz",
            },
        )

        parsed = parse_event(event)

        assert parsed.customer_email == "fallback@example.com"


class TestSubscriptionDeleted:
    def test_maps_to_cancel_action(self):
        event = _make_stripe_event(
            "customer.subscription.deleted",
            {
                "customer": "cus_abc",
                "id": "sub_xyz",
                "items": {"data": [{"price": {"id": "price_monthly_10"}}]},
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.CANCEL
        assert parsed.subscription_id == "sub_xyz"


class TestSubscriptionUpdated:
    def test_cancel_at_period_end_maps_to_cancel(self):
        event = _make_stripe_event(
            "customer.subscription.updated",
            {
                "customer": "cus_abc",
                "id": "sub_xyz",
                "cancel_at_period_end": True,
                "items": {"data": [{"price": {"id": "price_monthly_10"}}]},
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.CANCEL

    def test_plan_change_maps_to_update(self):
        event = _make_stripe_event(
            "customer.subscription.updated",
            {
                "customer": "cus_abc",
                "id": "sub_xyz",
                "cancel_at_period_end": False,
                "items": {"data": [{"price": {"id": "price_annual_100"}}]},
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.UPDATE
        assert parsed.price_id == "price_annual_100"


class TestInvoicePaymentFailed:
    def test_extracts_failure_details(self):
        event = _make_stripe_event(
            "invoice.payment_failed",
            {
                "customer": "cus_abc",
                "customer_email": "fail@example.com",
                "subscription": "sub_xyz",
                "lines": {"data": [{"price": {"id": "price_monthly_10"}}]},
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.PAYMENT_FAILED
        assert parsed.customer_email == "fail@example.com"


class TestCustomerUpdated:
    def test_extracts_email_change(self):
        event = _make_stripe_event(
            "customer.updated",
            {
                "id": "cus_abc",
                "email": "newemail@example.com",
            },
        )

        parsed = parse_event(event)

        assert parsed.action == SubscriptionAction.UPDATE
        assert parsed.customer_email == "newemail@example.com"
        assert parsed.customer_id == "cus_abc"
