"""
Concurrency: the claim path and FOR UPDATE SKIP LOCKED (P3).

These are the tests that need a REAL database, because the guarantee under
test ("two workers never process the same delivery") lives in Postgres's
locking, not in Python. Mocking the DB would test nothing.

The locking tests below are deterministic rather than timing-based: we take
a genuine row lock on one connection and then prove a second connection
cannot claim that row. A probabilistic "fire 20 threads and hope" test would
pass most of the time even against broken code, which is worse than useless.
"""

import threading

import pytest
from sqlalchemy.exc import IntegrityError

from shared.db import get_session
from shared.models import Delivery, Event
from worker.worker import claim_one_delivery


def test_a_locked_delivery_is_skipped_not_stolen(db, make_endpoint, make_event):
    """The core guarantee: a claimed row is invisible to a second worker.

    Simulates worker A mid-delivery by holding a row lock on its own
    connection. Worker B must skip it and return None rather than either
    blocking on the lock or, worse, processing the same delivery again.
    """
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery_id = db.query(Delivery).filter_by(event_id=event.id).one().id

    # Worker A: takes the row lock, does NOT commit.
    a_session = get_session()
    a_session.query(Delivery).filter(Delivery.id == delivery_id).with_for_update().first()

    b_session = get_session()
    try:
        # Worker B: a different connection. Must skip the locked row.
        assert claim_one_delivery(b_session) is None
    finally:
        b_session.close()
        a_session.rollback()
        a_session.close()

    # Once A releases, the row is claimable again.
    c_session = get_session()
    try:
        claimed = claim_one_delivery(c_session)
        assert claimed is not None
        assert claimed.id == delivery_id
    finally:
        c_session.close()


def test_skip_locked_moves_on_to_the_next_available_row(
    db, make_endpoint, make_event
):
    """SKIP LOCKED should not just block or give up - it should keep working.

    If A is holding row one, B must still make progress on row two. A
    naive SELECT ... FOR UPDATE without SKIP LOCKED would BLOCK here and
    serialise the whole worker fleet behind the slowest delivery.
    """
    endpoint = make_endpoint()
    event_a = make_event(endpoints=[endpoint], payload={"n": 1})
    event_b = make_event(endpoints=[endpoint], payload={"n": 2})
    db.query(Delivery).update({"status": "pending"})
    db.commit()

    locked_id = db.query(Delivery).filter_by(event_id=event_a.id).one().id

    a_session = get_session()
    a_session.query(Delivery).filter(Delivery.id == locked_id).with_for_update().first()

    b_session = get_session()
    try:
        claimed = claim_one_delivery(b_session)
        assert claimed is not None, "B stalled on the locked row instead of skipping it"
        assert claimed.event_id == event_b.id
    finally:
        b_session.close()
        a_session.rollback()
        a_session.close()


def test_claim_marks_in_flight_and_bumps_attempt_count(
    db, make_endpoint, make_event
):
    """P2: the attempt is counted at claim, so crashes are counted too."""
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()
    assert delivery.attempt_count == 0

    claimed = claim_one_delivery(db)
    db.refresh(delivery)

    assert delivery.status == "in_flight"
    assert delivery.attempt_count == 1
    assert delivery.locked_by is not None
    assert delivery.locked_until is not None


def test_parallel_workers_never_double_claim(db, make_endpoint, make_event):
    """Many threads, one shared pool of deliveries, no delivery handed out twice.

    Each thread uses its own session/connection, as real worker processes do.
    """
    endpoint = make_endpoint()
    for i in range(12):
        make_event(endpoints=[endpoint], payload={"n": i})

    delivery_ids = {d.id for d in db.query(Delivery).all()}
    assert len(delivery_ids) == 12

    claimed_ids = []
    lock = threading.Lock()
    start = threading.Barrier(8)
    errors = []

    def worker_thread():
        session = get_session()
        try:
            start.wait(timeout=10)
            while True:
                claimed = claim_one_delivery(session)
                if claimed is None:
                    break
                with lock:
                    claimed_ids.append(claimed.id)
        except Exception as exc:  # surface, don't swallow
            errors.append(repr(exc))
        finally:
            session.rollback()
            session.close()

    threads = [threading.Thread(target=worker_thread) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker threads raised: {errors}"
    assert len(claimed_ids) == len(set(claimed_ids)), "a delivery was claimed twice"
    assert set(claimed_ids) == delivery_ids, "some deliveries were never claimed"


def test_duplicate_endpoint_fanout_is_rejected_by_the_db(db, make_endpoint, make_event):
    """The UNIQUE constraint is the last line of defence against double delivery.

    A bug in application code could try to create the same (event, endpoint)
    delivery twice; the database refuses, so the subscriber is not sent
    two copies because of a lost race.
    """
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    db.add(Delivery(event_id=event.id, endpoint_id=endpoint.id, status="pending"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.query(Delivery).filter_by(event_id=event.id).count() == 1
