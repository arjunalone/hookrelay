# HookRelay

A reliable webhook delivery platform — like a mini version of what Stripe or GitHub use internally to notify other systems when something happens. Built as a learning + interview project to demonstrate backend reliability engineering, Docker, and cloud deployment.

## What it does

1. A client registers a **subscriber endpoint** — a URL that wants to be notified about a specific event type.
2. A publisher sends an **event** (e.g. `order.created`) to HookRelay's API.
3. HookRelay saves the event, **fans it out** into one delivery job per subscribed endpoint, and immediately acknowledges (`202`) — the publisher isn't blocked waiting for delivery.
4. Background **workers** pick up delivery jobs and send real HTTP POST requests to each subscriber.
5. Failures are **retried with exponential backoff**; permanently failing deliveries are marked **dead** instead of retrying forever.
6. Multiple workers can run at once safely — no duplicate deliveries, no lost jobs, even if a worker crashes mid-delivery.

## Why this project

Most student projects are CRUD apps. This one is a genuine distributed-systems problem: **reliable delivery under failure**. It gives real, defensible answers to backend interview questions about retries, idempotency, concurrency, and database design — because the answers are running code, not theory.

---

## Architecture (current state)

```
┌─────────────┐      POST /events       ┌──────────────┐
│  Publisher   │ ───────────────────────▶│   API (Flask) │
│ (e.g. curl)  │      202 Accepted        │               │
└─────────────┘ ◀───────────────────────└──────┬───────┘
                                                 │ saves Event +
                                                 │ fans out Deliveries
                                                 ▼
                                          ┌─────────────┐
                                          │  PostgreSQL  │◀────────┐
                                          │  (Docker)    │         │
                                          └──────┬───────┘         │
                                                 │ claims ready     │ writes
                                                 │ deliveries       │ status
                                                 ▼                  │
                                          ┌─────────────┐          │
                                          │   Worker(s)  │──────────┘
                                          │ (1 or many)  │
                                          └──────┬───────┘
                                                 │ HTTP POST
                                                 ▼
                                          ┌─────────────┐
                                          │  Subscriber  │
                                          │  (webhook)   │
                                          └─────────────┘
```

**Postgres is the source of truth.** The API only replies success after data is committed to the database — never before. Workers read from the same table, so even if all workers crash, nothing is lost; the ledger remembers everything.

---

## Progress log

