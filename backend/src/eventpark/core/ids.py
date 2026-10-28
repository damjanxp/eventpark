"""UUIDv7 primary keys (RFC 9562).

Why v7 instead of the usual random v4: the first 48 bits are the creation time in milliseconds, so new IDs
are (roughly) increasing. Postgres stores a primary key in a B-tree index; random v4 IDs land all over the
index (many page splits, poor caching), while v7 IDs append near the end, like a bigserial would, yet
they're still globally unique and can be generated anywhere without asking the database.

Python's `uuid.uuid7()` only arrives in 3.14 and Postgres' `uuidv7()` in 18, so we generate them here.

Bit layout (128 bits, most significant first):
    48 bits  unix_ts_ms   milliseconds since 1970-01-01 UTC
     4 bits  version      0b0111 (= 7)
    12 bits  rand_a       random
     2 bits  variant      0b10 (RFC 9562)
    62 bits  rand_b       random
"""

import os
import time
import uuid

_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80  # everything after the timestamp; version and variant bits are overwritten below


def uuid7() -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000
    random_part = int.from_bytes(os.urandom(_RANDOM_BITS // 8))

    value = ((unix_ms & ((1 << _TIMESTAMP_BITS) - 1)) << _RANDOM_BITS) | random_part
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7 in bits 79..76
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # variant 0b10 in bits 63..62
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """The creation time (ms since epoch) stored in a UUIDv7. Handy for debugging and tests."""
    return value.int >> _RANDOM_BITS
