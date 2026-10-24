"""
MODULE: worker/signing.py
WHAT IT DOES: Signs outgoing delivery payloads with HMAC-SHA256, using
              each endpoint's own secret, plus a timestamp.
PROBLEM SOLVED: US-2.4 - without this, anyone who discovers a subscriber's
                 URL could send it fake events pretending to be HookRelay.
                 Signing lets the subscriber verify authenticity, and the
                 timestamp lets them reject old/replayed requests.
DATA SOURCE: Pure functions - takes the endpoint's secret + raw payload
             bytes, returns headers to attach to the HTTP request.
             No DB access here; worker.py calls this before sending.
"""

import hmac
import hashlib
import time


def sign_payload(secret, payload_bytes):
    """
    Returns headers to attach to the outgoing request:
      X-HookRelay-Timestamp: unix timestamp when this was signed
      X-HookRelay-Signature: HMAC-SHA256 of "timestamp.payload"

    Signing "timestamp.payload" together (not just payload) means an
    attacker can't reuse an old valid signature on a new timestamp,
    and can't reuse an old payload+timestamp pair after it's expired.
    """
    timestamp = str(int(time.time()))
    signed_content = f"{timestamp}.".encode() + payload_bytes

    signature = hmac.new(
        key=secret.encode(),
        msg=signed_content,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return {
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": signature,
    }