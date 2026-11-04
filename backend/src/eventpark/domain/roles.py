"""Application roles (docs/01-product-and-domain.md). Checked by the API, not by AWS IAM (CLAUDE.md §3)."""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "ADMIN"
    EVENT_MANAGER = "EVENT_MANAGER"
    GATE_OPERATOR = "GATE_OPERATOR"
    PARKING_STAFF = "PARKING_STAFF"
    DRIVER = "DRIVER"


# Everyone who works at the venue (can sign in to the staff app and see dashboards). Drivers are customers.
STAFF_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.EVENT_MANAGER, Role.GATE_OPERATOR, Role.PARKING_STAFF})
