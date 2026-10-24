# PART 2 — Architecture & System Design Deep Dive

---

## System Architecture Diagram

```
                         ┌───────────────────────────────────────┐
                         │          HookRelay System              │
                         │                                        │
┌──────────────┐         │  ┌─────────────────────────────────┐  │
│  Publisher   │ ──POST──▶  │        API (Flask)               │  │
│  (your app)  │         │  │  /endpoints  /events  /health    │  │
│              │ ◀──202──   │  /deliveries /dashboard          │  │
└──────────────┘         │  └─────────────┬───────────────────┘  │
                         │                │                        │
                         │                │ saves Event +          │
                         │                │ fans out Deliveries    │
                         │                ▼                        │
                         │  ┌─────────────────────────────────┐  │
                         │  │        PostgreSQL                │  │
                         │  │                                  │  │
                         │  │  endpoints | events | deliveries │  │
                         │  └───────────────┬─────────────────┘  │
                         │                  │                      │
                         │       ┌──────────┴──────────┐          │
                         │       │ SELECT FOR UPDATE    │          │
                         │       │ SKIP LOCKED          │          │
                         │       ▼                      ▼          │
                         │  ┌──────────┐         ┌──────────┐     │
                         │  │ Worker 1 │         │ Worker 2 │     │
                         │  └────┬─────┘         └─────┬────┘     │
                         └───────│─────────────────────│──────────┘
                                 │                     │
                          HTTP POST               HTTP POST
                                 │                     │
                         ┌───────▼─────┐      ┌───────▼─────┐
                         │ Subscriber  │      │ Subscriber  │
                         │     A       │      │     B       │
                         └─────────────┘      └─────────────┘
```

---

## Component Breakdown

### 1. API Layer (`api/`)

**Technology:** Flask (Python microframework)

**Purpose:** Accept events from publishers and manage subscribers

**Key Design Decisions:**
- Returns `202 Accepted` immediately — the publisher is never blocked waiting for delivery
- Writes the Event and all Delivery rows **in a single transaction** — either everything is saved or nothing is (atomicity)
- Uses `session.flush()` to get the new event's UUID before committing, so it can create delivery rows referencing it

**Files:**
```
api/app.py          — creates Flask app, registers blueprints, /health route
api/config.py       — reads DATABASE_URL from environment
api/extensions.py   — provides get_session() helper
api/schemas.py      — validates request bodies before DB touch
api/routes/
    endpoints.py    — POST/GET/PATCH/DELETE /endpoints
    events.py       — POST /events (fan-out + idempotency)
    deliveries.py   — GET /deliveries, /stats, POST /replay
    dashboard.py    — serves the HTML dashboard page
    index.py        — home page with test-event form
```

---

### 2. Worker Layer (`worker/`)

**Technology:** Plain Python process (no framework, no Celery, no Redis)

**Purpose:** Poll the database, claim delivery rows, send HTTP requests to subscribers

**Key Design Decisions:**
- Uses PostgreSQL directly as the job queue (no Redis/RabbitMQ/SQS) — because Postgres already has the data and the locking primitives needed
- `SELECT FOR UPDATE SKIP LOCKED` provides concurrency safety without a separate queue system
- The lease mechanism (`locked_until`, `locked_by`) handles crash recovery

**Files:**
```
worker/worker.py    — main polling loop + claim_one_delivery + deliver + sweep_expired_leases
worker/backoff.py   — pure functions: retry delay schedule + give-up logic
worker/signing.py   — HMAC-SHA256 payload signing
```

---

### 3. Shared Layer (`shared/`)

**Technology:** SQLAlchemy (ORM)

**Purpose:** Single source of truth for database models — both `api/` and `worker/` import from here so the table definitions can never drift

**Files:**
```
shared/db.py        — SQLAlchemy engine, session factory, Base
shared/models.py    — Endpoint, Event, Delivery table definitions
```

---

### 4. Migrations (`migrations/`)

**Technology:** Alembic

**Purpose:** Versioned database schema changes — no `CREATE TABLE` in Python code

**Key point:** Both API and Worker entrypoints run `alembic upgrade head` before starting. This closes a race where a worker could boot and query a table that didn't exist yet.

---

### 5. Infrastructure (`docker-compose.yml`)

**Three services:**
```yaml
db:      PostgreSQL 16
api:     Flask app (waits for db healthcheck)
worker:  Python worker (waits for api)
```

**One command to start everything:** `docker compose up -d`

---

## Why PostgreSQL as the Queue (Not Kafka/Redis)?

This is a common interview question. Here is the argument:

| Concern                  | PostgreSQL is fine because...                     |
|--------------------------|---------------------------------------------------|
| Durability               | Postgres is ACID — committed rows survive crashes |
| Concurrency              | `FOR UPDATE SKIP LOCKED` is designed for this exact use case |
| Per-endpoint tracking    | We need to track delivery state per (event, endpoint) pair — that's relational data, not a stream |
| Transactional fan-out    | The event and its delivery rows are created in the same transaction — impossible with Kafka |
| Complexity               | No separate queue infrastructure to operate or monitor |

**When PostgreSQL is NOT the right choice:**
- Millions of events per second (Kafka is better)
- Cross-datacenter pub/sub (Kafka is better)
- Pure pub/sub with no per-subscriber tracking needed (Redis Streams or SQS)

---

## Delivery Status State Machine

```
                ┌──────────┐
    Published   │  pending │
    ────────────▶          │
                └────┬─────┘
                     │
             Worker claims it
                     │
                     ▼
                ┌──────────┐
                │ in_flight│  ← lease is held here (locked_until, locked_by)
                └────┬─────┘
                     │
           ┌─────────┴────────┐
           │                  │
      2xx response       Non-2xx / error
           │                  │
           ▼                  ▼
      ┌──────────┐       ┌──────────┐
      │succeeded │       │ retrying │ ← backoff: 10s/30s/60s/120s/300s
      └──────────┘       └────┬─────┘
                              │
                   After 5 attempts total
                              │
                              ▼
                         ┌──────────┐
                         │  dead    │ ← can be replayed via API
                         └──────────┘
```

---

## The Fan-Out Pattern (Core Concept)

When `POST /events` receives `{ event_type: "order.created" }`:

```python
# 1. Create one Event row
new_event = Event(event_type="order.created", payload={...}, idempotency_key="abc")
session.add(new_event)
session.flush()  # get the UUID without committing

# 2. Find ALL endpoints subscribed to this event_type
matching = session.query(Endpoint).filter(Endpoint.event_type == "order.created").all()
# Result: [Warehouse URL, Email Service URL, Analytics URL]

# 3. Create ONE delivery row per subscriber
for endpoint in matching:
    session.add(Delivery(event_id=new_event.id, endpoint_id=endpoint.id))

# 4. Commit EVERYTHING together — atomically
session.commit()

# 5. Return 202 immediately — worker handles delivery in background
```

If there are 3 subscribers → 3 delivery rows → 3 independent deliveries tracked separately.
