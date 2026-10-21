"""Unit tests for integrations (PRD 17.8): no network, httpx.MockTransport only."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx
import jwt
import pytest

from phonepilot.errors import PhonePilotError
from phonepilot.integrations import kis, orders, upbit, youtube
from phonepilot.integrations.orders import LimitsLedger, OrderDraft, OrderGate, OrderLimits
from phonepilot.security.secrets import SecretVault

UPBIT_AK = "AK-canary-7f3e9a1b2c4d"
UPBIT_SK = "SK-CANARY-0123456789abcdef0123456789abcdef"
KIS_KEY = "KISKEY-canary-55aa55aa55aa"
KIS_SECRET = "KISSECRET-canary-99bb99bb99bb99bb"
YT_KEY = "AIzaYT-canary-1234567890"
ALL_CANARIES = (UPBIT_AK, UPBIT_SK, KIS_KEY, KIS_SECRET, YT_KEY)
FIXED_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes


def make_vault(upbit_hosts=("api.upbit.com",), yt_hosts=("www.googleapis.com",)) -> SecretVault:
    kis_hosts = ["openapivts.koreainvestment.com", "openapi.koreainvestment.com"]
    return SecretVault.from_mapping(
        {
            "UPBIT_MAIN_ACCESS_KEY": UPBIT_AK,
            "UPBIT_MAIN_SECRET_KEY": UPBIT_SK,
            "KIS_MAIN_APP_KEY": KIS_KEY,
            "KIS_MAIN_APP_SECRET": KIS_SECRET,
            "KIS_MAIN_ACCOUNT_NO": "12345678-01",
            "YOUTUBE_API_KEY": YT_KEY,
        },
        {
            "UPBIT_MAIN_SECRET_KEY": {"kind": "api_key", "allowed_hosts": list(upbit_hosts)},
            "KIS_MAIN_APP_KEY": {"kind": "api_key", "allowed_hosts": kis_hosts},
            "KIS_MAIN_APP_SECRET": {"kind": "api_key", "allowed_hosts": kis_hosts},
            "YOUTUBE_API_KEY": {"kind": "api_key", "allowed_hosts": list(yt_hosts)},
        },
    )


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def utc_now() -> datetime:
    return datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)  # 12:00 KST


def make_gate(**limit_overrides) -> OrderGate:
    limits = OrderLimits(allowed_symbols=["KRW-BTC", "005930"], **limit_overrides)
    return OrderGate(limits, LimitsLedger(now=utc_now), clock=Clock())


def upbit_draft(**kw) -> OrderDraft:
    base = dict(
        exchange="upbit",
        env="real",
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        price=Decimal("100000000"),
        quantity=Decimal("0.0005"),  # 50,000 KRW
    )
    base.update(kw)
    return OrderDraft(**base)


MARKET = Decimal("100000000")


def approved(gate: OrderGate, draft: OrderDraft, market: Decimal = MARKET) -> str:
    assert gate.check(draft, market).ok
    return gate.issue_token(draft)


class Recorder:
    """MockTransport handler that records requests and returns canned responses."""

    def __init__(self, responder) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def json_ok(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


# --------------------------------------------------------------------------- Upbit signing


def test_build_jwt_is_deterministic_and_hashes_query():
    params = {"market": "KRW-BTC", "side": "bid", "volume": "0.01", "price": "100", "ord_type": "limit"}
    a = upbit.build_jwt("access", FIXED_KEY, params, "nonce-fixed")
    b = upbit.build_jwt("access", FIXED_KEY, params, "nonce-fixed")
    assert a == b
    payload = jwt.decode(a, FIXED_KEY, algorithms=["HS256"])
    assert payload == {
        "access_key": "access",
        "nonce": "nonce-fixed",
        "query_hash": hashlib.sha512(urlencode(params, doseq=True).encode()).hexdigest(),
        "query_hash_alg": "SHA512",
    }
    assert jwt.get_unverified_header(a)["alg"] == "HS256"


def test_build_jwt_without_params_has_no_query_hash():
    payload = jwt.decode(upbit.build_jwt("access", FIXED_KEY, None, "n"), FIXED_KEY, algorithms=["HS256"])
    assert payload == {"access_key": "access", "nonce": "n"}


def test_build_jwt_doseq_lists():
    params = {"states": ["wait", "watch"]}
    payload = jwt.decode(upbit.build_jwt("a", FIXED_KEY, params, "n"), FIXED_KEY, algorithms=["HS256"])
    expected = hashlib.sha512(b"states=wait&states=watch").hexdigest()
    assert payload["query_hash"] == expected


@pytest.mark.parametrize("hosts", [(), ("evil.example.com",)])
def test_sign_refused_when_host_not_allowed(hosts):
    vault = make_vault(upbit_hosts=hosts)
    with pytest.raises(PhonePilotError) as ei:
        upbit.sign(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), {"a": "1"})
    assert ei.value.code == "SECRET_SCOPE_DENIED"


async def test_upbit_auth_header_matches_sent_query():
    vault = make_vault()
    rec = Recorder(json_ok([]))
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), make_gate(), rec.client())
    await client.open_orders("KRW-BTC")
    req = rec.requests[0]
    assert req.url.path == "/v1/orders/open"
    token = req.headers["Authorization"].removeprefix("Bearer ")
    payload = jwt.decode(token, UPBIT_SK.encode(), algorithms=["HS256"])
    assert payload["access_key"] == UPBIT_AK
    assert payload["query_hash"] == hashlib.sha512(req.url.query).hexdigest()


async def test_upbit_quote_is_public():
    vault = make_vault()
    rec = Recorder(json_ok([{"market": "KRW-BTC", "trade_price": 1}]))
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), make_gate(), rec.client())
    out = await client.quote(["KRW-BTC", "KRW-ETH"])
    assert out[0]["market"] == "KRW-BTC"
    req = rec.requests[0]
    assert "Authorization" not in req.headers
    assert req.url.params["markets"] == "KRW-BTC,KRW-ETH"


# --------------------------------------------------------------------------- approval tokens


@pytest.mark.parametrize("token", ["", None, "made-up-token"])
async def test_submit_without_valid_token_raises_before_network(token):
    vault = make_vault()
    rec = Recorder(json_ok({"uuid": "x"}, 201))
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), make_gate(), rec.client())
    with pytest.raises(PhonePilotError) as ei:
        await client.submit(upbit_draft(), token)
    assert ei.value.code == "APPROVAL_DENIED"
    assert rec.requests == []


async def test_kis_submit_without_token_raises_before_network():
    vault = make_vault()
    rec = Recorder(json_ok({}))
    c = kis.KisClient(vault.get("KIS_MAIN_APP_KEY"), vault.get("KIS_MAIN_APP_SECRET"), vault.get("KIS_MAIN_ACCOUNT_NO"), make_gate(), client=rec.client())
    d = OrderDraft("kis", "mock", "005930", "buy", "limit", price=Decimal(70000), quantity=Decimal(1))
    with pytest.raises(PhonePilotError) as ei:
        await c.submit(d, "")
    assert ei.value.code == "APPROVAL_DENIED"
    assert rec.requests == []


def test_token_bound_to_draft_hash():
    gate = make_gate()
    draft = upbit_draft()
    token = approved(gate, draft)
    changed = dataclasses.replace(draft, quantity=Decimal("0.0006"))
    assert changed.client_order_id == draft.client_order_id
    assert changed.draft_hash != draft.draft_hash
    with pytest.raises(PhonePilotError) as ei:
        gate.consume_token(changed, token)
    assert ei.value.code == "APPROVAL_DENIED"
    # a misuse attempt burns the token
    with pytest.raises(PhonePilotError):
        gate.consume_token(draft, token)


def test_draft_hash_ignores_created_at_but_not_economics():
    d = upbit_draft()
    assert dataclasses.replace(d, created_at=datetime(2000, 1, 1, tzinfo=timezone.utc)).draft_hash == d.draft_hash
    assert dataclasses.replace(d, price=Decimal("100000000.00")).draft_hash == d.draft_hash  # canonical decimals
    for change in ({"side": "sell"}, {"price": Decimal("99000000")}, {"env": "mock"}, {"symbol": "KRW-ETH"}):
        assert dataclasses.replace(d, **change).draft_hash != d.draft_hash


def test_token_single_use():
    gate = make_gate()
    draft = upbit_draft()
    token = approved(gate, draft)
    gate.consume_token(draft, token)
    with pytest.raises(PhonePilotError) as ei:
        gate.consume_token(draft, token)
    assert ei.value.code == "APPROVAL_DENIED"


def test_token_expiry(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(orders.time, "monotonic", lambda: now[0])
    gate = OrderGate(OrderLimits(allowed_symbols=["KRW-BTC"]), LimitsLedger(now=utc_now))
    draft = upbit_draft()
    token = approved(gate, draft)
    now[0] += 59.0
    token2 = approved(gate, draft)
    gate.consume_token(draft, token2)  # within 60s: fine
    now[0] += 2.0  # first token is now 61s old
    with pytest.raises(PhonePilotError) as ei:
        gate.consume_token(draft, token)
    assert ei.value.code == "APPROVAL_DENIED"
    assert "expired" in ei.value.message


def test_issue_token_requires_passing_check():
    gate = make_gate()
    with pytest.raises(PhonePilotError) as ei:
        gate.issue_token(upbit_draft())  # never checked
    assert ei.value.code == "POLICY_BLOCKED"
    blocked = upbit_draft(symbol="KRW-DOGE")
    assert not gate.check(blocked, MARKET).ok
    with pytest.raises(PhonePilotError) as ei:
        gate.issue_token(blocked)
    assert ei.value.code == "LIMIT_EXCEEDED"


def test_cancel_token_bound_to_order_id():
    gate = make_gate()
    tok = gate.issue_cancel_token("upbit", "uuid-1")
    with pytest.raises(PhonePilotError):
        gate.consume_cancel_token("upbit", "uuid-2", tok)
    tok = gate.issue_cancel_token("upbit", "uuid-1")
    gate.consume_cancel_token("upbit", "uuid-1", tok)


def test_withdrawals_are_not_representable():
    for bad in ({"side": "withdraw"}, {"side": "transfer"}, {"order_type": "withdraw"}):
        with pytest.raises(PhonePilotError):
            upbit_draft(**bad)
    assert not any(path.startswith(("/withdraw", "/deposit")) for _, path in upbit._ALLOWED)
    assert not hasattr(upbit.UpbitClient, "withdraw")


# --------------------------------------------------------------------------- gate rules


def _reason_codes(result) -> set[str]:
    return {r.split(":")[0] for r in result.reasons}


def test_gate_ok():
    r = make_gate().check(upbit_draft(), MARKET)
    assert (r.ok, r.level, r.reasons, r.notional_krw) == (True, "ok", [], Decimal("50000"))


@pytest.mark.parametrize(
    "draft_kw, market, limits_kw, code",
    [
        ({"symbol": "KRW-XRP"}, MARKET, {}, "symbol_not_allowed"),
        ({"quantity": Decimal("0.0011")}, MARKET, {}, "order_max_exceeded"),  # 110,000 > 100,000
        ({"quantity": Decimal("0.00004")}, MARKET, {}, "below_min_order"),  # 4,000 < 5,000
        ({"price": Decimal("120000000")}, MARKET, {}, "price_deviation_blocked"),  # +20%
        ({"price": Decimal("84000000"), "quantity": Decimal("0.0005")}, MARKET, {}, "price_deviation_blocked"),  # -16%
        ({}, MARKET, {"order_daily_krw": 40000}, "daily_amount_exceeded"),
        ({}, Decimal(0), {}, "market_price_unavailable"),
    ],
)
def test_gate_block_rules(draft_kw, market, limits_kw, code):
    r = make_gate(**limits_kw).check(upbit_draft(**draft_kw), market)
    assert not r.ok and r.level == "block"
    assert code in _reason_codes(r)


def test_gate_empty_allowlist_blocks_everything():
    gate = OrderGate(OrderLimits(), LimitsLedger(now=utc_now), clock=Clock())
    assert "symbol_not_allowed" in _reason_codes(gate.check(upbit_draft(), MARKET))


def test_gate_daily_amount_and_count_with_ledger():
    gate = make_gate(order_daily_count=2)
    for _ in range(2):
        d = upbit_draft()
        gate.consume_token(d, approved(gate, d))
        gate.record_fill(d)
    assert gate.ledger.snapshot() == (Decimal("100000"), 2)
    r = gate.check(upbit_draft(), MARKET)
    assert "daily_count_exceeded" in _reason_codes(r)

    gate = make_gate()
    for _ in range(6):  # 6 x 50,000 = 300,000 (exactly the daily limit)
        d = upbit_draft()
        gate.record_fill(d, Decimal("50000"))
    r = gate.check(upbit_draft(), MARKET)
    assert "daily_amount_exceeded" in _reason_codes(r)
    assert r.daily_remaining_krw == 0


def test_consume_rechecks_daily_limit():
    gate = make_gate(order_daily_count=1)
    d = upbit_draft()
    token = approved(gate, d)
    gate.record_fill(upbit_draft(), Decimal("5000"))  # another order landed in between
    with pytest.raises(PhonePilotError) as ei:
        gate.consume_token(d, token)
    assert ei.value.code == "LIMIT_EXCEEDED"


def test_gate_warn_on_moderate_deviation():
    r = make_gate().check(upbit_draft(price=Decimal("93000000"), quantity=Decimal("0.0005")), MARKET)  # -7%
    assert r.ok and r.level == "warn"
    assert _reason_codes(r) == {"price_deviation_warning"}
    assert r.deviation_pct == Decimal(7)


def test_gate_market_buy_by_amount_and_spread_warning():
    d = upbit_draft(order_type="market", price=None, quantity=None, notional_krw=Decimal(20000))
    r = make_gate().check(d, MARKET, spread_pct=Decimal("1.5"))
    assert r.ok and r.level == "warn" and r.notional_krw == Decimal(20000)
    assert _reason_codes(r) == {"wide_spread_warning"}


def test_gate_market_sell_uses_market_price():
    d = upbit_draft(side="sell", order_type="market", price=None, quantity=Decimal("0.0002"))
    assert make_gate().check(d, MARKET).notional_krw == Decimal(20000)


def test_draft_structural_validation():
    with pytest.raises(PhonePilotError):
        upbit_draft(price=None)  # limit without price
    with pytest.raises(PhonePilotError):
        upbit_draft(quantity=Decimal(-1))
    with pytest.raises(PhonePilotError):
        upbit_draft(order_type="market", price=None, notional_krw=Decimal(10000))  # both qty and notional


# --------------------------------------------------------------------------- ledger


def test_ledger_resets_on_kst_date_change_and_persists_aggregates_only(tmp_path):
    path = tmp_path / "limits_state.json"
    now = [datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc)]  # 23:59 KST on 10-11
    ledger = LimitsLedger(path, now=lambda: now[0])
    ledger.add(Decimal(70000))
    ledger.add(Decimal(30000))
    assert ledger.snapshot() == (Decimal(100000), 2)
    assert json.loads(path.read_text(encoding="utf-8")) == {"date": "2026-10-11", "total_krw": "100000", "count": 2}

    # restart on the same KST day: usage is restored, not reset
    assert LimitsLedger(path, now=lambda: now[0]).snapshot() == (Decimal(100000), 2)

    now[0] = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)  # 00:00 KST on 10-12 (still 10-11 in UTC)
    assert ledger.snapshot() == (Decimal(0), 0)
    assert json.loads(path.read_text(encoding="utf-8"))["date"] == "2026-10-12"

    # a stale file from a previous day is ignored on load
    path.write_text(json.dumps({"date": "2026-10-01", "total_krw": "999999", "count": 9}), encoding="utf-8")
    assert LimitsLedger(path, now=lambda: now[0]).snapshot() == (Decimal(0), 0)


def test_ledger_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "limits_state.json"
    path.write_text("{not json", encoding="utf-8")
    assert LimitsLedger(path, now=utc_now).snapshot() == (Decimal(0), 0)


# --------------------------------------------------------------------------- Upbit submit


async def test_upbit_submit_success_signs_body_and_records_usage():
    vault = make_vault()
    rec = Recorder(json_ok({"uuid": "u-1", "state": "wait"}, 201))
    gate = make_gate()
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, rec.client())
    d = upbit_draft()
    out = await client.submit(d, approved(gate, d))
    assert out["uuid"] == "u-1"
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("POST", "/v1/orders")
    body = json.loads(req.content)
    assert body == {
        "market": "KRW-BTC",
        "side": "bid",
        "ord_type": "limit",
        "volume": "0.0005",
        "price": "100000000",
        "identifier": d.client_order_id,
    }
    payload = jwt.decode(req.headers["Authorization"][7:], UPBIT_SK.encode(), algorithms=["HS256"])
    assert payload["query_hash"] == hashlib.sha512(urlencode(body).encode()).hexdigest()
    assert gate.ledger.snapshot() == (Decimal(50000), 1)


def test_upbit_order_params_market_orders():
    buy = upbit_draft(order_type="market", price=None, quantity=None, notional_krw=Decimal(10000))
    assert upbit.order_params(buy)["ord_type"] == "price" and upbit.order_params(buy)["price"] == "10000"
    sell = upbit_draft(side="sell", order_type="market", price=None, quantity=Decimal("0.001"))
    p = upbit.order_params(sell)
    assert (p["side"], p["ord_type"], p["volume"]) == ("ask", "market", "0.001") and "price" not in p


@pytest.mark.parametrize(
    "responder",
    [
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("connection reset", request=req)),
        lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=req)),
        lambda req: httpx.Response(502, text="bad gateway"),
    ],
)
async def test_upbit_submit_state_unknown_without_retry(responder):
    vault = make_vault()
    rec = Recorder(responder)
    gate = make_gate()
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, rec.client())
    d = upbit_draft()
    token = approved(gate, d)
    with pytest.raises(PhonePilotError) as ei:
        await client.submit(d, token)
    assert ei.value.code == "ORDER_STATE_UNKNOWN"
    assert ei.value.retryable is False
    assert ei.value.extra["identifier"] == d.client_order_id
    assert len(rec.requests) == 1  # exactly one attempt
    assert gate.ledger.snapshot()[1] == 1  # counted conservatively
    with pytest.raises(PhonePilotError) as ei:  # the token cannot be replayed
        await client.submit(d, token)
    assert ei.value.code == "APPROVAL_DENIED"
    assert len(rec.requests) == 1


async def test_upbit_submit_rejected_maps_error():
    vault = make_vault()
    rec = Recorder(json_ok({"error": {"name": "insufficient_funds_bid", "message": "잔고 부족"}}, 400))
    gate = make_gate()
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, rec.client())
    d = upbit_draft()
    with pytest.raises(PhonePilotError) as ei:
        await client.submit(d, approved(gate, d))
    assert ei.value.code == "ORDER_REJECTED"
    assert "insufficient_funds_bid" in ei.value.message
    assert gate.ledger.snapshot() == (Decimal(0), 0)


async def test_upbit_refuses_mock_env_and_wrong_exchange():
    vault = make_vault()
    rec = Recorder(json_ok({}))
    gate = make_gate()
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, rec.client())
    for d in (upbit_draft(env="mock"), upbit_draft(exchange="kis")):
        with pytest.raises(PhonePilotError) as ei:
            await client.submit(d, "tok")
        assert ei.value.code == "POLICY_BLOCKED"
    assert rec.requests == []


async def test_upbit_cancel_requires_token():
    vault = make_vault()
    rec = Recorder(json_ok({"uuid": "u-1", "state": "cancel"}))
    gate = make_gate()
    client = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, rec.client())
    with pytest.raises(PhonePilotError):
        await client.cancel("u-1", "nope")
    assert rec.requests == []
    await client.cancel("u-1", gate.issue_cancel_token("upbit", "u-1"))
    assert rec.requests[0].method == "DELETE" and rec.requests[0].url.params["uuid"] == "u-1"


# --------------------------------------------------------------------------- KIS


def kis_responder(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/oauth2/tokenP":
        return httpx.Response(200, json={"access_token": "tok-abc", "token_type": "Bearer", "expires_in": 86400})
    if request.url.path.endswith("/order-cash"):
        return httpx.Response(200, json={"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "0000123", "KRX_FWDG_ORD_ORGNO": "91252"}})
    return httpx.Response(200, json={"rt_cd": "0", "output": {"stck_prpr": "70000"}})


def make_kis(rec: Recorder, gate: OrderGate | None = None, **kw) -> kis.KisClient:
    vault = make_vault()
    return kis.KisClient(
        vault.get("KIS_MAIN_APP_KEY"), vault.get("KIS_MAIN_APP_SECRET"), vault.get("KIS_MAIN_ACCOUNT_NO"),
        gate or make_gate(), client=rec.client(), **kw,
    )


async def test_kis_defaults_to_mock_and_caches_token():
    rec = Recorder(kis_responder)
    c = make_kis(rec)
    assert c.env == "mock"
    assert c.base_url == "https://openapivts.koreainvestment.com:29443"
    await c.quote(["005930"])
    await c.quote(["000660"])
    paths = [r.url.path for r in rec.requests]
    assert paths.count("/oauth2/tokenP") == 1
    assert paths.count(kis.ENDPOINTS["price"]["path"]) == 2
    token_req = rec.requests[0]
    assert str(token_req.url).startswith("https://openapivts.koreainvestment.com:29443/")
    assert json.loads(token_req.content) == {"grant_type": "client_credentials", "appkey": KIS_KEY, "appsecret": KIS_SECRET}
    price_req = rec.requests[1]
    assert price_req.headers["authorization"] == "Bearer tok-abc"
    assert price_req.headers["tr_id"] == "FHKST01010100"
    assert price_req.url.params["FID_INPUT_ISCD"] == "005930"


async def test_kis_real_env_base_url():
    rec = Recorder(kis_responder)
    c = make_kis(rec, env="real")
    assert c.base_url == "https://openapi.koreainvestment.com:9443"


async def test_kis_submit_mock_buy():
    rec = Recorder(kis_responder)
    gate = make_gate()
    c = make_kis(rec, gate)
    d = OrderDraft("kis", "mock", "005930", "buy", "limit", price=Decimal(70000), quantity=Decimal(1))
    out = await c.submit(d, approved(gate, d, Decimal(70000)))
    assert out["order_no"] == "0000123"
    req = rec.requests[-1]
    assert req.headers["tr_id"] == kis.ENDPOINTS["order_buy"]["tr_id"]["mock"]
    assert json.loads(req.content) == {
        "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_DVSN": "00", "ORD_QTY": "1", "ORD_UNPR": "70000",
    }
    assert gate.ledger.snapshot() == (Decimal(70000), 1)


async def test_kis_env_mismatch_blocked():
    rec = Recorder(kis_responder)
    c = make_kis(rec)  # mock client
    d = OrderDraft("kis", "real", "005930", "buy", "limit", price=Decimal(70000), quantity=Decimal(1))
    with pytest.raises(PhonePilotError) as ei:
        await c.submit(d, "tok")
    assert ei.value.code == "POLICY_BLOCKED"
    assert rec.requests == []


async def test_kis_submit_state_unknown_without_retry():
    def responder(req):
        if req.url.path == "/oauth2/tokenP":
            return kis_responder(req)
        raise httpx.ConnectError("reset", request=req)

    rec = Recorder(responder)
    gate = make_gate()
    c = make_kis(rec, gate)
    d = OrderDraft("kis", "mock", "005930", "sell", "limit", price=Decimal(70000), quantity=Decimal(1))
    with pytest.raises(PhonePilotError) as ei:
        await c.submit(d, approved(gate, d, Decimal(70000)))
    assert ei.value.code == "ORDER_STATE_UNKNOWN"
    assert sum(1 for r in rec.requests if r.url.path.endswith("/order-cash")) == 1


# --------------------------------------------------------------------------- YouTube


async def test_youtube_quota_and_key_scope():
    rec = Recorder(json_ok({"items": [{"id": "c1"}]}))
    yt = youtube.YouTubeClient(make_vault().get("YOUTUBE_API_KEY"), client=rec.client())
    await yt.search_channels("캠핑")
    await yt.channels(["c1", "c2"])
    await yt.videos([f"v{i}" for i in range(60)])  # 2 chunks
    assert yt.quota_used == 100 + 1 + 2
    first = rec.requests[0]
    assert first.url.host == "www.googleapis.com" and first.url.path == "/youtube/v3/search"
    assert first.url.params["type"] == "channel" and first.url.params["regionCode"] == "KR"
    assert first.url.params["key"] == YT_KEY

    denied = youtube.YouTubeClient(make_vault(yt_hosts=()).get("YOUTUBE_API_KEY"), client=rec.client())
    with pytest.raises(PhonePilotError) as ei:
        await denied.categories()
    assert ei.value.code == "SECRET_SCOPE_DENIED"


async def test_youtube_search_cap():
    rec = Recorder(json_ok({"items": []}))
    yt = youtube.YouTubeClient(make_vault().get("YOUTUBE_API_KEY"), client=rec.client(), max_search_calls=2)
    await yt.search_channels("a")
    await yt.search_channels("b")
    with pytest.raises(PhonePilotError) as ei:
        await yt.search_channels("c")
    assert ei.value.code == "LIMIT_EXCEEDED"
    assert len(rec.requests) == 2


@pytest.mark.parametrize(
    "value, seconds",
    [("PT1M5S", 65), ("PT59S", 59), ("PT3M", 180), ("PT1H2M3S", 3723), ("P1DT1S", 86401), ("P0D", 0), ("bogus", None), ("", None)],
)
def test_parse_iso8601_duration(value, seconds):
    assert youtube.parse_iso8601_duration(value) == seconds


def _video(vid, published, duration, views, title):
    return {
        "id": vid,
        "snippet": {"publishedAt": published, "title": title},
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": str(views)},
    }


def test_analyze_channel_metrics():
    videos = [
        # 2026-10-05 is a Monday. 11:00Z = 20:00 KST Monday.
        _video("a", "2026-10-05T11:00:00Z", "PT45S", 1000, "캠핑 장비 추천 5가지"),
        _video("b", "2026-10-07T11:00:00Z", "PT2M30S", 2000, "솔캠 가능할까?"),
        _video("c", "2026-10-09T11:00:00Z", "PT12M", 3000, "캠핑 요리 레시피"),
        _video("d", "2026-10-12T15:30:00Z", "PT10M5S", 20000, "겨울 캠핑 준비"),  # 00:30 KST Tuesday 10-13
    ]
    m = youtube.analyze_channel(videos, subscribers=10000)
    assert m["video_count"] == 4
    assert m["upload_interval_median_days"] == 2.0  # gaps: 3.1875, 2, 2
    assert m["weekday_hour_kst"][0][20] == 1  # Monday 20h
    assert m["weekday_hour_kst"][4][20] == 1  # Friday 20h
    assert m["weekday_hour_kst"][1][0] == 1  # Tuesday 00h (UTC date was Monday)
    assert sum(map(sum, m["weekday_hour_kst"])) == 4
    assert m["shorts_ratio"] == 0.5 and m["shorts60_ratio"] == 0.25 and m["longform_ratio"] == 0.5
    assert m["median_duration_s"] == (150 + 605) / 2
    assert m["median_views"] == 2500
    assert [o["id"] for o in m["outliers"]] == ["d"] and m["outliers"][0]["ratio"] == 8.0
    assert m["views_per_subscriber"] == 0.25
    assert m["title_avg_length"] == sum(len(v["snippet"]["title"]) for v in videos) / 4
    assert m["title_digit_ratio"] == 0.25
    assert m["title_question_ratio"] == 0.25
    assert m["top_words"][0] == ["캠핑", 3]
    assert len(m["top_words"]) <= 20


def test_analyze_channel_empty():
    m = youtube.analyze_channel([])
    assert m["video_count"] == 0 and m["median_views"] is None and m["outliers"] == []


# --------------------------------------------------------------------------- secret hygiene


def _assert_clean(text: str) -> None:
    for canary in ALL_CANARIES:
        assert canary not in text


async def test_secrets_never_in_repr_or_errors():
    vault = make_vault()
    gate = make_gate()
    echo = Recorder(
        lambda req: httpx.Response(401, json={"error": {"name": "invalid_access_key", "message": f"bad key {UPBIT_SK} {UPBIT_AK}"}})
    )
    up = upbit.UpbitClient(vault.get("UPBIT_MAIN_ACCESS_KEY"), vault.get("UPBIT_MAIN_SECRET_KEY"), gate, echo.client())
    kc = make_kis(Recorder(lambda req: httpx.Response(403, json={"error_code": "EGW", "error_description": f"bad {KIS_SECRET}"})))

    def leak_url(req):
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

    yt = youtube.YouTubeClient(vault.get("YOUTUBE_API_KEY"), client=Recorder(leak_url).client())
    yt_echo = youtube.YouTubeClient(
        vault.get("YOUTUBE_API_KEY"),
        client=Recorder(lambda req: httpx.Response(400, json={"error": {"message": f"key {YT_KEY} invalid", "errors": [{"reason": "keyInvalid"}]}})).client(),
    )

    for obj in (up, kc, yt, gate, vault.get("UPBIT_MAIN_SECRET_KEY")):
        _assert_clean(repr(obj))
        _assert_clean(str(obj))
        _assert_clean(f"{obj}")

    errors: list[PhonePilotError] = []
    for coro in (up.balances(), kc.quote(["005930"]), yt.categories(), yt_echo.categories()):
        with pytest.raises(PhonePilotError) as ei:
            await coro
        errors.append(ei.value)
    assert [e.code for e in errors] == ["INTEGRATION_AUTH_FAILED"] * 2 + ["INTEGRATION_ERROR", "INTEGRATION_AUTH_FAILED"]
    for e in errors:
        _assert_clean(str(e))
        _assert_clean(repr(e))
        _assert_clean(json.dumps(e.to_dict(), default=str))
        assert e.__suppress_context__ or e.__cause__ is None

    d = upbit_draft()
    with pytest.raises(PhonePilotError) as ei:
        await up.submit(d, approved(gate, d))
    _assert_clean(str(ei.value))
