"""Helpers for database errors.

When Postgres refuses a write (UNIQUE, CHECK, FOREIGN KEY), it reports *which* constraint failed. Our constraints have
predictable names (db/base.py naming convention), so services can turn "uq_zones_venue_id_code" into a clear
409 "A zone with this code already exists" instead of a generic 500.
"""

from sqlalchemy.exc import IntegrityError


def constraint_name(exc: IntegrityError) -> str | None:
    # SQLAlchemy wraps the driver error: exc.orig is the DB-API error, whose __cause__ is asyncpg's own exception
    # carrying `constraint_name`.
    for candidate in (getattr(exc.orig, "__cause__", None), exc.orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
