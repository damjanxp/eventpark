"""Table models (SQLAlchemy ORM). Spec: docs/02-architecture.md "Data model".

Each class describes one table. Alembic compares these classes with the real database to draft migrations;
the migration files (not these classes) are what actually change the database.
"""

import uuid
from datetime import datetime

from sqlalchemy import Enum, Text, func, true
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from eventpark.core.ids import uuid7
from eventpark.db.base import Base
from eventpark.domain.roles import Role


class User(Base):
    __tablename__ = "users"

    # Generated in Python (UUIDv7, see core/ids.py), so the app knows the ID before the INSERT.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # citext = case-insensitive text: "Damjan@Example.com" and "damjan@example.com" are the same email,
    # so the UNIQUE constraint can't be fooled by capital letters.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)  # never the password itself (hashing: Phase 2)
    full_name: Mapped[str] = mapped_column(Text)
    # Stored as text + CHECK constraint, not a Postgres enum type (ADR-023).
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            name="role",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda roles: [r.value for r in roles],
        )
    )
    is_active: Mapped[bool] = mapped_column(server_default=true())
    # server_default: the database fills it in (its clock, in UTC), so every writer agrees on the time.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
