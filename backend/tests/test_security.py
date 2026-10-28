import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from eventpark.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from eventpark.domain.roles import Role

SECRET = "test-secret-that-is-at-least-32-bytes-long"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=15)


# --- passwords ---


def test_password_hash_verifies_only_the_right_password() -> None:
    stored = hash_password("correct horse battery staple")

    assert verify_password(stored, "correct horse battery staple")
    assert not verify_password(stored, "correct horse battery stapl")


def test_password_is_not_stored_and_hashes_are_salted() -> None:
    first = hash_password("same password")
    second = hash_password("same password")

    assert "same password" not in first
    assert first.startswith("$argon2id$")
    assert first != second  # different random salt each time


def test_garbage_hash_does_not_crash_verification() -> None:
    assert not verify_password("not-a-hash", "whatever")


def test_fresh_hash_does_not_need_rehash() -> None:
    assert not password_needs_rehash(hash_password("pw"))


# --- access tokens ---


def _token(**overrides: object) -> str:
    args: dict[str, object] = {"user_id": uuid.uuid4(), "role": Role.ADMIN, "secret": SECRET, "ttl": TTL, "now": NOW}
    args.update(overrides)
    return create_access_token(**args)  # type: ignore[arg-type]


def test_token_round_trip() -> None:
    user_id = uuid.uuid4()
    token = _token(user_id=user_id, role=Role.EVENT_MANAGER)

    claims = decode_access_token(token, secret=SECRET, now=NOW + timedelta(minutes=1))

    assert claims.user_id == user_id
    assert claims.role is Role.EVENT_MANAGER
    assert claims.issued_at == NOW
    assert claims.expires_at == NOW + TTL
    assert claims.token_id


def test_payload_is_readable_without_the_secret() -> None:
    """Signed, NOT encrypted: anyone can read the claims, so never put secrets in a JWT."""
    payload_part = _token(role=Role.DRIVER).split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))

    assert payload["role"] == "DRIVER"


def test_expired_token_is_rejected() -> None:
    token = _token()

    with pytest.raises(InvalidTokenError, match="expired"):
        decode_access_token(token, secret=SECRET, now=NOW + TTL)


def test_token_signed_with_another_secret_is_rejected() -> None:
    token = _token(secret="another-secret-that-is-also-32-bytes-long!")

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET, now=NOW)


def test_tampered_payload_is_rejected() -> None:
    """Change role DRIVER -> ADMIN in the payload: the signature no longer matches."""
    header, payload_part, signature = _token(role=Role.DRIVER).split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))
    payload["role"] = "ADMIN"
    forged_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{forged_payload}.{signature}", secret=SECRET, now=NOW)


def test_unsigned_alg_none_token_is_rejected() -> None:
    payload = {"sub": str(uuid.uuid4()), "role": "ADMIN", "iat": 0, "exp": 2_000_000_000, "jti": "x"}
    unsigned = jwt.encode(payload, key=None, algorithm="none")

    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned, secret=SECRET, now=NOW)


def test_token_missing_a_required_claim_is_rejected() -> None:
    token = jwt.encode({"sub": str(uuid.uuid4()), "exp": 2_000_000_000, "iat": 0, "jti": "x"}, SECRET, "HS256")

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET, now=NOW)


def test_token_with_unknown_role_is_rejected() -> None:
    payload = {"sub": str(uuid.uuid4()), "role": "SUPERUSER", "iat": 0, "exp": 2_000_000_000, "jti": "x"}
    token = jwt.encode(payload, SECRET, "HS256")

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET, now=NOW)


# --- refresh tokens ---


def test_refresh_token_is_random_and_only_its_hash_is_stored() -> None:
    token, token_hash = new_refresh_token()
    other_token, _ = new_refresh_token()

    assert token != other_token
    assert len(token) >= 43  # 32 random bytes, base64url
    assert token_hash == hash_refresh_token(token)  # deterministic: we can look the row up by hash
    assert token not in token_hash
