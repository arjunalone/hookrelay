# PART 1 — What Is HookRelay? (Plain English for Interviews)

---

## One-Line Answer

> **HookRelay is a webhook delivery platform** — it reliably sends HTTP notifications to external URLs whenever something happens in your system, like Stripe does when a payment succeeds.

---

## The Real-World Problem It Solves

Imagine you run an e-commerce store. When an order is placed:
- Your **warehouse system** needs to pack it
- Your **email service** needs to confirm to the customer
- Your **analytics platform** needs to record the sale

The naive approach: call each service's API yourself, in sequence.

**Problems with that:**
- If any one of them is slow or down, your checkout freezes
- If your server crashes mid-way, some services got notified, some didn't
- If you retry, services that already got notified get it twice → duplicate orders

**HookRelay solves this** by:
1. Letting external services **register** a URL (subscriber endpoint) to be notified
2. Accepting events from your app with an **instant 202 response** — no blocking
3. **Reliably delivering** those events in the background, with retries, dead-lettering, and duplicate prevention

This pattern is used by Stripe, GitHub, Shopify, Twilio — it's called a **webhook relay** or **event fan-out system**.

---

## How a Single Event Flows (Step by Step)

```
1.  Your app (Publisher) sends:
    POST /events  { event_type: "order.created", payload: {order_id: 42}, idempotency_key: "abc-123" }

2.  API responds 202 immediately — your checkout is not blocked

3.  API saves to Postgres:
    - 1 row in `events` table
    - 1 row in `deliveries` table per registered subscriber (fan-out)

4.  Worker process polls `deliveries` table every 2 seconds

5.  Worker claims a delivery row with SELECT FOR UPDATE SKIP LOCKED
    (so two workers never grab the same row)

6.  Worker sends HTTP POST to subscriber URL with:
    - The event payload
    - HMAC-SHA256 signature (so the subscriber can verify it's really from us)
    - Timestamp (to prevent replay attacks)

7.  If it succeeds → status = "succeeded"
    If it fails → status = "retrying", retry in 10s / 30s / 60s / 120s / 300s
    If it fails 5 times → status = "dead" (dead-lettered)

8.  Dead deliveries can be replayed via: POST /deliveries/<id>/replay
```

---

## What Makes This Different from a Simple CRUD App

| CRUD App                  | HookRelay                              |
|---------------------------|----------------------------------------|
| Creates rows in database  | Creates rows AND ensures reliable delivery |
| No concurrent workers     | Multiple workers, no duplicates        |
| No failure handling       | Retries, dead-lettering, crash recovery |
| No distributed systems    | Leases, SELECT FOR UPDATE, idempotency |
| No security               | HMAC signing, replay protection        |

---

## The Three Tables (Mental Model)

```
endpoints    — WHO wants to be notified
             (url, secret, event_type they are subscribed to)

events       — WHAT happened
             (event_type, payload, when, idempotency_key)

deliveries   — the DELIVERY TASK for each (event, endpoint) pair
             (status: pending → in_flight → succeeded / retrying → dead)
```

---

## Where to Start Reading the Code

| If you want to understand...   | Read this file                       |
|--------------------------------|--------------------------------------|
| Database tables                | `shared/models.py`                   |
| How an event is accepted       | `api/routes/events.py`               |
| How subscribers are managed    | `api/routes/endpoints.py`            |
| How delivery actually happens  | `worker/worker.py`                   |
| Retry timing                   | `worker/backoff.py`                  |
| How signatures work            | `worker/signing.py`                  |
| How the dashboard works        | `api/routes/deliveries.py`           |

---

## Interview One-Liner to Open With

> "HookRelay is a reliable webhook delivery system built with Flask, PostgreSQL, and Python workers. It demonstrates at-least-once delivery semantics, concurrent job claiming with row-level locking, exponential backoff with dead-lettering, and HMAC-based payload authentication — the same patterns Stripe and GitHub use internally for their webhook infrastructure."
