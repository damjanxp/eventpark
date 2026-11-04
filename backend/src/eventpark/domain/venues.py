"""Physical model of a venue (docs/01-product-and-domain.md §3): zones, numbered spaces, gates, devices."""

from enum import StrEnum


class GateDirection(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class SpaceKind(StrEnum):
    VIP = "VIP"
    RESERVED = "RESERVED"
    ACCESSIBLE = "ACCESSIBLE"
    EV = "EV"


class DeviceStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"  # set by an admin: the device's key is refused (401) until re-enabled


class PrinterStatus(StrEnum):
    OK = "OK"
    PAPER_LOW = "PAPER_LOW"
    PAPER_OUT = "PAPER_OUT"


class DeviceCommandType(StrEnum):
    OPEN_BARRIER = "OPEN_BARRIER"
    REBOOT = "REBOOT"
