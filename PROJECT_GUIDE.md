# HookRelay — Understanding Guide

**A complete reading guide to this project: what it does, why it matters, and
how to explain it to anyone.**

Written in three parts:

- **Part 1** — for a non-technical audience (HR, recruiters, product managers,
  a family member). No jargon. Read this one first.
- **Part 2** — the technical walkthrough: every computer science concept used,
  which file implements it, why that design, and what breaks without it.
- **Part 3** — architecture decisions, what was rejected and why, and how to
  defend the project in an interview.

---

# PART 1 — The Non-Technical Version

## 1. The problem, in one sentence

**When one company's computer system needs to tell another computer system
something happened, there is no reliable way to do it — and businesses run on
this being reliable.**

## 2. An analogy that actually works

Picture a **restaurant kitchen with an order rail**.

The kitchen is the "publisher" — the company's system that has news. The
server at the pass is HookRelay. The cooks waiting at the pass are the
"subscribers" — other companies' systems waiting to hear the news.

An order goes up on the rail. The server picks it up and carries it to the
cook. Now think about what can go wrong:

| Kitchen problem | What HookRelay calls it |
|---|---|
| The cook was busy, so you write the order on the ticket again later | Retry |
| The cook's printer is jammed, so the ticket goes in the bin | Dead-letter |
| Two servers grab the same ticket | Duplicate delivery |
| The server carrying the ticket gets fired mid-delivery | Worker crash |
| A customer orders "one burger" but the server is unsure the order arrived, so sends it twice | Idempotency |
| A fake server in a trenchcoat walks in and orders 500 free steaks | Security / signing |

**That is the whole project.** It is a kitchen ticket rail that never loses
an order, never sends one twice, and can prove who sent it.

## 3. Why is this a real problem worth solving?

Consider a real online store. When you place an order, four separate systems
need to know:

1. **The warehouse** — pick and ship the item
2. **Billing** — charge the card
3. **Analytics** — record the sale
4. **Email** — send the confirmation

Four systems. Four separate connections. Now the failure modes:

- **The email server is down for 90 seconds.** The customer's order is
  *paid for and shipped* but they never get a confirmation, so they order
  again. Now they have two orders.
- **The billing system is briefly unreachable.** The store's naive code
  retries immediately. Five rapid retries flood a system that was only
  briefly struggling — and now it is fully down. One small failure became a
  big one. (This is called a **retry storm**, and it is how small outages
  become company-wide outages in the real world. It happens constantly at
  large companies.)
- **The connection drops at the exact moment the message was delivered.** The
  store has no idea if billing got it. Retrying risks charging twice.
  Not retrying risks not charging at all. There is no way to be sure.

**HookRelay is the piece that makes all four of those cases survivable.** It
sits in the middle and takes responsibility for the "did they get it?" problem,
which no individual system can solve alone.

## 4. So what is the project, concretely?

A **webhook delivery service**. Here is literally what it does:

```
POST /events    →  HookRelay stores the event, tells every interested worker
                  "a new order exists"  →  HookRelay POSTs it to each
                  subscriber's URL, with retries, and records exactly what happened
```

It then shows you the result in a **web dashboard** so you can see failures
and re-send them.

## 5. Why it is worth putting on a resume

Anyone can build a working version in a day. What this project demonstrates is
the part that separates a demo from an engineer: **knowing what can go wrong,
finding the bugs, and proving the fixes with evidence.**

Three specific things stand out:

**a) It found a bug in itself by using it.** The same subscriber URL was
registered twice, so every event was delivered to that customer *twice*. The
database had a uniqueness rule that *looked* like it prevented exactly this —
but it was enforcing the rule at the wrong level. The developer found this by
looking at real dashboard data, not by reading the code. That is exactly how
production bugs work.

**b) It found a second, subtler bug by re-auditing its own design.** Retries
were counted in the "record the failure" code. But a worker that *crashes*
never reaches that code. So a task that reliably crashed a worker would be
retried **forever**, silently. The fix was to count the attempt at the moment
the task is picked up, because *that* always happens.

**c) The fixes are proven, not asserted.** 56 automated tests. And a live
demonstration where a request that took 45 seconds was protected by a 30-second
"lease" without being delivered twice.

## 6. If you only remember five things

1. **At-least-once, not exactly-once.** Exactly-once delivery across a network
   is not possible, and any project claiming it is wrong. The honest goal is
   to never *lose* an event, tolerate occasional duplicates, and make
   duplicates **identifiable** so the receiver can discard them. This project
   does that by putting the event's ID inside the cryptographically signed
   message.