### Milestone 1 — Database foundation ✅
- Set up Docker Compose with a Postgres 16 container.
- Built `shared/db.py`: the SQLAlchemy engine, session factory, and declarative `Base`.
- Built the first model, `Endpoint` (a subscriber's URL + secret + subscribed event type).
- Proved the full chain works: Python → SQLAlchemy → Docker → Postgres, by creating a table, inserting a row, and reading it back — verified directly with `psql`.
- **Key concept learned:** UUIDs instead of auto-increment IDs, to avoid ID enumeration attacks.

### Milestone 2 — Event ingestion API ✅
Implements three user stories:
- **US-1.1** — `POST /endpoints`: register a subscriber URL for an event type.
- **US-1.2** — `POST /events`: accept an event, save it, and fan it out into one `Delivery` row per subscribed endpoint, all in a single transaction. Replies `202` only after commit.
- **US-1.3** — Idempotency: a `UNIQUE` constraint on `idempotency_key` blocks duplicate event submissions at the database level (`409 Conflict` on resend).

Built with:
- `shared/models.py` — added `Event` and `Delivery` tables. `Delivery` has a `UniqueConstraint(event_id, endpoint_id)` so one event can never double-book the same subscriber.
- `api/app.py`, `api/config.py`, `api/extensions.py` — minimal Flask app with a `/health` check that verifies real DB connectivity.
- `api/schemas.py` — request validation before anything touches the database.
- `api/routes/endpoints.py`, `api/routes/events.py` — the actual routes, using Flask Blueprints.

**Verified:** registered an endpoint, sent an event, confirmed 3 delivery rows were created in Postgres with correct foreign keys, and confirmed a duplicate `idempotency_key` was correctly rejected.

### Milestone 3 — The worker (reliability core) ✅
This is the heart of the project — the part that turns "we recorded intent to deliver" into "we actually deliver, safely, under failure."

**Step 3a — Read the queue.** Worker polls `deliveries` for rows where `status IN (pending, retrying)` and `next_attempt_at <= now`.

**Step 3b — Real delivery.** Worker sends an actual HTTP POST (via `requests`, 10s timeout) to the subscriber's URL. Updates `status` to `succeeded` or `retrying` based on the response.

**Step 3c — Safe concurrency.** Added `worker/backoff.py`-independent claiming logic using `SELECT ... FOR UPDATE SKIP LOCKED`, so multiple worker processes can run at once without ever grabbing the same row. Each claim sets a **lease** (`locked_until`, `locked_by`) — if a worker crashes mid-delivery, a sweep function (`sweep_expired_leases`) detects the expired lease and returns the job to the queue for another worker to pick up.
- **Verified live** with two worker processes running simultaneously: 10 deliveries fanned out correctly across both workers with zero duplicate deliveries and zero lost jobs.

**Step 3d — Exponential backoff + dead-lettering.** Added `worker/backoff.py`: on failure, `next_attempt_at` is pushed into the future using a growing schedule (10s → 30s → 60s → 120s → 300s, with jitter to avoid synchronized retry storms). After `MAX_ATTEMPTS` (5) failures, the delivery is marked `dead` instead of retrying forever.
- **Verified live**: watched a failing delivery retry at increasing intervals (8s → 33s → 64s...) exactly matching the backoff schedule.

### Milestone 4 — Delivery authenticity (HMAC) ✅
**US-2.4.** Every outbound delivery is signed with the subscriber's own secret using HMAC-SHA256.
- `worker/signing.py` — computes the signature over the raw request body and returns a `sha256=<hex>` digest plus the timestamp it was computed at.
- Delivered as two headers: `X-HookRelay-Signature` and `X-HookRelay-Timestamp`. The timestamp is included in the signed payload so a captured request can't be replayed indefinitely.
- A subscriber verifies by recomputing the HMAC with the shared secret and comparing against the header — this is what makes a forged request distinguishable from a genuine one.

**Verified:** inspected the received headers on webhook.site and confirmed the digest matched a locally recomputed HMAC.

### Milestone 5 — Real schema migrations ✅
Replaced the old manual "create tables" script with real Alembic migrations.
- `migrations/env.py` points Alembic at the same `Base` in `shared/models.py`, so migrations and models can never drift apart.
- `alembic.ini` deliberately leaves `sqlalchemy.url` blank — the URL is injected from `DATABASE_URL` at runtime instead of being committed to the repo.
- Both `api` and `worker` entrypoints run `alembic upgrade head` before starting. This closes a real race: previously the worker could boot and poll a table that didn't exist yet, because the API hadn't created it.

### Milestone 6 — Full containerization ✅
`docker compose up -d` now starts the entire system: Postgres, the API, and the worker.
- `api/Dockerfile` + `worker/Dockerfile` build from the repo root so each image can import the shared `shared/` package.
- `docker-compose.yml` gates the worker on the API via `depends_on`, and both on Postgres passing its `pg_isready` healthcheck.

**Verified end to end inside Docker:** registered an endpoint → published an event → worker delivered it with a valid signature, all without touching the host Python at all.

### Milestone 7 — Failure visibility (dashboard + replay) ✅
Implements the two failure-visibility user stories:
- **US-3.1** — `GET /deliveries?status=dead` lists deliveries, filterable by any status and paginated via `limit`/`offset`. `GET /deliveries/stats` returns per-status counts.
- **US-3.2** — `POST /deliveries/<id>/replay` puts a dead delivery back on the queue.

`GET /dashboard` serves a small server-rendered page that reads those two endpoints, so the browser does the filtering and polling. No JS framework and no new dependencies — it's an internal ops view, so a dependency-free page was the right trade.

Two design points worth calling out in interviews:
- **Replay reuses the normal queue path.** It resets the row to `status='pending'`, `attempt_count=0`, `next_attempt_at=now` — byte-for-byte what a fresh delivery looks like. `claim_one_delivery()` picks it up with no knowledge that replay exists, so there's no second code path to keep in sync.
- **Replay is guarded, not blind.** Only `dead` rows can be replayed. Replaying a `pending`/`retrying` row would race the worker that already owns it, and replaying a `succeeded` one would double-deliver — so those return `409` instead of corrupting state. `last_error` is deliberately preserved so the reason for the original failure stays visible.

**Verified end to end:** forced a delivery to dead-letter (5 failed attempts against an unreachable URL), fixed the endpoint, replayed it via the API, and watched the worker pick it up and deliver it with a valid HMAC signature — `attempt_count` correctly back to 0.

### Milestone 8 — Fixing a real double-delivery bug ✅
Not a planned milestone — a bug found by actually using the dashboard. Registering the same subscriber URL twice created **two** endpoint rows, so every event fanned out to both and the subscriber silently received each event twice.

The subtle part: the existing `uq_event_endpoint` constraint on `deliveries` *looked* like it prevented this. It only stops one event hitting one endpoint **row** twice — it says nothing about two rows existing for the same URL. The database was enforcing uniqueness at the wrong level.

Fix, in three parts:
- `shared/models.py` — added `UniqueConstraint("url", "event_type")` to `Endpoint`. Scoped to `event_type` on purpose: one URL legitimately subscribing to two different event types is normal and must stay allowed.
- `migrations/versions/bc5a376aa80e_*.py` — a real data migration, because a constraint can't be added while duplicates exist. It keeps the **oldest** row of each duplicate group and repoints its deliveries onto the survivor, so history isn't lost. Where a repoint would collide with `uq_event_endpoint` (the same event was already delivered to the survivor), the row is deleted — those rows *are* the accidental double-sends.
- `api/routes/endpoints.py` — catches the resulting `IntegrityError` and returns `409`, so the caller learns at registration time instead of discovering duplicate deliveries later.

**Verified:** a duplicate `POST /endpoints` now returns `409`; the same URL with a *different* `event_type` still returns `201`; and after publishing an event, every subscriber URL has exactly one delivery row. `alembic check` reports no drift, and the migration round-trips cleanly on downgrade/upgrade.

### Milestone 9 — Correctness hardening + first real test suite ✅
Five reliability defects found by re-auditing the worker and the ingestion path, plus automated tests that pin the behaviour down. No schema changes were needed.

**P0 — deliveries now carry their own identity.** `event_id` and `delivery_id` were added to the delivery body (and mirrored as `X-HookRelay-Event-Id` / `X-HookRelay-Delivery-Id` headers). The IDs are in the *body* on purpose: `sign_payload()` signs only `"{timestamp}.{body}"`, so a bare header sits outside the signature and could be swapped in transit. This is what makes at-least-once delivery *usable* — a subscriber redelivered after a worker crash can compare `event_id` and discard the duplicate itself. Previously it had no way to tell.

**P1 — duplicate `idempotency_key` now replays the original result.** It returned `409`, which is a dead end for a publisher that lost the response to its first POST: it can neither learn the event id nor safely retry. It now returns `200` with the original `event_id`, `subscribers_notified`, and `duplicate: true`. The `IntegrityError` handler also got narrowed — it only reports "duplicate" if a row with that key actually exists, otherwise it's a real integrity failure and returns `500` rather than sending the caller chasing the wrong problem.

**P2 — `attempt_count` moved to claim time.** It used to increment only in `deliver()`'s failure branches, which a worker that *crashed* mid-request never reaches. A job that reliably killed the worker was therefore reclaimed forever and never dead-lettered. Counting at claim gives crashes and ordinary failures one shared, bounded budget. The lease sweeper now applies `should_give_up()` and schedules a backoff instead of blindly resurrecting rows.

**P3 — the sweeper takes row locks.** It now uses `FOR UPDATE SKIP LOCKED` with a bounded batch (50), so N workers sweeping at once don't pile onto the same row, and a lease renewed in the meantime isn't stolen from the worker still holding it.

**P4 — retry jitter no longer fires early.** The jitter was symmetric (`base ± 20%`), which can produce `base * 0.8` — a retry *earlier* than the schedule promises, the opposite of what prevents a retry storm. It is now one-directional (`base + uniform(0, 20%)`).

**Verified:** 56 pytest tests pass against a separate `hookrelay_test` database. Live in Docker: a crashed delivery at attempt 5 was swept to `dead` (previously it would loop forever); a crash at attempt 1 landed in `retrying` with a 10–12s backoff; 100 events through **two competing workers** produced 200/200 deliveries with **zero** double-claims; the receiver logged 204 requests with 204 unique `delivery_id`s and no duplicate delivery to any subscriber.

**Behaviour changes to be aware of:** a first-try success now shows `attempt_count = 1`, not `0`, and the dashboard's "Attempts" column reflects that. The delivery body has two new fields. No migration required.

---

### Milestone 10 — Endpoint management, `TIMESTAMPTZ`, and the lease heartbeat ✅
The last three known correctness gaps, all closed. Test count 37 → 56.

**E1 — `PATCH` / `DELETE /endpoints`.** Fixing a broken subscriber previously meant a direct `psql` update, which is not something a dashboard should ever require.
- `PATCH` rotates a secret or corrects a URL in place, with a shared `validate_endpoint_patch()` used by both routes. It rejects unknown fields, an empty body, and non-HTTP URLs, and returns `409` on a collision.
- `DELETE` returns `409` while any delivery is `in_flight` — silently orphaning a row a live worker still owns is worse than refusing. Otherwise it deletes the child delivery history in the same transaction and reports how many rows went with it. A `404` on a missing id is deliberate: silent `204`s make typos look like success.
- Used for real: the two dead `legacy-sink` endpoints left over from verification were removed through the API rather than by hand.

**E2 — all timestamps are now `TIMESTAMPTZ`.** `created_at`, `next_attempt_at`, and `locked_until` were naive `DateTime` columns that *held* UTC without saying so, so the API returned timestamps with no offset and the worker relied on Postgres silently coercing aware-vs-naive comparisons.
- Migration `c238d8f0b655` adds `postgresql_using="<col> AT TIME ZONE 'UTC'"` to each `alter_column`. **Alembic's autogenerated version omits the `USING` clause**, which makes Postgres reinterpret existing rows in the *session's* timezone — a silent `+05:30` shift for every historical row on an IST-configured host, with no error raised. The `USING` clause states the assumption instead of inheriting it from the environment.
- The API now returns `2026-09-27T20:56:10.562305+00:00`.
- Verified by recording a timestamp before the migration and confirming the wall-clock reading was unchanged after, plus a full downgrade → upgrade round-trip. `alembic check` reports no drift.

**E3 — the lease is now renewed while a delivery is in flight.** This closes the subtlest gap in the project. A 30s lease is set once at claim, but `requests`' `timeout=10` is a **per-socket-read** timeout, not a total deadline: a subscriber sending one byte every 3s keeps every read under the limit while the request runs for minutes. The sweeper would see the expired lease and hand the job to a second worker — duplicating the delivery while the first request was still running.
- `_LeaseHeartbeat` in `worker/worker.py` renews `locked_until` every 10s on a background thread with its own session, for the duration of the request.
- The guard is the important part: the update filters on `Delivery.id`, `locked_by == WORKER_ID`, and `status == "in_flight"`. If the lease was already stolen, **zero rows match**, and the heartbeat stops. Without that filter a slow-but-alive worker would keep renewing a lease now owned by someone else, leaving the job permanently unsweepable while neither worker claims it. A fix that prevents one duplicate can create a stuck job.
- **Verified live**, not just in tests: a subscriber trickling bytes for 45s against a 30s lease with two competing workers. Result: `status=succeeded`, `attempt_count=1`, and the second worker never even attempted a sweep (`0` sweeps). The remaining lease was observed being pushed forward repeatedly (22s → 25s → 27s → 20s → 23s → 26s) instead of counting down to zero.
- Tests: `test_heartbeat_extends_the_lease`, `test_lease_keeps_moving_forward_and_survives_the_sweeper`, `test_heartbeat_stops_promptly`, `test_heartbeat_stops_renewing_once_the_lease_is_stolen`, `test_heartbeat_does_not_touch_a_finished_delivery`, `test_heartbeat_interval_leaves_room_for_missed_beats`.

---

## Current file structure

```
hookrelay/
├── api/
│   ├── app.py              # Flask entrypoint, registers blueprints, /health route
│   ├── config.py           # env-based configuration
│   ├── extensions.py       # shared DB session accessor
│   ├── schemas.py          # request validation
│   ├── Dockerfile          # builds the api image
│   ├── entrypoint.sh       # alembic upgrade head, then starts Flask
│   ├── requirements.txt
│   ├── routes/
│   │   ├── endpoints.py    # POST/GET/PATCH/DELETE /endpoints
│   │   ├── events.py       # POST /events (ingestion + fan-out + idempotency)
│   │   ├── deliveries.py   # GET /deliveries, /deliveries/stats, POST replay
│   │   ├── dashboard.py    # GET /dashboard (the HTML page)
│   │   └── index.py        # GET / (home + test-event form)
│   └── templates/
│       ├── base.html       # shared shell + styling
│       ├── index.html      # home page with a working test-event form
│       └── dashboard.html  # delivery dashboard
├── worker/
│   ├── worker.py           # polling loop, claiming, delivery, lease sweeping, _LeaseHeartbeat
│   ├── backoff.py          # pure functions: retry delay + give-up logic
│   ├── signing.py          # HMAC-SHA256 signature for outbound deliveries
│   ├── Dockerfile          # builds the worker image
│   ├── entrypoint.sh       # alembic upgrade head, then starts the worker
│   └── requirements.txt
├── shared/
│   ├── db.py               # SQLAlchemy engine/session/Base
│   └── models.py           # Endpoint, Event, Delivery tables
├── migrations/
│   ├── env.py
│   └── versions/           # Alembic revisions
├── docs/
│   ├── engine_notes.txt    # SQLAlchemy engine/session notes
│   └── milestone2.txt      # epics, user stories, milestone plan
├── tests/
│   ├── conftest.py           # creates/truncates the separate hookrelay_test DB
│   ├── test_backoff.py       # retry schedule: never earlier than the base delay
│   ├── test_concurrency.py   # real Postgres locking: FOR UPDATE SKIP LOCKED
│   ├── test_crash_recovery.py# crashed workers dead-letter instead of looping
│   ├── test_idempotency.py   # duplicate keys replay, never double fan out
│   ├── test_signing.py       # HMAC covers the body, including the IDs
│   ├── test_replay_guards.py # replay only allowed from `dead`
│   ├── test_heartbeat.py     # lease renewal during long in-flight deliveries
│   └── test_endpoint_management.py  # PATCH/DELETE endpoint lifecycle
├── alembic.ini
├── docker-compose.yml      # db + api + worker, all containerized
├── pytest.ini              # test discovery config
├── requirements.txt        # local (non-Docker) dev only
├── requirements-dev.txt    # adds pytest; kept out of the Docker images
├── .env.example
├── README.md
├── PROJECT_GUIDE.md        # plain-English + technical walkthrough, for reading/review
└── HANDOFF.md
```

---

## Data model summary

**`endpoints`** — a subscriber: `id`, `url`, `secret`, `event_type`
Unique on `(url, event_type)` — registering the same URL twice for the same event type is rejected with `409`, which is what stops a subscriber receiving every event twice.

**`events`** — something that happened: `id`, `event_type`, `payload` (JSONB), `idempotency_key` (unique), `created_at`

**`deliveries`** — one delivery attempt-tracker per (event, endpoint) pair:
`id`, `event_id`, `endpoint_id`, `status`, `attempt_count`, `next_attempt_at`, `locked_until`, `locked_by`, `last_status_code`, `last_error`

**Status lifecycle:**
```
pending → in_flight → succeeded
                    → retrying → in_flight → ... → dead
```

---

## How to run it

**The whole stack, one command** (recommended — migrations run automatically on startup):

```bash
docker compose up -d
docker compose ps          # db, api, worker should all be Up
curl http://127.0.0.1:5000/health
```

**Local dev without Docker** (uses `venv/`, Postgres still from Docker):

```bash
docker compose up -d db   # just the database
source venv/bin/activate
alembic upgrade head      # create/update the schema

python -m api.app         # terminal 1 — API on :5000
python -m worker.worker   # terminal 2, 3, ... — as many workers as you like
```

**Try it end to end:**

```bash
# Register a subscriber
curl -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{"url": "https://webhook.site/YOUR-ID", "secret": "mysecret", "event_type": "order.created"}'

# Publish an event
curl -X POST http://127.0.0.1:5000/events \
  -H "Content-Type: application/json" \
  -d '{"event_type": "order.created", "payload": {"order_id": 1}, "idempotency_key": "unique-key-1"}'

# Watch the worker deliver it
docker compose logs -f worker
```

Open **http://localhost:5000** in a browser for the home page — it has a working
"send a test event" form, live delivery counts, and your registered endpoints.
**http://localhost:5000/dashboard** is the delivery dashboard.

`POST /events` and `POST /endpoints` are POST-only by design (they're publisher
actions), so `GET /events` renders the form page rather than a bare `405`.

---

## Known gaps / not yet built

- **No circuit breaker** — a repeatedly failing endpoint isn't automatically paused. Today each delivery retries on its own backoff schedule, so a dead endpoint still consumes worker capacity on every event. (US-3.3.)
- **No metrics** — queue depth, success rate, and retry counts aren't exposed as time series, so reliability can't be tracked over time. (US-4.3.)
- **Not yet deployed** — everything runs on localhost. Cloud deployment (Oracle free-tier VM) and CI/CD are planned.
- **Single polling loop per worker** — the loop processes one delivery per iteration with a 2s sleep, so throughput is capped at roughly one delivery per worker per 2s. Fine for correctness, needs a batch loop before real load. `LISTEN/NOTIFY` is the upgrade path for the polling half.
- **No auth on the API** — anyone who can reach the port can publish events or manage endpoints. Fine for local work, needs API keys before it's exposed.
- **Dashboard has no controls yet** — `PATCH`/`DELETE /endpoints` exist in the API, but the HTML dashboard still only displays. Wiring the buttons up is the natural next UI step.

**Closed since:** naive timestamps and the missing lease heartbeat were both listed here and are now fixed — see Milestone 10 above.

---

## Running the tests

The suite runs against a **separate** `hookrelay_test` database in the same Postgres container, so it can never touch real data. It's created automatically on first run and truncated between tests.

```bash
./venv/bin/pip install -r requirements-dev.txt   # once
./venv/bin/python -m pytest                      # 56 tests, ~8s
```

Requires the database to be up (`docker compose up -d db`). Override the connection with `TEST_DATABASE_URL` / `TEST_DATABASE_ADMIN_URL` if yours differs.

The locking tests use a real database and take a genuine row lock on one connection to prove a second worker skips it. They're deterministic rather than timing-based on purpose: a "fire 20 threads and hope" test would pass most of the time even against broken code.

---

## Roadmap (what's next)

1. **Circuit breaker** — automatically pause a repeatedly failing endpoint instead of burning worker capacity on it.
2. **Metrics** — queue depth, success rate, and retry counts exposed for observability.
3. **Dashboard controls** — wire the existing `PATCH`/`DELETE /endpoints` routes to UI buttons, so a broken subscriber can be fixed or removed without touching the API.
4. **Batch claim loop** — raise the per-worker throughput ceiling.
5. **`LISTEN/NOTIFY`** — replace the 2s poll for new work with a database notification. No schema change; cuts latency and removes the polling bottleneck.
6. **API authentication** — API keys for publishers, before anything is exposed publicly.
7. **Deploy to cloud** — Oracle Cloud free-tier VM, running the full `docker-compose` stack.
8. **CI/CD** — GitHub Actions pipeline: test → build → deploy on every push.
9. **Chaos testing** — scripted tests that kill a worker mid-delivery, stop Postgres briefly, and verify zero event loss, to back up the project's core reliability claim with real evidence. The crash-recovery tests in `tests/test_crash_recovery.py` are the seed of this.

**Done since the original roadmap:** endpoint management (`PATCH`/`DELETE`), the lease heartbeat, and `TIMESTAMPTZ` — all shipped in Milestone 10.

---

## What this project demonstrates (for interviews)

- At-least-once delivery semantics, and why exactly-once isn't attempted
- Idempotency via database constraints, not application-level guesswork
- **Enforcing uniqueness at the right level** — a constraint on `deliveries(event_id, endpoint_id)` *looked* like it prevented duplicate delivery, but it only guarded one event against one endpoint *row*. Two rows for the same URL slipped straight through. This is the single best "I found my own bug" story in the project.
- **Counting attempts at claim, not at failure** — incrementing in the failure path looked correct but was invisible to a worker that crashed mid-request, so a job that reliably killed the worker retried forever. Bounding crashes took moving the counter to the claim.
- **One-directional jitter** — symmetric jitter (`base ± 20%`) can fire a retry *earlier* than the schedule, which re-synchronises the very cohort the backoff was spreading out.
- **Signing the payload, not the headers** — `event_id`/`delivery_id` are inside the signed body because the signature only covers `"{timestamp}.{body}"`; a header would be unsigned and therefore forgeable in transit.
- **Replaying idempotent results instead of returning 409** — a publisher that lost a response has no way to recover from a conflict, so the API replays the original `event_id`.
- Safe concurrent job claiming (`SELECT ... FOR UPDATE SKIP LOCKED`)
- Crash recovery via leases, without needing a separate queue system
- Exponential backoff with jitter, and bounded retries (dead-lettering)
- Choosing Postgres over Kafka/Redis deliberately, based on the actual problem (per-endpoint delivery tracking) rather than defaulting to a trendier tool
- **Testing the database, not a mock of it** — the locking guarantees live in Postgres, so the concurrency tests take a real row lock and prove a second connection skips it. A mocked DB would have tested nothing.

**#HOW  TO RUN THIS
cd ~/hookrelay

docker compose up -d          # start db + api + worker (migrations auto-run)
docker compose ps             # confirm all 3 are Up
docker compose logs -f        # watch everything
docker compose logs -f worker # just the worker (shows [OK] / [FAIL] / [DEAD])

docker compose down           # stop
docker compose down -v        # stop AND wipe the database
Then open http://localhost:5000/dashboard
Option B — local Python (edit code without rebuilding)
cd ~/hookrelay
docker compose up -d db       # just Postgres
source venv/bin/activate

alembic upgrade head          # schema (safe to re-run)

python -m api.app             # terminal 1
python -m worker.worker       # terminal 2, 3, ... run as many workers as you want
Send a test event
# 1. register a subscriber
curl -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{"url": "https://webhook.site/YOUR-ID", "secret": "mysecret", "event_type": "order.created"}'

# 2. publish an event
curl -X POST http://127.0.0.1:5000/events \
  -H "Content-Type: application/json" \
  -d '{"event_type": "order.created", "payload": {"order_id": 42}, "idempotency_key": "key-1"}'
Other routes: GET /health · GET /endpoints · GET /deliveries?status=dead · GET /deliveries/stats · POST /deliveries/<id>/replay
⚠️ Heads-up before your first run
Two endpoints left in the DB point at http://hookrelay_receiver:9099 — a test container I created and then deleted. Publishing an order.created event will fan out to both, and the dead one will retry 5 times then dead-letter. That's the system working correctly, but it'll look like a bug.
Clear my test rows with:
docker compose exec db psql -U hookrelay -d hookrelay -c \
  "DELETE FROM endpoints WHERE url LIKE 'http://hookrelay_receiver%';"
Want me to run that cleanup for you?