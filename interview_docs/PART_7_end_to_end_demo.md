# PART 7 — End-to-End Demo Walkthrough

This is a complete guided demo you can run live in an interview or walk through verbally. Every command is real and tested.

---

## Prerequisites

```bash
cd ~/hookrelay
docker compose up -d          # starts Postgres, API (port 5000), and Worker
docker compose ps             # verify: db, api, worker all "Up"
```

---

## Step 1: Register a Subscriber Endpoint

```bash
curl -s -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://webhook.site/YOUR-UUID-HERE",
    "secret": "my-super-secret",
    "event_type": "order.created"
  }' | python3 -m json.tool
```

**Expected response:**
```json
{
  "id": "3f7a2b1c-...",
  "url": "https://webhook.site/YOUR-UUID",
  "event_type": "order.created",
  "created_at": "2026-10-06T09:30:00+00:00"
}
```

**What happened:**
- A row was inserted into the `endpoints` table
- Any event with `event_type = "order.created"` will now fan out a delivery to this URL

---

## Step 2: Publish an Event

```bash
curl -s -X POST http://127.0.0.1:5000/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "order.created",
    "payload": {"order_id": 42, "total": 99.99, "customer": "Alice"},
    "idempotency_key": "order-42-v1"
  }' | python3 -m json.tool
```

**Expected response (202 Accepted):**
```json
{
  "event_id": "7a3bc9f2-...",
  "subscribers_notified": 1,
  "duplicate": false
}
```

**What happened in one transaction:**
1. `events` table: 1 new row saved
2. `deliveries` table: 1 new row (status=`pending`) for each subscriber

---

## Step 3: Watch the Worker Deliver It

```bash
docker compose logs -f worker
```

**Expected output:**
```
Worker worker-a3f2bc9d started. Polling every 2 seconds...
(worker-a3f2bc9d) No deliveries ready.
[OK] (worker-a3f2bc9d) 9b2a1c3d-... -> https://webhook.site/... (200)
```

**On webhook.site you will see the POST with:**
```json
{
  "event_id": "7a3bc9f2-...",
  "delivery_id": "9b2a1c3d-...",
  "event_type": "order.created",
  "payload": {
    "order_id": 42,
    "total": 99.99,
    "customer": "Alice"
  }
}
```

**Plus these headers:**
```
X-HookRelay-Timestamp: 1728227400
X-HookRelay-Signature: sha256=abc123...
X-HookRelay-Event-Id: 7a3bc9f2-...
X-HookRelay-Delivery-Id: 9b2a1c3d-...
```

---

## Step 4: Test Idempotency

Send the **exact same request** again:

```bash
curl -s -X POST http://127.0.0.1:5000/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "order.created",
    "payload": {"order_id": 42, "total": 99.99, "customer": "Alice"},
    "idempotency_key": "order-42-v1"
  }' | python3 -m json.tool
```

**Expected response (200, not 202 or 409):**
```json
{
  "event_id": "7a3bc9f2-...",
  "subscribers_notified": 1,
  "duplicate": true
}
```

**webhook.site receives ZERO new requests** — the subscriber is not notified again.

---

## Step 5: Test Duplicate Subscriber Registration

Try to register the same URL twice:

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://webhook.site/YOUR-UUID-HERE",
    "secret": "different-secret",
    "event_type": "order.created"
  }'
# Returns: 409
```

Same URL, **different** event_type — should work:

```bash
curl -s -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://webhook.site/YOUR-UUID-HERE",
    "secret": "my-super-secret",
    "event_type": "order.shipped"
  }' | python3 -m json.tool
# Returns: 201 — different event type is allowed
```

---

## Step 6: Simulate a Failure and Watch Retries

Register a **broken subscriber URL**:

```bash
curl -s -X POST http://127.0.0.1:5000/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:9999",
    "secret": "test-secret",
    "event_type": "payment.failed"
  }' | python3 -m json.tool

# Publish an event to it
curl -s -X POST http://127.0.0.1:5000/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "payment.failed",
    "payload": {"payment_id": "pay_001"},
    "idempotency_key": "fail-demo-1"
  }'
