"""HID++ 2.0 wire format. Pure: no I/O.

Every message is ``[report id, device index, feature index, function|sw id,
*params]``. Report ``0x10`` is 7 bytes and ``0x11`` is 20; this driver only
sends long reports, because not every receiver child accepts short ones.

* The feature index is per device and per firmware: feature 0 (ROOT) maps a
  feature ID (``0x2201`` = adjustable DPI) to its index, so nothing here
  hard-codes an index.
* The low nibble of byte 3 is the software ID. The device echoes it in its
  reply, which is how a reply is told apart from the kernel's own requests
  (``hid-logitech-hidpp`` uses sw id 1) and from notifications (sw id 0).
* An error reply has feature index ``0xFF`` and carries the request's
  feature index and function byte, then an error code. HID++ 1.0 errors
  (``0x8F``) can come from the receiver.
* Writes through a receiver child (``hid-logitech-dj``) get their device
  index rewritten by the kernel, so replies are matched on feature index,
  function and software ID only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

SHORT, LONG, VERY_LONG = 0x10, 0x11, 0x12
REPORT_SIZES = {SHORT: 7, LONG: 20, VERY_LONG: 64}
ERROR_20 = 0xFF
ERROR_10 = 0x8F
DEVICE_INDEX = 0xFF  # the device itself when wired; rewritten by hid-logitech-dj
SW_ID = 0x0B  # anything but 0 (notifications) and 1 (the kernel driver)


class Feature(IntEnum):
    ROOT = 0x0000
    FEATURE_SET = 0x0001
    FEATURE_INFO = 0x0002
    DEVICE_FW_VERSION = 0x0003
    DEVICE_NAME = 0x0005
    DEVICE_FRIENDLY_NAME = 0x0007
    CONFIG_CHANGE = 0x0020
    CRYPTO_ID = 0x0021
    BATTERY_STATUS = 0x1000
    BATTERY_VOLTAGE = 0x1001
    UNIFIED_BATTERY = 0x1004
    REPROG_CONTROLS_V4 = 0x1B04
    WIRELESS_DEVICE_STATUS = 0x1D4B
    FIRMWARE_PROPERTIES = 0x1F1F
    HIRES_WHEEL = 0x2121
    ADJUSTABLE_DPI = 0x2201
    EXTENDED_ADJUSTABLE_DPI = 0x2202
    REPORT_RATE = 0x8060
    EXTENDED_ADJUSTABLE_REPORT_RATE = 0x8061
    COLOR_LED_EFFECTS = 0x8070
    RGB_EFFECTS = 0x8071
    MODE_STATUS = 0x8090
    ONBOARD_PROFILES = 0x8100
    MOUSE_BUTTON_SPY = 0x8110
    LATENCY_MONITORING = 0x8111


class FeatureFlags(IntEnum):
    OBSOLETE = 0x80
    HIDDEN = 0x40
    ENGINEERING = 0x20


class ErrorCode(IntEnum):
    NO_ERROR = 0
    UNKNOWN = 1
    INVALID_ARGUMENT = 2
    OUT_OF_RANGE = 3
    HARDWARE_ERROR = 4
    LOGITECH_INTERNAL = 5
    INVALID_FEATURE_INDEX = 6
    INVALID_FUNCTION_ID = 7
    BUSY = 8
    UNSUPPORTED = 9


def feature_name(feature_id: int) -> str:
    try:
        return Feature(feature_id).name
    except ValueError:
        return f"0x{feature_id:04x}"


def request(feature_index: int, function: int, *params: int, device_index: int = DEVICE_INDEX, sw_id: int = SW_ID) -> bytes:
    if not 0 <= function <= 0x0F:
        raise ValueError(f"function {function} out of range")
    if len(params) > REPORT_SIZES[LONG] - 4:
        raise ValueError("too many parameters for a long report")
    return bytes([LONG, device_index, feature_index, (function << 4) | sw_id, *params]).ljust(REPORT_SIZES[LONG], b"\0")


@dataclass(frozen=True)
class Message:
    """An input report: a reply to one of our requests, someone else's reply,
    or a notification (sw id 0)."""

    device_index: int
    feature_index: int
    function: int
    sw_id: int
    params: bytes


@dataclass(frozen=True)
class Error:
    device_index: int
    feature_index: int
    function: int
    sw_id: int
    code: int

    @property
    def name(self) -> str:
        try:
            return ErrorCode(self.code).name
        except ValueError:
            return f"error 0x{self.code:02x}"


def parse(data: bytes) -> Message | Error | None:
    """Classify an input report; None for anything that isn't HID++ (the
    mouse's own movement and button reports share the node)."""
    if len(data) < 4 or data[0] not in REPORT_SIZES:
        return None
    if data[2] in (ERROR_20, ERROR_10) and len(data) >= 6:
        return Error(data[1], data[3], data[4] >> 4, data[4] & 0x0F, data[5])
    return Message(data[1], data[2], data[3] >> 4, data[3] & 0x0F, bytes(data[4:]))


def is_reply_to(msg: Message | Error, feature_index: int, function: int, sw_id: int = SW_ID) -> bool:
    return (msg.feature_index, msg.function, msg.sw_id) == (feature_index, function, sw_id)


def u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")
