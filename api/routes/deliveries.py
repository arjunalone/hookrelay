"""
US-3.1: As a Developer, I want to see failed/dead deliveries in a
dashboard, so I know what needs attention.

US-3.2: As a Developer, I want to replay a dead delivery after fixing
my endpoint, so I don't lose that event permanently.

Until now this information was only reachable by opening psql and
writing SQL by hand. These routes expose it as a filterable API, which
the HTML dashboard in api/routes/dashboard.py reads from.
"""

import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from api.extensions import get_session
from shared.models import Delivery, Event, Endpoint

deliveries_bp = Blueprint("deliveries", __name__)

# Mirrors the status lifecycle documented in shared/models.py. Anything
# outside this set is rejected rather than silently returning an empty
# list, so a typo in a filter doesn't look like "no failures yet".
VALID_STATUSES = ("pending", "in_flight", "succeeded", "retrying", "dead")

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _serialize(delivery, event, endpoint):
    return {
        "id": str(delivery.id),
        "event_id": str(delivery.event_id),
        "event_type": event.event_type if event else None,
        "endpoint_id": str(delivery.endpoint_id),
        "endpoint_url": endpoint.url if endpoint else None,
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
        "last_status_code": delivery.last_status_code,
        "last_error": delivery.last_error,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        "next_attempt_at": (
            delivery.next_attempt_at.isoformat() if delivery.next_attempt_at else None
        ),
    }


@deliveries_bp.route("/deliveries", methods=["GET"])
def list_deliveries():
    status = request.args.get("status")
    if status is not None and status not in VALID_STATUSES:
        return jsonify({
            "error": f"invalid status filter '{status}'",
            "valid_statuses": list(VALID_STATUSES),
        }), 400

    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    session = get_session()
    try:
        query = (
            session.query(Delivery, Event, Endpoint)
            .join(Event, Delivery.event_id == Event.id)
            .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        )
        if status:
            query = query.filter(Delivery.status == status)

        rows = (
            query.order_by(Delivery.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        total = query.count()

        return jsonify({
            "total": total,
            "limit": limit,
            "offset": offset,
            "filter": {"status": status},
            "deliveries": [_serialize(d, e, ep) for d, e, ep in rows],
        })
    finally:
        session.close()


@deliveries_bp.route("/deliveries/stats", methods=["GET"])
def delivery_stats():
    session = get_session()
    try:
        rows = (
            session.query(Delivery.status, func.count(Delivery.id))
            .group_by(Delivery.status)
            .all()
        )

        counts = {status: 0 for status in VALID_STATUSES}
        for status, count in rows:
            counts[status] = count
        counts["total"] = sum(counts[s] for s in VALID_STATUSES)

        return jsonify(counts)
    finally:
        session.close()


@deliveries_bp.route("/deliveries/<delivery_id>/replay", methods=["POST"])
def replay_delivery(delivery_id):
    try:
        delivery_uuid = uuid.UUID(delivery_id)
    except (ValueError, AttributeError, TypeError):
        return jsonify({"error": "delivery id must be a valid UUID"}), 400

    session = get_session()
    try:
        delivery = session.get(Delivery, delivery_uuid)
        if delivery is None:
            return jsonify({"error": "delivery not found"}), 404

        # Only dead deliveries are replayable. Replaying a row that is
        # pending/retrying/in_flight would race the worker that already
        # owns it, and replaying a succeeded one would double-deliver.
        if delivery.status != "dead":
            return jsonify({
                "error": f"only dead deliveries can be replayed (current status: {delivery.status})"
            }), 409

        # Hand it back to the queue exactly the way a fresh delivery looks,
        # so claim_one_delivery() in worker/worker.py picks it up on its
        # next poll without needing to know replay exists.
        delivery.status = "pending"
        delivery.attempt_count = 0
        delivery.next_attempt_at = datetime.now(timezone.utc)
        delivery.locked_by = None
        delivery.locked_until = None
        # last_error / last_status_code are deliberately left in place so the
        # reason for the original failure is still visible after a replay.
        # Both get overwritten on the next attempt anyway.

        session.commit()

        return jsonify({
            "id": str(delivery.id),
            "status": delivery.status,
            "replayed": True,
        })
    finally:
        session.close()
