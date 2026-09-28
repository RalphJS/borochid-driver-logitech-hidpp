from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from borochid.common.manifest import Manifest
from borochid.common.models import Bus, DeviceIdentity
from borochid.service.channels import Channel
from borochid.service.settings import MemoryStore

from borochid_logitech_hidpp.driver import HidppDriver

# The G502 X's buttons, bit order as measured, with factory-like defaults.
BUTTONS = [
    ("left", "Left click", 1, False, None),
    ("right", "Right click", 2, True, None),
    ("middle", "Wheel click", 3, True, None),
    ("g4", "G4 (back)", 4, True, {"button": 4}),
    ("g6", "G6 (sniper)", 5, True, "dpi_shift"),
    ("g5", "G5 (forward)", 6, True, {"button": 5}),
    ("tilt_left", "Wheel left", 7, True, {"hwheel": -1}),
    ("tilt_right", "Wheel right", 8, True, {"hwheel": 1}),
    ("g9", "G9", 9, True, "disabled"),
    ("g8", "G8", 10, True, "dpi_up"),
    ("g7", "G7", 11, True, "dpi_down"),
]

MANIFEST = {
    "id": "test.mouse",
    "version": "1.0.0",
    "match": [{"bus": "usb", "vid": "0x046d", "pid": "0x409f"}],
    "channel": {"type": "hid"},
    "driver": {"type": "logitech-hidpp"},
    "hidpp": {
        "buttons": [
            {"id": i, "label": label, "number": n, "remap": r, **({"default": d} if d is not None else {})}
            for i, label, n, r, d in BUTTONS
        ],
        "defaults": {"stages": [800, 1600, 3200], "default_stage": 2, "shift_dpi": 400, "report_rate": 1000},
        "reply_timeout_s": 0.05,
        "wake_check_s": 0.05,
    },
}

# Feature indexes as a G502 X reports them.
INDEX = {0x0001: 1, 0x0003: 2, 0x0005: 3, 0x1D4B: 4, 0x0020: 5, 0x1004: 6, 0x2201: 7, 0x2121: 8, 0x8100: 9, 0x8110: 10, 0x8060: 11}
FEAT = {v: k for k, v in INDEX.items()}
INVALID_ARGUMENT, INVALID_FUNCTION = 2, 7


