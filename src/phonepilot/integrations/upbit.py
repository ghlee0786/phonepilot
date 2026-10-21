"""Upbit Open API async client (PRD 17.1-17.4).

Auth: a fresh HS256 JWT per request. Payload = access_key + nonce, plus query_hash (SHA-512 of the
urlencoded query/body) and query_hash_alg when the request has parameters. The secret key is only
ever revealed inside `sign()` (SEC-004).

API details (paths, `ord_type` values, `identifier`, `/orders/open`) follow the public Upbit docs
(https://docs.upbit.com) and must be re-verified there before real-money use.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from phonepilot.errors import PhonePilotError
from phonepilot.integrations.orders import OrderDraft, OrderGate, redact, require_allowed_host
from phonepilot.security.secrets import Secret

BASE_URL = "https://api.upbit.com/v1"
HOST = "api.upbit.com"

# Everything this client may ever call. Withdraw/deposit endpoints are intentionally absent.
_ALLOWED = {
    ("GET", "/ticker"),
    ("GET", "/orderbook"),
    ("GET", "/accounts"),
    ("GET", "/orders/open"),
    ("GET", "/order"),
    ("POST", "/orders"),
    ("DELETE", "/order"),
}


def _qs(params: dict[str, Any]) -> str:
    return urlencode(params, doseq=True)


def build_jwt(access_key: str, secret_bytes: bytes, params: dict[str, Any] | None, nonce: str) -> str:
    """Pure, deterministic JWT builder (testable with a fixed key and nonce)."""
    payload: dict[str, str] = {"access_key": access_key, "nonce": nonce}
    if params:
        payload["query_hash"] = hashlib.sha512(_qs(params).encode("utf-8")).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    return jwt.encode(payload, secret_bytes, algorithm="HS256")


def sign(access_key: Secret, secret_key: Secret, params: dict[str, Any] | None = None) -> str:
    """The only place the Upbit secret key is revealed. Returns `Bearer <jwt>`."""
    require_allowed_host(secret_key, HOST)
    require_allowed_host(access_key, HOST, strict=False)
    with access_key.reveal() as ak, secret_key.reveal() as sk:
        token = build_jwt(ak.decode("utf-8"), bytes(sk), params, str(uuid.uuid4()))
    return f"Bearer {token}"


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def order_params(draft: OrderDraft) -> dict[str, str]:
    """Map a draft onto Upbit POST /orders parameters (all values as strings)."""
    params: dict[str, str] = {"market": draft.symbol, "side": "bid" if draft.side == "buy" else "ask"}
    if draft.order_type == "limit":
        assert draft.price is not None and draft.quantity is not None
        params.update(ord_type="limit", volume=_fmt(draft.quantity), price=_fmt(draft.price))
    elif draft.side == "buy":
        if draft.notional_krw is None:
            raise PhonePilotError("ORDER_REJECTED", "Upbit market buys are by KRW amount (notional_krw)")
        params.update(ord_type="price", price=_fmt(draft.notional_krw))
    else:
        assert draft.quantity is not None
        params.update(ord_type="market", volume=_fmt(draft.quantity))
    params["identifier"] = draft.client_order_id
    return params


class UpbitClient:
    """Async Upbit client. Orders require a gate-issued approval token."""

    def __init__(
        self,
        access_key: Secret,
        secret_key: Secret,
        gate: OrderGate,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self.gate = gate
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=10.0)  # default transport: no retries
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"UpbitClient(access_key={self._access_key!r}, secret_key={self._secret_key!r})"

    __str__ = __repr__

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> UpbitClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- transport -----------------------------------------------------------

    async def _send(
        self, method: str, path: str, params: dict[str, Any] | None = None, *, auth: bool, body: bool = False
    ) -> httpx.Response:
        if (method, path) not in _ALLOWED:
            raise PhonePilotError("POLICY_BLOCKED", f"Upbit endpoint {method} {path} is not permitted")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = sign(self._access_key, self._secret_key, params or None)
        url = self.base_url + path
        if body:
            return await self._client.request(method, url, json=params or {}, headers=headers)
        if params:
            url = f"{url}?{_qs(params)}"  # send exactly the string that was hashed
        return await self._client.request(method, url, headers=headers)

    def _error(self, resp: httpx.Response, *, order: bool) -> PhonePilotError:
        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('name', '')}: {err.get('message', '')}"
        except (ValueError, AttributeError):
            msg = resp.text
        msg = redact(f"HTTP {resp.status_code} {msg}", (self._access_key, self._secret_key))
        if resp.status_code in (401, 403):
            return PhonePilotError("INTEGRATION_AUTH_FAILED", msg)
        if resp.status_code == 429:
            return PhonePilotError("RATE_LIMITED", msg)
        return PhonePilotError("ORDER_REJECTED" if order else "INTEGRATION_ERROR", msg)

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool) -> Any:
        try:
            resp = await self._send("GET", path, params, auth=auth)
        except httpx.HTTPError as e:
            raise PhonePilotError("INTEGRATION_ERROR", f"network error: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise self._error(resp, order=False)
        return resp.json()

    # -- read-only -----------------------------------------------------------

    async def quote(self, markets: list[str]) -> list[dict[str, Any]]:
        """Public ticker (no auth)."""
        return await self._get("/ticker", {"markets": ",".join(markets)}, auth=False)

    async def orderbook(self, market: str) -> dict[str, Any]:
        data = await self._get("/orderbook", {"markets": market}, auth=False)
        return data[0] if data else {}

    async def balances(self) -> list[dict[str, Any]]:
        return await self._get("/accounts", auth=True)

    async def open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        return await self._get("/orders/open", {"market": market} if market else None, auth=True)

    async def order_status(self, identifier: str | None = None, *, uuid_: str | None = None) -> dict[str, Any]:
        """Look up an order by our idempotency key (identifier) or Upbit's uuid."""
        params = {"uuid": uuid_} if uuid_ else {"identifier": identifier}
        return await self._get("/order", params, auth=True)

    # -- orders --------------------------------------------------------------

    async def submit(self, order: OrderDraft, approval_token: str) -> dict[str, Any]:
        """Submit an approved order. Never retried: an uncertain outcome raises ORDER_STATE_UNKNOWN."""
        if order.exchange != "upbit":
            raise PhonePilotError("POLICY_BLOCKED", "draft is not for Upbit")
        if order.env != "real":
            raise PhonePilotError("POLICY_BLOCKED", "Upbit has no mock environment; draft must say env='real'")
        order.validate()
        params = order_params(order)
        require_allowed_host(self._secret_key, HOST)  # fail before burning the approval
        self.gate.consume_token(order, approval_token)  # before any network I/O
        try:
            resp = await self._send("POST", "/orders", params, auth=True, body=True)
        except httpx.HTTPError as e:
            self.gate.record_fill(order)  # count it: it may have gone through
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN",
                f"network error during submit ({type(e).__name__}); query identifier before doing anything",
                identifier=order.client_order_id,
            ) from None
        if resp.status_code >= 500:
            self.gate.record_fill(order)
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN", f"HTTP {resp.status_code} during submit", identifier=order.client_order_id
            )
        if resp.status_code >= 400:
            raise self._error(resp, order=True)
        self.gate.record_fill(order)
        return resp.json()

    async def cancel(self, uuid: str, approval_token: str) -> dict[str, Any]:
        """Cancel an order by Upbit uuid. Requires a cancel token for this exact uuid."""
        self.gate.consume_cancel_token("upbit", uuid, approval_token)
        try:
            resp = await self._send("DELETE", "/order", {"uuid": uuid}, auth=True)
        except httpx.HTTPError as e:
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN", f"network error during cancel ({type(e).__name__})", uuid=uuid
            ) from None
        if resp.status_code >= 500:
            raise PhonePilotError("ORDER_STATE_UNKNOWN", f"HTTP {resp.status_code} during cancel", uuid=uuid)
        if resp.status_code >= 400:
            raise self._error(resp, order=True)
        return resp.json()
