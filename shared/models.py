"""
Table definitions. Both api/ and worker/ import from here so there's
only ever one definition of what a 'Delivery', 'Endpoint', or 'Event'
row looks like.

Milestone 1: Endpoint only.
Milestone 2: added Event and Delivery — an Event can fan out to many
Deliveries, one per subscribed Endpoint (this is US-1.2 from our epics).
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from shared.db import Base


class Endpoint(Base):
    __tablename__ = "endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=False)  # used later for HMAC signing
    event_type = Column(String, nullable=False)  # e.g. "order.created" — which events this endpoint wants
    # TIMESTAMPTZ, not a naive DateTime. Every timestamp here is UTC, but a
    # column without a timezone doesn't say so: it returned API timestamps
    # with no +00:00 on them, and made the worker's aware/naive comparison
    # depend on Postgres silently coercing the value.
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Without this, registering the same URL twice creates two rows, and
    # every event then fans out to both — the subscriber gets the same
    # event twice. POST /endpoints turns the resulting IntegrityError
    # into a 409 so the caller finds out at registration time instead of
    # discovering the duplicate delivery later.
    __table_args__ = (
        UniqueConstraint("url", "event_type", name="uq_endpoint_url_event_type"),
    )

    def __repr__(self):
        return f"<Endpoint {self.id} {self.url} subscribed_to={self.event_type}>"


class Event(Base):
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(String, nullable=False)       # e.g. "order.created"
    payload = Column(JSONB, nullable=False)            # the actual data, e.g. {"order_id": 4521}
    idempotency_key = Column(String, nullable=False, unique=True)  # US-1.3: blocks duplicate submissions
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # One Event can have many Delivery rows (one per subscribed endpoint)
    deliveries = relationship("Delivery", back_populates="event")

    def __repr__(self):
        return f"<Event {self.id} {self.event_type}>"


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=False)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("endpoints.id"), nullable=False)

    status = Column(String, nullable=False, default="pending")
    # pending -> in_flight -> succeeded
    #                      -> retrying -> in_flight ... -> dead

    attempt_count = Column(Integer, nullable=False, default=0)
    # Scheduling columns are TIMESTAMPTZ so the worker can compare them
    # against datetime.now(timezone.utc) without relying on coercion.
    next_attempt_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    locked_until = Column(DateTime(timezone=True), nullable=True)   # the "lease" we discussed
    locked_by = Column(String, nullable=True)         # which worker holds the lease

    last_status_code = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    event = relationship("Event", back_populates="deliveries")

    __table_args__ = (
        # US-1.2 constraint: one event should only ever create ONE delivery
        # per endpoint — prevents accidental duplicate fan-out.
        UniqueConstraint("event_id", "endpoint_id", name="uq_event_endpoint"),
    )

    def __repr__(self):
        return f"<Delivery {self.id} status={self.status} attempt={self.attempt_count}>"