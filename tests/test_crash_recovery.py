"""
Crash recovery (P2).

The scenario: a worker claims a delivery, starts the HTTP request, and then
the process dies. It never runs the code that records success or failure.

Before the fix this was unbounded - the sweeper would resurrect the job
forever and no attempt counter ever moved, because the only increments lived
in the failure branches the crashed worker never reached. These tests pin
down that a delivery now dead-letters after MAX_ATTEMPTS crashes.
"""

from datetime import datetime, timedelta, timezone

from shared.db import get_session
from shared.models import Delivery
from worker.backoff import MAX_ATTEMPTS, should_give_up
from worker.worker import LEASE_SECONDS, claim_one_delivery, sweep_expired_leases


def is_in_future(value):
    """True if `value` is later than now.

    next_attempt_at / locked_until are naive DateTime columns while the
    worker code passes timezone-aware UTC, so the values read back here have
    no tzinfo. Normalising keeps the comparison from raising instead of
    testing what we mean. (The mixed naive/aware usage itself is a known
    wart, tracked separately - it works only because Postgres coerces.)
    """
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > now


def crash_once(db, delivery_id):
    """Claim the delivery, then die: no success, no failure, no release.

    Each crash runs in its own session so the row lock behaves the way it
    would with a genuinely separate worker process.
    """
    session = get_session()
    try:
        # Pretend any backoff has already elapsed. A crashed job is still
        # subject to backoff; this test is about crash COUNTING, not timing.
        session.query(Delivery).filter(Delivery.id == delivery_id).update(
            {"next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        session.commit()

        claimed = claim_one_delivery(session)
        assert claimed is not None, "could not claim the delivery to simulate a crash"

        # The worker dies here. The lease is left behind, already expired,
        # and nothing at all was recorded about the attempt.
        claimed.status = "in_flight"
        claimed.locked_by = "worker-that-died"
        claimed.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    db.expire_all()


def fresh_delivery(db, make_endpoint, make_event):
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    return db.query(Delivery).filter_by(event_id=event.id).one().id


def test_sweep_reclaims_an_expired_lease(db, make_endpoint, make_event):
    delivery_id = fresh_delivery(db, make_endpoint, make_event)

    crash_once(db, delivery_id)
    sweep_expired_leases(db)
    db.refresh(db.query(Delivery).filter_by(id=delivery_id).one())

    delivery = db.query(Delivery).filter_by(id=delivery_id).one()
    assert delivery.status == "retrying"
    assert delivery.locked_by is None
    assert delivery.locked_until is None


def test_sweep_leaves_a_live_lease_alone(db, make_endpoint, make_event):
    """A worker that is merely slow must not have its job stolen."""
    delivery_id = fresh_delivery(db, make_endpoint, make_event)

    session = get_session()
    try:
        claimed = claim_one_delivery(session)
        claimed.locked_until = datetime.now(timezone.utc) + timedelta(
            seconds=LEASE_SECONDS
        )
        session.commit()
    finally:
        session.close()

    sweep_expired_leases(db)
    delivery = db.query(Delivery).filter_by(id=delivery_id).one()
    assert delivery.status == "in_flight", "swept a lease that was still valid"
    assert delivery.locked_by is not None


def test_repeated_crashes_eventually_dead_letter(db, make_endpoint, make_event):
    """The bug this fixes: a job that keeps killing the worker loops forever.

    Every crash is invisible to deliver()'s failure branches, so before the
    fix nothing ever incremented attempt_count and the delivery would be
    reclaimed indefinitely.
    """
    delivery_id = fresh_delivery(db, make_endpoint, make_event)

    for i in range(MAX_ATTEMPTS):
        crash_once(db, delivery_id)
        sweep_expired_leases(db)

        delivery = db.query(Delivery).filter_by(id=delivery_id).one()
        expected = "dead" if should_give_up(delivery.attempt_count) else "retrying"
        assert delivery.status == expected, (
            f"after crash {i + 1} (attempt_count={delivery.attempt_count}) "
            f"expected {expected}, got {delivery.status}"
        )

    assert delivery.status == "dead", (
        f"still {delivery.status} after {MAX_ATTEMPTS} crashes - "
        "crash recovery is not bounded"
    )
    assert delivery.attempt_count == MAX_ATTEMPTS


def test_crashed_retry_is_backed_off_not_hot_looped(db, make_endpoint, make_event):
    """A crash is a local fault, so the retry is delayed like any other failure.

    Instantly re-claiming a job that reliably kills the worker just kills
    the worker again.
    """
    delivery_id = fresh_delivery(db, make_endpoint, make_event)

    crash_once(db, delivery_id)
    sweep_expired_leases(db)

    delivery = db.query(Delivery).filter_by(id=delivery_id).one()
    assert delivery.status == "retrying"
    assert delivery.next_attempt_at is not None
    assert is_in_future(delivery.next_attempt_at), (
        "crashed delivery scheduled immediately - no backoff"
    )


def test_swept_delivery_is_not_reclaimable_until_its_backoff_elapses(
    db, make_endpoint, make_event
):
    """The sweep must not hand the job straight back to a claimant."""
    delivery_id = fresh_delivery(db, make_endpoint, make_event)

    crash_once(db, delivery_id)
    sweep_expired_leases(db)
    db.commit()

    session = get_session()
    try:
        assert claim_one_delivery(session) is None, (
            "delivery was reclaimable immediately, ignoring its backoff"
        )
    finally:
        session.close()
