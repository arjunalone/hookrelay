"""
Replay guards on POST /deliveries/<id>/replay.

Replay is an operator escape hatch for a delivery that already dead-lettered.
It must be tightly fenced: replaying a healthy delivery would double-deliver
to a subscriber who may well have processed the original.
"""

import uuid

from shared.db import get_session
from shared.models import Delivery


def get_delivery(event_id):
    session = get_session()
    delivery = session.query(Delivery).filter_by(event_id=event_id).one()
    session.close()
    return delivery


def test_replay_rejects_a_succeeded_delivery(db, make_endpoint, make_event):
    """Fencing guard: never double-deliver something already accepted."""
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint], status="succeeded")
    delivery = get_delivery(event.id)

    from api.app import create_app
    app = create_app()
    with app.test_client() as client:
        response = client.post(f"/deliveries/{delivery.id}/replay")

    assert response.status_code == 409, (
        "replaying a succeeded delivery would re-notify the subscriber"
    )


def test_replay_rejects_a_live_in_flight_delivery(db, make_endpoint, make_event):
    """A worker may be holding it right now; replay would race with it."""
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint], status="in_flight")
    delivery = get_delivery(event.id)

    from api.app import create_app
    app = create_app()
    with app.test_client() as client:
        response = client.post(f"/deliveries/{delivery.id}/replay")

    assert response.status_code == 409


def test_replay_resets_a_dead_delivery(db, make_endpoint, make_event):
    """The happy path: a dead delivery becomes deliverable again."""
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint], status="dead")
    delivery = get_delivery(event.id)
    delivery.attempt_count = 5
    delivery.last_error = "HTTP 500"
    db.commit()

    from api.app import create_app
    app = create_app()
    with app.test_client() as client:
        response = client.post(f"/deliveries/{delivery.id}/replay")

    assert response.status_code == 200

    session = get_session()
    refreshed = session.query(Delivery).filter_by(id=delivery.id).one()
    assert refreshed.status == "pending"
    assert refreshed.attempt_count == 0, (
        "attempt_count not reset, so the replay dies immediately at the cap"
    )
    assert refreshed.locked_by is None
    assert refreshed.locked_until is None
    session.close()


def test_replay_rejects_a_malformed_id(client):
    assert client.post("/deliveries/not-a-uuid/replay").status_code == 400


def test_replay_404s_on_an_unknown_but_valid_id(client):
    assert client.post(
        f"/deliveries/{uuid.uuid4()}/replay"
    ).status_code == 404
