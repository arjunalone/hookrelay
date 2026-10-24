# PART 3 — Key Technical Concepts (Interview Deep Dives)

Each section is a common interview question with the exact answer you should give, rooted in real code from this project.

---

## Concept 1: Idempotency

### The Problem
A publisher sends `POST /events` and their network drops before they receive the `202`. They do not know if the event was saved. If they retry, will the event be processed twice?

### How HookRelay Solves It

**Step 1 — DB Constraint (prevents duplicate storage):**
```python
# shared/models.py
idempotency_key = Column(String, nullable=False, unique=True)
# The UNIQUE constraint means Postgres rejects a second row with the same key
```

**Step 2 — Replay the original result (not a 409):**
```python
# api/routes/events.py
except IntegrityError:
    session.rollback()
    existing = session.query(Event).filter(Event.idempotency_key == data["idempotency_key"]).one_or_none()
    if existing is not None:
        return jsonify({
            "event_id": str(existing.id),
            "subscribers_notified": notified,
            "duplicate": True,
        }), 200   # <-- 200, not 409
```

**Why not 409?** A publisher that receives `409` is at a dead end. They cannot learn the original `event_id` and cannot safely retry. Returning `200` with `duplicate: true` and the original event data lets the publisher act as if they received the first response.

**Example:**
```
First call:   POST /events { idempotency_key: "order-42" }  → 202, event_id: "abc-123"
Network drop: publisher never sees the 202
Retry:        POST /events { idempotency_key: "order-42" }  → 200, event_id: "abc-123", duplicate: true
Result:       Warehouse/Email/Analytics each get exactly ONE notification
```

---

## Concept 2: Concurrent Job Claiming (SELECT FOR UPDATE SKIP LOCKED)

### The Problem
Two workers boot at the same time. Both poll the database. Both see delivery row `#7` as `pending`. Both try to send it. The subscriber gets the same event twice.

### How HookRelay Solves It

```python
# worker/worker.py — claim_one_delivery()
delivery = (
    session.query(Delivery)
    .filter(Delivery.status.in_(["pending", "retrying"]))
    .filter(Delivery.next_attempt_at <= now)
    .with_for_update(skip_locked=True)   # ← the key line
    .first()
)
```

**What `FOR UPDATE SKIP LOCKED` does:**
- `FOR UPDATE` → acquires a row-level write lock on the matched row
- `SKIP LOCKED` → if a row is already locked by another transaction, skip it instead of waiting

**Timeline with two workers:**
```
Worker 1: SELECT FOR UPDATE SKIP LOCKED → gets row #7 (lock granted)
Worker 2: SELECT FOR UPDATE SKIP LOCKED → sees row #7 is locked → SKIPS IT → gets row #8
Result:   Worker 1 delivers #7, Worker 2 delivers #8. Zero duplicates.
```

This is **built into PostgreSQL** — no external queue, no distributed lock manager, no Redis.

---

## Concept 3: Lease / Crash Recovery

### The Problem
Worker 1 claims delivery row `#7` and starts sending the HTTP request. Halfway through, the worker process crashes (OOM, bad deploy, etc.). Row `#7` is now `in_flight` forever — the subscriber never receives it.

### How HookRelay Solves It

**At claim time**, the worker sets a lease:
```python
delivery.status = "in_flight"
delivery.locked_by = "worker-a3f2bc9d"    # unique worker ID
delivery.locked_until = now + 30 seconds  # the lease expiry
```

**The sweeper runs every loop:**
```python
# worker/worker.py — sweep_expired_leases()
stuck = (
    session.query(Delivery)
    .filter(Delivery.status == "in_flight")
    .filter(Delivery.locked_until < now)   # lease expired
    .with_for_update(skip_locked=True)
    .all()
)
for d in stuck:
    d.status = "retrying"
    d.next_attempt_at = now + backoff_delay
```

