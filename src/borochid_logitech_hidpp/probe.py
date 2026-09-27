"""List what a HID++ 2.0 device supports, without changing anything.

    python -m borochid_logitech_hidpp.probe [--node /dev/hidrawN]

Without ``--node`` it probes every Logitech device paired to a receiver
(the per-device nodes ``hid-logitech-dj`` creates). Only getters are called:
feature discovery, names, firmware, DPI and report rate, onboard profile
description, mode and memory (read), and the button (control ID) table.
Nothing is set and flash is never written, so it is safe next to the kernel
driver and the Borochid service. The output is what device packages are
written from.
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import time

import pyudev

from borochid_logitech_hidpp import protocol
from borochid_logitech_hidpp.protocol import Error, Feature, FeatureFlags, u16

TIMEOUT = 1.0


class ProbeError(Exception):
    pass


class Device:
    def __init__(self, node: str):
        self.node = node
        self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        self.index: dict[int, int] = {Feature.ROOT: 0}

    def close(self) -> None:
        os.close(self.fd)

    def call(self, feature_index: int, function: int, *params: int) -> bytes:
        os.write(self.fd, protocol.request(feature_index, function, *params))
        deadline = time.monotonic() + TIMEOUT
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([self.fd], [], [], left)[0]:
                break
            try:
                msg = protocol.parse(os.read(self.fd, 64))
            except BlockingIOError:
                continue
            if msg is None or not protocol.is_reply_to(msg, feature_index, function):
                continue
            if isinstance(msg, Error):
                raise ProbeError(msg.name)
            return msg.params
        raise ProbeError("no reply (asleep, switched off or out of range?)")

    def feature(self, feature_id: int, function: int, *params: int) -> bytes:
        return self.call(self.index[feature_id], function, *params)

    def discover(self) -> list[tuple[int, int, int, int]]:
        """(index, feature id, flags, version) for every feature."""
        idx = self.call(0, 0, Feature.FEATURE_SET >> 8, Feature.FEATURE_SET & 0xFF)[0]
        count = self.call(idx, 0)[0]
        found = [(0, Feature.ROOT, 0, 0)]
        for i in range(1, count + 1):
            r = self.call(idx, 1, i)
            found.append((i, u16(r, 0), r[2], r[3]))
            self.index[u16(r, 0)] = i
        return found


def hexs(data: bytes) -> str:
    return data.rstrip(b"\0").hex(" ") or "00"


def section(title: str) -> None:
    print(f"\n## {title}")


def show(label: str, fn) -> None:
    try:
        print(f"  {label}: {fn()}")
    except ProbeError as e:
        print(f"  {label}: <{e}>")


def device_name(d: Device) -> str:
    length = d.feature(Feature.DEVICE_NAME, 0)[0]
    name = b""
    while len(name) < length:
        name += d.feature(Feature.DEVICE_NAME, 1, len(name))[: length - len(name)]
    return name.decode(errors="replace")


def dpi_list(raw: bytes) -> str:
    """Words after the sensor byte: DPI values, with ``0xE000|step`` meaning a
    range from the previous value to the next one."""
    out, i = [], 1
    while i + 1 < len(raw) and (w := u16(raw, i)):
        out.append(f"step {w & 0x1FFF}" if w >> 13 == 0b111 else str(w))
        i += 2
    return ", ".join(out)


def probe(d: Device) -> None:
    ver = d.call(0, 1, 0, 0, 0x5A)
    print(f"# {d.node}: HID++ {ver[0]}.{ver[1]}")
    if ver[0] < 2:
        print("  HID++ 1.0 only (a receiver, not a device); skipping")
        return

    section("Features (index, id, name, version, flags)")
    for i, fid, flags, version in d.discover():
        tags = [f.name.lower() for f in FeatureFlags if flags & f]
        print(f"  {i:3d}  0x{fid:04x}  {protocol.feature_name(fid):<34} v{version}  {' '.join(tags)}")
    has = d.index.__contains__

    section("Identity")
    if has(Feature.DEVICE_NAME):
        show("name", lambda: device_name(d))
    if has(Feature.DEVICE_FW_VERSION):
        n = d.feature(Feature.DEVICE_FW_VERSION, 0)[0]
        for e in range(n):
            show(f"firmware entity {e}", lambda e=e: hexs(d.feature(Feature.DEVICE_FW_VERSION, 1, e)))

    if has(Feature.ADJUSTABLE_DPI):
        section("ADJUSTABLE_DPI (0x2201)")
        sensors = d.feature(Feature.ADJUSTABLE_DPI, 0)[0]
        for s in range(sensors):
            show(f"sensor {s} list", lambda s=s: dpi_list(d.feature(Feature.ADJUSTABLE_DPI, 1, s)))
            show(f"sensor {s} current/default", lambda s=s: (lambda r: f"{u16(r, 1)} / {u16(r, 3)}")(d.feature(Feature.ADJUSTABLE_DPI, 2, s)))
    if has(Feature.EXTENDED_ADJUSTABLE_DPI):
        section("EXTENDED_ADJUSTABLE_DPI (0x2202), raw")
        for fn in range(3):
            show(f"fn{fn}(0)", lambda fn=fn: hexs(d.feature(Feature.EXTENDED_ADJUSTABLE_DPI, fn, 0)))
        show("fn5(0) parameters", lambda: hexs(d.feature(Feature.EXTENDED_ADJUSTABLE_DPI, 5, 0)))

    if has(Feature.REPORT_RATE):
        section("REPORT_RATE (0x8060)")
        show("supported (bit n = n+1 ms)", lambda: f"0b{d.feature(Feature.REPORT_RATE, 0)[0]:08b}")
        show("current (ms)", lambda: d.feature(Feature.REPORT_RATE, 1)[0])
    if has(Feature.EXTENDED_ADJUSTABLE_REPORT_RATE):
        section("EXTENDED_ADJUSTABLE_REPORT_RATE (0x8061), raw")
        for fn in range(3):
            show(f"fn{fn}", lambda fn=fn: hexs(d.feature(Feature.EXTENDED_ADJUSTABLE_REPORT_RATE, fn)))

    if has(Feature.ONBOARD_PROFILES):
        section("ONBOARD_PROFILES (0x8100), read only")
        f = Feature.ONBOARD_PROFILES
        try:
            desc = d.feature(f, 0)
            sector_size = u16(desc, 7)
            print(
                f"  description: memory model {desc[0]}, profile format {desc[1]}, macro format {desc[2]}, "
                f"profiles {desc[3]} (+{desc[4]} factory), buttons {desc[5]}, sectors {desc[6]} x {sector_size} B, "
                f"layout 0x{desc[9]:02x}, info 0x{desc[10]:02x}"
            )
        except ProbeError as e:
            print(f"  description: <{e}>")
            sector_size = 0
        show("mode (1 onboard, 2 host)", lambda: d.feature(f, 2)[0])
        show("current profile", lambda: f"0x{u16(d.feature(f, 4), 0):04x}")
        show("current DPI index", lambda: d.feature(f, 0x0B)[0])
        for label, sector in (("profile directory (sector 0x0000)", 0x0000),):
            show(label, lambda sector=sector: hexs(d.feature(f, 5, sector >> 8, sector & 0xFF, 0, 0)))
        try:
            current = u16(d.feature(f, 4), 0)
            data = b""
            while sector_size and len(data) < sector_size:
                off = min(len(data), sector_size - 16)  # reads may not cross the sector end
                chunk = d.feature(f, 5, current >> 8, current & 0xFF, off >> 8, off & 0xFF)
                data = data[:off] + chunk[:16]
            print(f"  current profile sector 0x{current:04x} ({len(data)} B):")
            for i in range(0, len(data), 16):
                print(f"    {i:04x}  {data[i:i + 16].hex(' ')}")
        except ProbeError as e:
            print(f"  current profile sector: <{e}>")

    if has(Feature.REPROG_CONTROLS_V4):
        section("REPROG_CONTROLS_V4 (0x1b04): cid, task, flags, pos, group, gmask, flags2 | reporting")
        f = Feature.REPROG_CONTROLS_V4
        for i in range(d.feature(f, 0)[0]):
            try:
                info = d.feature(f, 1, i)
                cid = u16(info, 0)
                rep = d.feature(f, 2, cid >> 8, cid & 0xFF)
                print(
                    f"  0x{cid:04x} task 0x{u16(info, 2):04x} flags 0x{info[4]:02x} pos {info[5]} "
                    f"group {info[6]} gmask 0x{info[7]:02x} flags2 0x{info[8]:02x} | {hexs(rep[2:])}"
                )
            except ProbeError as e:
                print(f"  control {i}: <{e}>")

    if has(Feature.HIRES_WHEEL):
        section("HIRES_WHEEL (0x2121)")
        show("capability (multiplier, flags)", lambda: hexs(d.feature(Feature.HIRES_WHEEL, 0)))
        show("mode flags", lambda: hexs(d.feature(Feature.HIRES_WHEEL, 1)))
        show("ratchet switch", lambda: hexs(d.feature(Feature.HIRES_WHEEL, 3)))

    for fid in (Feature.MOUSE_BUTTON_SPY, Feature.MODE_STATUS, Feature.LATENCY_MONITORING):
        if has(fid):
            section(f"{fid.name} (0x{fid:04x}), raw fn0")
            show("fn0", lambda fid=fid: hexs(d.feature(fid, 0)))


def receiver_children() -> list[str]:
    """hidraw nodes of Logitech devices paired to a receiver: their HID
    device's parent is another HID device (the receiver) rather than USB."""
    nodes = []
    for dev in pyudev.Context().list_devices(subsystem="hidraw"):
        hid = dev.find_parent("hid")
        if hid is None or ":046D:" not in hid.sys_name.upper():
            continue
        if hid.parent is not None and hid.parent.subsystem == "hid" and dev.device_node:
            nodes.append(dev.device_node)
    return sorted(nodes)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m borochid_logitech_hidpp.probe")
    ap.add_argument("--node", action="append", help="hidraw node (default: every receiver-paired Logitech device)")
    args = ap.parse_args()
    nodes = args.node or receiver_children()
    if not nodes:
        sys.exit("no Logitech receiver-paired devices found; pass --node")
    for node in nodes:
        try:
            d = Device(node)
        except PermissionError:
            print(f"# {node}: permission denied (install the udev rule, or run once with sudo)")
            continue
        try:
            probe(d)
        except ProbeError as e:
            print(f"# {node}: {e}")
        finally:
            d.close()
        print()


if __name__ == "__main__":
    main()
