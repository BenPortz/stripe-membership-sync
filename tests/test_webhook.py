"""Integration tests for the webhook endpoint."""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Set test env vars before importing app
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
os.environ.setdefault("WORDPRESS_URL", "https://test.example.com")
os.environ.setdefault("WORDPRESS_USERNAME", "testadmin")
os.environ.setdefault("WORDPRESS_APP_PASSWORD", "test-app-password")
os.environ.setdefault("API_KEY", "test-api-key-0123456789")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# Clear the lru_cache so settings pick up test env
from app.config import get_settings
get_settings.cache_clear()

from app.main import app
from app.models.events import init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db():
    """Initialize an in-memory DB before each test."""
    get_settings.cache_clear()
    await init_db("sqlite+aiosqlite:///:memory:")


class TestWebhookEndpoint:
    """Tests for POST /webhooks/stripe."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self):
        """Requests with invalid signatures should return 400."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhooks/stripe",
                content=b'{"id": "evt_test"}',
                headers={"Stripe-Signature": "invalid_sig"},
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_skips_unhandled_event_types(self):
        """Event types we don't handle should return 200 with 'skipped'."""
        fake_event = MagicMock()
        fake_event.__getitem__ = MagicMock(
            side_effect=lambda k: {
                "id": "evt_test",
                "type": "account.updated",
                "data": {"object": {}},
            }[k]
        )

        with patch(
            "app.main.verify_webhook",
            return_value=fake_event,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/webhooks/stripe",
                    content=b"{}",
                    headers={"Stripe-Signature": "valid"},
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"


class TestHealthEndpoint:
    """Tests for GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "total_events_processed" in data


class TestAdminEndpoints:
    """Tests for authenticated admin API."""

    @pytest.mark.asyncio
    async def test_events_requires_api_key(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/events")

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_events_with_valid_key(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/events",
                headers={"X-API-Key": "test-api-key-0123456789"},
            )

        assert resp.status_code == 200
        assert "events" in resp.json()
