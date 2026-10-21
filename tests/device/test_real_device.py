"""TC-DEV-01..03 on a real phone. Run with:  pytest -m device tests/device  (USB debugging authorized, screen unlocked)."""

import asyncio
import time

import pytest

pytestmark = pytest.mark.device


@pytest.fixture(scope="module")
def real():
    from phonepilot.core.adb import DeviceManager
    from phonepilot.core.real_device import RealDevice

    dm = DeviceManager()
    dm.start_server()
    ready = [d for d in dm.devices() if d.state == "device"]
    if not ready:
        pytest.skip("no authorized device connected")
    dev = RealDevice(dm.device(ready[0].serial))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(dev.start())
    yield dev, loop
    loop.run_until_complete(dev.close())
    loop.close()


def test_first_frame_tc_dev_01(real):
    dev, loop = real
    t0 = time.monotonic()
    while dev.framebus.latest() is None and time.monotonic() - t0 < 5:
        loop.run_until_complete(asyncio.sleep(0.05))
    frame = dev.framebus.latest()
    assert frame is not None, "no frame within 5 s (K7 target is 3 s once the server jar is on the device)"
    fw, fh = dev.frame_size
    dw, dh = dev.display_size
    assert abs(fw / fh - dw / dh) < 0.02


def test_input_and_ui_tree_tc_dev_02_03(real):
    from phonepilot.observe.observer import Observer
    from phonepilot.security.policy import PolicyEngine
    from phonepilot.security.redactor import Redactor
    from phonepilot.core.arbiter import InputArbiter
    from phonepilot.core.scrcpy import protocol as p

    dev, loop = real
    arb = InputArbiter(dev)
    obs = Observer(dev, Redactor(), PolicyEngine())

    async def flow():
        await arb.ai_key(p.Keycode.HOME)
        await asyncio.sleep(1.0)
        await dev.shell("am start -a android.settings.SETTINGS")
        await asyncio.sleep(1.5)
        o = await obs.observe()
        assert o.package == "com.android.settings", o.package
        assert len(o.elements) >= 5 and any(e.label for e in o.elements)
        before = o.screen_hash
        await arb.ai_gesture(__import__("phonepilot.core.gesture", fromlist=["x"]).precise_scroll(dev.display_size[0] // 2, int(dev.display_size[1] * 0.7), dev.display_size[0] // 2, int(dev.display_size[1] * 0.3)))
        await obs.wait_stable(3)
        o2 = await obs.observe()
        assert o2.screen_hash != before
        await arb.ai_key(p.Keycode.BACK)
        assert arb.open_pointers == {}

    loop.run_until_complete(flow())
