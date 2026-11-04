"""Payment state machine and webhook signature verification."""

import pytest

from eventpark.core.webhooks import InvalidSignature, expected_signature, verify_signature
from eventpark.domain.payments import (
    PaymentOutcome,
    PaymentStatus,
    PaymentTransitionError,
    normalize_plate,
    payment_transition,
)

# --- payment state machine ---


@pytest.mark.parametrize(
    ("current", "outcome", "expected"),
    [
        (PaymentStatus.PENDING, PaymentOutcome.SUCCEEDED, PaymentStatus.SUCCEEDED),
        (PaymentStatus.PENDING, PaymentOutcome.FAILED, PaymentStatus.FAILED),
        (PaymentStatus.PENDING, PaymentOutcome.NO_ANSWER, PaymentStatus.UNKNOWN),
        (PaymentStatus.UNKNOWN, PaymentOutcome.SUCCEEDED, PaymentStatus.SUCCEEDED),
        (PaymentStatus.UNKNOWN, PaymentOutcome.FAILED, PaymentStatus.FAILED),
        (PaymentStatus.UNKNOWN, PaymentOutcome.NO_ANSWER, PaymentStatus.UNKNOWN),
        (PaymentStatus.SUCCEEDED, PaymentOutcome.SUCCEEDED, PaymentStatus.SUCCEEDED),  # duplicate webhook
        (PaymentStatus.SUCCEEDED, PaymentOutcome.REFUNDED, PaymentStatus.REFUNDED),
    ],
)
def test_allowed_transitions(current: PaymentStatus, outcome: PaymentOutcome, expected: PaymentStatus) -> None:
    assert payment_transition(current, outcome) is expected


@pytest.mark.parametrize(
    ("current", "outcome"),
    [
        (PaymentStatus.SUCCEEDED, PaymentOutcome.FAILED),  # the provider can't change its mind
        (PaymentStatus.FAILED, PaymentOutcome.SUCCEEDED),
        (PaymentStatus.PENDING, PaymentOutcome.REFUNDED),  # can't refund what wasn't charged
        (PaymentStatus.REFUNDED, PaymentOutcome.SUCCEEDED),
    ],
)
def test_contradictions_are_errors(current: PaymentStatus, outcome: PaymentOutcome) -> None:
    assert isinstance(payment_transition(current, outcome), PaymentTransitionError)


def test_plate_normalisation() -> None:
    assert normalize_plate(" bg 123-ab ") == normalize_plate("BG123AB") == "BG123AB"


# --- webhook signatures ---

SECRET = "whsec_test"
BODY = b'{"id":"evt_1","type":"payment_intent.succeeded"}'
NOW = 1_730_736_000


def header(body: bytes = BODY, *, secret: str = SECRET, t: int = NOW) -> str:
    return f"t={t},v1={expected_signature(secret, t, body)}"


def test_valid_signature() -> None:
    verify_signature(header(), BODY, secret=SECRET, now=NOW + 10)


def test_tampered_body_is_rejected() -> None:
    with pytest.raises(InvalidSignature, match="mismatch"):
        verify_signature(header(), BODY.replace(b"succeeded", b"failed"), secret=SECRET, now=NOW)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(InvalidSignature, match="mismatch"):
        verify_signature(header(secret="attacker"), BODY, secret=SECRET, now=NOW)


@pytest.mark.parametrize("age", [301, -301, 86_400])
def test_old_or_future_timestamp_is_rejected(age: int) -> None:
    with pytest.raises(InvalidSignature, match="tolerance"):
        verify_signature(header(t=NOW - age), BODY, secret=SECRET, now=NOW)


def test_refreshed_timestamp_breaks_the_signature() -> None:
    """Replay with a new timestamp: the old v1 was computed over the old t, so it no longer matches."""
    old = header(t=NOW - 3600)
    replay = old.replace(f"t={NOW - 3600}", f"t={NOW}")

    with pytest.raises(InvalidSignature, match="mismatch"):
        verify_signature(replay, BODY, secret=SECRET, now=NOW)


@pytest.mark.parametrize("bad", [None, "", "v1=abc", "t=notanumber,v1=abc", "garbage"])
def test_malformed_headers_are_rejected(bad: str | None) -> None:
    with pytest.raises(InvalidSignature):
        verify_signature(bad, BODY, secret=SECRET, now=NOW)


def test_compatible_with_fakepay_signing() -> None:
    """The provider (fakepay/src/fakepay/signing.py) and we must compute exactly the same thing."""
    import hashlib
    import hmac

    provider_side = hmac.new(SECRET.encode(), f"{NOW}.".encode() + BODY, hashlib.sha256).hexdigest()

    assert expected_signature(SECRET, NOW, BODY) == provider_side
