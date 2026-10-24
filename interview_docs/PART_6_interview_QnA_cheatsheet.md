# PART 6 — Interview Q&A Cheat Sheet

Exact questions you will be asked and exactly how to answer them.

---

## Section A: "Tell Me About This Project" (Opening)

### Q: Walk me through HookRelay in 60 seconds.

> "HookRelay is a webhook delivery system — the same infrastructure that Stripe and GitHub use to notify external services when something happens. Publishers send events to a REST API; the API saves them and fans out delivery tasks in Postgres; background workers pick up those tasks and send HTTP POST requests to each subscriber's URL. The interesting engineering is what happens when things go wrong: retries with exponential backoff, dead-lettering after 5 failures, crash recovery via database leases, and HMAC signing so subscribers can verify authenticity. Everything runs in Docker — one `docker compose up` starts Postgres, the API, and the workers."

---

### Q: Why did you build this instead of a CRUD app?

> "I wanted to work on a real distributed systems problem where the interesting engineering lives in the failure handling, not the happy path. With a CRUD app, everything either works or returns an error. With a webhook system, the failure modes are: what if the subscriber is slow? What if the worker crashes mid-delivery? What if two workers both try to deliver the same event? What if the publisher retries? Each of those has a specific engineering answer — leases, idempotency, SELECT FOR UPDATE SKIP LOCKED — and I wanted to implement and test all of them."

---

## Section B: Core Technical Questions

### Q: How do you prevent two workers from delivering the same event?

> "PostgreSQL's `SELECT FOR UPDATE SKIP LOCKED`. When a worker queries for a pending delivery, it adds `FOR UPDATE` which takes a row-level write lock, and `SKIP LOCKED` which tells Postgres to skip any row that already has a lock held by another transaction. So Worker 1 gets row #7, Worker 2 gets row #8 — they can never both claim the same row. This is built into Postgres and requires no external infrastructure."

**Follow-up — "What if a worker crashes after claiming but before delivering?"**

> "That's covered by leases. At claim time, the worker sets `locked_until = now + 30 seconds` and `locked_by = worker-id`. A sweeper runs every poll cycle and looks for rows where `status = 'in_flight' AND locked_until < now`. If the worker crashed, the lease expires and the sweeper revives the delivery with a backoff."

---

### Q: How do you handle idempotency?

> "Two layers. First, the `events` table has a UNIQUE constraint on `idempotency_key`, so Postgres rejects the second insert at the database level — no application logic needed. Second, when the IntegrityError fires, instead of returning 409, I look up the original event and return 200 with the original event_id and a `duplicate: true` flag. A publisher that lost a response can retry safely — they get back the same data they would have gotten the first time."

**Follow-up — "Why 200 and not 409?"**

> "A 409 leaves the publisher with no recovery path. They don't know what event_id was created, they can't safely assume delivery happened, and they can't send a new key because that's a different event. A 200 with `duplicate: true` is what real idempotency looks like — the caller can retry as many times as they want and always get a useful response."

---

### Q: How does your exponential backoff work?

> "There's a schedule: 10 seconds, 30 seconds, 60 seconds, 120 seconds, 300 seconds — indexed by attempt number. On top of the base delay I add one-directional jitter: a random value between 0 and 20% of the base. One-directional is important — symmetric jitter can produce a delay shorter than the base, which re-synchronizes a cohort of retries that all failed at the same time. One-directional jitter always adds, so retries can only spread out, never collapse back into a storm."

---

### Q: How does your HMAC signing work?

> "Each endpoint has a secret set at registration. When the worker sends a delivery, it signs `timestamp.body` with that secret using HMAC-SHA256. The subscriber receives the signature and the timestamp as headers, and verifies by recomputing the HMAC with the same secret. Including the timestamp in the signed content prevents replay attacks — an attacker can capture a valid `(body, signature)` pair but cannot reuse it later because the timestamp is part of what was signed."

**Follow-up — "Why are the event_id and delivery_id in the body, not just in headers?"**

> "The signature only covers the body. A header is outside the signature, so anything in a header could be swapped in transit and the signature would still verify. Putting the IDs inside the body means any tampering with them breaks signature verification. The headers are there as a convenience for subscribers, but the authoritative, signed copies are in the body."

---

### Q: How does your crash recovery work?

> "Three mechanisms. First, at claim time — not at failure time — I increment `attempt_count` and persist it to the database before making the HTTP request. If the worker crashes, the count is already recorded. Second, a lease sweeper runs every poll cycle and identifies `in_flight` rows whose `locked_until` has passed — those are crashed or stalled workers. Third, the sweeper applies the same give-up logic as normal failure: if `attempt_count >= 5`, mark it `dead`; otherwise set it to `retrying` with a backoff delay."

---

### Q: How do you prevent a slow subscriber from causing duplicate delivery?

> "With a lease heartbeat. The issue is that `requests.timeout=10` is a per-socket-read timeout, not a total deadline — a subscriber sending one byte every 9 seconds holds the connection indefinitely. I start a background thread that renews the lease every 10 seconds while the HTTP request is in flight. The renewal is filtered by `locked_by == WORKER_ID AND status == 'in_flight'` — if those don't match, the heartbeat stops. This prevents a heartbeat from renewing a lease that was legitimately stolen by another worker."

---

### Q: Why did you use PostgreSQL as your job queue instead of Redis or RabbitMQ?

