"""
Idempotent event creation at the API boundary (P1).

The publisher-facing guarantee: POSTing the same idempotency_key twice must
return the SAME event_id and must not fan out a second round of deliveries.

Returning 409 was wrong here. A publisher that lost the response to its
first POST has no way to recover - it either retries into a permanent 409
or, worse, invents a new key and delivers the event twice. Replaying the
original result is what makes at-least-once publishing safe.
"""

import uuid

from shared.db import get_session
from shared.models import Delivery, Endpoint, Event


def register_endpoint(client, url, event_type="order.created", secret="s3cret"):
    return client.post("/endpoints", json={
        "url": url,
        "event_type": event_type,
        "secret": secret,
    })


def post_event(client, key, event_type="order.created", payload=None):
    return client.post("/events", json={
        "idempotency_key": key,
        "event_type": event_type,
        "payload": payload if payload is not None else {"order_id": 1},
    })


def test_first_post_creates_the_event(client):
    response = post_event(client, f"key-{uuid.uuid4()}")
    assert response.status_code == 202
    body = response.get_json()
    assert body["duplicate"] is False
    assert body["event_id"]


def test_duplicate_key_replays_the_original_result(client):
    """The P1 fix: 200 + the original event_id, not 409."""
    key = f"key-{uuid.uuid4()}"

    first = post_event(client, key)
    assert first.status_code == 202
    original_id = first.get_json()["event_id"]

    second = post_event(client, key)
    assert second.status_code == 200, (
        f"expected 200 replay, got {second.status_code} - "
        "a publisher cannot recover from a 409 here"
    )
    body = second.get_json()
    assert body["duplicate"] is True
    assert body["event_id"] == original_id, "replay returned a different event_id"


def test_duplicate_key_does_not_fan_out_a_second_time(client):
    """The real harm a duplicate caused: the subscriber got told twice."""
    register_endpoint(client, "https://example.com/hook")
    key = f"key-{uuid.uuid4()}"

    post_event(client, key)
    session = get_session()
    after_first = session.query(Delivery).count()
    assert after_first == 1, "expected the registered endpoint to be notified once"
    session.close()

    post_event(client, key)
    session = get_session()
    assert session.query(Delivery).count() == after_first, (
        "replay created extra deliveries - subscribers will see duplicates"
    )
    assert session.query(Event).filter_by(idempotency_key=key).count() == 1
    session.close()


def test_replayed_response_reports_the_original_delivery_count(client):
    """subscribers_notified must describe reality, not echo the request."""
    register_endpoint(client, "https://example.com/hook")
    key = f"key-{uuid.uuid4()}"

    post_event(client, key)
    second = post_event(client, key)

    assert second.get_json()["subscribers_notified"] == 1


def test_distinct_keys_still_create_distinct_events(client):
    """Idempotency must not collapse genuinely different events."""
    first = post_event(client, f"key-{uuid.uuid4()}")
    second = post_event(client, f"key-{uuid.uuid4()}")

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.get_json()["event_id"] != second.get_json()["event_id"]


def test_many_repeats_collapse_to_one_event(client):
    """A publisher hammering retry after a timeout must not multiply events."""
    key = f"key-{uuid.uuid4()}"

    for _ in range(10):
        post_event(client, key)

    session = get_session()
    assert session.query(Event).filter_by(idempotency_key=key).count() == 1
    session.close()


def test_duplicate_endpoint_registration_is_rejected(client):
    """Uniqueness is per (url, event_type), not per url."""
    url = "https://example.com/hook"

    first = register_endpoint(client, url)
    assert first.status_code == 201

    duplicate = register_endpoint(client, url)
    assert duplicate.status_code == 409

    # Same URL, different event type is a legitimately different subscriber.
    other_type = register_endpoint(client, url, event_type="order.shipped")
    assert other_type.status_code == 201

    session = get_session()
    assert session.query(Endpoint).filter_by(url=url).count() == 2
    session.close()
