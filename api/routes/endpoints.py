"""
US-1.1: As a Publisher/Developer, I want to register an endpoint URL
so HookRelay knows where to deliver events.

Also PATCH/DELETE, because without them a broken subscriber can only be
fixed by opening psql — which is exactly the kind of thing the dashboard
and the API should cover.
"""

import uuid

from flask import Blueprint, request, jsonify
from sqlalchemy.exc import IntegrityError
from api.extensions import get_session
from api.schemas import (
    validate_endpoint_payload,
    validate_endpoint_patch,
)
from shared.models import Delivery, Endpoint

endpoints_bp = Blueprint("endpoints", __name__)


@endpoints_bp.route("/endpoints", methods=["POST"])
def create_endpoint():
    data = request.get_json(silent=True)

    is_valid, error = validate_endpoint_payload(data)
    if not is_valid:
        return jsonify({"error": error}), 400

    session = get_session()
    try:
        new_endpoint = Endpoint(
            url=data["url"],
            secret=data["secret"],
            event_type=data["event_type"],
        )
        session.add(new_endpoint)
        session.commit()

        return jsonify({
            "id": str(new_endpoint.id),
            "url": new_endpoint.url,
            "event_type": new_endpoint.event_type,
        }), 201

    except IntegrityError:
        # uq_endpoint_url_event_type in the DB. This fires if the same
        # url + event_type pair is registered twice. Failing here means the
        # caller learns about it at registration time, instead of each
        # event later being delivered to the same subscriber twice.
        session.rollback()
        return jsonify({
            "error": "endpoint already registered for this url and event_type"
        }), 409

    finally:
        session.close()


@endpoints_bp.route("/endpoints", methods=["GET"])
def list_endpoints():
    session = get_session()
    try:
        all_endpoints = session.query(Endpoint).all()
        return jsonify([
            {
                "id": str(e.id),
                "url": e.url,
                "event_type": e.event_type,
            }
            for e in all_endpoints
        ])
    finally:
        session.close()


def _parse_endpoint_id(raw):
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


@endpoints_bp.route("/endpoints/<endpoint_id>", methods=["PATCH"])
def update_endpoint(endpoint_id):
    """
    Update a subscriber in place.

    The real driver here is SECRET ROTATION: a subscriber needs to change
    its shared secret without losing its delivery history, and the only way
    to do that before this route existed was to register a new endpoint (a
    second row for the same URL) and delete the old one by hand.
    """
    parsed = _parse_endpoint_id(endpoint_id)
    if parsed is None:
        return jsonify({"error": "endpoint id must be a valid UUID"}), 400

    data = request.get_json(silent=True)
    is_valid, error = validate_endpoint_patch(data)
    if not is_valid:
        return jsonify({"error": error}), 400

    session = get_session()
    try:
        endpoint = session.get(Endpoint, parsed)
        if endpoint is None:
            return jsonify({"error": "endpoint not found"}), 404

        # Only assign keys that were actually sent, so a caller patching just
        # the secret doesn't blank out the url or event_type.
        for field in ("url", "secret", "event_type"):
            if field in data:
                setattr(endpoint, field, data[field])

        try:
            session.commit()
        except IntegrityError:
            # Moving the endpoint onto a (url, event_type) pair that another
            # endpoint already occupies would violate uq_endpoint_url_event_type
            # and create the exact double-delivery bug Milestone 8 fixed.
            session.rollback()
            return jsonify({
                "error": "another endpoint is already registered for that url and event_type"
            }), 409

        return jsonify({
            "id": str(endpoint.id),
            "url": endpoint.url,
            "event_type": endpoint.event_type,
            "updated": True,
        })
    finally:
        session.close()


@endpoints_bp.route("/endpoints/<endpoint_id>", methods=["DELETE"])
def delete_endpoint(endpoint_id):
    """
    Remove a subscriber and its delivery history.

    Two deliberate decisions here:

    1. A 409 while anything is in_flight. A worker is actively writing to
       that row right now; deleting underneath it leaves a dangling
       in-flight row and a delivery the worker believes it is still
       responsible for. Refusing is honest; silently orphaning is not.

    2. The delivery history goes with it. The deliveries table has a
       foreign key to endpoints with no ON DELETE CASCADE, so the rows
       have to be removed explicitly in the same transaction. That means
       unsubscribing destroys the record of what was sent to that URL, so
       the count comes back in the response and the caller can't be
       surprised by it.
    """
    parsed = _parse_endpoint_id(endpoint_id)
    if parsed is None:
        return jsonify({"error": "endpoint id must be a valid UUID"}), 400

    session = get_session()
    try:
        endpoint = session.get(Endpoint, parsed)
        if endpoint is None:
            return jsonify({"error": "endpoint not found"}), 404

        in_flight = (
            session.query(Delivery)
            .filter(
                Delivery.endpoint_id == parsed,
                Delivery.status == "in_flight",
            )
            .count()
        )
        if in_flight:
            return jsonify({
                "error": (
                    f"endpoint has {in_flight} delivery(ies) currently in flight; "
                    "wait for them to settle, then retry"
                )
            }), 409

        # Order matters: children before parent, or the foreign key rejects it.
        deleted = (
            session.query(Delivery)
            .filter(Delivery.endpoint_id == parsed)
            .delete(synchronize_session=False)
        )
        session.delete(endpoint)
        session.commit()

        return jsonify({
            "id": str(parsed),
            "deleted": True,
            "deliveries_deleted": deleted,
        })
    finally:
        session.close()