"""Webhook signatures, the provider side (the EventPark API has the matching verifier).

    Fakepay-Signature: t=1730736000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd

v1 = HMAC-SHA256(secret, f"{t}.{raw_body}") in hex. Signing the timestamp together with the body means an attacker
can't take an old, genuinely signed webhook and replay it later with a fresh timestamp: the receiver rejects
signatures older than a few minutes, and changing `t` breaks the HMAC.
"""

import hashlib
import hmac


def signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={signature(secret, timestamp, body)}"
