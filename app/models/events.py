"""Event ledger: tracks every webhook event and the actions taken."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, Text, Boolean, Index
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker


class Base(DeclarativeBase):
    pass


class WebhookEvent(Base):
    """Immutable record of every Stripe webhook event received."""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stripe_event_id = Column(String(255), unique=True, nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    customer_email = Column(String(255), nullable=True)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    stripe_price_id = Column(String(255), nullable=True)
    payload_summary = Column(Text, nullable=True)

    # Processing state
    status = Column(
        String(20),
        nullable=False,
        default="received",
        comment="received | processing | completed | failed | skipped",
    )
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # WordPress side
    wp_user_id = Column(Integer, nullable=True)
    wp_user_created = Column(Boolean, default=False)
    pmpro_level_id = Column(Integer, nullable=True)
    pmpro_action = Column(
        String(50),
        nullable=True,
        comment="granted | cancelled | updated | payment_failed",
    )

    # Timestamps
    received_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_events_status", "status"),
        Index("ix_events_received", "received_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookEvent(id={self.id}, type={self.event_type}, "
            f"email={self.customer_email}, status={self.status})>"
        )


# --- Database helpers ---

_engine = None
_session_factory = None


async def init_db(database_url: str) -> None:
    """Create the engine, session factory, and tables."""
    global _engine, _session_factory

    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory
