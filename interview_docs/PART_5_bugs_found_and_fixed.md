# PART 5 — Real Bugs Found & Fixed (The Best Interview Stories)

These are real bugs that were discovered while building the project — not theoretical. Each one demonstrates the kind of reasoning that distinguishes backend engineers who have shipped production systems.

---

## Bug 1 — The Double-Delivery That Looked Impossible

### Setup
There was a `UniqueConstraint("event_id", "endpoint_id")` on the `deliveries` table.
This *looked like* it prevented any event from being delivered twice to any subscriber.

### How the Bug Appeared
A user registered the same webhook URL twice (calling `POST /endpoints` twice).
After publishing a single event, the subscriber received **two notifications** — the exact thing the constraint was supposed to prevent.

### Root Cause
The constraint says: *one event can only have one delivery row per endpoint **row***.
But two endpoint rows existed for the same URL. The constraint was satisfied (each row had one delivery), but the subscriber URL received the event twice.

```
endpoints table:
  row A: url="https://my-service.com/hook", event_type="order.created"
  row B: url="https://my-service.com/hook", event_type="order.created"  ← duplicate

When order.created fires:
  delivery for (event_1, endpoint_A) → HTTP POST to my-service.com  ← 1st delivery
  delivery for (event_1, endpoint_B) → HTTP POST to my-service.com  ← 2nd delivery

UniqueConstraint satisfied ✓ — but subscriber got two requests ✗
```

### Fix

Added the constraint at the **correct** semantic level — on `endpoints`:
```python
# shared/models.py
__table_args__ = (
    UniqueConstraint("url", "event_type", name="uq_endpoint_url_event_type"),
)
```

Now `POST /endpoints` with the same `(url, event_type)` returns `409 Conflict`.
The same URL can still subscribe to *different* event types — that's a valid use case.

### The Lesson
**Enforce uniqueness at the level where the actual duplication happens, not at a structural level that looks similar.**

---

## Bug 2 — The Worker That Looped Forever

### Setup
`attempt_count` was incremented inside the failure branch of `deliver()`:
```python
# ORIGINAL (buggy)
if response.status_code >= 300:
    delivery.attempt_count += 1
    if should_give_up(delivery.attempt_count):
        delivery.status = "dead"
```

### How the Bug Appeared
A specific delivery was causing the worker process to crash (unhandled exception in the HTTP request stack). The sweeper would revive it. The worker would crash again. Repeat. The delivery **never dead-lettered** — it looped forever.

### Root Cause
A crashed worker never reaches the failure branch. `attempt_count` stayed at 0 forever because the increment was inside code that never executed when the worker died.

```
T+0:  Worker claims delivery, attempt_count = 0
T+1:  Worker crashes (unhandled exception)
T+35: Sweeper sees expired lease, attempt_count is STILL 0
T+35: Sweeper: should_give_up(0) → False → status = "retrying"
T+37: Worker claims it again, attempt_count = 0
T+38: Worker crashes again
(infinite loop)
```

### Fix

Move `attempt_count += 1` to **claim time**, before any HTTP request happens:
```python
# worker/worker.py — claim_one_delivery()
delivery.attempt_count += 1   # ← happens before HTTP, crash-safe
delivery.status = "in_flight"
session.commit()              # ← persisted to DB before anything can crash
```

Now even if the worker crashes, the claim is recorded. The sweeper sees `attempt_count = 1` and applies the backoff schedule. After 5 crashes, `should_give_up(5)` is True and the delivery is dead-lettered.

Also added give-up logic to the sweeper itself:
```python
# worker/worker.py — sweep_expired_leases()
if should_give_up(d.attempt_count):
    d.status = "dead"   # ← sweeper can dead-letter too, not just deliver()
```

### The Lesson
**Any counter that tracks "how many times has something gone wrong" must be incremented at the point where you can no longer recover from the wrong thing happening — not inside the recovery handler.**

---

## Bug 3 — The Slow Subscriber That Caused Duplicate Delivery

### Setup
Lease is 30 seconds. `requests.timeout=10`. It seemed like deliveries could never take longer than 10 seconds.

### How the Bug Appeared (Theoretical, Then Verified)
`requests.timeout=10` is a **per-socket-read timeout** — it fires if 10 seconds pass *without receiving any new data*. A subscriber that sends one byte every 9 seconds satisfies the condition continuously. The overall request duration is unbounded.

A subscriber trickling data could hold the connection for 45 seconds. At T+30s, the sweeper would see the expired lease and hand the delivery to Worker 2. Worker 1 is still running. **Both workers deliver the same event**.

### Fix

The `_LeaseHeartbeat` class renews `locked_until` every 10 seconds while the request is in flight:
```python
class _LeaseHeartbeat:
    def _run(self):
        while not self._stop.wait(10):  # every 10 seconds
            updated = session.query(Delivery).filter(
                Delivery.id == self.delivery_id,
                Delivery.locked_by == WORKER_ID,   # still ours
                Delivery.status == "in_flight",     # still active
            ).update({"locked_until": now + 30s})

            if updated == 0:
                self.lost_lease = True
                return  # lease was stolen — stop renewing
```

**The guard on `locked_by == WORKER_ID AND status == "in_flight"` is critical.** Without it:
- Worker 1 loses its lease (dies / sweeper reassigns)
- Worker 2 claims the delivery
- Worker 1's heartbeat is still running (Worker 1 is slow, not dead)
- Worker 1's heartbeat keeps renewing the lock that now belongs to Worker 2
- Worker 2 can never sweep its own lease if it crashes
- Delivery is permanently stuck

With the guard, when zero rows match, the heartbeat stops. Worker 2 owns the lock cleanly.

### Verified Live
- Subscriber configured to send 1 byte every 9 seconds for 45 seconds
- Two competing workers running
- Result: `status=succeeded`, `attempt_count=1`, Worker 2 never started a sweep
- Lease was observed being pushed forward repeatedly: 22s → 25s → 27s → 20s → 23s

### The Lesson
**HTTP timeouts do not cap total request duration. If you are using leases for crash recovery, you must also renew them during long in-flight operations.**

---

## Bug 4 — The Retry That Fired Too Early

### Setup
Jitter was symmetric: `base ± 20%`:
```python
# ORIGINAL (buggy)
return base + base * random.uniform(-0.2, 0.2)
```

### The Problem
With `base = 10s` and uniform jitter `±20%`, retries could fire at `10 * 0.8 = 8s`.

If 100 deliveries all fail at the same time (endpoint goes down), they all have `base = 10s`. With symmetric jitter, roughly half will get delays around 8–10s — pulling the cohort back into near-synchronization. The retry storm you were trying to avoid is only partially spread out.

### Fix
One-directional jitter — always add, never subtract:
```python
# worker/backoff.py
return base + base * random.uniform(0, 0.2)   # minimum = base, maximum = base * 1.2
```

Now retries always wait *at least* the scheduled time. The cohort can only spread out, never collapse back.

### The Lesson
**Jitter exists to spread out synchronized retries. Symmetric jitter partially defeats its own purpose by allowing early retries that re-synchronize the cohort.**

---

## Bug 5 — The Idempotency Key That Left Publishers Stuck

### Setup
Original behavior: duplicate `idempotency_key` → `409 Conflict`.

### The Problem
A publisher sends `POST /events` with `idempotency_key: "order-42"`. Network drops. They never see the response. They retry.

They receive `409 Conflict`. Now what?
- They cannot learn what `event_id` was created
- They cannot safely assume the event was processed (maybe it failed mid-fan-out)
- They cannot send a new event with a different key (that's a different event)
- They are stuck

### Fix
Return the original response when the key already exists:
```python
# api/routes/events.py
existing = session.query(Event).filter(Event.idempotency_key == data["idempotency_key"]).one_or_none()
if existing is not None:
    return jsonify({
        "event_id": str(existing.id),
        "subscribers_notified": notified,
        "duplicate": True,
    }), 200
```

The publisher receives the same `event_id` they would have gotten the first time, plus a `duplicate: true` flag so they know it was a replay. They can verify the event was processed or take any other action.

### The Lesson
**Idempotency is not just about preventing duplicate processing. It's about giving callers a way to recover from uncertainty. A `409` leaves a publisher with no recovery path.**

---

## Summary: The Bug Stories, Ranked by Interview Value

| Bug | Interview Value | Why |
|-----|----------------|-----|
| Double-delivery (wrong constraint level) | ⭐⭐⭐⭐⭐ | Shows understanding of semantic vs structural correctness |
| Forever-looping crashed worker | ⭐⭐⭐⭐⭐ | Shows understanding of distributed failure modes |
| Slow-subscriber lease expiry | ⭐⭐⭐⭐ | Shows understanding of timeout subtleties + threading |
| Symmetric jitter retry storm | ⭐⭐⭐ | Shows mathematical reasoning about distributed systems |
| 409 idempotency dead end | ⭐⭐⭐ | Shows API design thinking from the client's perspective |

---

## How to Tell These Stories in an Interview

**Opener:** "One bug I'm proud of finding..."

**Structure:**
1. What I expected to happen (based on the code I wrote)
2. What actually happened (the symptoms I observed)
3. Why the code was wrong (the actual root cause)
4. How I fixed it
5. What I verified

**Example:**
> "I had a UniqueConstraint on `deliveries(event_id, endpoint_id)` and assumed that prevented double delivery. But when I tested the full flow, a subscriber was getting the same event twice. It took me a while to realize the constraint was at the wrong level — it only prevented one event from having two delivery rows for the same endpoint **row**, not for the same endpoint **URL**. Two rows with the same URL satisfied the constraint perfectly while causing exactly the duplication I was trying to prevent. The fix was adding the constraint on `endpoints(url, event_type)` — enforcing uniqueness where the semantic problem actually lived."