**Timeline:**
```
T+0s:  Worker 1 claims row #7, sets locked_until = T+30s
T+15s: Worker 1 crashes (OOM kill)
T+35s: Worker 2 runs sweeper, sees locked_until < now
T+35s: Worker 2 resets row #7 to "retrying"
T+45s: Worker 2 claims and delivers row #7
Result: Event delivered exactly once (eventually)
```

---

## Concept 4: Lease Heartbeat (The Subtle Bug)

### The Problem
`requests.timeout=10` looks like it caps delivery at 10 seconds. But `timeout` in `requests` is a **per-socket-read timeout**, not a total deadline. A slow subscriber sending 1 byte every 9 seconds keeps the connection alive indefinitely. The sweeper would see the 30-second lease expire and hand the job to Worker 2 — **while Worker 1 is still delivering it**.

### How HookRelay Solves It

```python
# worker/worker.py — _LeaseHeartbeat class
class _LeaseHeartbeat:
    def _run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL):  # every 10s
            updated = session.query(Delivery).filter(
                Delivery.id == self.delivery_id,
                Delivery.locked_by == WORKER_ID,    # still ours
                Delivery.status == "in_flight",      # still active
            ).update({
                "locked_until": now + 30 seconds     # push the lease forward
            })
            if updated == 0:
                self.lost_lease = True  # someone stole it — stop renewing
                return
```

**The guard matters:** The update filters on `locked_by == WORKER_ID AND status == "in_flight"`. If the lease was already stolen by another worker (or the delivery finished), zero rows match and the heartbeat stops. Without this guard, a slow-but-alive worker would keep renewing a lease now owned by someone else, making the row permanently un-claimable.

**Usage:**
```python
# Started BEFORE the HTTP request, stopped AFTER
heartbeat = _LeaseHeartbeat(delivery.id)
heartbeat.start()
try:
    response = requests.post(endpoint.url, ...)
finally:
    heartbeat.stop()
```

---

## Concept 5: Exponential Backoff with One-Directional Jitter

### The Problem
100 deliveries fail simultaneously (endpoint goes down). If they all retry after exactly 10 seconds, they all hit the endpoint at the same moment — making the outage worse. This is called a **retry storm**.

### How HookRelay Solves It

```python
# worker/backoff.py
BASE_SCHEDULE = [10, 30, 60, 120, 300]  # seconds
JITTER_RATIO = 0.2

def get_delay_seconds(attempt_count):
    index = min(attempt_count - 1, len(BASE_SCHEDULE) - 1)
    base = BASE_SCHEDULE[index]
    return base + base * random.uniform(0, JITTER_RATIO)  # ← one-directional
```

**Why one-directional jitter (not ±20%)?**

Symmetric jitter: `base ± 20%` → minimum delay = `base * 0.8` — retries can fire **earlier** than the schedule, which re-synchronizes the cohort.

One-directional jitter: `base + 0 to 20%` → minimum delay = `base` — retries always spread **out**, never pull back in.

**Retry schedule for a single delivery:**
```
Attempt 1 fails → retry in 10–12 seconds
Attempt 2 fails → retry in 30–36 seconds
Attempt 3 fails → retry in 60–72 seconds
Attempt 4 fails → retry in 120–144 seconds
Attempt 5 fails → DEAD (dead-lettered)
```

---

## Concept 6: HMAC Payload Signing

### The Problem
If the subscriber's URL is discovered by an attacker, they can send fake events to it pretending to be HookRelay. The subscriber cannot tell the difference.

### How HookRelay Solves It

