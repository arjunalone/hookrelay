"""
Shared pytest fixtures.

These tests run against a SEPARATE database (hookrelay_test) in the same
Postgres container, never the real hookrelay database. Tables are truncated
between tests, so a failing test can never leave real data behind.

Note on migrations: fixtures use Base.metadata.create_all rather than running
Alembic. That keeps the suite fast and tests the models directly. Migration
correctness is covered separately by `alembic check`, which compares the
models against the live schema and fails on drift.

The trade-off: create_all will NOT alter an existing table. After changing a
model, drop the test database so the schema is rebuilt from the new models:

    docker compose exec -T db psql -U hookrelay -d hookrelay \
        -c "DROP DATABASE hookrelay_test;"

Symptoms of forgetting: tests fail with "can't compare offset-naive and
offset-aware datetimes" after a naive DateTime -> TIMESTAMPTZ change.
"""

import os

TEST_DB_NAME = "hookrelay_test"

# Connection to the default `postgres` database, used only to CREATE DATABASE.
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql://hookrelay:hookrelay_dev_password@localhost:5432/postgres",
)
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql://hookrelay:hookrelay_dev_password@localhost:5432/{TEST_DB_NAME}",
)

# shared/db.py builds its engine at import time, so DATABASE_URL has to point
# at the test database BEFORE anything imports it. Import order matters here.
os.environ["DATABASE_URL"] = TEST_URL

import pytest
from sqlalchemy import create_engine, text

from shared.db import Base, get_session
from shared.models import Endpoint, Event, Delivery  # noqa: F401  (registers tables on Base)


def _ensure_database_exists():
    """CREATE DATABASE hookrelay_test if it isn't there yet."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine():
    _ensure_database_exists()
    eng = create_engine(TEST_URL)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Truncate everything, then hand back a real session.

    Deliberately the same get_session() the app and worker use, so the tests
    exercise production code paths rather than a parallel test-only setup.
    """
    with engine.begin() as conn:
        # A leaked row lock from a buggy test would otherwise make TRUNCATE
        # block forever with no clue why. Fail fast and loudly instead.
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(text("TRUNCATE deliveries, events, endpoints CASCADE"))
    session = get_session()
    yield session
    session.close()


@pytest.fixture
def client(db):
    """Flask test client against the test database."""
    from api.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def make_endpoint(db):
    """Factory for a registered endpoint row."""
    def _make(url="https://example.com/hook", secret="s3cret", event_type="order.created"):
        endpoint = Endpoint(url=url, secret=secret, event_type=event_type)
        db.add(endpoint)
        db.commit()
        return endpoint
    return _make


@pytest.fixture
def make_event(db):
    """Factory for an event plus one delivery per matching endpoint (fan-out)."""
    def _make(event_type="order.created", payload=None, idempotency_key=None,
              endpoints=None, status="pending"):
        import uuid
        event = Event(
            event_type=event_type,
            payload=payload if payload is not None else {"order_id": 1},
            idempotency_key=idempotency_key or f"key-{uuid.uuid4()}",
        )
        db.add(event)
        db.flush()
        for endpoint in (endpoints or []):
            db.add(Delivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                status=status,
            ))
        db.commit()
        return event
    return _make
