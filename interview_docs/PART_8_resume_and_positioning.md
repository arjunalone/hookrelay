# PART 8 — Resume Bullet Points & Project Description

Copy-paste ready text for your resume, LinkedIn, and project descriptions.

---

## Resume Section: Projects

### Option A (Concise, 2-3 bullets)

**HookRelay** — Webhook Delivery Platform | Python, Flask, PostgreSQL, Docker
- Built a reliable webhook relay system that fans events out to registered subscribers with at-least-once delivery guarantees, using PostgreSQL row-level locking (`SELECT FOR UPDATE SKIP LOCKED`) to safely coordinate multiple concurrent worker processes without duplicate deliveries
- Implemented exponential backoff with one-directional jitter, lease-based crash recovery, and HMAC-SHA256 payload signing — the same patterns used by Stripe and GitHub's webhook infrastructure
- Wrote 56 automated tests against a real PostgreSQL test database (not mocks) covering concurrent claiming, crash recovery, idempotency, and signature verification

---

### Option B (Detailed, 4-5 bullets)

**HookRelay** — Reliable Webhook Delivery Platform | Python, Flask, PostgreSQL, SQLAlchemy, Alembic, Docker

- Built a production-grade webhook delivery system: publishers POST events to a REST API that immediately responds `202 Accepted` and fans out delivery tasks in Postgres; background workers claim tasks using `SELECT FOR UPDATE SKIP LOCKED` and deliver via HTTP POST to each registered subscriber
- Solved concurrent job claiming without Redis or RabbitMQ — used PostgreSQL's native `FOR UPDATE SKIP LOCKED` to ensure multiple worker instances never process the same delivery row, and lease-based crash recovery to handle worker failures without data loss
- Implemented HMAC-SHA256 payload authentication: each outbound delivery is signed with `timestamp.body` using the subscriber's secret, with IDs inside the signed body (not headers) to prevent forgery in transit — identical in design to Stripe's webhook signing scheme
- Found and fixed 5 real bugs during development including: a UniqueConstraint at the wrong database level causing silent double-delivery, attempt counting in the failure branch missing crashed workers causing infinite retry loops, and symmetric jitter re-synchronizing retry storms
- Built a failure visibility dashboard (`GET /deliveries?status=dead`, `GET /deliveries/stats`, `POST /deliveries/<id>/replay`) and a 56-test suite covering concurrent locking, idempotency, crash recovery, backoff timing, and lease heartbeat behavior

---

## LinkedIn / Portfolio Description

> **HookRelay — A Reliable Webhook Delivery Platform**
>
> Built to learn and demonstrate the backend reliability patterns that power Stripe's, GitHub's, and Shopify's webhook infrastructure.
>
> When your app publishes an event (e.g. `order.created`), HookRelay fans it out to every registered subscriber URL in the background — immediately returning `202 Accepted` so your checkout flow isn't blocked. Workers deliver in parallel, retry on failure with exponential backoff, and dead-letter after 5 attempts. Dead deliveries are replayable via a REST API once the subscriber is fixed.
>
> **Technical highlights:**
> - Safe concurrent delivery with PostgreSQL `FOR UPDATE SKIP LOCKED` — no Redis, no external queue
> - Lease-based crash recovery with a lease heartbeat to handle slow subscribers
> - Idempotent event ingestion — duplicate submissions replay the original result, not a 409
> - HMAC-SHA256 payload signing with timestamp to prevent replay attacks
> - 56 automated tests against a real PostgreSQL test database
> - Full Docker containerization — `docker compose up` starts everything
>
> **Stack:** Python, Flask, PostgreSQL, SQLAlchemy, Alembic, Docker

---

## GitHub Repository Description (one line)

> Reliable webhook delivery platform with fan-out, exponential backoff, crash recovery, and HMAC signing — the same patterns used by Stripe and GitHub internally.

---

## Tags / Skills to Add to Resume/LinkedIn

```
Backend Engineering · Distributed Systems · PostgreSQL · Python · Flask
SQLAlchemy · Alembic · Docker · Webhook Infrastructure · Reliability Engineering
REST API Design · Idempotency · Exponential Backoff · HMAC Authentication
At-least-once Delivery · Concurrent Job Processing · Database Migrations
```

---

## How to Position This Project for Different Roles

### Backend Engineer (General)
Lead with the architecture: "I built a webhook relay system — a distributed background job processor that fans events out to subscribers. The interesting part is the reliability layer: safe concurrent workers, crash recovery via database leases, and exponential backoff."

### Platform / Infrastructure Engineer
Lead with operational concerns: "I chose PostgreSQL over Redis/Kafka deliberately — the per-endpoint delivery tracking is relational, and the fan-out needs to be transactional. I also built a delivery visibility dashboard and replay API so the system is operable in production."

### Payments / Fintech Backend
Lead with Stripe comparison: "The HMAC signing in HookRelay is identical to Stripe's webhook authentication scheme — sign `timestamp.body`, send the signature as a header, subscriber verifies by recomputing. I also implemented the idempotency pattern Stripe uses for payment APIs."

### SDE-2 / Senior Backend
Lead with bugs found: "I found 5 real bugs building this — including a UniqueConstraint at the wrong database level causing silent double-delivery, attempt counting in the failure branch that missed crashed workers, and symmetric jitter re-synchronizing the cohort it was supposed to spread out. Each bug was a lesson in why distributed failure handling is harder than it looks."

---

## Elevator Pitch (30 seconds)

> "I built HookRelay — a webhook delivery system, like what Stripe uses internally. When something happens in your system, you publish an event and get back an immediate 202. Background workers deliver it to each registered subscriber, retry on failure with exponential backoff, and dead-letter after 5 attempts. The interesting part is the reliability: safe concurrent workers with PostgreSQL row locking, crash recovery via leases, HMAC signing, and idempotent delivery. 56 automated tests, all running against a real database. One Docker command to start the whole system."