```python
# worker/signing.py
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

**What gets signed:** `"{timestamp}.{body}"` — not just the body.

**Why include the timestamp in the signed content?**
- Without it: attacker captures a valid `(body, signature)` pair and replays it later. Signature is still valid.
- With it: replaying a captured request fails because the new timestamp does not match the one inside the signature.

**Why put event_id/delivery_id INSIDE the body (not just as headers)?**
```
The signature only covers: "{timestamp}.{body}"
A header is OUTSIDE the signature.
An attacker in the middle could swap a header — signature would still verify.
IDs inside the body are covered by the signature — any tampering breaks verification.
```

**Subscriber verification:**
```python
# How the subscriber verifies (example)
import hmac, hashlib
expected = hmac.new(
    key=b"mysecret",
    msg=f"{received_timestamp}.".encode() + received_body,
    digestmod=hashlib.sha256
).hexdigest()
assert hmac.compare_digest(expected, received_signature)
```

---

## Concept 7: Counting Attempts at Claim, Not Failure

### The Hidden Bug (Important Interview Story)

Original code incremented `attempt_count` only in the failure branch of `deliver()`:
```python
# WRONG: only increments if the worker lives to see the failure
if response.status_code >= 300:
    delivery.attempt_count += 1
```

**The bug:** If the HTTP request causes the worker to crash (segfault, OOM, unhandled exception), the worker never reaches the failure branch. `attempt_count` stays at 0. The sweeper revives the row. The worker crashes again. Forever loop — the delivery **never** dead-letters.

**Fix:** Increment at **claim time**, before the HTTP request:
```python
# worker/worker.py — claim_one_delivery()
delivery.attempt_count += 1   # ← happens BEFORE any HTTP request
delivery.status = "in_flight"
session.commit()
# Now if the worker crashes, attempt_count is already incremented
# The sweeper will eventually check should_give_up(attempt_count) and dead-letter it
```

---

## Concept 8: Enforcing Uniqueness at the Right Level

### The Double-Delivery Bug (Most Interesting Story)

There was a `UniqueConstraint` on `deliveries(event_id, endpoint_id)`. This *looked* like it prevented duplicate deliveries. But:

- `deliveries(event_id, endpoint_id)` only prevents one event from having two delivery rows for the same **endpoint row**
- If someone calls `POST /endpoints` twice with the same URL, two **endpoint rows** are created
- The constraint says nothing about two rows pointing to the same URL
- Every event now fans out to BOTH rows → subscriber gets every event twice

**Fix — constraint at the right level:**
```python
# shared/models.py — Endpoint
__table_args__ = (
    UniqueConstraint("url", "event_type", name="uq_endpoint_url_event_type"),
)
```

Now registering the same URL+event_type twice returns `409 Conflict` at registration time, not a silent duplicate delivery later.

**The lesson:** Database constraints must enforce invariants at the semantic level, not just the structural level.

---

## Concept 9: Replay Without a Second Code Path

### How Replay Works

`POST /deliveries/<id>/replay` brings a dead delivery back to life:
```python
delivery.status = "pending"
delivery.attempt_count = 0
delivery.next_attempt_at = datetime.now(timezone.utc)
delivery.locked_by = None
delivery.locked_until = None
# last_error preserved — so you can still see why it died
session.commit()
```

This is **byte-for-byte what a fresh delivery looks like**. `claim_one_delivery()` in the worker picks it up with zero knowledge that replay exists. No second code path to maintain.

**Guard:** Only `dead` deliveries can be replayed. A `pending` replay would race the worker that owns it. A `succeeded` replay would double-deliver.

---

## Concept 10: Testing the Real Database (Not Mocks)

### Why Real Postgres Tests Matter

The locking guarantees live **inside PostgreSQL's internals**. A mock database would confirm that Python code calls `with_for_update(skip_locked=True)` — not that the lock actually works.

```python
# tests/test_concurrency.py
def test_skip_locked_prevents_double_claim(db_session, another_session):
    # Create a delivery
    delivery = make_delivery(db_session)

    # Session 1 claims it — takes a real row lock in Postgres
    claimed_1 = claim_one_delivery(db_session)
    assert claimed_1.id == delivery.id

    # Session 2 tries to claim — SKIP LOCKED means it gets nothing
    claimed_2 = claim_one_delivery(another_session)
    assert claimed_2 is None   # ← real Postgres lock proved this
```

**Test database:** A separate `hookrelay_test` database is created automatically. Tests truncate tables between runs so they never touch real data.
