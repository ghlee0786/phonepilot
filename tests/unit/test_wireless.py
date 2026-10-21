"""Wireless debugging pairing (PRD 8.2, FR-DEV-010..013) against a scripted adb."""

import asyncio

import pytest

from phonepilot.core.wireless import (
    CONNECT_SERVICE,
    PAIRING_SERVICE,
    WirelessADB,
    is_wireless_serial,
    parse_mdns_services,
    qr_payload,
    qr_png,
    validate_code,
    validate_host_port,
)
from phonepilot.errors import PhonePilotError

MDNS_EMPTY = "List of discovered mdns services\n"


def mdns(*rows: tuple[str, str, str]) -> str:
    return MDNS_EMPTY + "".join(f"{a}\t{b}\t{c}\n" for a, b, c in rows)


class FakeAdb:
    """Scripted `adb` CLI: mdns outputs are consumed one per call (last one repeats)."""

    def __init__(self, mdns_outputs, pair_out="Successfully paired to 192.168.0.12:37123 [guid=adb-R5C-xyz]", connect_out="connected to 192.168.0.12:41235"):
        self.mdns_outputs = list(mdns_outputs)
        self.pair_out = pair_out
        self.connect_out = connect_out
        self.calls: list[list[str]] = []

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[:2] == ["mdns", "services"]:
            out = self.mdns_outputs.pop(0) if len(self.mdns_outputs) > 1 else self.mdns_outputs[0]
            return 0, out
        if args[0] == "pair":
            return (0 if "Successfully" in self.pair_out else 1), self.pair_out
        if args[0] == "connect":
            return 0, self.connect_out
        return 1, "unknown"


def test_qr_payload_format_and_randomness():
    s1, p1, payload = qr_payload()
    s2, p2, _ = qr_payload()
    assert payload == f"WIFI:T:ADB;S:{s1};P:{p1};;"
    assert s1.startswith("phonepilot-") and len(p1) == 12 and (s1, p1) != (s2, p2)
    with pytest.raises(ValueError):
        qr_payload("bad;name", "x" * 10)
    png = qr_png(payload)
    assert png[:4] == b"\x89PNG"


def test_parse_mdns_services():
    out = mdns(("adb-R5CT123-AbCd", CONNECT_SERVICE, "192.168.0.12:41235"), ("phonepilot-abc", PAIRING_SERVICE + ".", "192.168.0.12:37123"))
    svcs = parse_mdns_services(out + "garbage line\n")
    assert [(s.instance, s.service, s.address) for s in svcs] == [
        ("adb-R5CT123-AbCd", CONNECT_SERVICE, "192.168.0.12:41235"),
        ("phonepilot-abc", PAIRING_SERVICE, "192.168.0.12:37123"),
    ]


@pytest.mark.parametrize("value", ["192.168.0.12:37123", "10.0.0.5:5555", "172.16.3.4:1", "[fe80::1]:40000"])
def test_host_port_private_ok(value):
    assert validate_host_port(value) == value


@pytest.mark.parametrize("value,code", [("8.8.8.8:5555", "POLICY_BLOCKED"), ("192.168.0.12", "DEVICE_NOT_FOUND"), ("evil.com:5555", "DEVICE_NOT_FOUND"), ("192.168.0.12:99999", "DEVICE_NOT_FOUND"), ("192.168.0.12:1; rm -rf", "DEVICE_NOT_FOUND")])
def test_host_port_rejected(value, code):
    with pytest.raises(PhonePilotError) as e:
        validate_host_port(value)
    assert e.value.code == code


def test_code_validation():
    assert validate_code(" 123456 ") == "123456"
    for bad in ("12345", "abcdef", "1234567"):
        with pytest.raises(PhonePilotError):
            validate_code(bad)


def test_is_wireless_serial():
    assert is_wireless_serial("192.168.0.12:41235")
    assert is_wireless_serial("adb-R5CT123-AbCd._adb-tls-connect._tcp")
    assert not is_wireless_serial("R5CT1234567")


async def test_qr_flow_pairs_then_connects():
    service, password, _ = qr_payload()
    adb = FakeAdb([
        MDNS_EMPTY,
        MDNS_EMPTY,
        mdns((service, PAIRING_SERVICE, "192.168.0.12:37123")),
        mdns(("adb-R5CT123-AbCd", CONNECT_SERVICE, "192.168.0.12:41235")),
    ])
    statuses = []
    res = await WirelessADB(adb, poll_interval=0.01).pair_qr(service, password, timeout=5, on_status=statuses.append)
    assert res.ok and res.address == "192.168.0.12:41235"
    assert ["pair", "192.168.0.12:37123", password] in adb.calls
    assert ["connect", "192.168.0.12:41235"] in adb.calls
    assert statuses[0].startswith("폰에서 QR") and "페어링 중…" in statuses and "연결 중…" in statuses
    assert all(password not in s for s in statuses)


async def test_qr_ignores_other_pairing_services():
    service, password, _ = qr_payload()
    adb = FakeAdb([mdns(("someone-else", PAIRING_SERVICE, "192.168.0.99:30000"))])
    res = await WirelessADB(adb, poll_interval=0.01).pair_qr(service, password, timeout=0.1)
    assert not res.ok and "시간 초과" in res.message
    assert not any(c[0] == "pair" for c in adb.calls)


async def test_qr_cancel():
    service, password, _ = qr_payload()
    w = WirelessADB(FakeAdb([MDNS_EMPTY]), poll_interval=0.05)
    task = asyncio.create_task(w.pair_qr(service, password, timeout=30))
    await asyncio.sleep(0.1)
    w.cancel()
    res = await asyncio.wait_for(task, 2)
    assert not res.ok and res.message == "취소됨"


async def test_pair_failure_reported():
    service, password, _ = qr_payload()
    adb = FakeAdb([mdns((service, PAIRING_SERVICE, "192.168.0.12:37123"))], pair_out="Failed: Wrong password or connection was dropped.")
    res = await WirelessADB(adb, poll_interval=0.01).pair_qr(service, password, timeout=2)
    assert not res.ok and "페어링 실패" in res.message
    assert not any(c[0] == "connect" for c in adb.calls)


async def test_code_flow_and_validation():
    adb = FakeAdb([mdns(("adb-R5CT123-AbCd", CONNECT_SERVICE, "192.168.0.12:41235"))])
    w = WirelessADB(adb, poll_interval=0.01)
    res = await w.pair_code("192.168.0.12:37123", "123456")
    assert res.ok and ["pair", "192.168.0.12:37123", "123456"] in adb.calls
    with pytest.raises(PhonePilotError):
        await w.pair_code("192.168.0.12:37123", "12ab56")
    with pytest.raises(PhonePilotError):
        await w.pair_code("1.1.1.1:5555", "123456")


async def test_reconnect_known_connects_advertised_phones():
    adb = FakeAdb([mdns(("adb-A", CONNECT_SERVICE, "192.168.0.12:41235"), ("x", PAIRING_SERVICE, "192.168.0.13:1"))])
    found = await WirelessADB(adb).reconnect_known()
    assert found == ["192.168.0.12:41235"]
    assert ["connect", "192.168.0.12:41235"] in adb.calls and not any(c[0] == "pair" for c in adb.calls)


async def test_connect_failure_message():
    adb = FakeAdb([MDNS_EMPTY], connect_out="failed to connect to 192.168.0.12:41235")
    res = await WirelessADB(adb).connect("192.168.0.12:41235")
    assert not res.ok and "연결 실패" in res.message
