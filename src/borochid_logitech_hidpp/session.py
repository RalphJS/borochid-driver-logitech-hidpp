"""HID++ 2.0 request/reply on top of Borochid's push-based HID channel.

The channel hands every input report to the driver: mouse movement, the
kernel driver's own HID++ replies (sw id 1), notifications (sw id 0) and
our replies. Only the last kind is fed here. One request is in flight at a
time; the device answers in order, and a reply is matched on feature index,
function and software ID.

Feature indexes differ per device and firmware, so they are looked up
through ROOT once per connection and cached until ``forget()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from borochid_logitech_hidpp import protocol
from borochid_logitech_hidpp.protocol import Error, Feature, Message


class HidppError(Exception):
    def __init__(self, feature: int, function: int, code: int, name: str):
        super().__init__(f"{protocol.feature_name(feature)} fn{function}: {name}")
        self.code = code


class NoReply(Exception):
    """The device did not answer: asleep, switched off or out of range. Also
    raised when its receiver answers instead (UNKNOWN_DEVICE: the device is
    on its cable or switched off)."""


class Unsupported(Exception):
    """The device does not have this feature."""


class Session:
    def __init__(self, write: Callable[[bytes], Awaitable[Any]], timeout: float = 0.5):
        self._write = write
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._pending: tuple[int, int, asyncio.Future] | None = None
        self.index: dict[int, int | None] = {Feature.ROOT: 0}

    def forget(self) -> None:
        """Drop cached feature indexes (another firmware may answer next)."""
        self.index = {Feature.ROOT: 0}

    def feed(self, msg: Message | Error) -> bool:
        """Offer an input message; True if it answered our pending request."""
        if self._pending is None:
            return False
        feature_index, function, fut = self._pending
        if fut.done() or not protocol.is_reply_to(msg, feature_index, function):
            return False
        fut.set_result(msg)
        return True

    async def call(self, feature_index: int, function: int, *params: int, timeout: float | None = None) -> bytes:
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._pending = (feature_index, function, fut)
            try:
                await self._write(protocol.request(feature_index, function, *params))
                try:
                    msg = await asyncio.wait_for(fut, timeout or self.timeout)
                except TimeoutError:
                    raise NoReply(f"no reply to feature index {feature_index} fn{function}") from None
            finally:
                self._pending = None
        if isinstance(msg, Error):
            if msg.from_receiver:
                raise NoReply(f"the receiver answered for the device: {msg.name}")
            feature = next((f for f, i in self.index.items() if i == feature_index), feature_index)
            raise HidppError(feature, function, msg.code, msg.name)
        return msg.params

    async def feature_index(self, feature: int) -> int:
        if feature not in self.index:
            reply = await self.call(0, 0, feature >> 8, feature & 0xFF)
            # Index 0 in a reply means "not supported" (only ROOT lives there).
            self.index[feature] = reply[0] or None
        if (index := self.index[feature]) is None:
            raise Unsupported(protocol.feature_name(feature))
        return index

    async def has(self, feature: int) -> bool:
        try:
            await self.feature_index(feature)
        except Unsupported:
            return False
        return True

    async def feature(self, feature: int, function: int, *params: int, timeout: float | None = None) -> bytes:
        return await self.call(await self.feature_index(feature), function, *params, timeout=timeout)
