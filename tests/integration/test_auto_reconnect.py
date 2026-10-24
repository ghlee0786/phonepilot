"""FR-DEV-003: after the reconnect attempts give up, a phone that shows up again in adb is connected without a click."""

import time

from phonepilot.core.adb import DeviceEntry
from phonepilot.runtime import ConnState, CoreRuntime


def wait_state(rt: CoreRuntime, state: ConnState, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rt.state == state:
            return True
        time.sleep(0.05)
    return False


def test_phone_reappearing_reconnects_automatically():
    rt = CoreRuntime(demo=True)
    rt.start()
    try:
        rt.loop.call_soon_threadsafe(rt._set_state, ConnState.FAILED, "DEVICE_OFFLINE")
        assert wait_state(rt, ConnState.FAILED)

        # still waiting for "allow USB debugging": nothing to connect to yet
        rt.loop.call_soon_threadsafe(rt._on_devices_changed, [DeviceEntry("TESTSERIAL01", "unauthorized")])
        time.sleep(0.3)
        assert rt.state == ConnState.FAILED

        rt.loop.call_soon_threadsafe(rt._on_devices_changed, [DeviceEntry("TESTSERIAL01", "device")])
        assert wait_state(rt, ConnState.STREAMING)
        assert rt.device is not None

        # while streaming, further adb events do not start a second connect
        rt.loop.call_soon_threadsafe(rt._on_devices_changed, [DeviceEntry("TESTSERIAL01", "device")])
        time.sleep(0.3)
        assert rt.state == ConnState.STREAMING
    finally:
        rt.call_sync(rt.shutdown())
        rt.stop_loop()
        rt.thread.join(timeout=5)
