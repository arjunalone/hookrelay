# PART 4 — File-by-File Code Walkthrough

This is a guided tour through every important file in the project, explaining what each does, why it exists, and what to say about it in an interview.

---

## `shared/db.py` — Database Foundation

```python
# shared/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def get_session():
    return SessionLocal()
```

**What it does:** Creates the SQLAlchemy engine (connection pool to Postgres) and provides a `get_session()` factory.

**Why `pool_pre_ping=True`?** Before handing a connection from the pool to the application, SQLAlchemy sends `SELECT 1` to verify it's alive. Without this, a connection that was closed by Postgres (e.g., after a restart) would raise a cryptic error instead of transparently reconnecting.

**Why shared?** Both `api/` and `worker/` import `get_session()` from here. If each had its own, they could end up with different connection settings or different `Base` objects — and Alembic migrations would become ambiguous.

---

## `shared/models.py` — The Three Tables

### Endpoint
```python
class Endpoint(Base):
    __tablename__ = "endpoints"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=False)      # used for HMAC signing
    event_type = Column(String, nullable=False)  # "order.created"
    created_at = Column(DateTime(timezone=True), ...)

    __table_args__ = (
        UniqueConstraint("url", "event_type"),  # prevents double-subscription
    )
```

**Why UUID instead of integer ID?**
Integer IDs are sequential — an attacker can enumerate them: `/endpoints/1`, `/endpoints/2`, etc. UUIDs are random 128-bit values. Guessing one is computationally infeasible.

### Event
```python
class Event(Base):
    __tablename__ = "events"
    id = Column(UUID(...), primary_key=True, default=uuid.uuid4)
    event_type = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)        # Postgres JSONB, not JSON string
    idempotency_key = Column(String, unique=True)  # UNIQUE prevents duplicate events
    created_at = Column(DateTime(timezone=True), ...)
```

**Why JSONB instead of JSON?**
Postgres `JSONB` stores data in a binary format (decomposed). `JSON` stores the raw text. `JSONB` supports indexing on nested fields and is faster to query. For an event platform where payload querying matters, `JSONB` is the right choice.

### Delivery
```python
class Delivery(Base):
    __tablename__ = "deliveries"
    id = Column(UUID(...), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID, ForeignKey("events.id"))
    endpoint_id = Column(UUID, ForeignKey("endpoints.id"))

    status = Column(String, default="pending")      # pending/in_flight/succeeded/retrying/dead
    attempt_count = Column(Integer, default=0)
    next_attempt_at = Column(DateTime(timezone=True), ...)  # when to try next
    locked_until = Column(DateTime(timezone=True))           # lease expiry
    locked_by = Column(String)                               # which worker holds it

    last_status_code = Column(Integer)   # e.g. 500
    last_error = Column(String)          # e.g. "Connection refused"

    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id"),  # one delivery per (event, endpoint)
    )
```

**Why `DateTime(timezone=True)` (TIMESTAMPTZ)?**
A naive `DateTime` column holds UTC but does not declare it. The API returns timestamps without `+00:00`. The worker's comparison of aware vs naive datetimes depends on Postgres silently coercing — which it does based on the session's timezone setting, not always UTC. `TIMESTAMPTZ` stores the timezone, returning `2026-09-27T20:56:10+00:00`.

---

## `api/app.py` — Flask Application Factory

```python
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.register_blueprint(endpoints_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(deliveries_bp)
    app.register_blueprint(dashboard_bp)

    @app.route("/health")
    def health():
        session.execute(text("SELECT 1"))   # proves DB is reachable, not just Flask
        return jsonify({"status": "ok", "database": "connected"})
```

**Why the factory pattern (`create_app()`)?**
Testing. You can call `create_app()` with a test config and get an isolated Flask app with a test database — without touching the production one. It also prevents circular imports.

**Why does `/health` query the database?**
A health check that only returns `{"status": "ok"}` from Flask is useless — it tells you Flask is up but not whether the application can actually do its job (which requires Postgres). A real health check proves the entire critical path is working.

