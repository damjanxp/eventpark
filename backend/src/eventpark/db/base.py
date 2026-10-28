"""The SQLAlchemy declarative base that every table model inherits from."""

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# Predictable names for indexes and constraints (e.g. "uq_users_email", "ck_users_role").
# Without this, Postgres invents names, and a later migration that wants to drop or change a constraint
# can't refer to it reliably (and autogenerate produces noisy diffs).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Every `Mapped[datetime]` column becomes `timestamptz`: an absolute moment, stored in UTC (CLAUDE.md §3).
    # Plain `timestamp` (without time zone) would be a wall-clock time with no zone, a classic source of bugs.
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012 (SQLAlchemy reads this class attribute)
