"""
PATCH/DELETE /endpoints/<id>.

Both routes exist because the alternative was a direct psql update, which is
exactly the kind of thing the API and dashboard should cover. DELETE in
particular has two behaviours worth pinning down: it refuses while a delivery
is in flight, and it reports how much history it removed.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from shared.db import get_session
from shared.models import Delivery, Endpoint


def register(client, url, event_type="order.created", secret="s3cret"):
    return client.post("/endpoints", json={
        "url": url, "event_type": event_type, "secret": secret,
    })


# ---------------------------------------------------------------- PATCH

def test_patch_rotates_a_secret_without_touching_the_url(db, client):
    """The main real use: a subscriber changes its shared secret."""
    created = register(client, "https://example.com/hook").get_json()

    response = client.patch(
        f"/endpoints/{created['id']}",
        json={"secret": "rotated-secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["updated"] is True

    session = get_session()
    endpoint = session.get(Endpoint, uuid.UUID(created["id"]))
    assert endpoint.secret == "rotated-secret"
    assert endpoint.url == "https://example.com/hook", "url must not be blanked"
    assert endpoint.event_type == "order.created"
    session.close()


def test_patch_can_change_the_url(db, client):
    created = register(client, "https://example.com/old").get_json()

    response = client.patch(
        f"/endpoints/{created['id']}", json={"url": "https://example.com/new"}
    )

    assert response.status_code == 200
    assert response.get_json()["url"] == "https://example.com/new"


def test_patch_onto_a_taken_url_event_type_pair_is_rejected(db, client):
    """Otherwise a patch re-creates the Milestone 8 double-delivery bug."""
    register(client, "https://example.com/a", event_type="order.created")
    other = register(client, "https://example.com/b", event_type="order.created").get_json()

    response = client.patch(
        f"/endpoints/{other['id']}",
        json={"url": "https://example.com/a", "event_type": "order.created"},
    )

    assert response.status_code == 409
    assert "already registered" in response.get_json()["error"]


def test_patch_rejects_an_empty_body(db, client):
    """A no-op must not be reported as a successful update."""
    created = register(client, "https://example.com/hook").get_json()
    response = client.patch(f"/endpoints/{created['id']}", json={})
    assert response.status_code == 400
    assert "at least one" in response.get_json()["error"]


def test_patch_rejects_unknown_fields(db, client):
    """Silently ignoring a typo'd field name is how you lose an afternoon."""
    created = register(client, "https://example.com/hook").get_json()
    response = client.patch(
        f"/endpoints/{created['id']}", json={"ur1": "https://typo.example.com"}
    )
    assert response.status_code == 400
    assert "unknown field" in response.get_json()["error"]


def test_patch_rejects_a_non_http_url(db, client):
    """A patch must not bypass the rule POST enforces."""
    created = register(client, "https://example.com/hook").get_json()
    response = client.patch(
        f"/endpoints/{created['id']}", json={"url": "ftp://example.com/hook"}
    )
    assert response.status_code == 400


def test_patch_404s_and_400s_correctly(db, client):
    assert client.patch(
        f"/endpoints/{uuid.uuid4()}", json={"secret": "x"}
    ).status_code == 404
    assert client.patch(
        "/endpoints/not-a-uuid", json={"secret": "x"}
    ).status_code == 400


# --------------------------------------------------------------- DELETE

def test_delete_removes_the_endpoint(db, client):
    created = register(client, "https://example.com/hook").get_json()

    response = client.delete(f"/endpoints/{created['id']}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["deleted"] is True
    assert body["deliveries_deleted"] == 0

    assert client.get("/endpoints").get_json() == []


def test_delete_also_removes_delivery_history(db, client):
    """The deliveries have a FK to endpoints, so they must go with it.

    Also the reason the count is in the response: unsubscribing destroys
    the record of what was sent, and the caller deserves to know how much.
    """
    register(client, "https://example.com/hook")
    post = client.post("/events", json={
        "idempotency_key": f"del-{uuid.uuid4()}",
        "event_type": "order.created",
        "payload": {"order_id": 1},
    })
    event_id = uuid.UUID(post.get_json()["event_id"])

    endpoint_id = client.get("/endpoints").get_json()[0]["id"]
    response = client.delete(f"/endpoints/{endpoint_id}")

    assert response.get_json()["deliveries_deleted"] == 1

    session = get_session()
    assert session.query(Delivery).filter_by(event_id=event_id).count() == 0
    # The event itself survives — only the delivery to this endpoint went.
    from shared.models import Event
    assert session.query(Event).filter_by(id=event_id).count() == 1
    session.close()


def test_delete_refuses_while_a_delivery_is_in_flight(db, client):
    """Deleting under an active worker would orphan a row it still owns."""
    register(client, "https://example.com/hook")
    post = client.post("/events", json={
        "idempotency_key": f"del-{uuid.uuid4()}",
        "event_type": "order.created",
        "payload": {"order_id": 1},
    })
    event_id = uuid.UUID(post.get_json()["event_id"])

    session = get_session()
    session.query(Delivery).filter_by(event_id=event_id).update({"status": "in_flight"})
    session.commit()
    session.close()

    endpoint_id = client.get("/endpoints").get_json()[0]["id"]
    response = client.delete(f"/endpoints/{endpoint_id}")

    assert response.status_code == 409
    assert "in flight" in response.get_json()["error"]

    # And the endpoint is still there.
    assert len(client.get("/endpoints").get_json()) == 1


def test_delete_allows_terminal_deliveries(db, client):
    """succeeded/dead/pending are all safe to remove — nothing is mid-flight."""
    register(client, "https://example.com/hook")
    post = client.post("/events", json={
        "idempotency_key": f"del-{uuid.uuid4()}",
        "event_type": "order.created",
        "payload": {"order_id": 1},
    })
    event_id = uuid.UUID(post.get_json()["event_id"])

    session = get_session()
    session.query(Delivery).filter_by(event_id=event_id).update({"status": "dead"})
    session.commit()
    session.close()

    endpoint_id = client.get("/endpoints").get_json()[0]["id"]
    assert client.delete(f"/endpoints/{endpoint_id}").status_code == 200


def test_delete_404s_and_400s_correctly(db, client):
    assert client.delete(f"/endpoints/{uuid.uuid4()}").status_code == 404
    assert client.delete("/endpoints/not-a-uuid").status_code == 400


def test_delete_is_not_idempotently_silent(db, client):
    """Deleting twice must 404 the second time, not claim success."""
    created = register(client, "https://example.com/hook").get_json()
    assert client.delete(f"/endpoints/{created['id']}").status_code == 200
    assert client.delete(f"/endpoints/{created['id']}").status_code == 404