2. **Push your problems to a middle layer.** Retry logic duplicated in every
   service is a bug factory. Centralise it.
3. **Correctness lives in the database, not in application code.** Application
   code can have bugs; a `UNIQUE` constraint cannot be bypassed by accident.
4. **Leases, not locks.** Never hold a resource hostage waiting for a process
   to finish. Take it, agree on a time limit, and let it be reclaimed if the
   holder dies.
5. **Measure your own claim.** "It works" is not evidence. "I fired 100 events
   at two competing workers and got 100 delivered with zero duplicates" is.

---

# PART 2 — The Technical Walkthrough

## How the system is laid out

```
                    ┌──────────────────────────────────────┐
   publisher ──────▶│  API  (Flask)                        │
   POST /events     │  validates, stores event, fans out   │
                    │  one delivery row per subscriber     │
                    └──────────────┬───────────────────────┘
                                   │ writes to Postgres
                    ┌──────────────▼───────────────────────┐
                    │  Postgres                            │
                    │  endpoints │ events │ deliveries     │
                    └──────────────┬───────────────────────┘
                                   │ workers poll & claim
              ┌────────────────────┴────────────────────┐
              │                                         │
   ┌──────────▼─────────┐                    ┌──────────▼─────────┐
   │  Worker A          │                    │  Worker B          │
   │  claim → POST →    │                    │  claim → POST →    │
   │  record result     │                    │  record result     │
   └──────────┬─────────┘                    └──────────┬─────────┘
              │                                           │
              └──────────────► subscribers ◄─────────────┘
                               (third-party URLs)
```

Two independent workers is the default, not an accident — it is the cheapest
possible way to *prove* the concurrency safety, because a bug in claiming
shows up immediately as a duplicate delivery.

---

## Topic 1 — Relational data modelling & normalisation

**What it is:** designing tables so that each fact is stored once, in the
right place.

**Where:** `shared/models.py`

**The design:** three tables instead of one.

| Table | One row is… | Key columns |
|---|---|---|
| `endpoints` | a subscriber that wants to hear about a type of event | `url`, `secret`, `event_type` |
| `events` | a thing that happened | `event_type`, `payload` (JSONB), `idempotency_key` |
| `deliveries` | one attempt-tracker for (event → endpoint) | `status`, `attempt_count`, `next_attempt_at`, `locked_until`, `locked_by` |

**Why this shape:** `deliveries` is the important one. It is deliberately *not*
a field on the event, because a single event can fail for one subscriber and
succeed for another. If delivery state lived on the event, a single stubborn
subscriber would block or corrupt the record for everyone else. Splitting it
out means each (event, subscriber) pair has its **own independent lifecycle** —
its own retry count, its own schedule, its own dead-letter state.

**What breaks without it:** one subscriber being down would stop all delivery,
or would make it impossible to answer "did subscriber X get event Y?" without
re-reading application logs.

---

## Topic 2 — Database constraints as the last line of defence

**What it is:** rules the database enforces no matter what the application does.

**Where:** `shared/models.py:38-40` and `:87-91`

```python
__table_args__ = (
    UniqueConstraint("url", "event_type", name="uq_endpoint_url_event_type"),
)
```

**Why, and the story behind it:** this project shipped with a uniqueness rule
on `deliveries(event_id, endpoint_id)` and reasonably assumed it prevented
double-delivery. It did not. The same URL registered twice created **two rows
in `endpoints`**, and the constraint only stopped *one event* reaching *one
endpoint row* twice. It said nothing about two rows existing for the same
address.

Result: every subscriber registered twice received every event twice. The
constraint was real, correct, and enforcing the rule at the wrong level.

**Why constraints rather than an application check:** an `if endpoint exists`
check in Python is defeated by two requests arriving at the same instant —
both check, both see "no", both insert. A `UNIQUE` constraint is evaluated by
the database atomically, so one of them fails. The application cannot be
bypassed, and cannot be raced.

**Why the constraint includes `event_type`:** because one URL legitimately
subscribing to two different event types is normal and must remain allowed. A
constraint on `url` alone would have been wrong.

**What breaks without it:** duplicate deliveries that no amount of
application-level checking prevents, because the check is not atomic with the
write.

---

## Topic 3 — Idempotency

**What it is:** performing the same request twice has the same effect as
performing it once. Keyed on a **client-supplied** `idempotency_key`.

