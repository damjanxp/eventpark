"""Cursor ("keyset") pagination.

Offset pagination (`LIMIT 50 OFFSET 5000`) makes the database read and throw away 5,000 rows, and skips or repeats
rows when new ones are inserted while you page. Keyset pagination remembers *where you stopped* instead:
`WHERE id > :last_id ORDER BY id LIMIT 51`. With UUIDv7 ids (time-ordered, see core/ids.py) "order by id" is also
"order by creation time", so a single indexed column is enough.

The cursor handed to clients is opaque (base64 of the last id): clients must not build or parse it, which leaves us
free to change what's inside.
"""

import base64
import binascii
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from eventpark.core.errors import ValidationFailed

MAX_LIMIT = 200


def encode_cursor(last_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).decode().rstrip("=")


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed("Invalid cursor.", code="INVALID_CURSOR") from exc


async def paginate[T](
    session: AsyncSession,
    query: Select[T],
    id_column: InstrumentedAttribute[uuid.UUID],
    *,
    cursor: str | None,
    limit: int,
) -> tuple[Sequence[T], str | None]:
    """Returns (items, next_cursor). next_cursor is None on the last page."""
    limit = max(1, min(limit, MAX_LIMIT))
    if cursor:
        query = query.where(id_column > decode_cursor(cursor))
    rows: Sequence[Any] = (await session.scalars(query.order_by(id_column).limit(limit + 1))).all()
    # Asked for one extra row: if it exists, there is a next page (and we don't return it now).
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1].id)
    return rows, None
