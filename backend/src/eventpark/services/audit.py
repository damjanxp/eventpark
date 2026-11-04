"""Audit log: who changed what, when, and why. Written in the SAME transaction as the change itself, so there is
never a change without its audit row (or an audit row for a change that was rolled back)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.db.models import AuditLog


def record(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | None = None,
    actor_device_id: uuid.UUID | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            reason=reason,
            details=details or {},
        )
    )
