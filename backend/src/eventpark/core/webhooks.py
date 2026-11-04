"""Verifying the payment provider's webhook signature (docs/02-architecture.md §9).

    Fakepay-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, f"{t}.{raw body}")>

Checks, in order:
1. the header is well-formed;
2. the timestamp is recent (|now - t| <= 5 min): an attacker who captured a genuine webhook can't replay it later,
   and can't refresh `t` because `t` is inside the signed message;
3. our own HMAC over the RAW body equals v1. Raw bytes, exactly as received: re-serialising parsed JSON could change
   whitespace or key order and break the signature;
4. comparison with hmac.compare_digest: it takes the same time however many characters match, so an attacker can't
   find the right signature byte by byte by measuring how long our "no" takes (a timing attack). `==` stops at the
   first different character.
"""

import hashlib
import hmac

TOLERANCE_SECONDS = 300


class InvalidSignature(Exception):
    pass


def expected_signature(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify_signature(
    header: str | None, body: bytes, *, secret: str, now: int, tolerance: int = TOLERANCE_SECONDS
) -> None:
    if not header:
        raise InvalidSignature("missing signature header")
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError) as exc:
        raise InvalidSignature("malformed signature header") from exc
    if abs(now - timestamp) > tolerance:
        raise InvalidSignature("timestamp outside tolerance (replay?)")
    if not hmac.compare_digest(expected_signature(secret, timestamp, body), given):
        raise InvalidSignature("signature mismatch")
