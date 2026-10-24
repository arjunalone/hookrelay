"""
Request signing, and the event_id / delivery_id that ride along with it (P0).

The point of putting the IDs INSIDE the body rather than only in headers:
sign_payload() signs "{timestamp}.{body}" and nothing else. A header is
therefore outside the signature and could be swapped in transit. Anything a
subscriber is meant to trust has to be in the body.
"""

import hashlib
import hmac
import json

from shared.models import Delivery
from worker.worker import deliver


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def capture(monkeypatch, status_code=200, exc=None):
    """Intercept the outbound HTTP call and return what would have been sent."""
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
        seen["url"] = url
        seen["body"] = data
        seen["headers"] = headers or {}
        if exc is not None:
            raise exc
        return FakeResponse(status_code)

    monkeypatch.setattr("worker.worker.requests.post", fake_post)
    return seen


def verify(secret, headers, body):
    """Re-implement the subscriber's verification, independently of signing.py."""
    timestamp = headers["X-HookRelay-Timestamp"]
    signature = headers["X-HookRelay-Signature"]
    expected = hmac.new(
        key=secret.encode(),
        msg=timestamp.encode() + b"." + body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def test_signature_verifies_with_the_shared_secret(
    db, make_endpoint, make_event, monkeypatch
):
    endpoint = make_endpoint(secret="subscriber-secret")
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    seen = capture(monkeypatch)
    deliver(db, delivery)

    assert seen, "no outbound request was made"
    assert verify("subscriber-secret", seen["headers"], seen["body"]) is True


def test_signature_rejects_a_wrong_secret(
    db, make_endpoint, make_event, monkeypatch
):
    endpoint = make_endpoint(secret="the-real-secret")
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    seen = capture(monkeypatch)
    deliver(db, delivery)

    assert verify("a-guessed-secret", seen["headers"], seen["body"]) is False


def test_event_and_delivery_ids_are_in_the_signed_body(
    db, make_endpoint, make_event, monkeypatch
):
    """This is what lets a subscriber dedupe redeliveries after a crash."""
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    seen = capture(monkeypatch)
    deliver(db, delivery)

    body = json.loads(seen["body"])
    assert body["event_id"] == str(event.id)
    assert body["delivery_id"] == str(delivery.id)
    # The original fields must survive the change.
    assert body["event_type"] == event.event_type
    assert body["payload"] == event.payload


def test_tampering_with_the_body_breaks_the_signature(
    db, make_endpoint, make_event, monkeypatch
):
    """Proves the IDs are actually COVERED by the signature, not just present.

    Without this, "the ID is in the body" would be a claim rather than a
    guarantee: someone could rewrite the event_id and the signature would
    still check out.
    """
    endpoint = make_endpoint(secret="subscriber-secret")
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    seen = capture(monkeypatch)
    deliver(db, delivery)

    tampered = json.loads(seen["body"])
    tampered["event_id"] = "00000000-0000-0000-0000-000000000000"
    tampered_bytes = json.dumps(tampered).encode()

    assert verify("subscriber-secret", seen["headers"], tampered_bytes) is False


def test_delivery_succeeds_and_is_recorded(
    db, make_endpoint, make_event, monkeypatch
):
    endpoint = make_endpoint()
    event = make_event(endpoints=[endpoint])
    delivery = db.query(Delivery).filter_by(event_id=event.id).one()

    capture(monkeypatch, status_code=204)
    deliver(db, delivery)
    db.refresh(delivery)

    assert delivery.status == "succeeded"
    assert delivery.last_status_code == 204
    # attempt_count is bumped at claim time, not by deliver(), so a direct
    # call to deliver() must not inflate it.
    assert delivery.attempt_count == 0