**Where:** `api/routes/events.py`, enforced by `events.idempotency_key UNIQUE`

**How it works:** the client generates a key (e.g. `order-4521-created`) and
sends it with the event. If the client retries — because the response was lost,
or the network was flaky — the database rejects the second insert, and the API
returns the **original** result rather than an error.

```python
except IntegrityError:
    session.rollback()
    existing = session.query(Event).filter(
        Event.idempotency_key == data["idempotency_key"]
    ).one_or_none()
    if existing is not None:
        return jsonify({
            "event_id": str(existing.id),
            "subscribers_notified": notified,
            "duplicate": True,
        }), 200
    # Not a duplicate key after all — a genuine integrity failure.
    return jsonify({"error": "internal error creating event"}), 500
```

**The subtle part — why `200` and not `409`.** The earlier version returned
`409 Conflict`. That is a dead end: a publisher that lost the response to its
first request has **no way to recover**. It does not know the event ID, so it
cannot proceed. It cannot retry (permanent 409), and inventing a new key
delivers the event twice. An idempotent API must *replay the original answer*.

**Why the handler checks whether a row actually exists:** the `except` block
catches *any* integrity failure, not just this one. Reporting every one as
"duplicate key" would send an engineer hunting the wrong bug. The narrow check
distinguishes "your key collided" from "something else broke."

**What breaks without it:** every ambiguous network failure becomes a
duplicate business transaction. Real-world damage: double charges, double
orders, double emails.

---

## Topic 4 — Concurrency: `SELECT … FOR UPDATE SKIP LOCKED`

**What it is:** when many workers pull from one queue, exactly one must get
each job.

**Where:** `worker/worker.py` — `claim_one_delivery()`

```python
delivery = (
    session.query(Delivery)
    .filter(Delivery.status.in_(["pending", "retrying"]))
    .filter(Delivery.next_attempt_at <= now)
    .with_for_update(skip_locked=True)
    .first()
)
```

**How it works, step by step:**

1. `FOR UPDATE` — Postgres locks the row it returns, so nobody else can touch it.
2. `SKIP LOCKED` — if a row is *already* locked, **do not wait**. Skip it and
   return the next available one.

**Why `SKIP LOCKED` and not plain `FOR UPDATE`:** without it, worker B *blocks*
on worker A's row until A finishes. Ten workers and one slow delivery would
serialise the entire fleet behind it. With it, B ignores A's row and takes the
next one — throughput stays flat and one slow subscriber cannot stall the
system. This is the mechanism behind every major queue system.

**Why the same pattern is used for the lease sweeper** (`sweep_expired_leases`):
without locks, N workers all find the same expired row and all try to reclaim
it, fighting over it. With `SKIP LOCKED`, each sweeps a different row. It is
also bounded with `.limit(50)`, so a huge backlog cannot lock the whole table
in one pass.

**How it is tested:** `tests/test_concurrency.py` takes a **genuine row lock
on a separate connection** and asserts a second connection skips it. This is
deliberately deterministic, not timing-based. A "fire 20 threads and check"
test would pass most of the time *even against broken code* — which is worse
than no test, because it manufactures false confidence.

**What breaks without it:** the same event is delivered several times, from
several workers, simultaneously.

---

## Topic 5 — Leases and crash recovery

**What it is:** a time-limited claim on a job, so a dead worker never blocks
work forever.

**Where:** `worker/worker.py` — `locked_by`, `locked_until`, `sweep_expired_leases()`

**The problem it solves:** if a worker picks up a job and is then killed — OOM
kill, container restart, a bad deploy — the job is stuck `in_flight` forever.
Nobody will ever retry it. The event is silently lost.

**The design:** claiming a job sets `locked_until = now + 30s`. Every worker
also periodically runs a **sweeper** that finds `in_flight` rows whose lease
expired and hands them back. No separate "is the worker alive?" service is
needed — expiry *is* the liveness check.

**Topic 5b — the heartbeat (the subtlest fix in the project).**

A lease assumes work finishes inside the lease window. But the HTTP timeout
does **not** guarantee that. `requests`' `timeout=10` is a timeout on each
individual socket read, **not a total deadline for the request**. A subscriber
that sends one byte every 3 seconds keeps every individual read comfortably
under the limit while the request as a whole runs for minutes.

That means a delivery can outlive its 30-second lease while still in progress.
Without a heartbeat, the sweeper sees an expired lease, hands the job to a
second worker, and the event goes out twice — while the first request is still
running.