> "The data is fundamentally relational — I need to track delivery state per (event, endpoint) pair, and the event and its deliveries need to be created in the same transaction. With a separate queue system, the event creation and the queue insertion would be in separate transactions, which means they can diverge. Postgres solves this with ACID guarantees and `FOR UPDATE SKIP LOCKED`. For the throughput this project targets, Postgres is entirely sufficient. I would consider Kafka if I needed millions of events per second or cross-datacenter pub/sub — but adding infrastructure complexity to solve a problem I don't have yet is the wrong call."

---

### Q: How does your replay feature work?

> "When a delivery is dead-lettered, an operator can send `POST /deliveries/<id>/replay`. That route resets the row to `status='pending'`, `attempt_count=0`, `next_attempt_at=now` — byte-for-byte what a fresh delivery looks like. The worker picks it up on its next poll with no knowledge that replay exists. The guard is important: only `dead` deliveries can be replayed. A `pending` replay would race the worker that already owns it; a `succeeded` replay would double-deliver. And `last_error` is preserved, so the reason for the original failure is still visible after the replay."

---

## Section C: Design Decisions

### Q: Why Flask and not FastAPI or Django?

> "The goal was to demonstrate backend reliability patterns, not web framework features. Flask's simplicity means there's no magic between my code and what happens — every route is explicit. For a project where the interesting engineering is in the database and worker layers, a minimal web layer is the right choice. If I were building a consumer API with request validation schemas or async database access, FastAPI would be worth the complexity."

---

### Q: How does your test suite work?

> "56 tests against a separate `hookrelay_test` database in the same Docker container — never touches the real data. Tests truncate tables between runs via a pytest fixture. The key design choice is that the concurrency tests take real row locks in Postgres rather than mocking the database. Mocking would confirm that my Python code *calls* `FOR UPDATE SKIP LOCKED` — not that the lock *works*. I wanted tests that would catch a Postgres bug or a wrong SQLAlchemy flag, not just a Python logic bug."

---

### Q: What are the known limitations?

> "No circuit breaker — a repeatedly failing endpoint still consumes worker capacity on every event; it should eventually be paused automatically. No API authentication — anyone who can reach the port can publish events. The polling loop processes one delivery per iteration with a 2-second sleep, so throughput caps at about one delivery per worker per 2 seconds. `LISTEN/NOTIFY` is the natural upgrade to replace polling with database push notifications. And it hasn't been deployed to cloud yet — that's next."

---

### Q: What would you change if this were going to production?

> "Several things: API keys for publishers before exposing the port publicly. A circuit breaker to auto-pause repeatedly failing endpoints. LISTEN/NOTIFY to replace the 2-second poll loop. A batch claim loop to process multiple deliveries per cycle. Proper metrics — queue depth, success rate, p99 delivery latency — so reliability can be tracked over time. And an actual deployment pipeline with Docker in a cloud VM and CI/CD on GitHub Actions."

---

## Section D: Behavioral / Process Questions

### Q: What was the hardest bug to find?

> "The double-delivery bug. I had a UniqueConstraint on `deliveries(event_id, endpoint_id)` and thought it prevented duplicate deliveries. It took me a while to realize the constraint was correct but at the wrong level. It only prevented one event from having two delivery rows for the same endpoint *row*. Two endpoint rows with the same URL satisfied the constraint perfectly — and caused exactly the duplication I thought I'd prevented. The fix was moving the constraint to `endpoints(url, event_type)`. The lesson is that correctness-by-constraint requires the constraint to live where the semantic problem actually is."

---

### Q: What would you add next?

> "In order of value: First, a circuit breaker — a dead endpoint burning worker time on every event is a real operational problem. Second, an auth layer — API keys before any port exposure. Third, metrics — without queue depth and delivery latency, you cannot tell if the system is healthy. Fourth, a batch claim loop and LISTEN/NOTIFY for throughput. And then full cloud deployment with GitHub Actions CI/CD."

---

### Q: Why is this project more impressive than a CRUD app?

> "It demonstrates failure handling, which is where most backend engineering work actually lives. The happy path of a CRUD app is trivial. What's interesting is: what happens when a worker crashes mid-delivery? What happens when a subscriber is unreachable? What happens when a publisher retries? What happens when two workers both try to claim the same job? Each answer requires a specific engineering mechanism — and I've implemented and tested all of them. This is closer to what backend engineers at Stripe or GitHub actually spend their time on than a basic CRUD API."

---

## Quick Reference — Terms to Know Cold

| Term | One-Sentence Definition |
|------|------------------------|
| Webhook | An HTTP callback from a server to a URL when an event occurs |
| Fan-out | Creating one delivery task per subscriber for a single event |
| At-least-once delivery | Every event is delivered at least once; duplicates are possible |
| Exactly-once delivery | Every event is delivered exactly once; much harder, not attempted here |
| Idempotency | The same operation, applied multiple times, has the same effect as once |
| Idempotency key | A client-supplied unique ID that prevents duplicate event processing |
| SELECT FOR UPDATE SKIP LOCKED | Postgres row locking that allows concurrent workers without blocking |
| Lease | A time-limited claim on a job row — survives worker crashes |
| Exponential backoff | Retry delays that grow exponentially (10s, 30s, 60s...) to reduce load |
| Jitter | Randomness added to retry delays to prevent synchronized retry storms |
| Dead letter | A delivery that has exhausted its retry budget — preserved for manual inspection |
| Replay | Manually resetting a dead delivery to pending for another attempt |
| HMAC | Hash-based Message Authentication Code — used to sign payloads |
| TIMESTAMPTZ | PostgreSQL column type that stores timezone-aware timestamps |
| Alembic | Python database migration tool — tracks schema changes as versioned files |
