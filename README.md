# borochid-driver-logitech-hidpp

[Borochid](../borochid) driver for Logitech mice that speak **HID++ 2.0**
(first model: G502 X LIGHTSPEED).

This repo is **code only**. Mouse models are described by signed data
packages (for example [`borochid-logitech-g502x`](../borochid-logitech-g502x)):
button names and numbers, default bindings and the starting profile. A
model with the same features needs a new device package and a line in the
udev rule, not new driver code.

## What it does

While the service runs, the driver keeps the mouse in **host mode** and
applies the active Borochid profile:

* **Profiles** are Borochid's, shared by every device (switching one
  switches them all). This driver keeps the mouse's settings for each, in
  the service's settings store, never in the mouse: 1-5 DPI stages with a
  default, a shift DPI, a report rate and a binding per button.
* **Bindings**: another mouse button (remapped inside the mouse, in RAM),
  a key shortcut or a wheel step (replayed through the service's virtual
  input device while the button is held), DPI up/down/cycle/shift, or
  nothing. The left click can't be remapped.
* **Battery** comes from the kernel (`host.power`), not from this driver,
  except on the cable, where the kernel has none: there the driver reads
  `UNIFIED_BATTERY` and publishes `battery` and `charging`.

**The driver never writes the mouse's profile memory (flash).** When the
service stops, the mouse goes back to onboard mode and its own profile.

## Design

| Module | Role |
|---|---|
| `protocol.py` | HID++ 2.0 framing and parsing. Pure. |
| `session.py` | Request/reply over the push-based channel; feature index lookup. |
| `profiles.py` | The mouse's settings per Borochid profile: model, validation, edits. Pure. |
| `buttons.py` | Binding vocabulary; what the mouse vs the host does for each. Pure. |
| `profile.py` | The device package's `hidpp` section (buttons, defaults), validated. |
| `driver.py` | Link state machine (`connecting`/`online`/`asleep`), host mode, spy events, actions. |
| `probe.py` | Read-only report of what a mouse supports. |

## Measured on a G502 X LIGHTSPEED (`046d:409f`, receiver `c547`)

These shaped the design; the tests' `FakeMouse` models each of them.

* **Features**: `ADJUSTABLE_DPI` (100-25600, step 50), `REPORT_RATE`
  (125/250/500/1000 Hz), `ONBOARD_PROFILES` (5 slots, format 3),
  `MOUSE_BUTTON_SPY` (11 buttons), `UNIFIED_BATTERY`, `HIRES_WHEEL`. **No
  `REPROG_CONTROLS_V4`**, so buttons can't be diverted the usual way, and
  **no lighting feature** (`0x8070`/`0x8071`/`0x1300`): the DPI LEDs can't
  be recoloured. Hidden engineering features are left alone.
* **Onboard mode refuses report-rate changes** (`INVALID_ARGUMENT`) but
  accepts DPI. Host mode accepts both.
* **In host mode every button is reported twice**: as a normal HID button
  (bit n-1 = button n) and as a `MOUSE_BUTTON_SPY` notification carrying
  the full button mask. The spy's remap table (fn3/fn4, one entry per
  button, RAM only) decides the first: an entry of 0 silences the button
  for the desktop while the spy still reports it. That is how a button
  becomes a shortcut without the desktop also seeing a click.
* **Button numbers**: 1 left, 2 right, 3 wheel, 4 G4 (back), 5 G6 (sniper),
  6 G5 (forward), 7/8 wheel tilt left/right, 9 G9, 10 G8, 11 G7. The
  ratchet switch behind the wheel is mechanical and reports nothing.
* **Host mode doesn't survive a power cycle.** The mouse comes back in
  onboard mode on the same hidraw node (no detach) and sends a
  `WIRELESS_DEVICE_STATUS` notification; the driver takes over again. As a
  fallback it checks the mode when the mouse becomes active after a quiet
  spell.
* **The mouse sleeps after 60 s idle** and answers nothing until moved. A
  missing reply means asleep; the next report from the mouse wakes the
  driver, which sets everything up again. Changes made meanwhile are
  saved and applied then.
* **Leaving host mode keeps the host's last DPI**; re-selecting the onboard
  DPI level (`ONBOARD_PROFILES` fn 0x0C) restores the profile's value.
* **Through the receiver**, `hid-logitech-dj` rewrites the device index,
  and the kernel's own HID++ driver talks on the same node with software
  ID 1. Replies are matched on feature index, function and software ID
  (this driver uses 0x0B); notifications have software ID 0.
* **On its cable** the mouse is `046d:c098`, bound to `hid-generic`
  (no kernel battery), with HID++ on USB interface 2 and the same
  features and unit ID as through the receiver. Plugging the cable in drops
  the receiver link: the kernel's receiver battery goes offline, and the
  receiver answers requests for the mouse with a HID++ 1.0 `UNKNOWN_DEVICE`
  error, which the driver treats like no reply. Unplugging it, the mouse
  is back on the receiver (in onboard mode) within a second.

## Device access

`udev/70-borochid-logitech-hidpp.rules` grants the session user the mouse's
own HID node, matched on the paired device's HID ID
(`KERNELS=="0003:046D:409F.*"`), and on the cable only its HID++
interface (`c098`, interface 2). Never the receiver: its node carries every
paired device's traffic, keyboards included. Never vendor-wide.

Shortcuts need `/dev/uinput` for the service. The platform's `input` extra
(python-evdev) creates the virtual device; access to `/dev/uinput` is not
granted by this package.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ../borochid/packages/common -e ../borochid/packages/service -e '.[test]'
.venv/bin/pytest
```

What a mouse supports, read-only (after installing the udev rule):

```sh
python -m borochid_logitech_hidpp.probe
```

## Adding a model

1. Run the probe. The driver needs `ADJUSTABLE_DPI`, `REPORT_RATE`,
   `ONBOARD_PROFILES` and `MOUSE_BUTTON_SPY`.
2. Find the button numbers (press each one in host mode and read the spy
   mask), then write the device package's `hidpp` section.
3. Add the model's HID ID to the udev rule and release a new version.