The fix, `worker/worker.py` → `_LeaseHeartbeat`, renews `locked_until` every
10 seconds while the request is in flight. Its critical detail is the guard:

```python
.filter(
    Delivery.id == self.delivery_id,
    Delivery.locked_by == WORKER_ID,      # ← only renew OUR lease
    Delivery.status == "in_flight",
)
```

If the lease was already stolen and reassigned, **zero rows match** and the
heartbeat stops. Without that filter, a slow-but-alive worker would keep
renewing a lease that now belongs to someone else — leaving the job
permanently un-sweepable while neither worker will claim it. A fix that
prevents one duplicate can create a permanently stuck job; the guard is what
makes the fix safe.

**Verified live:** a subscriber trickling bytes for 45 seconds, against a
30-second lease, with two workers competing. Result: succeeded on attempt 1,
`attempt_count` never exceeded 1, and the second worker never even attempted
a sweep. The lease counter was observed being pushed forward repeatedly
(22s → 25s → 27s → 20s → 23s → 26s) instead of counting down to zero.

---

## Topic 6 — Bounded retries, exponential backoff, and jitter

**What it is:** failing fast is not an option; failing *infinitely* is worse.

**Where:** `worker/backoff.py`

```python
BASE_SCHEDULE = [10, 30, 60, 120, 300]
JITTER_RATIO = 0.2

def get_delay_seconds(attempt_count):
    index = min(attempt_count - 1, len(BASE_SCHEDULE) - 1)
    base = BASE_SCHEDULE[index]
    return max(1, base + base * random.uniform(0, JITTER_RATIO))
```

**Three distinct problems, three solutions:**

**(a) Immediate retry** — hammers a struggling system and turns a small
outage into a large one. → **exponential backoff**: 10s, 30s, 60s, 120s, 300s.

**(b) Retrying forever** — a permanently broken subscriber consumes worker
capacity forever. → **dead-letter after 5 attempts.** It stops and becomes
visible in the dashboard, to be replayed after a human fixes the cause.

**(c) Synchronised retries — the subtle one.** Suppose a subscriber goes down
at 3pm and 500 events are queued. All 500 fail together. With no jitter, they
all retry at 3:00:10 — together. The subscriber gets 500 simultaneous requests
the instant it recovers, and is knocked over again. The system oscillates and
never recovers. This is a real, well-documented failure mode.

Jitter exists to break up that synchronised wave. **And the direction of the
jitter matters**, which is the bug this project found: the original code used
**symmetric** jitter, `base ± 20%`. That can produce `base × 0.8` — a retry
*earlier* than the schedule promised. That is precisely backwards: it pulls a
spread-out cohort back together and re-synchronises them. The fix is
**one-directional** jitter, `base + uniform(0, 20%)`, which only ever stretches
the wave out.

**Topic 6b — counting attempts at claim, not at failure.**

`attempt_count` used to increment inside the failure-recording code. But a
worker that *crashes mid-request* never reaches that code. So a job that
reliably crashed the worker accumulated no attempts, was swept, retried,
crashed the worker again — **forever**, never dead-lettering, never surfacing
to a human. The counter now increments at the moment of claiming, because that
always happens. Crashes and ordinary failures now share one bounded budget.

**What breaks without these:** a dead subscriber either gets hammered into the
ground or silently absorbs infinite worker capacity while the real problem is
invisible.

---

## Topic 7 — Cryptographic signing (HMAC-SHA256)

**What it is:** proving a request genuinely came from HookRelay, and detecting
tampering.

**Where:** `worker/signing.py`, consumed in `worker/worker.py`

**How it works:** the sender and receiver share a secret only they know. The
sender computes `HMAC(secret, timestamp + "." + body)`. The receiver recomputes
it and compares. A mismatch means the message was altered in transit.

**Why the timestamp is signed too:** signing the body alone would let an
attacker capture a valid request and replay it forever. Including the timestamp
means an old signature cannot be reused on a fresh request, and a receiver can
reject anything outside a tolerance window.

**Topic 7b — why the IDs are inside the body, not in headers. This is the
single most instructive decision in the project.**

The signature covers `"{timestamp}.{body}"`. **Nothing else.** So anything in
a header is *outside* the signature.

That makes headers untrustworthy for anything security-relevant. If `event_id`
were sent only as a header, anyone in the middle could rewrite it while
leaving the body and signature perfectly valid. The receiver would verify the
signature, see a forged ID, and deduplicate against the wrong event — silently
discarding a legitimate message.

