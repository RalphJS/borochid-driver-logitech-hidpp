import pytest
from conftest import make_driver, settle

from borochid.service.drivers import DriverError
from borochid.service.profiles import Profile

DEFAULT = Profile("default", "Default")

SUPER_V = {"keys": ["KEY_LEFTMETA", "KEY_V"]}


async def online(settings=None, wired=False):
    driver, mouse, events, store = make_driver(settings, wired)
    await driver.start()
    await settle(driver)
    assert driver.state["link"] == "online"
    return driver, mouse, events, store


def test_takes_over_with_the_default_profile(run):
    driver, mouse, _, _ = run(online())
    s = driver.state
    assert mouse.mode == 2 and mouse.spy and s["status"] == "Default"
    assert s["stages"] == [800, 1600, 3200] and s["stage"] == 2 and mouse.dpi == 1600
    assert mouse.rate_ms == 1
    # Clicks stay in the mouse (back/forward as buttons 4/5); the rest is host-handled.
    #                  L  R  M  G4 G6 G5 tL tR G9 G8 G7
    assert mouse.table == [1, 2, 3, 4, 0, 5, 0, 0, 0, 0, 0]
    assert s["bind.g6"] == "dpi_shift" and s["bind.g9"] == "disabled"


def test_key_chord_is_held_as_long_as_the_button(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": SUPER_V})
        mouse.press(4)
        await settle(driver, 1)
        mouse.press(4, down=False)
        await settle(driver, 1)
        return driver, mouse

    driver, mouse = run(main())
    assert driver.host.input.is_open
    assert driver.host.input.events == [("down", "g4", SUPER_V), ("up", "g4")]
    assert mouse.table[3] == 0 and mouse.clicks == [0, 0]  # the desktop never sees G4 itself


def test_tilt_scrolls_sideways(run):
    async def main():
        driver, mouse, _, _ = await online()
        mouse.press(7)
        await settle(driver, 1)
        return driver

    assert run(main()).host.input.events == [("down", "tilt_left", {"hwheel": -1})]


def test_dpi_buttons_step_through_the_stages(run):
    async def main():
        driver, mouse, _, _ = await online()
        seen = []
        for number in (10, 10, 11, 11, 11):  # G8 up twice, G7 down three times
            mouse.press(number)
            mouse.press(number, down=False)
            await settle(driver, 1)
            seen.append(mouse.dpi)
        return seen

    assert run(main()) == [3200, 3200, 1600, 800, 800]


def test_dpi_shift_holds_the_shift_dpi(run):
    async def main():
        driver, mouse, _, _ = await online()
        mouse.press(5)
        await settle(driver, 1)
        during = mouse.dpi
        mouse.press(5, down=False)
        await settle(driver, 1)
        return during, mouse.dpi

    assert run(main()) == (400, 1600)


def test_mouse_button_remaps_stay_inside_the_mouse(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": {"button": 2}})
        mouse.press(4)
        await settle(driver, 1)
        return driver, mouse

    driver, mouse = run(main())
    assert mouse.table[3] == 2 and mouse.clicks[-1] == 0b10  # a right click
    assert driver.host.input.events == []


def test_reset_binding_returns_to_the_package_default(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": "disabled"})
        await driver.invoke("reset_binding", {"button": "g4"})
        return driver, mouse

    driver, mouse = run(main())
    assert driver.state["bind.g4"] == {"button": 4} and mouse.table[3] == 4


def test_stages_can_be_added_and_removed(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("add_stage", {"value": 6400})
        await driver.invoke("add_stage", {"value": 12800})
        with pytest.raises(DriverError):
            await driver.invoke("add_stage", {"value": 100})  # six is too many
        await driver.invoke("select_stage", {"stage": 5})
        await driver.invoke("remove_stage", {"stage": 1})
        await driver.invoke("set_default_stage", {"stage": 3})
        return driver, mouse

    driver, mouse = run(main())
    s = driver.state
    assert s["stages"] == [1600, 3200, 6400, 12800] and s["default_stage"] == 3
    assert s["stage"] == 4 and mouse.dpi == 12800  # still on the stage that was selected


def test_the_last_stage_stays(run):
    async def main():
        driver, _, _, _ = await online()
        for _ in range(2):
            await driver.invoke("remove_stage", {"stage": 1})
        with pytest.raises(DriverError) as e:
            await driver.invoke("remove_stage", {"stage": 1})
        return str(e.value)

    assert run(main()) == "a profile needs at least one DPI stage"

def test_dpi_is_rounded_to_the_sensor_step_and_checked(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_stage", {"stage": 2, "value": 2437})
        with pytest.raises(DriverError):
            await driver.invoke("set_stage", {"stage": 2, "value": 99999})
        return driver, mouse

    driver, mouse = run(main())
    assert driver.state["stages"][1] == 2450 and mouse.dpi == 2450


def test_power_cycle_takes_over_again(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": {"button": 2}})
        mouse.power_cycle()
        assert mouse.mode == 1
        await settle(driver)
        return mouse

    mouse = run(main())
    assert mouse.mode == 2 and mouse.spy and mouse.table[3] == 2 and mouse.dpi == 1600


def test_the_receiver_connection_waits_while_the_mouse_is_on_its_cable(run):
    async def main():
        driver, mouse, _, _ = make_driver()
        mouse.on_cable = True
        await driver.start()
        await settle(driver)
        waiting = driver.state["link"]
        mouse.on_cable = False
        mouse.power_cycle()  # unplugged: back on the receiver, which announces it
        await settle(driver)
        return waiting, driver.state["link"], mouse

    waiting, link, mouse = run(main())
    assert waiting == "asleep" and link == "online" and mouse.mode == 2


def test_revert_without_notification_is_caught_when_the_mouse_is_used_again(run):
    async def main():
        driver, mouse, _, _ = await online()
        mouse.mode, mouse.spy = 1, False  # reverted silently
        await settle(driver, 2)  # longer than wake_check_s
        mouse.move()
        await settle(driver)
        return mouse

    assert run(main()).mode == 2


def test_asleep_mouse_is_set_up_when_it_wakes(run):
    async def main():
        driver, mouse, _, _ = make_driver()
        mouse.asleep = True
        await driver.start()
        await settle(driver, 10)  # past the reply timeout
        asleep = driver.state["link"]
        mouse.move()
        await settle(driver)
        return asleep, driver, mouse

    asleep, driver, mouse = run(main())
    assert asleep == "asleep" and driver.state["status"] == "Default"
    assert driver.state["link"] == "online" and mouse.mode == 2


def test_changes_while_asleep_apply_on_wake(run):
    async def main():
        driver, mouse, _, store = await online()
        mouse.asleep = True
        await driver.invoke("set_binding", {"button": "g4", "binding": "disabled"})
        await settle(driver, 10)
        state = driver.state["link"]
        mouse.move()
        await settle(driver)
        return state, mouse, store

    state, mouse, store = run(main())
    assert state == "asleep"
    assert mouse.table[3] == 0 and store.load()["profiles"]["default"]["bindings"]["g4"] == "disabled"


def test_stop_hands_the_mouse_back_to_its_onboard_profile(run):
    async def main():
        driver, mouse, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": SUPER_V})
        mouse.press(4)  # held while the service stops
        await settle(driver, 1)
        await driver.stop()
        return driver, mouse

    driver, mouse = run(main())
    assert mouse.mode == 1 and not mouse.spy and mouse.table == list(range(1, 12))
    assert mouse.dpi == 3200  # the onboard level again, not the last host DPI
    assert driver.host.input.events[-1] == ("up", "g4") and not driver.host.input.is_open


def test_bad_requests_are_refused(run):
    async def main():
        driver, _, _, _ = await online()
        errors = []
        for action, params in (
            ("set_binding", {"button": "left", "binding": "disabled"}),  # never remappable
            ("set_binding", {"button": "g4", "binding": {"keys": ["KEY_POWER"]}}),
            ("set_binding", {"button": "g4", "binding": {"text": "hi"}}),
            ("set_report_rate", {"value": 333}),
            ("nope", {}),
        ):
            with pytest.raises(DriverError) as e:
                await driver.invoke(action, params)
            errors.append(str(e.value))
        return errors

    errors = run(main())
    assert "can't be remapped" in errors[0] and "KEY_POWER" in errors[1]


def test_bad_stored_settings_fall_back_to_defaults(run):
    bad = {"profiles": {"default": {"stages": [800, "x"], "report_rate": 7, "bindings": {"g4": {"keys": ["KEY_POWER"]}, "zz": 1}}}}
    driver, _, _, _ = run(online(bad))
    s = driver.state
    assert s["stages"] == [800, 1600, 3200] and s["report_rate"] == 1000
    assert s["bind.g4"] == {"button": 4}


def test_kernel_traffic_and_movement_are_not_mistaken_for_replies():
    driver, _, _, _ = make_driver()
    # The kernel driver's own HID++ replies use software ID 1.
    driver.on_data(bytes([0x11, 0x01, 0x06, 0x11, 0x40]).ljust(20, b"\0"))
    driver.on_data(bytes([0x02, 0x01, 0x00, 0x05, 0x00]))
    assert driver.state["link"] == "connecting"


def test_switching_profiles_switches_the_mouse(run):
    async def main():
        driver, mouse, _, store = await online()
        games = Profile("p1", "Games", copy_of="default")
        await driver.use_profile(games, {"default", "p1"})  # starts as a copy of Default
        await driver.invoke("set_stage", {"stage": 2, "value": 400})
        await driver.invoke("set_report_rate", {"value": 500})
        in_games = (mouse.dpi, mouse.rate_ms, driver.state["status"])
        await driver.use_profile(DEFAULT, {"default", "p1"})
        return driver, mouse, store, in_games

    driver, mouse, store, in_games = run(main())
    assert in_games == (400, 2, "Games")
    assert (mouse.dpi, mouse.rate_ms, driver.state["status"]) == (1600, 1, "Default")
    saved = store.load()["profiles"]
    assert saved["p1"]["stages"] == [800, 400, 3200] and saved["default"]["stages"] == [800, 1600, 3200]


def test_new_profile_starts_from_defaults_or_its_source(run):
    async def main():
        driver, _, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g4", "binding": SUPER_V})
        await driver.use_profile(Profile("copy", "Copy", copy_of="default"), {"default", "copy"})
        copied = driver.state["bind.g4"]
        await driver.use_profile(Profile("new", "New"), {"default", "copy", "new"})
        return copied, driver.state["bind.g4"]

    assert run(main()) == (SUPER_V, {"button": 4})


def test_deleted_profiles_are_dropped_and_settings_survive_a_restart(run):
    async def main():
        driver, _, _, store = await online()
        await driver.use_profile(Profile("work", "Work"), {"default", "work"})
        await driver.invoke("set_binding", {"button": "g5", "binding": SUPER_V})
        await driver.use_profile(Profile("work", "Work"), {"work"})  # "default" was deleted
        await driver.stop()
        again, mouse, _, _ = make_driver(store.load())
        await again.use_profile(Profile("work", "Work"), {"work"})
        await again.start()
        await settle(again)
        return store, again, mouse

    store, again, mouse = run(main())
    assert set(store.load()["profiles"]) == {"work"}
    assert again.state["bind.g5"] == SUPER_V and mouse.table[5] == 0 and again.state["status"] == "Work"


def test_the_mouse_identifies_itself_by_unit_id(run):
    driver, *_ = run(online())
    assert driver.device_id == "01AB0945"


def test_battery_is_left_to_the_kernel_behind_the_receiver(run):
    async def main():
        driver, mouse, _, _ = await online()
        mouse.battery_event(60, 0)
        await settle(driver, 1)
        return driver, mouse

    driver, mouse = run(main())
    assert not mouse.called(0x1004, 1) and "battery" not in driver.state


def test_battery_is_read_on_the_cable(run):
    async def main():
        driver, mouse, _, _ = await online(wired=True)
        first = (driver.state["battery"], driver.state["charging"])
        mouse.battery_event(62, 3)  # full
        await settle(driver, 1)
        full = (driver.state["battery"], driver.state["charging"])
        mouse.battery_event(62, 0)
        await settle(driver, 1)
        return first, full, (driver.state["battery"], driver.state["charging"])

    assert run(main()) == ((61, True), (62, True), (62, False))
