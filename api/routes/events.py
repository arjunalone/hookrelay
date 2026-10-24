"""
US-1.2: As a Publisher, I want to send an event via POST /events and
get an immediate acknowledgment, so my system isn't blocked waiting
for delivery.

US-1.3: As a Publisher, I want to send the same event twice safely
(idempotency key) so network retries on my side don't cause duplicate
processing.

This is the most important route in the project: it must save the
event AND create all its deliveries in one transaction, and only
reply 202 after that transaction is committed. If anything fails,
nothing is half-saved.
"""

import logging
from flask import Blueprint, request, jsonify, render_template
from sqlalchemy.exc import IntegrityError
from api.extensions import get_session
from api.schemas import validate_event_payload
from shared.models import Event, Endpoint, Delivery

app_logger = logging.getLogger(__name__)

events_bp = Blueprint("events", __name__)


@events_bp.route("/events", methods=["GET"])
def events_page():
    # POST /events is the real endpoint. This GET exists purely so that
    # opening /events in a browser lands on the event form instead of a
    # bare 405 — publishers still POST to the same URL.
    return render_template("index.html")


@events_bp.route("/events", methods=["POST"])
def create_event():
    data = request.get_json(silent=True)

    is_valid, error = validate_event_payload(data)
    if not is_valid:
        return jsonify({"error": error}), 400

    session = get_session()
    try:
        # Step 1: create the Event row (not committed yet)
        new_event = Event(
            event_type=data["event_type"],
            payload=data["payload"],
            idempotency_key=data["idempotency_key"],
        )
        session.add(new_event)
        session.flush()  # assigns new_event.id without fully committing yet

        # Step 2: find every endpoint subscribed to this event_type
        matching_endpoints = (
            session.query(Endpoint)
            .filter(Endpoint.event_type == data["event_type"])
            .all()
        )

        # Step 3: create one Delivery row per matching endpoint (fan-out)
        for endpoint in matching_endpoints:
            delivery = Delivery(
                event_id=new_event.id,
                endpoint_id=endpoint.id,
                status="pending",
            )
            session.add(delivery)

        # Step 4: commit everything together - event + all deliveries,
        # or none of it, if anything above raised an error.
        session.commit()

        return jsonify({
            "event_id": str(new_event.id),
            "subscribers_notified": len(matching_endpoints),
            "duplicate": False,
        }), 202

    except IntegrityError:
        # US-1.3: idempotency_key has a UNIQUE constraint in the DB.
        # This catch fires if the same idempotency_key is sent twice.
        session.rollback()

        # A real idempotent API replays the ORIGINAL result rather than
        # reporting a conflict. Otherwise a publisher that lost the response
        # can never learn which event was created, and is stuck retrying
        # into a 409 forever.
        existing = (
            session.query(Event)
            .filter(Event.idempotency_key == data["idempotency_key"])
            .one_or_none()
        )
        if existing is not None:
            notified = (
                session.query(Delivery)
                .filter(Delivery.event_id == existing.id)
                .count()
            )
            return jsonify({
                "event_id": str(existing.id),
                "subscribers_notified": notified,
                "duplicate": True,
            }), 200

        # The constraint that fired was NOT idempotency_key — the duplicate
        # key we just looked for isn't there. That means a genuine integrity
        # failure elsewhere (e.g. the event/endpoint fan-out constraint), and
        # reporting it as "duplicate idempotency_key" would send the caller
        # chasing the wrong problem.
        session.rollback()
        app_logger.exception("integrity error while creating event")
        return jsonify({"error": "internal error creating event"}), 500

    finally:
        session.close()
