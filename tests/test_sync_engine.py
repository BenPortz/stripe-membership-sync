"""Tests for the sync engine, the core orchestration layer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.stripe_handler import SubscriptionAction
from app.services.wordpress_client import WPUser, WordPressAPIError


# ---- Grant (new subscription) ----


@pytest.mark.asyncio
async def test_grant_creates_user_and_assigns_level(sync_engine, make_parsed_event, db_session):
    """A new subscription should create/find a WP user and assign the correct level."""
    event = make_parsed_event(
        price_id="price_monthly_10",
        action=SubscriptionAction.GRANT,
    )

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "completed"
    assert record.pmpro_action == "granted"
    assert record.pmpro_level_id == 1
    sync_engine.wp.get_or_create_user.assert_awaited_once_with("subscriber@example.com")
    sync_engine.wp.assign_membership_level.assert_awaited_once_with(42, 1)


@pytest.mark.asyncio
async def test_grant_annual_maps_to_level_2(sync_engine, make_parsed_event, db_session):
    """Annual price should map to level 2."""
    event = make_parsed_event(
        price_id="price_annual_100",
        action=SubscriptionAction.GRANT,
    )

    record = await sync_engine.process_event(event, db_session)

    assert record.pmpro_level_id == 2
    assert record.status == "completed"


@pytest.mark.asyncio
async def test_grant_unknown_price_uses_default(sync_engine, make_parsed_event, db_session):
    """An unmapped price_id should fall back to the default level."""
    event = make_parsed_event(
        price_id="price_unknown_xyz",
        action=SubscriptionAction.GRANT,
    )

    record = await sync_engine.process_event(event, db_session)

    assert record.pmpro_level_id == 1  # default
    assert record.status == "completed"


@pytest.mark.asyncio
async def test_grant_records_new_user_creation(sync_engine, make_parsed_event, db_session):
    """When a new WP user is created, the record should reflect that."""
    sync_engine.wp.get_or_create_user = AsyncMock(
        return_value=WPUser(id=99, email="new@example.com", username="new", created=True)
    )

    event = make_parsed_event(customer_email="new@example.com")

    record = await sync_engine.process_event(event, db_session)

    assert record.wp_user_created is True
    assert record.wp_user_id == 99


# ---- Cancellation ----


@pytest.mark.asyncio
async def test_cancel_removes_membership(sync_engine, make_parsed_event, db_session):
    """Cancellation should call cancel_membership on the WP user."""
    event = make_parsed_event(action=SubscriptionAction.CANCEL)

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "completed"
    assert record.pmpro_action == "cancelled"
    sync_engine.wp.cancel_membership.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_cancel_no_wp_user_skips(sync_engine, make_parsed_event, db_session):
    """If the user doesn't exist in WordPress, cancellation should be skipped."""
    sync_engine.wp.find_user_by_email = AsyncMock(return_value=None)

    event = make_parsed_event(action=SubscriptionAction.CANCEL)

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "skipped"
    sync_engine.wp.cancel_membership.assert_not_awaited()


# ---- Idempotency ----


@pytest.mark.asyncio
async def test_duplicate_event_is_skipped(sync_engine, make_parsed_event, db_session):
    """Processing the same event_id twice should skip the second time."""
    event = make_parsed_event(event_id="evt_duplicate_001")

    # Process once
    first = await sync_engine.process_event(event, db_session)
    assert first.status == "completed"

    # Process again. The second call should be a no-op.
    second = await sync_engine.process_event(event, db_session)
    assert second.status == "completed"

    # WP client should only have been called once
    assert sync_engine.wp.get_or_create_user.await_count == 1


# ---- Error handling ----


@pytest.mark.asyncio
async def test_wp_api_error_marks_failed(sync_engine, make_parsed_event, db_session):
    """A WordPress API error should mark the event as failed."""
    sync_engine.wp.get_or_create_user = AsyncMock(
        side_effect=WordPressAPIError("Connection refused", status_code=503)
    )

    event = make_parsed_event()

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "failed"
    assert "WordPress API error" in record.error_message
    assert record.retry_count == 1


@pytest.mark.asyncio
async def test_no_email_marks_failed(sync_engine, make_parsed_event, db_session):
    """An event with no customer email should fail gracefully."""
    event = make_parsed_event(customer_email=None, customer_id="")

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "failed"
    assert "No customer email" in record.error_message


# ---- Payment failed ----


@pytest.mark.asyncio
async def test_payment_failed_logs_without_cancelling(sync_engine, make_parsed_event, db_session):
    """Payment failure should be recorded but NOT cancel the membership."""
    event = make_parsed_event(action=SubscriptionAction.PAYMENT_FAILED)

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "completed"
    assert record.pmpro_action == "payment_failed"
    sync_engine.wp.cancel_membership.assert_not_awaited()


# ---- Update (plan change) ----


@pytest.mark.asyncio
async def test_update_changes_level(sync_engine, make_parsed_event, db_session):
    """A plan change should update the membership level."""
    event = make_parsed_event(
        price_id="price_annual_100",
        action=SubscriptionAction.UPDATE,
    )

    record = await sync_engine.process_event(event, db_session)

    assert record.status == "completed"
    assert record.pmpro_level_id == 2
    assert record.pmpro_action == "updated"