Because the IDs are inside the body, the receiver's own signature check covers
them:

```python
body = json.dumps({
    "event_id": str(event.id),        # ← inside what gets signed
    "delivery_id": str(delivery.id),
    "event_type": event.event_type,
    "payload": event.payload,
}).encode()
signature_headers = sign_payload(endpoint.secret, body)  # signs exactly these bytes
```

The headers are still sent, as convenience mirrors for subscribers that want
them — but they are explicitly *not* the authoritative copy.

**Why this matters to the reliability story:** at-least-once delivery means a
subscriber **will** occasionally get the same event twice. That is only
acceptable if duplicates are *identifiable*. A signed, stable `event_id` is
what lets a receiver say "I have already processed this" and discard the
repeat. Without it, at-least-once is a licence to double-charge people.

**Tested adversarially** (`tests/test_signing.py`): a test rewrites the
`event_id` in the body and asserts verification now **fails** — proving the ID
is genuinely covered by the signature, not merely present in it.

**What breaks without it:** anyone who learns a subscriber's URL can send it
fake events, and subscribers cannot distinguish real from forged.

---

## Topic 8 — At-least-once vs exactly-once delivery

**The most important concept in the project, and the one most often
misunderstood.**

**Exactly-once delivery is not achievable across a network.** The reason is
not lack of effort — it is physics. Consider the moment that makes it
impossible:

> The worker sends the request. The subscriber processes it and commits it.
> The worker is then killed *before it records that it succeeded*.

From the worker's perspective nothing was recorded, so it must retry. From the
subscriber's perspective it already processed the event. The worker has no way
to distinguish "never sent" from "sent but crashed before I wrote it down." The
information does not exist.

Any system claiming exactly-once is either lying, or quietly reimplementing
this problem somewhere else.

**The honest engineering answer is three parts:**

1. **Never lose an event** — at-least-once is the goal.
2. **Make duplicates detectable** — a stable, authenticated `event_id`.
3. **Let the receiver be idempotent** — the receiver discards repeats.

This project implements all three. Stating the boundary explicitly, and
explaining *why* the boundary exists, is the mark of someone who understands
distributed systems rather than someone who has memorised vocabulary.

**This is the answer to give if asked "how do you guarantee exactly-once?"**
The correct answer is: *"I don't, because it isn't possible. Here's the
boundary, here's why, and here's how I make the duplicates harmless."*
Interviewers ask this question specifically to see whether you know the answer.

---

## Topic 9 — Transaction safety and the fan-out

**What it is:** either an event and all its delivery rows are created, or none
are.

**Where:** `api/routes/events.py`

The event plus one `deliveries` row per matching endpoint are added in a single
session and committed **once**. If anything fails, nothing is persisted.

**Why it matters:** a partial write would create an event that no subscriber
was ever told about — an event that silently will never be delivered, and which
nothing would ever retry, because the system believes it succeeded.

**What breaks without it:** silent, permanent event loss that no dashboard
would show, because the system recorded the event as accepted.

---

## Topic 10 — Database migrations (Alembic)

**What it is:** versioned, reversible changes to the database structure.

**Where:** `migrations/versions/`, `alembic.ini`, run by
`api/entrypoint.sh` and `worker/entrypoint.sh` on container startup.

**Why it matters here specifically:** both containers run
`alembic upgrade head` before doing anything else. This fixes a genuine race
condition — previously the worker could start and query tables that the API had
not yet created.

**The lesson worth telling in an interview — Alembic's output must be
reviewed, not trusted.**

When converting the timestamp columns to `TIMESTAMPTZ`, Alembic generated the
right statements but **omitted the `USING` clause**. That is dangerous, because
`ALTER COLUMN ... TYPE TIMESTAMPTZ` alone makes Postgres reinterpret every
existing value as being in the *current session's* timezone. In a container
that is usually UTC by luck. On a host configured for IST, every historical
timestamp would silently shift by +05:30 — corrupting retry schedules already
read through the API, with no error raised anywhere.

The fix was one explicit line, `USING <col> AT TIME ZONE 'UTC'`, stating the
assumption rather than inheriting it from the environment:

```python
op.alter_column(
    "deliveries", "next_attempt_at",
    existing_type=postgresql.TIMESTAMP(),
    type_=sa.DateTime(timezone=True),
    existing_nullable=True,
    postgresql_using=f"{column} AT TIME ZONE 'UTC'",
)
```

