"""
Validates incoming JSON before it touches the database.
Kept simple (plain functions) rather than pulling in a validation
library yet — enough to reject bad requests with a clear error.
"""


def validate_endpoint_payload(data):
    """
    Expects: {"url": "...", "secret": "...", "event_type": "..."}
    Returns: (is_valid, error_message)
    """
    if not isinstance(data, dict):
        return False, "Request body must be a JSON object"

    required_fields = ["url", "secret", "event_type"]
    for field in required_fields:
        if field not in data or not data[field]:
            return False, f"Missing required field: {field}"

    if not data["url"].startswith("http"):
        return False, "url must start with http:// or https://"

    return True, None


def validate_event_payload(data):
    """
    Expects: {"event_type": "...", "payload": {...}, "idempotency_key": "..."}
    Returns: (is_valid, error_message)
    """
    if not isinstance(data, dict):
        return False, "Request body must be a JSON object"

    required_fields = ["event_type", "payload", "idempotency_key"]
    for field in required_fields:
        if field not in data or not data[field]:
            return False, f"Missing required field: {field}"

    if not isinstance(data["payload"], dict):
        return False, "payload must be a JSON object"

    return True, None


def validate_endpoint_patch(data):
    """
    Expects any subset of: {"url": "...", "secret": "...", "event_type": "..."}
    Returns: (is_valid, error_message)

    A PATCH is allowed to carry any subset, unlike POST which requires all
    three — that's the whole point of a partial update. But at least one
    field must be present, or "success" would be a lie: the caller thinks
    something changed when nothing did.
    """
    if not isinstance(data, dict):
        return False, "Request body must be a JSON object"

    allowed = {"url", "secret", "event_type"}
    unknown = set(data) - allowed
    if unknown:
        return False, f"Cannot patch unknown field(s): {sorted(unknown)}"

    if not data:
        return False, "Must supply at least one of: url, secret, event_type"

    # Reuse the same rules POST applies, so a patch can't smuggle in a value
    # that a create would have rejected (e.g. a non-http url).
    if "url" in data:
        if not isinstance(data["url"], str) or not data["url"].startswith("http"):
            return False, "url must start with http:// or https://"

    for field in ("url", "secret", "event_type"):
        if field in data and not isinstance(data[field], str):
            return False, f"{field} must be a string"

    return True, None