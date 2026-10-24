"""
Lease heartbeat: a slow-but-alive delivery must not be swept mid-request.

The hazard this guards against is easy to miss. `requests`' timeout is a
per-socket-operation timeout, not a total deadline, so a subscriber that
trickles one byte at a time can hold a request open indefinitely. Without a
heartbeat the sweeper eventually reclaims that job and a second worker
delivers the same event again, while the first request is still running.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from shared.db import get_session
from shared.models import Delivery
from worker.worker import (
    HEARTBEAT_INTERVAL,
    LEASE_SECONDS,
    _LeaseHeartbeat,
    claim_one_delivery,
    sweep_expired_leases,
)


def claimed_delivery(db, make_endpoint, make_event):
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery_id = db.query(Delivery).filter_by(event_id=event.id).one().id

    session = get_session()
    try:
        claimed = claim_one_delivery(session)
        assert claimed is not None
    finally:
        session.close()
    db.expire_all()
    return delivery_id


def test_heartbeat_interval_leaves_room_for_missed_beats():
    """Must beat at least twice per lease, or one hiccup loses the job."""
    assert 0 < HEARTBEAT_INTERVAL < LEASE_SECONDS
    assert LEASE_SECONDS / HEARTBEAT_INTERVAL >= 2


def test_heartbeat_extends_the_lease(db, make_endpoint, make_event):
    delivery_id = claimed_delivery(db, make_endpoint, make_event)

    before = db.query(Delivery).filter_by(id=delivery_id).one().locked_until

    heartbeat = _LeaseHeartbeat(delivery_id, interval=0.05)
    heartbeat.start()
    time.sleep(0.4)
    heartbeat.stop()

    after = db.query(Delivery).filter_by(id=delivery_id).one().locked_until
    assert after > before, "heartbeat did not push locked_until forward"
    assert heartbeat.lost_lease is False


def test_heartbeat_stops_renewing_once_the_lease_is_stolen(
    db, make_endpoint, make_event
):
    """The critical guard: don't renew a lease that now belongs to someone else.

    Without the `locked_by = us` filter, a worker that is merely slow would
    keep extending a lease another worker has already taken — the job
    becomes permanently un-sweepable while neither worker will claim it.
    """
    delivery_id = claimed_delivery(db, make_endpoint, make_event)

    # Simulate the sweeper handing the job to a different worker.
    row = db.query(Delivery).filter_by(id=delivery_id).one()
    row.locked_by = "some-other-worker"
    db.commit()

    heartbeat = _LeaseHeartbeat(delivery_id, interval=0.05)
    heartbeat.start()
    time.sleep(0.4)
    heartbeat.stop()

    after = db.query(Delivery).filter_by(id=delivery_id).one()
    assert heartbeat.lost_lease is True, "heartbeat kept a stolen lease alive"
    assert after.locked_by == "some-other-worker", "heartbeat overwrote the new owner"


def test_heartbeat_does_not_touch_a_finished_delivery(
    db, make_endpoint, make_event
):
    """Once delivered, the lease is released; the heartbeat must not revive it."""
    delivery_id = claimed_delivery(db, make_endpoint, make_event)

    row = db.query(Delivery).filter_by(id=delivery_id).one()
    row.status = "succeeded"
    row.locked_by = None
    row.locked_until = None
    db.commit()

    heartbeat = _LeaseHeartbeat(delivery_id, interval=0.05)
    heartbeat.start()
    time.sleep(0.3)
    heartbeat.stop()

    after = db.query(Delivery).filter_by(id=delivery_id).one()
    assert after.status == "succeeded"
    assert after.locked_until is None, "heartbeat re-leased a completed delivery"


def test_lease_keeps_moving_forward_and_survives_the_sweeper(
    db, make_endpoint, make_event
):
    """End-to-end: a long delivery keeps its lease, so the sweeper leaves it alone.

    Note what this does NOT do: artificially backdate locked_until and then
    sweep. That races the next heartbeat beat and would fail for a reason
    that can't happen in production — a real heartbeat renews well before
    the lease could expire. What matters is that the lease is pushed
    forward repeatedly, which is what the sampling below checks.
    """
    delivery_id = claimed_delivery(db, make_endpoint, make_event)

    def lease_now():
        return db.query(Delivery).filter_by(id=delivery_id).one().locked_until

    first = lease_now()

    heartbeat = _LeaseHeartbeat(delivery_id, interval=0.05)
    heartbeat.start()
    try:
        time.sleep(0.35)
        second = lease_now()
        time.sleep(0.35)
        third = lease_now()
    finally:
        heartbeat.stop()

    assert second > first, "lease was not renewed"
    assert third > second, "renewal only happened once, not continuously"

    # Well ahead of now, so the sweeper has no reason to touch it.
    assert third > datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS - 5)

    sweep_expired_leases(db)

    after = db.query(Delivery).filter_by(id=delivery_id).one()
    assert after.status == "in_flight", "sweeper reclaimed a live, heartbeating job"
    assert after.attempt_count == 1, "a live delivery must not burn an attempt"


def test_heartbeat_stops_promptly(db, make_endpoint, make_event):
    """stop() must not leave a thread running past the delivery."""
    delivery_id = claimed_delivery(db, make_endpoint, make_event)
    before_threads = threading.active_count()

    heartbeat = _LeaseHeartbeat(delivery_id, interval=0.05)
    heartbeat.start()
    time.sleep(0.15)
    heartbeat.stop()

    time.sleep(0.15)
    assert threading.active_count() <= before_threads, (
        "heartbeat thread outlived stop()"
    )