Verified by recording a timestamp before the migration and confirming the
wall-clock reading was **unchanged** afterwards, plus a full
downgrade → upgrade round-trip. `alembic check` confirms no drift between
models and schema.

**What breaks without it:** production databases drift out of sync with the
code, and changes are applied by hand, unrepeatably, under pressure.

---

## Topic 11 — Time handling (`TIMESTAMPTZ`)

**What it was:** `DateTime` with no timezone, on every timestamp column.

**The bug it hides:** the values *were* UTC, but the column did not say so. So:

- API responses returned timestamps with no `+00:00` — ambiguous to any
  consumer in another timezone.
- The worker compared an aware UTC datetime against a naive column, working
  only because Postgres silently coerced it. That is an accident, not a
  guarantee, and it changes if the server's timezone setting changes.

**Now:** `DateTime(timezone=True)` on all five columns, and the API returns
`2026-09-27T20:56:10.562305+00:00`.

**What breaks without it:** timezone-dependent bugs that appear only in
production, on a machine configured differently from the developer's.

---

## Topic 12 — API design: validation and status codes

**What it is:** rejecting bad input clearly, and using HTTP status codes
that mean something consistent.

**Where:** `api/schemas.py`, route modules

| Code | Used for | Why |
|---|---|---|
| `201` | endpoint created | a resource now exists |
| `202` | event accepted | accepted for delivery — **not** "delivered"; the HTTP response only covers the hop to HookRelay, not to subscribers |
| `200` | duplicate idempotency key | success — the original result is replayed |
| `400` | malformed input | caller must fix the request |
| `404` | no such endpoint/delivery | |
| `409` | conflict | duplicate endpoint, replaying a non-dead delivery, deleting an in-flight endpoint |
| `500` | genuine server fault | |

**The `202` choice matters and is worth explaining.** Returning `200` on
`POST /events` would imply the event reached every subscriber. It has not —
it has only been accepted. A client that treats `200` as "delivered" will
report success for events that later dead-letter.

**`409` on delete-while-in-flight** is a deliberate guard: deleting an endpoint
underneath an active worker would leave a delivery row that worker still
believes it owns. Refusing is honest; silently orphaning it is not.

---

## Topic 13 — Containerisation (Docker)

**What it is:** the application and everything it needs packaged so it runs
identically everywhere.

**Where:** `docker-compose.yml`, `api/Dockerfile`, `worker/Dockerfile`

Three services: `db` (Postgres 16), `api` (Flask), `worker`. Plus a health
check so dependent services wait for the database instead of failing to start.

**Why it matters for a resume project:** the whole system starts with one
command, so a reviewer can run it in a single step:

```bash
docker compose up -d
curl http://127.0.0.1:5000/health
```

No Python setup, no database installation, no configuration. That is the
difference between a project someone can evaluate and one they take on faith.

---

## Topic 14 — Automated testing (56 tests)

**What it is:** executable proof that the reliability claims hold.

**Where:** `tests/` — `test_backoff.py`, `test_concurrency.py`,
`test_crash_recovery.py`, `test_idempotency.py`, `test_signing.py`,
`test_heartbeat.py`, `test_endpoint_management.py`, `test_replay_guards.py`

**Three principles used throughout:**

**(a) Test the database, not a mock of it.** The locking guarantees live in
Postgres. A mocked database would have tested nothing. The concurrency tests
open a real connection and take a real row lock.

**(b) Be deterministic, not probabilistic.** Real row locks prove
`SKIP LOCKED` behaviour every time. "Fire 20 threads and hope" passes most of
the time even against broken code — manufacturing false confidence.

**(c) Test isolation so the suite cannot damage real data.** Tests run against
a separate `hookrelay_test` database, created automatically and truncated
between tests, so a failing test can never touch real data.

**Notable adversarial tests:**

| Test | What it proves |
|---|---|
| `test_tampering_with_the_body_breaks_the_signature` | The IDs are genuinely *covered* by the signature, not merely present in the body |
| `test_repeated_crashes_eventually_dead_letter` | A job that keeps killing a worker is bounded — the bug that previously caused infinite retries |
| `test_heartbeat_stops_renewing_once_the_lease_is_stolen` | The heartbeat cannot keep a stolen lease alive (the fix that could have caused stuck jobs) |
| `test_duplicate_key_does_not_fan_out_a_second_time` | Replaying a result does not create a second round of deliveries |
| `test_delay_never_falls_below_its_base` | The symmetric-jitter bug cannot return |
| `test_a_locked_delivery_is_skipped_not_stolen` | `SKIP LOCKED` skips rather than blocks |