---

## `api/routes/events.py` — The Core Route

```python
@events_bp.route("/events", methods=["POST"])
def create_event():
    # 1. Validate input
    is_valid, error = validate_event_payload(data)

    # 2. Create Event row (not committed yet)
    new_event = Event(event_type=..., payload=..., idempotency_key=...)
    session.add(new_event)
    session.flush()   # get UUID without committing

    # 3. Fan out — one Delivery per matching subscriber
    for endpoint in matching_endpoints:
        session.add(Delivery(event_id=new_event.id, endpoint_id=endpoint.id))

    # 4. Commit atomically — event + deliveries, or nothing
    session.commit()

    return jsonify({...}), 202  # 202 = Accepted (will be processed asynchronously)
```

**Why `session.flush()` instead of `session.commit()`?**
`flush()` sends the SQL to Postgres and gets the auto-generated UUID back, but the transaction is still open. If creating a delivery row fails, everything rolls back. `commit()` would make the event permanent even if fan-out fails — leaving orphaned events with no deliveries.

**Why `202` and not `200`?**
- `200 OK` means "request processed, here is the result"
- `202 Accepted` means "request received and valid, processing will happen asynchronously"

`202` is the semantically correct response for an async operation.

---

## `api/routes/endpoints.py` — Subscriber Management

Key behaviors to know:

**`PATCH /endpoints/<id>`** — partial update:
- Can update `url`, `secret`, or `event_type` independently
- Rejects unknown fields (so a typo like `"secert"` gets a clear error, not silent failure)
- Returns `409` if the new `(url, event_type)` combo already exists
- Uses `validate_endpoint_patch()` which reuses the same URL validation as POST

**`DELETE /endpoints/<id>`** — guarded delete:
- Returns `409` while any delivery for this endpoint is `in_flight`
- Reason: silently deleting an endpoint that a worker currently owns would orphan the delivery row
- Deletes child delivery history in the same transaction
- Returns `404` for a missing ID (not silent `204`) — because silent success on a bad ID makes typos look like success

---

## `api/routes/deliveries.py` — Dashboard API

**`GET /deliveries?status=dead`** — filterable, paginated list:
```python
VALID_STATUSES = ("pending", "in_flight", "succeeded", "retrying", "dead")
# Unknown status filter → 400, not empty list
# Empty list could mean "nothing failed" — that's wrong and dangerous
```

**`GET /deliveries/stats`** — counts by status:
```python
rows = session.query(Delivery.status, func.count(Delivery.id)).group_by(Delivery.status).all()
# Returns: {"pending": 3, "succeeded": 47, "dead": 2, "total": 52}
```

**`POST /deliveries/<id>/replay`** — brings a dead delivery back:
```python
delivery.status = "pending"
delivery.attempt_count = 0
delivery.next_attempt_at = now
# last_error preserved — reason for original failure stays visible
```

---

## `worker/worker.py` — The Heart of the Project

### `claim_one_delivery(session)`
```python
delivery = (
    session.query(Delivery)
    .filter(Delivery.status.in_(["pending", "retrying"]))
    .filter(Delivery.next_attempt_at <= now)
    .with_for_update(skip_locked=True)   # safe for concurrent workers
    .first()
)
delivery.attempt_count += 1   # counted HERE, before HTTP
delivery.status = "in_flight"
delivery.locked_by = WORKER_ID
delivery.locked_until = now + 30 seconds
session.commit()
```

### `deliver(session, delivery)`
```python
body = json.dumps({
    "event_id": str(event.id),       # inside body → covered by signature
    "delivery_id": str(delivery.id), # inside body → covered by signature
    "event_type": event.event_type,
    "payload": event.payload,
}).encode()

signature_headers = sign_payload(endpoint.secret, body)

heartbeat = _LeaseHeartbeat(delivery.id)
heartbeat.start()
try:
    response = requests.post(endpoint.url, data=body, headers={...}, timeout=10)
finally:
    heartbeat.stop()

if 200 <= response.status_code < 300:
    delivery.status = "succeeded"
else:
    if should_give_up(delivery.attempt_count):
        delivery.status = "dead"
    else:
        delivery.status = "retrying"
        delivery.next_attempt_at = now + get_delay_seconds(delivery.attempt_count)
```

