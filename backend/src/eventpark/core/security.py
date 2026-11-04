"""Password hashing, JWT access tokens and refresh-token secrets (docs/02-architecture.md §9).

Pure functions, no database: the auth service (next step) stores and looks things up; this module only does the
cryptography. Time is passed in (`now`) instead of read from the clock, so tests can control it.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from eventpark.domain.roles import Role

# ---------------------------------------------------------------------------------------------------------------------
# Passwords: argon2id
# ---------------------------------------------------------------------------------------------------------------------
# argon2id is deliberately slow (~50 ms) and memory-hard (~64 MiB per hash): logging in once is instant for a user,
# but an attacker who stole the `users` table can only test a handful of guesses per second per CPU core, and the
# memory cost makes GPUs/ASICs much less effective. Every hash has its own random salt, so equal passwords give
# different hashes and precomputed tables are useless. The library's defaults follow RFC 9106's recommendation.
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Returns e.g. '$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>': algorithm, parameters and salt travel with it."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with weaker parameters than today's: re-hash it after the next successful login."""
    return _password_hasher.check_needs_rehash(password_hash)


# ---------------------------------------------------------------------------------------------------------------------
# Access tokens: JWT (HS256)
# ---------------------------------------------------------------------------------------------------------------------
# A JWT is base64url(header).base64url(payload).signature. Anyone can READ the payload (it's only encoded, not
# encrypted), but nobody can CHANGE it without the secret, because the signature would no longer match.
# So: never put secrets in it; and the server trusts it only after checking the signature and expiry.
JWT_ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ["sub", "role", "exp", "iat", "jti"]


class InvalidTokenError(Exception):
    """The token is malformed, tampered with, signed with another key, or expired. The API answers 401."""


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID  # "sub" (subject)
    role: Role
    issued_at: datetime  # "iat"
    expires_at: datetime  # "exp"
    token_id: str  # "jti": unique per token, lets logs and a future denylist refer to one specific token


def create_access_token(*, user_id: uuid.UUID, role: Role, secret: str, ttl: timedelta, now: datetime) -> str:
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, *, secret: str, now: datetime) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            # Only accept the algorithm WE use. Never trust the "alg" field in the token's own header: that's how
            # "alg: none" and algorithm-confusion attacks work.
            algorithms=[JWT_ALGORITHM],
            options={"require": _REQUIRED_CLAIMS, "verify_exp": False},  # expiry checked below against `now`
        )
        expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
        if expires_at <= now:
            raise InvalidTokenError("token expired")
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            role=Role(payload["role"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=expires_at,
            token_id=str(payload["jti"]),
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:  # e.g. "sub" not a UUID, unknown role
        raise InvalidTokenError(f"invalid claims: {exc}") from exc


# ---------------------------------------------------------------------------------------------------------------------
# Refresh tokens: random secrets, stored as SHA-256
# ---------------------------------------------------------------------------------------------------------------------
# Unlike a password, a refresh token is 256 random bits: nobody can guess it, so it doesn't need a SLOW hash.
# A fast SHA-256 is enough, and it's deterministic, which lets us find the row with `WHERE token_hash = ...`
# (a UNIQUE index lookup). We still never store the token itself: whoever reads the table can't use the hashes.
REFRESH_TOKEN_BYTES = 32  # 256 bits


def new_refresh_token() -> tuple[str, str]:
    """Returns (token for the client cookie, hash for the database)."""
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# Device API keys: epd_<prefix>_<secret>, stored as SHA-256 (ADR-024)
# ---------------------------------------------------------------------------------------------------------------------
# The prefix is public (it identifies the row, like a username); the secret is 256 random bits. Like refresh tokens,
# a random secret can't be guessed, so a fast hash is enough, and it keeps argon2's ~50 ms / 64 MiB off the gate
# hot path. We compare with hmac.compare_digest so the comparison time doesn't leak how many characters matched.
DEVICE_KEY_PREFIX = "epd"


class InvalidDeviceKeyError(Exception):
    pass


def new_device_api_key() -> tuple[str, str, str]:
    """Returns (full key shown once to the admin, public prefix for lookup, hash for the database)."""
    prefix = secrets.token_hex(6)
    key = f"{DEVICE_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    return key, prefix, hash_device_api_key(key)


def parse_device_api_key(key: str) -> str:
    """Returns the prefix, or raises InvalidDeviceKeyError if the key doesn't have the expected shape."""
    parts = key.split("_", 2)  # maxsplit=2: the secret part may itself contain "_"
    if len(parts) != 3 or parts[0] != DEVICE_KEY_PREFIX or len(parts[1]) != 12 or not parts[2]:
        raise InvalidDeviceKeyError("malformed device key")
    return parts[1]


def hash_device_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def device_api_key_matches(key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_device_api_key(key), stored_hash)
