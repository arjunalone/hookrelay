"""
MODULE: worker/worker.py
WHAT IT DOES: Polls the `deliveries` table, atomically CLAIMS one row
              per worker using row-level locking (SKIP LOCKED), sends
              the HTTP POST, then updates status - now with exponential
              backoff and dead-lettering on repeated failure.
PROBLEM SOLVED: (3c) prevents two workers from claiming the same row.
                 (3d) stops instant-forever-retry loops by spacing out
                 retries and giving up after MAX_ATTEMPTS.
DATA SOURCE: Delivery/Event/Endpoint in Postgres. Delay/give-up logic
             comes from worker/backoff.py (pure functions, no DB access).
"""

import os
import time
import uuid
import requests
import json
import threading
from datetime import datetime, timezone, timedelta
from shared.db import get_session
from shared.models import Delivery, Event, Endpoint
from worker.backoff import get_delay_seconds, should_give_up
from worker.signing import sign_payload

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
LEASE_SECONDS = 30
SWEEP_BATCH = 50  # cap on rows reclaimed per sweep, so one pass can't lock the table
# Renew the lease this often while a delivery is in flight. A third of the
# lease means two consecutive missed beats are still survivable.
HEARTBEAT_INTERVAL = LEASE_SECONDS // 3


class _LeaseHeartbeat:
    """
    Keeps `locked_until` moving forward while a delivery is in flight.

    WHY THIS IS NEEDED, since the HTTP timeout looks like it already covers
    it: `requests`' timeout=10 is a per-socket-OPERATION timeout, not a
    total deadline for the request. A subscriber that replies with one byte
    every 9 seconds keeps the connection alive indefinitely, so a single
    delivery can run far past LEASE_SECONDS. The sweeper would then see an
    expired lease, hand the job to a second worker, and the event goes out
    twice while the first request is still in flight.

    The renewal is filtered by `locked_by = us AND status = 'in_flight'`
    on purpose. If our lease was already stolen and reassigned, zero rows
    match and the heartbeat stops, rather than renewing a lease that now
    belongs to someone else — which would keep the job permanently
    un-sweepable while neither worker could claim it.

    Each thread opens its OWN session: SQLAlchemy sessions are not
    thread-safe, and the main session is busy holding the delivery.
    """

    def __init__(self, delivery_id, interval=HEARTBEAT_INTERVAL):
        self.delivery_id = delivery_id
        self.interval = interval
        self.lost_lease = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="lease-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)

    def _run(self):
        while not self._stop.wait(self.interval):
            session = get_session()
            try:
                updated = (
                    session.query(Delivery)
                    .filter(
                        Delivery.id == self.delivery_id,
                        Delivery.locked_by == WORKER_ID,
                        Delivery.status == "in_flight",
                    )
                    .update(
                        {
                            "locked_until": datetime.now(timezone.utc)
                            + timedelta(seconds=LEASE_SECONDS)
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if updated == 0:
                    self.lost_lease = True
                    print(
                        f"[WARN] ({WORKER_ID}) {self.delivery_id} lost its lease "
                        "mid-delivery; another worker may retry it"
                    )
                    return
            except Exception as exc:
                session.rollback()
                # A transient DB blip must not kill a delivery already in
                # flight. The lease is 30s, so a single missed beat is
                # survivable; several in a row is the sweeper's problem to
                # recover from, which it already does.
                print(
                    f"[WARN] ({WORKER_ID}) lease heartbeat failed for "
                    f"{self.delivery_id}: {exc}"
                )
            finally:
                session.close()


def claim_one_delivery(session):
    now = datetime.now(timezone.utc)
    delivery = (
        session.query(Delivery)
        .filter(Delivery.status.in_(["pending", "retrying"]))
        .filter(Delivery.next_attempt_at <= now)
        .with_for_update(skip_locked=True)
        .first()
    )
    if delivery is None:
        return None

    delivery.status = "in_flight"
    delivery.locked_by = WORKER_ID
    delivery.locked_until = now + timedelta(seconds=LEASE_SECONDS)
    # Counted HERE, at claim time, not in deliver()'s failure branches.
    # A worker that dies mid-delivery never reaches those branches, so
    # counting only there meant a job that reliably crashed the worker could
    # be reclaimed forever and never dead-letter. One count per claim makes
    # crashes and ordinary failures share the same bounded budget.
    delivery.attempt_count += 1
    session.commit()
    return delivery


def deliver(session, delivery):
    event = session.get(Event, delivery.event_id)
    endpoint = session.get(Endpoint, delivery.endpoint_id)

    try:
        # Build the exact bytes we're sending, so what we sign is
        # byte-for-byte what the subscriber will hash on their end.
        #
        # The event and delivery IDs go INSIDE the body on purpose.
        # sign_payload() only signs "{timestamp}.{body}", so an ID sent as
        # a bare header would sit outside the signature and could be swapped
        # by anyone in the middle. Inside the body, the IDs are covered by
        # the subscriber's own signature check.
        #
        # This is what makes at-least-once delivery usable: a subscriber that
        # gets redelivered after a worker crash can see the SAME event_id
        # both times and discard the duplicate itself.
        body = json.dumps({
            "event_id": str(event.id),
            "delivery_id": str(delivery.id),
            "event_type": event.event_type,
            "payload": event.payload,
        }).encode()

        signature_headers = sign_payload(endpoint.secret, body)

        # A slow-but-alive subscriber must not have its job swept out from
        # under it mid-request. See _LeaseHeartbeat for why the HTTP timeout
        # does not already cover this.
        heartbeat = _LeaseHeartbeat(delivery.id)
        heartbeat.start()
        try:
            response = requests.post(
                endpoint.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    # Convenience mirrors for subscribers. The authoritative,
                    # signed copies are the ones in the body above.
                    "X-HookRelay-Event-Id": str(event.id),
                    "X-HookRelay-Delivery-Id": str(delivery.id),
                    **signature_headers,
                },
                timeout=10,
            )
        finally:
            heartbeat.stop()
        delivery.last_status_code = response.status_code

        if 200 <= response.status_code < 300:
            delivery.status = "succeeded"
            print(f"[OK] ({WORKER_ID}) {delivery.id} -> {endpoint.url} ({response.status_code})")
        else:
            delivery.last_error = f"HTTP {response.status_code}"
            if should_give_up(delivery.attempt_count):
                delivery.status = "dead"
                print(f"[DEAD] ({WORKER_ID}) {delivery.id} -> {endpoint.url} (gave up after {delivery.attempt_count} attempts)")
            else:
                delay = get_delay_seconds(delivery.attempt_count)
                delivery.status = "retrying"
                delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                print(f"[FAIL] ({WORKER_ID}) {delivery.id} -> {endpoint.url} ({response.status_code}), retry in {delay:.0f}s")

    except requests.exceptions.RequestException as e:
        delivery.last_error = str(e)
        if should_give_up(delivery.attempt_count):
            delivery.status = "dead"
            print(f"[DEAD] ({WORKER_ID}) {delivery.id} -> {endpoint.url} (gave up after {delivery.attempt_count} attempts)")
        else:
            delay = get_delay_seconds(delivery.attempt_count)
            delivery.status = "retrying"
            delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            print(f"[ERROR] ({WORKER_ID}) {delivery.id} -> {endpoint.url} ({e}), retry in {delay:.0f}s")

    delivery.locked_by = None
    delivery.locked_until = None
    session.commit()


def sweep_expired_leases(session):
    now = datetime.now(timezone.utc)
    # FOR UPDATE SKIP LOCKED so N workers sweeping at once don't all pile
    # onto the same expired row. Postgres re-checks the WHERE clause after
    # the lock is granted, so a row whose lease was renewed in the meantime
    # is skipped rather than stolen from the worker still holding it.
    # Bounded so a large backlog can't lock the whole table in one pass.
    stuck = (
        session.query(Delivery)
        .filter(Delivery.status == "in_flight")
        .filter(Delivery.locked_until < now)
        .with_for_update(skip_locked=True)
        .limit(SWEEP_BATCH)
        .all()
    )
    for d in stuck:
        print(f"[SWEEP] Reclaiming expired lease on {d.id} (was locked by {d.locked_by})")
        d.locked_by = None
        d.locked_until = None

        # The give-up check has to live HERE, not just in deliver(). The
        # sweep is what resurrects a crashed job, so it's the only place a
        # delivery that never reached a failure branch can be given up on.
        # Without this, a job that keeps killing the worker loops forever.
        if should_give_up(d.attempt_count):
            d.status = "dead"
            print(f"[DEAD] ({WORKER_ID}) {d.id} gave up after {d.attempt_count} crashed attempts")
        else:
            d.status = "retrying"
            # Crashes back off too. A crash is a local fault (bad deploy,
            # OOM), and instantly re-claiming a job that reliably kills the
            # worker just kills it again.
            d.next_attempt_at = now + timedelta(
                seconds=get_delay_seconds(d.attempt_count)
            )
    if stuck:
        session.commit()


def run_worker_loop():
    print(f"Worker {WORKER_ID} started. Polling every 2 seconds...")
    while True:
        session = get_session()
        try:
            sweep_expired_leases(session)
            delivery = claim_one_delivery(session)
            if delivery is None:
                print(f"({WORKER_ID}) No deliveries ready.")
            else:
                deliver(session, delivery)
        finally:
            session.close()
        time.sleep(2)


if __name__ == "__main__":
    run_worker_loop()