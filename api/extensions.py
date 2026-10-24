#Creates shared objects like the db (SQLAlchemy) and migrate instances, kept separate from app.py to avoid circular imports later.


"""
Objects shared across the Flask app (currently just re-exports the
DB session helper from shared/db.py). Kept as its own file so routes
can import from here without risking circular imports with app.py.
"""

from shared.db import get_session, engine, Base