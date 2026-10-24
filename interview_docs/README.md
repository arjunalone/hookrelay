# HookRelay — Interview Documentation Index

This folder contains everything you need to understand, explain, and demo the HookRelay project in a job interview. Read in order the first time through; use individual parts as reference later.

---

## Folder Contents

| File | What it covers | Read when... |
|------|---------------|--------------|
| **PART_1_what_is_this_project.md** | Plain-English explanation, the problem it solves, how a single event flows | You need the "tell me about this project" answer |
| **PART_2_architecture_and_design.md** | System diagram, component breakdown, why PostgreSQL over Kafka, delivery state machine | Interviewer asks about architecture or design decisions |
| **PART_3_key_concepts_and_interview_answers.md** | 10 core technical concepts (idempotency, FOR UPDATE, leases, HMAC, jitter, etc.) with code | Deep-dive technical questions |
| **PART_4_code_walkthrough_file_by_file.md** | Every important file explained with code snippets and interview talking points | "Walk me through the code" |
| **PART_5_bugs_found_and_fixed.md** | 5 real bugs with root cause analysis and the fix — ranked by interview value | "Tell me about a bug you found" |
| **PART_6_interview_QnA_cheatsheet.md** | Exact questions you'll be asked + exact answers | Final prep before the interview |
| **PART_7_end_to_end_demo.md** | Every curl command, expected output, and what to say at each step | Live demo or technical screen |
| **PART_8_resume_and_positioning.md** | Resume bullets, LinkedIn description, how to position for different roles | Writing your resume or LinkedIn |

---

## How to Use This

### Night Before the Interview

1. Read **Part 1** — lock in the 60-second opening
2. Read **Part 5** — memorize the top 2 bug stories (double-delivery + forever-looping worker)
3. Read **Part 6** — do the Q&A out loud
4. Run the demo from **Part 7** end-to-end

### Day Of

- Keep **Part 6** open as a reference during the call
- Run `docker compose up -d` before any technical screen

### For Resume

- Copy bullets from **Part 8**
- Match the positioning section to the company's domain

---

## Project at a Glance

```
HookRelay
├── What:       Webhook delivery platform (like Stripe's internal infrastructure)
├── Stack:      Python · Flask · PostgreSQL · SQLAlchemy · Alembic · Docker
├── Core problem: Reliable async delivery with multiple concurrent workers
│
├── Key mechanisms:
│   ├── Fan-out:          1 event → N delivery rows (1 per subscriber)
│   ├── Concurrency:      SELECT FOR UPDATE SKIP LOCKED
│   ├── Crash recovery:   Lease (locked_until / locked_by) + sweeper
│   ├── Slow delivery:    _LeaseHeartbeat renews lease during in-flight HTTP
│   ├── Retries:          Exponential backoff (10/30/60/120/300s) + 1-way jitter
│   ├── Dead-lettering:   Dead after 5 attempts, replayable via API
│   ├── Idempotency:      UNIQUE constraint + replay original result
│   └── Auth:             HMAC-SHA256 sign(timestamp.body) with endpoint secret
│
├── Tests:      56 tests against real Postgres (no mocks)
├── Start:      docker compose up -d
└── Dashboard:  http://localhost:5000/dashboard
```

---

## The 3 Things Every Interviewer Will Ask

1. **"How do you prevent duplicate delivery with multiple workers?"**
   → `SELECT FOR UPDATE SKIP LOCKED` — row-level lock, concurrent workers each skip locked rows

2. **"What happens if a worker crashes?"**
   → Lease (`locked_until`) expires → sweeper sees it → resets to `retrying` with backoff

3. **"How does idempotency work?"**
   → `UNIQUE constraint` on `idempotency_key` blocks the second insert; `IntegrityError` handler returns 200 with the original `event_id` instead of 409

---

## One-Page Summary for Quick Review

**What it is:** Webhook delivery platform — publishers POST events, workers deliver to subscribers

