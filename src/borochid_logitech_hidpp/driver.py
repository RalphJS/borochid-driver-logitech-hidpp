"""Borochid driver for Logitech mice that speak HID++ 2.0.

The driver keeps the mouse in **host mode** while it runs: the mouse stops
running its onboard profile, and the driver applies its settings for the active Borochid
profile (DPI stages, report rate, button bindings; see ``profiles.py``).
Profiles are the service's, shared by all devices; switching one switches
the mouse too (``use_profile``). Settings live in the service's settings
store, never in the mouse: this driver never writes the mouse's profile
memory (flash).

* Mouse-button remaps go into the mouse's RAM remap table (MOUSE_BUTTON_SPY
  fn4), so the click stays a real click from the mouse.
* Key chords and wheel steps: the button's table entry is 0, so the mouse
  reports it only to this driver (a spy notification), which replays the
  chord through the service's virtual input device for as long as the
  button is held.
* DPI actions (up, down, cycle, shift) are done here.

Link states, published as ``link``: ``connecting``, ``online``, ``asleep``.
An idle mouse (60 s on a G502 X) answers nothing until it is moved, so a
missing reply means "asleep", not "gone"; the driver sets everything up
again on the next report from the mouse.

**Host mode does not survive a power cycle** (measured on a G502 X): the
mouse comes back in onboard mode on the same HID node and announces it with
a WIRELESS_DEVICE_STATUS notification. The driver takes over again then,
and, as a fallback, checks the mode whenever the mouse becomes active after
``wake_check_s`` of silence. The flip side is safe: if the service dies, a
power cycle hands the mouse back to its onboard profile. ``stop()`` does
that at once, restoring the onboard DPI level too.

**On its cable** the mouse is a USB device of its own (the service shows it
as one device with the receiver connection, by its unit ID). The kernel
reads the battery through the receiver only, so on the cable this driver
reads it (UNIFIED_BATTERY) and publishes ``battery`` and ``charging``; the
package points that connection's battery at them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from borochid.service.drivers import Driver, DriverError
from borochid.service.host.input import InputError
from borochid.service.profiles import Profile as SharedProfile

from borochid_logitech_hidpp import buttons as bindings
from borochid_logitech_hidpp import profiles, protocol
from borochid_logitech_hidpp.profile import Model
from borochid_logitech_hidpp.profile import ProfileError as ModelError
from borochid_logitech_hidpp.protocol import Feature, Message, u16
from borochid_logitech_hidpp.session import HidppError, NoReply, Session, Unsupported

log = logging.getLogger(__name__)

ONBOARD_MODE, HOST_MODE = 1, 2


class HidppDriver(Driver):
    supports_profiles = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.model = Model.from_manifest(self.manifest.raw)
        except ModelError as e:
            raise DriverError(f"{self.manifest.id}: {e}") from e
        self.session = Session(self.channel.write, self.model.reply_timeout_s)
        self.remappable = [b for b in self.model.buttons if b.remap]
        self.profiles = profiles.Profiles(self.model.defaults, profiles.Limits(), {b.id for b in self.remappable})
        self.profiles.load(self.settings)
        self.profile_name = "Default"
        self._shared: tuple[str, str | None, set[str]] | None = None  # the last use_profile()
        self.button_count = max(b.number for b in self.model.buttons)
        self.mode: int | None = None
        self._onboard_level: int | None = None  # restored when handing the mouse back
        self._resolved: dict[str, bindings.Resolved] = {}
        self._stage = self.profiles.current.default_stage
        self._mask = 0
        self._shifted = False
        self._last_activity = time.monotonic()
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._ready = False  # set up in this connection
        # On the cable rather than behind a receiver: the kernel has no battery for it.
        self.wired = not self.channel.ident.attrs.get("receiver")

        self.state = {"link": "connecting", "status": "Connecting", "dpi": None, "input_error": None}
        self.state.update(self._profile_state())

    # -- state ---------------------------------------------------------------------

    def _profile_state(self) -> dict[str, Any]:
        p = self.profiles.current
        out: dict[str, Any] = {
            "stages": list(p.stages),
            "default_stage": p.default_stage,
            "stage": self._stage,
            "shift_dpi": p.shift_dpi,
            "report_rate": p.report_rate,
        }
        for b in self.remappable:
            out[f"bind.{b.id}"] = p.bindings.get(b.id, b.default_binding)
        return out

    def _status(self) -> str:
        if self.state.get("link") == "asleep":
            return "Asleep"
        return self.profile_name

    def _save(self) -> None:
        self.settings.update(self.profiles.dump())
        self.save_settings()

    # -- lifecycle -----------------------------------------------------------------

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            if isinstance(exc, NoReply):
                self._went_quiet()
            else:
                log.error("%s: background task failed: %s", self.channel.ident.uid, exc, exc_info=exc)

    async def start(self) -> None:
        # Bring-up doesn't wait for the mouse: it may be asleep for hours.
        self.spawn(self._run())

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._setup()
            except NoReply:
                self._went_quiet()
            except (HidppError, Unsupported) as e:
                log.error("%s: cannot set up the mouse: %s", self.channel.ident.uid, e)
                self.publish({"link": "error", "status": f"Error: {e}"})
            await self._wake.wait()

    def _went_quiet(self) -> None:
        self._ready = False
        self._release_all()
        self.publish({"link": "asleep", "status": "Asleep"})

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        if self.mode == HOST_MODE:
            with contextlib.suppress(NoReply, HidppError, Unsupported, OSError):
                await self._leave_host()
        self._release_all()
        if self.host and self.host.input:
            self.host.input.close()

    # -- setup -----------------------------------------------------------------------

    async def _setup(self) -> None:
        self.session.forget()
        s = self.session
        # First, so the mouse's own settings are the ones applied below.
        if unit := await self._read_unit_id():
            await self.identify(unit)
        lo, hi, step = await self._dpi_capabilities()
        mask = (await s.feature(Feature.REPORT_RATE, 0))[0]
        rates = tuple(sorted({1000 // (n + 1) for n in range(8) if mask >> n & 1}, reverse=True))
        self.profiles.limits = profiles.Limits(lo, hi, step, rates or profiles.Limits.rates)
        self.button_count = (await s.feature(Feature.MOUSE_BUTTON_SPY, 0))[0] or self.button_count
        await s.has(Feature.WIRELESS_DEVICE_STATUS)  # cache its index, to recognise its notification
        self.mode = (await s.feature(Feature.ONBOARD_PROFILES, 2))[0]
        if self.mode != HOST_MODE:
            self._onboard_level = (await s.feature(Feature.ONBOARD_PROFILES, 0x0B))[0]
        await self._enter_host()
        if self.wired:
            await self._read_battery()
        self._ready = True
        self._last_activity = time.monotonic()
        self.publish({"link": "online"})
        self.publish({"status": self._status()})
        log.info("%s: online, profile %r", self.channel.ident.uid, self.profile_name)

    async def _read_unit_id(self) -> str | None:
        """The mouse's unit ID (DEVICE_FW_VERSION getDeviceInfo): the same
        through the receiver and on the cable, unlike the USB serial or port."""
        try:
            info = await self.session.feature(Feature.DEVICE_FW_VERSION, 0)
        except (HidppError, Unsupported):
            return None
        unit = bytes(info[1:5])
        return unit.hex().upper() if any(unit) else None

    async def _read_battery(self) -> None:
        try:
            self._battery(await self.session.feature(Feature.UNIFIED_BATTERY, 1))
        except (HidppError, Unsupported) as e:
            log.info("%s: no battery reading: %s", self.channel.ident.uid, e)

    def _battery(self, params: bytes) -> None:
        """UNIFIED_BATTERY status (fn1 reply or event 0): charge %, level
        flags, charging status (1 charging, 2 slowly, 3 full), external power."""
        self.publish({"battery": params[0], "charging": params[2] in (1, 2, 3)})

    async def settings_reloaded(self) -> None:
        """The mouse's own settings were found (or its other connection
        changed them): rebuild the profiles and, if it listens, apply them."""
        limits = self.profiles.limits
        self.profiles = profiles.Profiles(self.model.defaults, limits, {b.id for b in self.remappable})
        self.profiles.load(self.settings)
        if self._shared is not None:
            self.profiles.use(*self._shared)
        self._stage = self.profiles.current.default_stage
        self._release_all()
        try:
            await self._changed("profile")
        except NoReply:
            self._went_quiet()

    async def _dpi_capabilities(self) -> tuple[int, int, int]:
        """ADJUSTABLE_DPI fn1: DPI words, ``0xE000|step`` between two values
        marking a range. Returns (min, max, step)."""
        raw = await self.session.feature(Feature.ADJUSTABLE_DPI, 1, 0)
        words, i = [], 1
        while i + 1 < len(raw) and (w := u16(raw, i)):
            words.append(w)
            i += 2
        values = [w for w in words if w >> 13 != 0b111]
        steps = [w & 0x1FFF for w in words if w >> 13 == 0b111]
        if not values:
            raise HidppError(Feature.ADJUSTABLE_DPI, 1, 0, "empty DPI list")
        return min(values), max(values), (steps[0] if steps else 1)

    async def _enter_host(self) -> None:
        s = self.session
        await s.feature(Feature.ONBOARD_PROFILES, 1, HOST_MODE)
        self.mode = HOST_MODE
        await s.feature(Feature.MOUSE_BUTTON_SPY, 1)
        self._mask = 0
        await self._apply_profile()

    async def _apply_profile(self) -> None:
        """Push the whole active profile to the mouse."""
        await self._apply_bindings()
        await self._apply_dpi()
        await self.session.feature(Feature.REPORT_RATE, 2, 1000 // self.profiles.current.report_rate)

    async def _leave_host(self) -> None:
        s = self.session
        self._release_all()
        with contextlib.suppress(HidppError, Unsupported):
            await s.feature(Feature.MOUSE_BUTTON_SPY, 4, *range(1, self.button_count + 1))
            await s.feature(Feature.MOUSE_BUTTON_SPY, 2)
        await s.feature(Feature.ONBOARD_PROFILES, 1, ONBOARD_MODE)
        self.mode = ONBOARD_MODE
        # The mouse keeps the last DPI the host set; re-selecting the onboard
        # level makes the firmware apply its profile's value again.
        if self._onboard_level is not None:
            with contextlib.suppress(HidppError):
                await s.feature(Feature.ONBOARD_PROFILES, 0x0C, self._onboard_level)

    async def _apply_bindings(self) -> None:
        table = list(range(1, self.button_count + 1))
        p = self.profiles.current
        self._resolved = {}
        for b in self.remappable:
            r = bindings.resolve(p.bindings.get(b.id, b.default_binding))
            self._resolved[b.id] = r
            if b.number <= self.button_count:
                table[b.number - 1] = r.slot
        await self.session.feature(Feature.MOUSE_BUTTON_SPY, 4, *table)
        await self._open_input()

    async def _open_input(self) -> None:
        if not any(r.chord for r in self._resolved.values()):
            return
        if not (self.host and self.host.input):
            self.publish({"input_error": "this package does not enable the input service"})
            return
        try:
            await self.host.input.open()
            self.publish({"input_error": None})
        except InputError as e:
            log.warning("%s: %s", self.channel.ident.uid, e)
            self.publish({"input_error": str(e)})

    async def _apply_dpi(self) -> None:
        p = self.profiles.current
        self._stage = min(max(self._stage, 1), len(p.stages))
        dpi = p.shift_dpi if self._shifted else p.stages[self._stage - 1]
        await self.session.feature(Feature.ADJUSTABLE_DPI, 3, 0, dpi >> 8, dpi & 0xFF)
        self.publish({"dpi": dpi, "stage": self._stage})

    # -- input from the mouse ------------------------------------------------------

    def on_data(self, data: bytes) -> None:
        msg = protocol.parse(data)
        if msg is None or msg.sw_id == 0:
            self._activity()
        if msg is None:
            return
        if msg.sw_id == protocol.SW_ID:
            self.session.feed(msg)
        elif msg.sw_id == 0 and isinstance(msg, Message):
            self._notification(msg)

    def _activity(self) -> None:
        now = time.monotonic()
        idle = now - self._last_activity
        self._last_activity = now
        if self.state["link"] == "asleep":
            self._wake.set()  # it's back: set everything up again
        elif self._ready and idle > self.model.wake_check_s:
            self.spawn(self._check_mode())

    async def _check_mode(self) -> None:
        if (await self.session.feature(Feature.ONBOARD_PROFILES, 2))[0] != HOST_MODE:
            log.info("%s: mouse is back in onboard mode; taking over again", self.channel.ident.uid)
            self._ready = False
            self._wake.set()

    def _notification(self, msg: Message) -> None:
        index = self.session.index
        if msg.feature_index == index.get(Feature.MOUSE_BUTTON_SPY) and msg.function == 0:
            self._buttons(u16(msg.params, 0))
        elif msg.feature_index == index.get(Feature.UNIFIED_BATTERY) and msg.function == 0:
            if self.wired:
                self._battery(msg.params)
        elif msg.feature_index == index.get(Feature.WIRELESS_DEVICE_STATUS):
            # Power-on or reconnect: the mouse has forgotten host mode.
            log.info("%s: mouse reconnected", self.channel.ident.uid)
            self._ready = False
            self._wake.set()

    def _buttons(self, mask: int) -> None:
        if self.mode != HOST_MODE:
            return
        changed, self._mask = mask ^ self._mask, mask
        for b in self.remappable:
            if not changed & b.bit or (r := self._resolved.get(b.id)) is None or not r.host_handled:
                continue
            down = bool(mask & b.bit)
            if r.chord is not None and self.host and self.host.input:
                if down:
                    self.host.input.down(b.id, r.chord)
                else:
                    self.host.input.up(b.id)
            elif r.dpi is not None:
                self.spawn(self._dpi_button(r.dpi, down))

    def _release_all(self) -> None:
        self._mask = 0
        self._shifted = False
        if self.host and self.host.input:
            for b in self.remappable:
                self.host.input.up(b.id)

    async def _dpi_button(self, action: str, down: bool) -> None:
        if action == "dpi_shift":
            self._shifted = down
        elif down:
            n = len(self.profiles.current.stages)
            if action == "dpi_up":
                self._stage = min(self._stage + 1, n)
            elif action == "dpi_down":
                self._stage = max(self._stage - 1, 1)
            else:
                self._stage = self._stage % n + 1
        else:
            return
        await self._apply_dpi()

    # -- actions ---------------------------------------------------------------------

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise DriverError(f"unknown action {action!r}")
        try:
            return await handler(params)
        except (profiles.ProfileError, bindings.BindingError, ModelError) as e:
            raise DriverError(str(e)) from None
        except NoReply:
            # Saved and shown; applied when the mouse wakes up.
            self._went_quiet()
        except (HidppError, Unsupported) as e:
            raise DriverError(str(e)) from e

    @staticmethod
    def _param(params: dict[str, Any], name: str) -> Any:
        if name not in params:
            raise DriverError(f"missing parameter {name!r}")
        return params[name]

    async def _changed(self, apply: str | None) -> None:
        """Save, publish, and push ``apply`` ("profile", "bindings", "dpi",
        "rate") to the mouse if it is listening."""
        self._save()
        self.publish({**self._profile_state(), "status": self._status()})
        if not self._ready or apply is None:
            return
        if apply == "profile":
            await self._apply_profile()
        elif apply == "bindings":
            await self._apply_bindings()
        elif apply == "dpi":
            await self._apply_dpi()
        elif apply == "rate":
            await self.session.feature(Feature.REPORT_RATE, 2, 1000 // self.profiles.current.report_rate)

    # profiles (the service's, shared by every device)

    async def use_profile(self, profile: SharedProfile, known: set[str]) -> None:
        self._shared = (profile.id, profile.copy_of, set(known))
        self.profiles.use(profile.id, profile.copy_of, known)
        self.profile_name = profile.name
        self._stage = self.profiles.current.default_stage
        self._release_all()
        try:
            await self._changed("profile")
        except NoReply:
            self._went_quiet()  # applied when the mouse wakes up

    # DPI and report rate (current profile)

    async def _do_add_stage(self, params: dict[str, Any]) -> None:
        stages = self.profiles.current.stages
        self.profiles.add_stage(params.get("value", stages[-1]))
        await self._changed(None)

    async def _do_remove_stage(self, params: dict[str, Any]) -> None:
        stage = self._param(params, "stage")
        self.profiles.remove_stage(stage)
        if self._stage >= stage and self._stage > 1:
            self._stage -= 1
        await self._changed("dpi")

    async def _do_set_stage(self, params: dict[str, Any]) -> None:
        self.profiles.set_stage(self._param(params, "stage"), self._param(params, "value"))
        await self._changed("dpi")

    async def _do_set_default_stage(self, params: dict[str, Any]) -> None:
        p = self.profiles.current
        p.default_stage = profiles.stage_number(self._param(params, "stage"), p.stages)
        await self._changed(None)

    async def _do_select_stage(self, params: dict[str, Any]) -> None:
        self._stage = profiles.stage_number(self._param(params, "stage"), self.profiles.current.stages)
        await self._changed("dpi")

    async def _do_set_shift_dpi(self, params: dict[str, Any]) -> None:
        self.profiles.current.shift_dpi = self.profiles.limits.dpi(self._param(params, "value"))
        await self._changed("dpi" if self._shifted else None)

    async def _do_set_report_rate(self, params: dict[str, Any]) -> None:
        self.profiles.current.report_rate = self.profiles.limits.rate(self._param(params, "value"))
        await self._changed("rate")

    # buttons (current profile)

    async def _do_set_binding(self, params: dict[str, Any]) -> None:
        b = self.model.button(self._param(params, "button"))
        if not b.remap:
            raise DriverError(f"{b.label} can't be remapped")
        value = bindings.validate(self._param(params, "binding"))
        if self.host and self.host.input:
            self.host.input.up(b.id)
        self.profiles.current.bindings[b.id] = value
        await self._changed("bindings")

    async def _do_reset_binding(self, params: dict[str, Any]) -> None:
        b = self.model.button(self._param(params, "button"))
        if self.host and self.host.input:
            self.host.input.up(b.id)
        self.profiles.current.bindings.pop(b.id, None)
        await self._changed("bindings")
