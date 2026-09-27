"""This mouse's settings per Borochid profile, never stored in the mouse.
Pure: no I/O.

Profiles themselves (names, which one is active) belong to the service and
are shared by every device (borochid.service.profiles); this module keeps
what the mouse does in each, keyed by the service's profile id::

    {"stages": [800, 1600, 3200],     1-5 DPI stages
     "default_stage": 2,              1-based; the stage a profile starts in
     "shift_dpi": 400,                while a "dpi_shift" button is held
     "report_rate": 1000,             Hz
     "bindings": {"back": {"button": 4}, ...}}

Buttons missing from ``bindings`` do what the device package's defaults say.
Everything read from settings is validated again, so a hand-edited or old
settings file can't put the driver in a bad state: bad fields fall back to
the defaults and are logged.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from borochid_logitech_hidpp import buttons as bindings

log = logging.getLogger(__name__)

MAX_STAGES = 5
DEFAULT_ID = "default"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    dpi_min: int = 100
    dpi_max: int = 25600
    dpi_step: int = 50
    rates: tuple[int, ...] = (1000, 500, 250, 125)

    def dpi(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ProfileError("DPI must be a number")
        try:
            v = int(value)
        except ValueError:
            raise ProfileError("DPI must be a number") from None
        if not self.dpi_min <= v <= self.dpi_max:
            raise ProfileError(f"DPI must be {self.dpi_min}-{self.dpi_max}")
        return self.dpi_min + round((v - self.dpi_min) / self.dpi_step) * self.dpi_step

    def rate(self, value: Any) -> int:
        if value not in self.rates or isinstance(value, bool):
            raise ProfileError(f"report rate must be one of {', '.join(map(str, self.rates))} Hz")
        return int(value)


@dataclass
class Profile:
    stages: list[int]
    default_stage: int
    shift_dpi: int
    report_rate: int
    bindings: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stages": list(self.stages),
            "default_stage": self.default_stage,
            "shift_dpi": self.shift_dpi,
            "report_rate": self.report_rate,
            "bindings": copy.deepcopy(self.bindings),
        }

    @classmethod
    def parse(cls, raw: Any, defaults: Profile, limits: Limits, buttons: set[str]) -> Profile:
        """Tolerant: each bad field falls back to the default."""
        raw = raw if isinstance(raw, dict) else {}
        p = copy.deepcopy(defaults)

        def take(key: str, fn):
            if key in raw:
                try:
                    return fn(raw[key])
                except (ProfileError, bindings.BindingError, TypeError, ValueError) as e:
                    log.warning("ignoring stored profile %s=%r: %s", key, raw[key], e)
            return getattr(p, key)

        p.stages = take("stages", lambda v: stages(v, limits))
        p.default_stage = take("default_stage", lambda v: stage_number(v, p.stages))
        p.shift_dpi = take("shift_dpi", limits.dpi)
        p.report_rate = take("report_rate", limits.rate)
        stored = raw.get("bindings", {}) if isinstance(raw.get("bindings"), dict) else {}
        for button, value in stored.items():
            if button not in buttons:
                continue
            try:
                p.bindings[button] = bindings.validate(value)
            except bindings.BindingError as e:
                log.warning("ignoring stored binding %s=%r: %s", button, value, e)
        if not 1 <= p.default_stage <= len(p.stages):
            p.default_stage = 1
        return p


def stages(value: Any, limits: Limits) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STAGES:
        raise ProfileError(f"a profile has 1-{MAX_STAGES} DPI stages")
    return [limits.dpi(v) for v in value]


def stage_number(value: Any, current: list[int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= len(current):
        raise ProfileError(f"stage must be 1-{len(current)}")
    return value


class Profiles:
    """Settings per profile id, and which one is in use, as stored::

        {"profiles": {"default": {...}, "p3f9a1c2e": {...}}}
    """

    def __init__(self, defaults: Profile, limits: Limits, buttons: set[str]):
        self.defaults = defaults
        self.limits = limits
        self.buttons = buttons
        self.items: dict[str, Profile] = {DEFAULT_ID: copy.deepcopy(defaults)}
        self.active = DEFAULT_ID

    def load(self, settings: dict[str, Any]) -> None:
        raw = settings.get("profiles")
        if isinstance(raw, dict):
            items = {k: Profile.parse(v, self.defaults, self.limits, self.buttons) for k, v in raw.items() if isinstance(k, str)}
            self.items = items or {DEFAULT_ID: copy.deepcopy(self.defaults)}
        if self.active not in self.items:
            self.items[self.active] = copy.deepcopy(self.defaults)

    def dump(self) -> dict[str, Any]:
        return {"profiles": {k: p.to_json() for k, p in self.items.items()}}

    @property
    def current(self) -> Profile:
        return self.items[self.active]

    def use(self, pid: str, copy_of: str | None = None, known: set[str] | None = None) -> None:
        """Switch to ``pid``, creating it from ``copy_of`` (or the defaults)
        the first time; drop profiles not in ``known``."""
        if pid not in self.items:
            source = self.items.get(copy_of) if copy_of else None
            self.items[pid] = copy.deepcopy(source or self.defaults)
        self.active = pid
        if known is not None:
            for stale in set(self.items) - set(known) - {pid}:
                del self.items[stale]

    # -- stage edits on the current profile -----------------------------------

    def add_stage(self, dpi: Any) -> int:
        p = self.current
        if len(p.stages) >= MAX_STAGES:
            raise ProfileError(f"at most {MAX_STAGES} DPI stages")
        p.stages.append(self.limits.dpi(dpi))
        return len(p.stages)

    def remove_stage(self, stage: Any) -> None:
        p = self.current
        n = stage_number(stage, p.stages)
        if len(p.stages) == 1:
            raise ProfileError("a profile needs at least one DPI stage")
        del p.stages[n - 1]
        if p.default_stage > n or p.default_stage > len(p.stages):
            p.default_stage = max(1, p.default_stage - 1)

    def set_stage(self, stage: Any, dpi: Any) -> None:
        p = self.current
        p.stages[stage_number(stage, p.stages) - 1] = self.limits.dpi(dpi)
