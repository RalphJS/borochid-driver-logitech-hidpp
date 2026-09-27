"""What a button does while software control is on. Pure: no I/O.

A binding is one of::

    "disabled"         nothing
    {"button": n}      mouse button n                  -> done by the mouse
    {"keys": [...]}    a key chord                     -> host.input
    {"wheel": ±1}      a wheel step (also "hwheel")    -> host.input
    "dpi_up", "dpi_down", "dpi_cycle", "dpi_shift"     -> this driver

Mouse buttons are remapped inside the mouse, in RAM (MOUSE_BUTTON_SPY's
remap table): the mouse keeps sending a normal click, only under another
number. Everything else sets the button's table entry to 0, so the mouse
reports it only to this driver (a spy event), and the driver acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from borochid.service.host.input import Chord, InputError

DPI_ACTIONS = ("dpi_up", "dpi_down", "dpi_cycle", "dpi_shift")
NAMED = ("disabled", *DPI_ACTIONS)
MAX_BUTTON = 16


class BindingError(ValueError):
    pass


def validate(value: Any) -> Any:
    """A binding as stored and published; raises BindingError."""
    if isinstance(value, str):
        if value not in NAMED:
            raise BindingError(f"unknown binding {value!r}")
        return value
    if isinstance(value, dict) and set(value) == {"button"}:
        n = value["button"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_BUTTON:
            raise BindingError(f"mouse button must be 1-{MAX_BUTTON}")
        return {"button": n}
    try:
        return Chord.parse(value).to_json()
    except InputError as e:
        raise BindingError(str(e)) from None


@dataclass(frozen=True)
class Resolved:
    """What the mouse and the host each do for one button."""

    slot: int  # the mouse's remap table entry: a mouse button number, or 0
    chord: Chord | None = None  # replayed through host.input
    dpi: str | None = None  # one of DPI_ACTIONS

    @property
    def host_handled(self) -> bool:
        return self.chord is not None or self.dpi is not None


def resolve(binding: Any) -> Resolved:
    """``binding`` must already be validated."""
    if binding == "disabled":
        return Resolved(slot=0)
    if binding in DPI_ACTIONS:
        return Resolved(slot=0, dpi=binding)
    if isinstance(binding, dict) and "button" in binding:
        return Resolved(slot=binding["button"])
    return Resolved(slot=0, chord=Chord.parse(binding))