**Why it's interesting:** Distributed failure handling, not just CRUD
- Multiple workers, no duplicates (FOR UPDATE SKIP LOCKED)
- Crash recovery without a separate queue (leases + sweeper)
- Retry storms prevented (exponential backoff + one-directional jitter)
- Forgery prevented (HMAC-SHA256 with timestamp)
- Publisher retries safe (idempotency key + replay original result)

**Best bug story:** UniqueConstraint on `deliveries(event_id, endpoint_id)` looked like it prevented double-delivery. It didn't — it only stopped one event from having two delivery rows for the same endpoint *row*. Two rows for the same URL satisfied the constraint and caused silent double-delivery. Fixed by adding the constraint on `endpoints(url, event_type)`.

**Stack:**
- Flask (API), Python workers, PostgreSQL, SQLAlchemy, Alembic, Docker Compose
- 56 pytest tests against real Postgres

**Run it:** `docker compose up -d` then `curl http://localhost:5000/health`
 1. API route — where the event enters
POST /events → create_event() at api/routes/events.py:36 (blueprint registered in api/app.py:27).
- In: JSON body {event_type, payload, idempotency_key}.
- Validation gate: validate_event_payload() (api/schemas.py:27) — required fields present, payload is an object.
- Out: 202 Accepted with event_id + subscribers_notified.
- If it fails: 400 and nothing touches the DB.
2. Database models — what's stored
shared/models.py:
- Endpoint (:20) — id, url, secret, event_type; unique (url, event_type).
- Event (:46) — id, event_type, payload (JSONB), idempotency_key (unique), created_at.
- Delivery (:62) — event_id FK, endpoint_id FK, status, attempt_count, next_attempt_at, locked_until, locked_by, last_status_code, last_error; unique (event_id, endpoint_id).
- States: pending → in_flight → succeeded or → retrying → in_flight … → dead (:70-71).
3. Transaction — what must commit before responding
api/routes/events.py:47-79: insert Event → flush() to get its id → find matching Endpoints → insert one Delivery per endpoint → one commit(), and only then reply 202.
- If it fails: IntegrityError → if it was a duplicate idempotency key, replay the original result as 200 duplicate (:90-105); otherwise 500 (:107-114). Never a half-saved event.
4. Worker loop — find and claim work
worker/worker.py:258 run_worker_loop polls every 2s; each pass calls sweep_expired_leases() then claim_one_delivery().
- Claim (:115): SELECT … FOR UPDATE SKIP LOCKED where status IN (pending, retrying) and next_attempt_at <= now; sets in_flight, locked_by, locked_until = now+30s, and increments attempt_count right at claim (:135), then commits.
- If it fails: SKIP LOCKED means two workers never grab the same row.
5. HTTP client — where the request is actually sent
worker/worker.py:172 requests.post(endpoint.url, data=body, …).
- Body built at :157 (event_id, delivery_id, event_type, payload), signed by sign_payload() (worker/signing.py:19) → X-HookRelay-Timestamp + X-HookRelay-Signature. A heartbeat thread (:169) keeps the lease alive while the request is in flight.
6. Failure handler — who decides to retry, and when
worker/backoff.py: should_give_up() (attempt_count >= MAX_ATTEMPTS = 5) and get_delay_seconds() (schedule 10/30/60/120/300s + upward-only jitter).
- Branches: non-2xx at worker.py:189-201; network exception at :203-212.
- Out: succeeded, retrying (+next_attempt_at), or dead.
- Gap: no 4xx-vs-5xx split today — that's the permanent-failure issue from your last message.
7. Recovery logic — if a worker crashes
Leases do the recovery. locked_until = now+30s (:129); the heartbeat renews every LEASE_SECONDS//3 = 10s (:31).
- If a worker dies, sweep_expired_leases() (:219) reclaims in_flight rows with locked_until < now (FOR UPDATE SKIP LOCKED), then either dead-letters (:243) or sets retrying with backoff (:247-253).
- Why it's safe: attempt_count is counted at claim time, so a job that reliably crashes a worker still has a bounded budget and can't loop forever.
The through-line: Postgres is the source of truth; the API replies only post-commit, workers claim via row locks + leases, and every failure mode (bad input, duplicate, crash, dead subscriber) has a defined terminal state.