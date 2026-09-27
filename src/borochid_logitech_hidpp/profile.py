"""The device package's ``hidpp`` section, validated. Holds the facts about a
model that the protocol can't report: what each button is called, what it
does out of the box, and the starting profile.

    "hidpp": {
      "buttons": [
        {"id": "left", "label": "Left click", "number": 1, "remap": false},
        {"id": "g4", "label": "G4 (back)", "number": 4, "default": {"button": 4}}
      ],
      "defaults": {"stages": [800, 1600, 3200], "default_stage": 2,
                   "shift_dpi": 400, "report_rate": 1000}
    }

``number`` is the button's position in the mouse's button reports (bit
``number - 1``). ``"remap": false`` keeps a button out of remapping, so the
left click can't be taken away. ``default`` is a binding (see
``buttons.py``); without one the button sends its own mouse button number.
DPI range and report rates are read from the device.

Validate a package with ``python -m borochid_logitech_hidpp.profile MANIFEST``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from borochid_logitech_hidpp import buttons as bindings
from borochid_logitech_hidpp import profiles

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_BUTTONS = 16


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Button:
    id: str
    label: str
    number: int
    remap: bool = True
    default: Any = None

    @property
    def bit(self) -> int:
        return 1 << (self.number - 1)

    @property
    def default_binding(self) -> Any:
        return self.default if self.default is not None else {"button": self.number}


@dataclass(frozen=True)
class Model:
    buttons: tuple[Button, ...]
    defaults: profiles.Profile
    reply_timeout_s: float = 0.5
    wake_check_s: float = 20.0

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> Model:
        spec = manifest.get("hidpp")
        if not isinstance(spec, dict):
            raise ProfileError("manifest needs a 'hidpp' section")
        buttons = []
        for b in spec.get("buttons", []):
            if not isinstance(b, dict) or not _ID_RE.match(str(b.get("id", ""))):
                raise ProfileError(f"button id must match {_ID_RE.pattern}: {b!r}")
            n = b.get("number")
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_BUTTONS:
                raise ProfileError(f"button {b['id']}: number must be 1-{MAX_BUTTONS}")
            default = None
            if "default" in b:
                try:
                    default = bindings.validate(b["default"])
                except bindings.BindingError as e:
                    raise ProfileError(f"button {b['id']}: default: {e}") from None
            buttons.append(Button(b["id"], str(b.get("label", b["id"])), n, bool(b.get("remap", True)), default))
        if not buttons:
            raise ProfileError("hidpp.buttons is empty")
        if len({b.id for b in buttons}) != len(buttons) or len({b.number for b in buttons}) != len(buttons):
            raise ProfileError("button ids and numbers must be unique")
        d = spec.get("defaults", {})
        limits = profiles.Limits()
        try:
            stages = profiles.stages(d.get("stages", [800, 1600, 3200]), limits)
            defaults = profiles.Profile(
                stages=stages,
                default_stage=profiles.stage_number(d.get("default_stage", 1), stages),
                shift_dpi=limits.dpi(d.get("shift_dpi", 400)),
                report_rate=limits.rate(d.get("report_rate", 1000)),
                bindings={b.id: b.default_binding for b in buttons if b.remap},
            )
        except (profiles.ProfileError, AttributeError) as e:
            raise ProfileError(f"hidpp.defaults: {e}") from None
        timing = {k: float(spec[k]) for k in ("reply_timeout_s", "wake_check_s") if k in spec}
        return cls(tuple(buttons), defaults, **timing)

    def button(self, button_id: str) -> Button:
        for b in self.buttons:
            if b.id == button_id:
                return b
        raise ProfileError(f"no button {button_id!r}")


def main() -> None:
    for arg in sys.argv[1:]:
        try:
            m = Model.from_manifest(json.loads(Path(arg).read_text()))
        except (OSError, ValueError) as e:
            sys.exit(f"{arg}: {e}")
        print(f"{arg}: {len(m.buttons)} buttons, default profile {m.defaults.stages} DPI: OK")


if __name__ == "__main__":
    main()
