"""Shared test fixtures."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.models.events import Base, WebhookEvent
from app.services.wordpress_client import WordPressClient, WPUser
from app.services.sync_engine import SyncEngine
from app.services.stripe_handler import ParsedEvent, SubscriptionAction


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite session for tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
def mock_wp_client() -> WordPressClient:
    """Mocked WordPress client that doesn't make real HTTP calls."""
    client = MagicMock(spec=WordPressClient)

    # Default: user exists
    client.find_user_by_email = AsyncMock(
        return_value=WPUser(id=42, email="test@example.com", username="testuser")
    )
    client.create_user = AsyncMock(
        return_value=WPUser(id=42, email="test@example.com", username="testuser", created=True)
    )
    client.get_or_create_user = AsyncMock(
        return_value=WPUser(id=42, email="test@example.com", username="testuser")
    )
    client.assign_membership_level = AsyncMock(return_value={"status": "ok"})
    client.cancel_membership = AsyncMock(return_value={"status": "ok"})
    client.get_membership_level = AsyncMock(return_value={"id": 1, "name": "Premium"})

    return client


@pytest.fixture
def sync_engine(mock_wp_client) -> SyncEngine:
    """SyncEngine with a mocked WordPress client."""
    engine = SyncEngine(mock_wp_client)
    engine.config = {
        "membership_map": [
            {
                "stripe_price_id": "price_monthly_10",
                "pmpro_level_id": 1,
                "level_name": "Basic",
                "duration": "month",
            },
            {
                "stripe_price_id": "price_annual_100",
                "pmpro_level_id": 2,
                "level_name": "Pro",
                "duration": "year",
            },
        ],
        "default_level_id": 1,
        "auto_create_users": True,
    }
    return engine


@pytest.fixture
def make_parsed_event():
    """Factory for creating test ParsedEvent objects."""

    def _make(
        event_id: str = "evt_test_123",
        event_type: str = "checkout.session.completed",
        customer_id: str = "cus_test_456",
        customer_email: str = "subscriber@example.com",
        subscription_id: str = "sub_test_789",
        price_id: str = "price_monthly_10",
        action: SubscriptionAction = SubscriptionAction.GRANT,
    ) -> ParsedEvent:
        raw_event = MagicMock()
        raw_event.__getitem__ = MagicMock(
            side_effect=lambda k: {
                "id": event_id,
                "type": event_type,
                "data": {"object": {}},
            }.get(k)
        )

        return ParsedEvent(
            event_id=event_id,
            event_type=event_type,
            customer_id=customer_id,
            customer_email=customer_email,
            subscription_id=subscription_id,
            price_id=price_id,
            action=action,
            raw_event=raw_event,
        )

    return _make