class FakeMouse(Channel):
    """A G502 X LIGHTSPEED behind its receiver, behaving as measured:

    * answers nothing while asleep; any report it sends means it's awake;
    * refuses report-rate changes in onboard mode;
    * in host mode, reports every button as a spy notification, and as a
      normal click only when its remap-table entry isn't 0;
    * a power cycle puts it back in onboard mode and sends a
      WIRELESS_DEVICE_STATUS notification.
    """

    def __init__(self):
        super().__init__(DeviceIdentity(Bus.USB, "usb:1-2.3/1", vid=0x046D, pid=0x409F, serial="01-ab-09-45"), {"type": "hid"})
        self.asleep = False
        self.mode = 1
        self.spy = False
        self.table = list(range(1, 12))
        self.onboard_levels = [800, 3200]  # the onboard profile's DPI levels
        self.dpi_index = 1
        self.dpi = 3200
        self.rate_ms = 1
        self.calls: list[tuple[int, int, bytes]] = []  # (feature id, function, params)
        self.clicks: list[int] = []  # button masks the kernel would see

    async def open(self): ...

    async def close(self): ...

    def called(self, feature: int, function: int) -> list[bytes]:
        return [p for f, fn, p in self.calls if (f, fn) == (feature, function)]

    async def write(self, data: bytes) -> None:
        assert len(data) == 20 and data[0] == 0x11
        index, fn, sw, params = data[2], data[3] >> 4, data[3] & 0x0F, data[4:]
        if self.asleep:
            return
        feature = FEAT.get(index, 0) if index else 0
        self.calls.append((feature, fn, params.rstrip(b"\0")))
        try:
            reply = self.handle(index, feature, fn, params)
        except LookupError:
            reply = None
        if isinstance(reply, int) and not isinstance(reply, bool) and reply < 0:
            out = bytes([0x11, 0x01, 0xFF, index, data[3], -reply])
        else:
            out = bytes([0x11, 0x01, index, data[3], *(reply or b"")])
        asyncio.get_running_loop().call_soon(self._deliver, out.ljust(20, b"\0")[:20])

    def handle(self, index: int, feature: int, fn: int, p: bytes):
        if index == 0:
            if fn == 0:
                fid = (p[0] << 8) | p[1]
                return bytes([INDEX.get(fid, 0), 0, 0]) if fid else bytes([0, 0, 0])
            return bytes([4, 2, p[2]])
        if feature == 0x0003 and fn == 0:  # getDeviceInfo: entities, unit ID, transport, model
            return bytes([2, 0x01, 0xAB, 0x09, 0x45, 0x00, 0x0B])
        if feature == 0x2201:
            if fn == 1:
                return bytes([0, 0x00, 0x64, 0xE0, 0x32, 0x64, 0x00])
            if fn == 2:
                return bytes([0, self.dpi >> 8, self.dpi & 0xFF, 0x06, 0x40])
            if fn == 3:
                self.dpi = (p[1] << 8) | p[2]
                return b""
        if feature == 0x8060:
            if fn == 0:
                return bytes([0b10001011])
            if fn == 1:
                return bytes([self.rate_ms])
            if fn == 2:
                if self.mode != 2:
                    return -INVALID_ARGUMENT
                self.rate_ms = p[0]
                return b""
        if feature == 0x8100:
            if fn == 1:
                self.mode = p[0]
                return b""
            if fn == 2:
                return bytes([self.mode])
            if fn == 0x0B:
                return bytes([self.dpi_index])
            if fn == 0x0C:
                self.dpi_index = p[0]
                if self.mode == 1:
                    self.dpi = self.onboard_levels[self.dpi_index]
                return b""
            if fn in (6, 7, 8):
                raise AssertionError("the driver must never write profile memory")
        if feature == 0x8110:
            if fn == 0:
                return bytes([11])
            if fn == 1:
                self.spy = True
                return b""
            if fn == 2:
                self.spy = False
                return b""
            if fn == 3:
                return bytes(self.table)
            if fn == 4:
                self.table = list(p[:11])
                return b""
        return -INVALID_FUNCTION

    # -- things the user does --------------------------------------------------

    def move(self) -> None:
        self.asleep = False
        self._deliver(bytes([0x02, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0x9F, 0x40, 0, 0]))

    def press(self, number: int, down: bool = True, held: int = 0) -> None:
        """Button ``number`` changes; ``held`` are other buttons held down."""
        mask = held | ((1 << (number - 1)) if down else 0)
        if self.mode == 2 and self.spy:
            self._deliver(bytes([0x11, 0x01, INDEX[0x8110], 0x00, mask >> 8, mask & 0xFF]).ljust(20, b"\0"))
        clicks = 0
        for n in range(1, 12):
            if mask >> (n - 1) & 1:
                slot = self.table[n - 1] if self.mode == 2 else n
                clicks |= (1 << (slot - 1)) if slot else 0
        self.clicks.append(clicks)

    def power_cycle(self) -> None:
        self.mode, self.spy, self.table, self.dpi = 1, False, list(range(1, 12)), 1600
        self._deliver(bytes([0x11, 0x01, INDEX[0x1D4B], 0x00, 0x01, 0x01, 0x00]).ljust(20, b"\0"))


class FakeInput:
    def __init__(self):
        self.events: list[tuple] = []
        self.held: set = set()
        self.is_open = False

    async def open(self):
        self.is_open = True

    def close(self):
        for token in list(self.held):
            self.up(token)
        self.is_open = False

    def down(self, token, chord):
        if token not in self.held:
            self.held.add(token)
            self.events.append(("down", token, chord.to_json()))

    def up(self, token):
        if token in self.held:
            self.held.discard(token)
            self.events.append(("up", token))


class FakeHost:
    def __init__(self):
        self.input = FakeInput()


def make_driver(settings=None, **hidpp):
    mouse = FakeMouse()
    events: list[dict] = []
    m = copy.deepcopy(MANIFEST)
    m["hidpp"].update(hidpp)
    store = MemoryStore(settings)
    driver = HidppDriver(Manifest.from_json(m), Path("."), mouse, events.append, store, FakeHost())
    mouse.on_data = driver.on_data
    return driver, mouse, events, store


async def settle(driver, rounds: int = 3) -> None:
    """Let the driver finish what it is doing (all replies are immediate)."""
    for _ in range(rounds):
        for _ in range(50):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)


@pytest.fixture
def run():
    def runner(coro):
        return asyncio.run(asyncio.wait_for(coro, 5))

    return runner
