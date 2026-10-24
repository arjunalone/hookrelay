"""
Sets up the connection to Postgres.
Both api/ and worker/ import from this file instead of each
creating their own connection — one source of truth for the DB.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Reads DATABASE_URL from the environment (set in .env / docker-compose).
# Falls back to localhost so it also works when you run Python directly
# on your laptop against the Dockerized Postgres.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://hookrelay:hookrelay_dev_password@localhost:5432/hookrelay",
)

# The engine is the low-level thing that actually talks to Postgres.
engine = create_engine(DATABASE_URL, echo=False)

# Session is what you use in code to add/query/commit rows.
# Each request (or each worker loop iteration) should open its own session.
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

# Every model (table) will inherit from this Base, so SQLAlchemy
# knows about all of them when it creates tables.
Base = declarative_base()


def get_session():
    """Call this whenever you need to talk to the DB. Always close it."""
    return SessionLocal()