```

**Watch the worker:**
```bash
docker compose logs -f worker
```

You will see:
```
[ERROR] (worker-...) ... -> http://localhost:9999 (Connection refused), retry in 10s
[ERROR] (worker-...) ... -> http://localhost:9999 (Connection refused), retry in 33s
[ERROR] (worker-...) ... -> http://localhost:9999 (Connection refused), retry in 61s
[ERROR] (worker-...) ... -> http://localhost:9999 (Connection refused), retry in 122s
[DEAD]  (worker-...) ... -> http://localhost:9999 (gave up after 5 attempts)
```

---

## Step 7: Check the Delivery Status

```bash
# See all deliveries
curl -s "http://127.0.0.1:5000/deliveries" | python3 -m json.tool

# See only dead ones
curl -s "http://127.0.0.1:5000/deliveries?status=dead" | python3 -m json.tool

# Get summary stats
curl -s "http://127.0.0.1:5000/deliveries/stats" | python3 -m json.tool
```

**Expected stats:**
```json
{
  "pending": 0,
  "in_flight": 0,
  "succeeded": 1,
  "retrying": 0,
  "dead": 1,
  "total": 2
}
```

---

## Step 8: Replay a Dead Delivery

Get the dead delivery's ID from Step 7, then:

```bash
# "Fix" the endpoint (register a working URL this time, or imagine you fixed your server)

# Replay the dead delivery
curl -s -X POST "http://127.0.0.1:5000/deliveries/DEAD-DELIVERY-UUID-HERE/replay" \
  | python3 -m json.tool
```

**Expected:**
```json
{
  "id": "DEAD-DELIVERY-UUID-HERE",
  "status": "pending",
  "replayed": true
}
```

Watch the worker pick it up and deliver it — `attempt_count` correctly resets to 0 and goes to 1 on the next claim.

---

## Step 9: View the Dashboard

Open in browser: **http://localhost:5000/dashboard**

Shows:
- Live delivery counts by status
- Table of recent deliveries with endpoint URL, status, attempt count, last error
- (Replay button not yet wired in the UI — use the API)

Also: **http://localhost:5000** — home page with a working test-event form.

---

## Step 10: Verify HMAC Signature

On webhook.site, inspect the headers of the received request:

```
X-HookRelay-Timestamp: 1728227400
X-HookRelay-Signature: abc123def456...
```

Locally verify:
```python
import hmac, hashlib, json

secret = "my-super-secret"
timestamp = "1728227400"
body = json.dumps({
    "event_id": "7a3bc9f2-...",
    "delivery_id": "9b2a1c3d-...",
    "event_type": "order.created",
    "payload": {"order_id": 42, "total": 99.99, "customer": "Alice"}
})

signed_content = f"{timestamp}.{body}".encode()
expected = hmac.new(
    key=secret.encode(),
    msg=signed_content,
    digestmod=hashlib.sha256
).hexdigest()

print(expected == "abc123def456...")   # True
```

---

## Demo Teardown

```bash
docker compose down        # stop everything
docker compose down -v     # stop AND wipe the database (clean slate)
```

---

## What to Say at Each Step

| Step | What to say |
|------|------------|
| 1    | "I'm registering a subscriber — this is the URL that will receive the webhook" |
| 2    | "Publishing an event. The 202 response comes back immediately — my app isn't blocked" |
| 3    | "The worker picked it up in the background and delivered it. See the HMAC headers on webhook.site" |
| 4    | "Same idempotency_key — returns 200 with the original event_id, subscriber gets nothing new" |
| 5    | "Second registration of the same URL returns 409. Different event_type returns 201" |
| 6    | "Watch the retries grow: 10s, 33s, 61s — that's the backoff schedule with jitter" |
| 7    | "The stats endpoint is what the dashboard reads — real-time queue health" |
| 8    | "Replay resets the row to pending. The worker picks it up with no knowledge that replay exists" |
| 10   | "The HMAC verification proves the payload is authentic. This is exactly how Stripe signing works" |