### `sweep_expired_leases(session)`
```python
stuck = (
    session.query(Delivery)
    .filter(Delivery.status == "in_flight")
    .filter(Delivery.locked_until < now)
    .with_for_update(skip_locked=True)  # multiple sweepers → each picks different rows
    .limit(50)   # bounded — won't lock the whole table
    .all()
)
for d in stuck:
    if should_give_up(d.attempt_count):
        d.status = "dead"   # crash-killed delivery dead-letters correctly
    else:
        d.status = "retrying"
        d.next_attempt_at = now + get_delay_seconds(d.attempt_count)
```

---

## `worker/backoff.py` — Retry Schedule

```python
BASE_SCHEDULE = [10, 30, 60, 120, 300]  # seconds
MAX_ATTEMPTS = 5

def get_delay_seconds(attempt_count):
    index = min(attempt_count - 1, len(BASE_SCHEDULE) - 1)
    base = BASE_SCHEDULE[index]
    return base + base * random.uniform(0, 0.2)   # one-directional jitter

def should_give_up(attempt_count):
    return attempt_count >= MAX_ATTEMPTS
```

**Why is `backoff.py` a separate file?**
It's a pure function with no database access. This makes it fully unit-testable without any database setup. `test_backoff.py` can run in milliseconds and verify every edge case (first attempt, last attempt, overflow).

---

## `worker/signing.py` — HMAC Authentication

```python
def sign_payload(secret, payload_bytes):
    timestamp = str(int(time.time()))
    signed_content = f"{timestamp}.".encode() + payload_bytes

    signature = hmac.new(
        key=secret.encode(),
        msg=signed_content,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return {
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": signature,
    }
```

**Interview note:** This is identical in pattern to Stripe's webhook signing. If an interviewer asks "have you worked with Stripe webhooks?", you can say "I implemented the same signing scheme in HookRelay."

---

## `migrations/` — Alembic Schema Versioning

**Key files:**
- `alembic.ini` — config, with `sqlalchemy.url` **left blank** (injected at runtime from `DATABASE_URL`)
- `migrations/env.py` — points Alembic at the same `Base` as `shared/models.py`
- `migrations/versions/` — numbered migration scripts

**Why Alembic instead of `Base.metadata.create_all()`?**

`create_all()` creates missing tables but never changes existing ones. Add a column? `create_all()` ignores it. Alembic tracks each change as a numbered revision and applies only the ones not yet applied. This is the only way to safely evolve a schema in production.

**Important note about TIMESTAMPTZ migration:**
Alembic's autogenerate omits the `USING` clause when changing column types. Without it, Postgres reinterprets existing timestamps in the session's timezone — a silent `+05:30` shift on an IST host. The handwritten migration includes `postgresql_using="<col> AT TIME ZONE 'UTC'"` to state the assumption explicitly.

---

## `tests/` — The Test Suite

| Test file                    | What it proves                                                  |
|------------------------------|------------------------------------------------------------------|
| `test_backoff.py`            | Retry delays are never earlier than the schedule                 |
| `test_concurrency.py`        | Real Postgres lock prevents double-claim by two sessions         |
| `test_crash_recovery.py`     | Crashed workers dead-letter instead of looping forever           |
| `test_idempotency.py`        | Duplicate keys replay; fan-out never doubles                     |
| `test_signing.py`            | HMAC covers the body including IDs; different secret fails verify|
| `test_replay_guards.py`      | Only `dead` deliveries can be replayed; others return 409        |
| `test_heartbeat.py`          | Lease advances during long deliveries; stops if lease is stolen  |
| `test_endpoint_management.py`| PATCH/DELETE lifecycle including in-flight guard                 |

**Total: 56 tests, ~8 seconds**