---

# PART 3 — Architecture & Interview Preparation

## 1. Why Postgres, and not Kafka / Redis / RabbitMQ / SQS?

This is the question most likely to be asked, so the reasoning matters more
than the conclusion.

**What the system actually needs:**

1. Durable storage of every event (never lose it)
2. A queue of pending work
3. **Per-(event, subscriber) delivery state** — including full history
4. A dashboard queryable over that history
5. Correct concurrent claiming across multiple workers

**Why Postgres satisfies all five:**

- **Atomic claiming is a solved problem.** `FOR UPDATE SKIP LOCKED` is exactly
  the primitive needed, and it is battle-tested. No additional infrastructure.
- **The data model is genuinely relational.** Event → many deliveries → one
  endpoint. Foreign-key referential integrity does real work here; a document
  store would require enforcing it in application code.
- **The dashboard is a query, not a feature.** "Show me dead deliveries for
  this subscriber, ordered by time" is a `WHERE` clause. In a queue-plus-store
  architecture, the queue has thrown the job away by the time you need to show
  it — you need a second store, and the two can disagree.
- **Transactions span the fan-out.** Creating an event and N delivery rows is
  one atomic commit. With a queue, "write the event" and "enqueue N jobs" are
  two systems and you get a dual-write problem: a crash between them loses
  events. Postgres avoids the problem by not having it.

**What Postgres genuinely cannot do well, and the honest upgrade path:**

- **Fan-out on read** — thousands of workers polling does not scale. Redis
  pub/sub, or `LISTEN/NOTIFY` (built in), would remove the polling entirely.
  The schema would not change.
- **Throughput ceiling** — one row per delivery is write-heavy. At very high
  volume, event *bodies* would move to object storage or a wide-column store,
  keeping Postgres for delivery state only.
- **Retention** — millions of historical deliveries would be pruned or moved to
  cold storage.

**The correct framing for an interview:** *"I chose Postgres because the
dominant requirement was per-subscriber delivery state with correct concurrent
claiming, and Postgres provides both with zero extra infrastructure. The
bottleneck is fan-out on read, and the migration path is `LISTEN/NOTIFY` or a
Redis layer for wakeups while Postgres remains the source of truth — that
scales the system without making the data model lie."*

That answer demonstrates you evaluated the trade-off rather than following a
trend, **and** that you know what the trade-off costs. Saying only
"I used Postgres because it's simple" is weak. Saying only "Kafka is the
professional choice" is wrong.

## 2. Architecture decisions, and what was rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Postgres as queue | Kafka / RabbitMQ | No dual-write problem, atomic fan-out, per-delivery state, dashboard is a query. Accepts a polling bottleneck. |
| `FOR UPDATE SKIP LOCKED` | Redis distributed lock | No extra service, no lock expiry bugs. A Redis lock can be lost mid-operation. |
| Leases, not a heartbeat service | External worker-monitor | Expiry *is* the liveness check. No new component to run or fail. |
| Pessimistic claim | Optimistic claim + retry | Optimistic claiming wastes HTTP round-trips on conflicts and is harder to reason about under load. |
| At-least-once + dedup keys | Attempting exactly-once | Not achievable across a network. Claiming otherwise is dishonest. |
| Sign the body only | Sign headers too | The signature covers `{timestamp}.{body}`; anything outside is forgeable. |
| One-directional jitter | Symmetric jitter | Symmetric jitter can fire retries *early*, re-synchronising the cohort backoff was spreading. |
| Count attempts at claim | Count at failure | A crashed worker never reaches the failure path, so crashes were unbudgeted and retried forever. |
| `UNIQUE(url, event_type)` | Application-level existence check | Checks are racy; the database evaluates constraints atomically. |
| Explicit `AT TIME ZONE 'UTC'` | Trust autogenerated migration | Omits the `USING` clause, so values get reinterpreted in the session timezone — silent corruption. |

## 3. The bug stories — the most valuable part of the project

Most candidates show a project that works. The strongest signal is finding
bugs in your own work and explaining *why* the bug was invisible. This project
has three, in increasing order of subtlety.

**Bug 1 — the constraint at the wrong level.** A `UNIQUE` constraint existed
and looked like it prevented double-delivery. It prevented one event reaching
one endpoint *row* twice, and said nothing about two rows existing for the
same URL. **Found by using the product** (seeing a duplicate in dashboard data),
not by reading code. Lesson: verify a guarantee end-to-end, at the level the
user experiences, not the level the schema describes.

