"""Korea Investment & Securities (KIS Developers) async client (PRD 17.1-17.4, FR-WFD-002).

Mock trading ('mock') is the default environment. Switching to 'real' must be gated by GUI
re-authentication in the caller (FR-HIL-010); this client only refuses drafts whose env differs.

The access token lives in memory only. App key/secret are revealed solely while building request
headers for a single call.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from phonepilot.errors import PhonePilotError
from phonepilot.integrations.orders import KST, OrderDraft, OrderGate, redact, require_allowed_host
from phonepilot.security.secrets import Secret

KisEnv = Literal["mock", "real"]

BASE_URLS: dict[str, str] = {
    "mock": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}

# !!! Paths and TR IDs MUST be verified against the official KIS Developers docs
# (https://apiportal.koreainvestment.com) before use - KIS renames TR IDs from time to time
# (e.g. cash order buy/sell moved from TTTC0802U/TTTC0801U to TTTC0012U/TTTC0011U, cancel from
# TTTC0803U to TTTC0013U). `None` = not offered in that environment.
ENDPOINTS: dict[str, dict[str, Any]] = {
    "token": {"path": "/oauth2/tokenP"},
    # 주식현재가 시세
    "price": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-price",
        "tr_id": {"real": "FHKST01010100", "mock": "FHKST01010100"},
    },
    # 주식현재가 호가/예상체결
    "orderbook": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "tr_id": {"real": "FHKST01010200", "mock": "FHKST01010200"},
    },
    # 주식잔고조회
    "balance": {
        "path": "/uapi/domestic-stock/v1/trading/inquire-balance",
        "tr_id": {"real": "TTTC8434R", "mock": "VTTC8434R"},
    },
    # 주식일별주문체결조회 (used for open orders and status: KIS has no client order id lookup)
    "daily_ccld": {
        "path": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        "tr_id": {"real": "TTTC8001R", "mock": "VTTC8001R"},
    },
    # 주식주문(현금) 매수
    "order_buy": {
        "path": "/uapi/domestic-stock/v1/trading/order-cash",
        "tr_id": {"real": "TTTC0012U", "mock": "VTTC0012U"},
    },
    # 주식주문(현금) 매도
    "order_sell": {
        "path": "/uapi/domestic-stock/v1/trading/order-cash",
        "tr_id": {"real": "TTTC0011U", "mock": "VTTC0011U"},
    },
    # 주식주문(정정취소)
    "cancel": {
        "path": "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        "tr_id": {"real": "TTTC0013U", "mock": "VTTC0013U"},
    },
}

ORD_DVSN = {"limit": "00", "market": "01"}  # 지정가 / 시장가
TOKEN_REFRESH_MARGIN_S = 300.0


def _int_str(d: Decimal, name: str) -> str:
    if d != d.to_integral_value():
        raise PhonePilotError("ORDER_REJECTED", f"KIS {name} must be a whole number")
    return str(int(d))


class KisClient:
    """Async KIS client. Orders require a gate-issued approval token."""

    def __init__(
        self,
        app_key: Secret,
        app_secret: Secret,
        account_no: Secret | str,
        gate: OrderGate,
        env: KisEnv = "mock",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if env not in BASE_URLS:
            raise PhonePilotError("POLICY_BLOCKED", f"unknown KIS env {env!r}")
        self.env: KisEnv = env
        self.base_url = BASE_URLS[env]
        self.host = urlsplit(self.base_url).hostname or ""
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self.gate = gate
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"KisClient(env={self.env}, app_key={self._app_key!r}, app_secret={self._app_secret!r})"

    __str__ = __repr__

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> KisClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- helpers -------------------------------------------------------------

    def _secrets(self) -> tuple[Secret, ...]:
        extra = (self._account_no,) if isinstance(self._account_no, Secret) else ()
        return (self._app_key, self._app_secret, *extra)

    def _check_scope(self) -> None:
        require_allowed_host(self._app_key, self.host)
        require_allowed_host(self._app_secret, self.host)
        if isinstance(self._account_no, Secret):
            require_allowed_host(self._account_no, self.host, strict=False)

    def _account(self) -> dict[str, str]:
        """CANO (8 digits) + ACNT_PRDT_CD (2 digits) from '12345678-01' or '1234567801'."""
        if isinstance(self._account_no, Secret):
            with self._account_no.reveal() as buf:
                raw = buf.decode("utf-8")
        else:
            raw = self._account_no
        digits = raw.replace("-", "").strip()
        if len(digits) != 10 or not digits.isdigit():
            raise PhonePilotError("INTEGRATION_AUTH_FAILED", "KIS account number must be 8+2 digits")
        return {"CANO": digits[:8], "ACNT_PRDT_CD": digits[8:]}

    def _tr_id(self, name: str) -> str:
        tr = ENDPOINTS[name]["tr_id"][self.env]
        if tr is None:
            raise PhonePilotError("POLICY_BLOCKED", f"KIS {name} is not available in {self.env} env")
        return tr

    def _error(self, resp: httpx.Response, *, order: bool) -> PhonePilotError:
        try:
            data = resp.json()
            msg = f"{data.get('msg_cd') or data.get('error_code', '')}: {data.get('msg1') or data.get('error_description', '')}"
        except (ValueError, AttributeError):
            msg = resp.text
        msg = redact(f"HTTP {resp.status_code} {msg}", self._secrets())
        if resp.status_code in (401, 403):
            return PhonePilotError("INTEGRATION_AUTH_FAILED", msg)
        if resp.status_code == 429:
            return PhonePilotError("RATE_LIMITED", msg)
        return PhonePilotError("ORDER_REJECTED" if order else "INTEGRATION_ERROR", msg)

    # -- token ---------------------------------------------------------------

    async def _access_token(self) -> str:
        """Issue (or reuse) the OAuth access token; memory only, refreshed shortly before expiry."""
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
                return self._token
            self._check_scope()
            url = self.base_url + ENDPOINTS["token"]["path"]
            try:
                with self._app_key.reveal() as k, self._app_secret.reveal() as s:
                    resp = await self._client.post(
                        url,
                        json={"grant_type": "client_credentials", "appkey": k.decode(), "appsecret": s.decode()},
                    )
            except httpx.HTTPError as e:
                raise PhonePilotError("INTEGRATION_ERROR", f"token request failed: {type(e).__name__}") from None
            if resp.status_code >= 400:
                err = self._error(resp, order=False)
                raise PhonePilotError("INTEGRATION_AUTH_FAILED", err.message) from None
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise PhonePilotError("INTEGRATION_AUTH_FAILED", "KIS token response had no access_token")
            self._token = token
            self._token_expires_at = time.monotonic() + float(data.get("expires_in", 86400))
            return token

    async def _send(self, method: str, name: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> httpx.Response:
        token = await self._access_token()
        url = self.base_url + ENDPOINTS[name]["path"]
        with self._app_key.reveal() as k, self._app_secret.reveal() as s:
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": k.decode(),
                "appsecret": s.decode(),
                "tr_id": self._tr_id(name),
                "custtype": "P",
            }
        try:
            return await self._client.request(method, url, params=params, json=body, headers=headers)
        finally:
            headers.clear()

    async def _get(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._send("GET", name, params=params)
        except httpx.HTTPError as e:
            raise PhonePilotError("INTEGRATION_ERROR", f"network error: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise self._error(resp, order=False)
        data = resp.json()
        if str(data.get("rt_cd", "0")) != "0":
            raise PhonePilotError("INTEGRATION_ERROR", redact(f"{data.get('msg_cd')}: {data.get('msg1')}", self._secrets()))
        return data

    # -- read-only -----------------------------------------------------------

    async def quote(self, symbols: list[str]) -> list[dict[str, Any]]:
        out = []
        for sym in symbols:
            data = await self._get("price", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": sym})
            out.append({"symbol": sym, **(data.get("output") or {})})
        return out

    async def orderbook(self, symbol: str) -> dict[str, Any]:
        data = await self._get("orderbook", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        return data.get("output1") or {}

    async def balances(self) -> dict[str, Any]:
        params = {
            **self._account(),
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = await self._get("balance", params)
        return {"holdings": data.get("output1") or [], "summary": data.get("output2") or []}

    def _ccld_params(self, *, odno: str = "", unfilled_only: bool) -> dict[str, str]:
        today = datetime.now(timezone.utc).astimezone(KST).strftime("%Y%m%d")
        return {
            **self._account(),
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "02" if unfilled_only else "00",
            "ORD_GNO_BRNO": "",
            "ODNO": odno,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

    async def open_orders(self) -> list[dict[str, Any]]:
        data = await self._get("daily_ccld", self._ccld_params(unfilled_only=True))
        return data.get("output1") or []

    async def order_status(self, order_no: str) -> list[dict[str, Any]]:
        data = await self._get("daily_ccld", self._ccld_params(odno=order_no, unfilled_only=False))
        return data.get("output1") or []

    # -- orders --------------------------------------------------------------

    async def submit(self, order: OrderDraft, approval_token: str) -> dict[str, Any]:
        """Submit an approved cash order. Never retried; uncertain outcome -> ORDER_STATE_UNKNOWN."""
        if order.exchange != "kis":
            raise PhonePilotError("POLICY_BLOCKED", "draft is not for KIS")
        if order.env != self.env:
            raise PhonePilotError("POLICY_BLOCKED", f"draft env {order.env!r} != client env {self.env!r}")
        order.validate()
        if order.quantity is None:
            raise PhonePilotError("ORDER_REJECTED", "KIS orders need a share quantity")
        body = {
            **self._account(),
            "PDNO": order.symbol,
            "ORD_DVSN": ORD_DVSN[order.order_type],
            "ORD_QTY": _int_str(order.quantity, "quantity"),
            "ORD_UNPR": _int_str(order.price, "price") if order.price is not None else "0",
        }
        name = "order_buy" if order.side == "buy" else "order_sell"
        self.gate.consume_token(order, approval_token)  # before any network I/O
        try:
            resp = await self._send("POST", name, body=body)
        except httpx.HTTPError as e:
            self.gate.record_fill(order)
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN",
                f"network error during submit ({type(e).__name__}); check today's orders before doing anything",
                client_order_id=order.client_order_id,
            ) from None
        if resp.status_code >= 500:
            self.gate.record_fill(order)
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN", f"HTTP {resp.status_code} during submit", client_order_id=order.client_order_id
            )
        if resp.status_code >= 400:
            raise self._error(resp, order=True)
        data = resp.json()
        if str(data.get("rt_cd")) != "0":
            raise PhonePilotError("ORDER_REJECTED", redact(f"{data.get('msg_cd')}: {data.get('msg1')}", self._secrets()))
        self.gate.record_fill(order)
        out = data.get("output") or {}
        return {"order_no": out.get("ODNO"), "org_no": out.get("KRX_FWDG_ORD_ORGNO"), "raw": out}

    async def cancel(self, order_no: str, approval_token: str, *, org_no: str = "") -> dict[str, Any]:
        """Cancel the full remaining quantity of `order_no`. Requires a cancel token for it."""
        self.gate.consume_cancel_token("kis", order_no, approval_token)
        body = {
            **self._account(),
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02 = 취소
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        try:
            resp = await self._send("POST", "cancel", body=body)
        except httpx.HTTPError as e:
            raise PhonePilotError(
                "ORDER_STATE_UNKNOWN", f"network error during cancel ({type(e).__name__})", order_no=order_no
            ) from None
        if resp.status_code >= 500:
            raise PhonePilotError("ORDER_STATE_UNKNOWN", f"HTTP {resp.status_code} during cancel", order_no=order_no)
        if resp.status_code >= 400:
            raise self._error(resp, order=True)
        data = resp.json()
        if str(data.get("rt_cd")) != "0":
            raise PhonePilotError("ORDER_REJECTED", redact(f"{data.get('msg_cd')}: {data.get('msg1')}", self._secrets()))
        return data.get("output") or {}