**Bug 2 — the counter in the wrong place.** `attempt_count` incremented on
failure. A crashed worker never reaches the failure branch, so crashes were
unbudgeted and a reliably-crashing job retried forever without ever
dead-lettering. **Found by re-auditing the design**, asking "what if the
process dies here?" Lesson: every counter must be incremented somewhere that
*always* executes for the event being counted — usually at the start.

**Bug 3 — the jitter pointing the wrong way.** Retry jitter was symmetric
(`±20%`), which can fire a retry *earlier* than the schedule. Backoff exists
to spread a cohort out; negative jitter pulls it back together. **Found while
explaining the design, before any test existed.** Lesson: a mechanism can be
named correctly, wired up correctly, and still be backwards. Read the maths.

## 4. The one-minute answer

> "HookRelay is a webhook delivery service. Systems post events to it and it
> delivers them to subscribers, with the reliability that requires: at-least-once
> delivery, exponential backoff with jitter, dead-lettering, crash recovery via
> leases, and HMAC-signed payloads so subscribers can authenticate us and
> deduplicate redeliveries.
>
> The interesting part is that I found real bugs in my own design. A uniqueness
> constraint that *looked* like it prevented double-delivery was enforcing the
> rule at the wrong level, so subscribers registered twice got every event
> twice. And a retry counter incremented on the failure path was invisible to a
> worker that crashed, so a job that reliably killed a worker retried forever.
> Both fixes are covered by 56 tests — including adversarial ones that tamper
> with a signed payload to prove the signature actually covers the fields
> subscribers depend on.
>
> I chose Postgres over Kafka because the hard requirement was per-subscriber
> delivery state with correct concurrent claiming, and Postgres gives both
> atomically with no extra infrastructure. The bottleneck is polling for new
> work, and `LISTEN/NOTIFY` is the upgrade path that keeps Postgres as the
> source of truth."

## 5. Honest limitations — state these before being asked

Volunteering weaknesses is a strong signal, not a weak one. It shows you are
evaluating your own work rather than presenting it uncritically.

| Limitation | Impact | Fix |
|---|---|---|
| No circuit breaker | A dead endpoint consumes worker capacity on every event | Pause endpoints failing repeatedly (US-3.3) |
| Polling fan-out | Throughput capped per worker; latency up to 2s | `LISTEN/NOTIFY` or Redis wakeups |
| One delivery per 2s loop | Low throughput ceiling | Batch claim loop |
| No auth on the API | Anyone who finds the URL can publish events | API keys; fine for a local project |
| No metrics endpoint | Cannot track reliability over time | Prometheus metrics |
| `deliver()` has a nested try/except | Readable, but a real dispatcher would restructure | Minor |

## 6. Suggested next steps, in priority order

1. **Circuit breaker** — the highest-value remaining reliability feature.
2. **Batch claim loop** — closes the throughput ceiling; small change.
3. **`LISTEN/NOTIFY`** — removes polling, cuts latency, no schema change.
4. **Metrics** — success rate, queue depth, retry count. Makes reliability
   *measurable* rather than asserted.
5. **API authentication** — API keys for publishers.
6. **Chaos testing** — script killing workers mid-delivery and stopping the
   database, then assert `events_sent == succeeded + dead`. `tests/test_crash_recovery.py`
   is the seed of this.
7. **Deploy + CI/CD** — a public URL and a pipeline that runs the tests on
   every push.

---

## Where everything lives

| File | Responsibility |
|---|---|
| `shared/models.py` | Tables, constraints, the status lifecycle |
| `shared/db.py` | Engine and session — one source of truth for the DB |
| `api/routes/events.py` | `POST /events`, fan-out, idempotent replay |
| `api/routes/endpoints.py` | `POST/GET/PATCH/DELETE /endpoints` |
| `api/routes/deliveries.py` | `GET /deliveries`, `/deliveries/stats`, replay |
| `api/routes/dashboard.py`, `index.py` | The HTML pages |
| `api/schemas.py` | Request validation |
| `worker/worker.py` | Claim loop, delivery, `_LeaseHeartbeat`, lease sweeping |
| `worker/backoff.py` | Retry delay and give-up rules (pure functions) |
| `worker/signing.py` | HMAC-SHA256 signing (pure functions) |
| `migrations/versions/` | Versioned schema changes |
| `tests/` | 56 tests |
