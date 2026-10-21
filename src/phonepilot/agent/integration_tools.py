"""Integration tools (YouTube research, market data, approval-gated orders) mixed into ToolService (PRD 10.8, 16.3, 17.4)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from phonepilot.agent.timeline import TimelineEvent
from phonepilot.errors import PhonePilotError
from phonepilot.security.approvals import ApprovalRequest
from phonepilot.security.policy import Level, PolicyDecision


def _price_of(row: dict[str, Any]) -> Decimal | None:
    for key in ("trade_price", "stck_prpr", "price", "last"):
        if row.get(key) not in (None, ""):
            return Decimal(str(row[key]))
    return None


class IntegrationMixin:
    integrations: dict[str, Any]
    timeline: Any
    approvals: Any
    redactor: Any

    # ---------------------------------------------------------------- youtube
    def _yt(self) -> Any:
        yt = self.integrations.get("youtube")
        if yt is None:
            raise PhonePilotError("POLICY_BLOCKED", "YouTube integration is disabled")
        return yt

    async def yt_search_channels(self, actor: str = "agent", q: str = "", max_results: int = 10) -> dict[str, Any]:
        items = await self._yt().search_channels(q, max_results=max_results)
        rows = [
            {"channel_id": (it.get("id") or {}).get("channelId") or it.get("snippet", {}).get("channelId"), "title": it.get("snippet", {}).get("title")}
            for it in items
        ]
        return {"channels": rows, "quota_used": self._yt().quota_used}

    async def yt_channel_report(self, actor: str = "agent", channel_id: str = "") -> dict[str, Any]:
        from phonepilot.integrations.youtube import analyze_channel

        yt = self._yt()
        chans = await yt.channels([channel_id])
        if not chans:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"channel {channel_id} not found")
        ch = chans[0]
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        subs = int(ch.get("statistics", {}).get("subscriberCount", 0) or 0)
        videos: list[dict[str, Any]] = []
        if uploads:
            items = await yt.recent_videos(uploads, 50)
            ids = [it.get("contentDetails", {}).get("videoId") for it in items if it.get("contentDetails", {}).get("videoId")]
            videos = await yt.videos(ids)
        return {
            "channel": {"id": channel_id, "title": ch.get("snippet", {}).get("title"), "statistics": ch.get("statistics", {})},
            "analysis": analyze_channel(videos, subscribers=subs),
            "quota_used": yt.quota_used,
        }

    async def yt_category_list(self, actor: str = "agent", region: str = "KR") -> dict[str, Any]:
        cats = await self._yt().categories(region)
        return {"categories": [{"id": c.get("id"), "title": c.get("snippet", {}).get("title")} for c in cats]}

    # ----------------------------------------------------------------- market
    def _market(self, exchange: str) -> Any:
        market = self.integrations.get("market") or {}
        client = market.get(exchange)
        if client is None:
            raise PhonePilotError("POLICY_BLOCKED", f"{exchange} integration is disabled")
        return client

    async def market_quote(self, actor: str = "agent", exchange: str = "upbit", symbols: list[str] | None = None) -> dict[str, Any]:
        rows = await self._market(exchange).quote(symbols or [])
        return {"exchange": exchange, "quotes": [{"symbol": r.get("market") or r.get("symbol"), "price": str(_price_of(r))} for r in rows]}

    async def account_balances(self, actor: str = "agent", exchange: str = "upbit") -> dict[str, Any]:
        data = await self._market(exchange).balances()
        return {"exchange": exchange, "balances": data, "disclaimer": "투자 자문이 아닙니다"}

    async def account_open_orders(self, actor: str = "agent", exchange: str = "upbit") -> dict[str, Any]:
        return {"exchange": exchange, "orders": await self._market(exchange).open_orders()}

    async def order_draft(
        self,
        actor: str = "agent",
        exchange: str = "upbit",
        symbol: str = "",
        side: str = "buy",
        order_type: str = "limit",
        price: str | None = None,
        quantity: str | None = None,
        notional_krw: str | None = None,
    ) -> dict[str, Any]:
        from phonepilot.integrations.orders import OrderDraft

        client = self._market(exchange)
        env = getattr(client, "env", "real")
        draft = OrderDraft(
            exchange=exchange,  # type: ignore[arg-type]
            env=env,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            order_type=order_type,  # type: ignore[arg-type]
            price=Decimal(price) if price else None,
            quantity=Decimal(quantity) if quantity else None,
            notional_krw=Decimal(notional_krw) if notional_krw else None,
        )
        quotes = await client.quote([symbol])
        market_price = _price_of(quotes[0]) if quotes else None
        check = client.gate.check(draft, market_price or Decimal(0))
        drafts: dict[str, Any] = self.integrations.setdefault("_drafts", {})
        drafts[draft.client_order_id] = (draft, check, market_price)
        self.timeline.add(TimelineEvent(actor, "order_draft", f"{exchange} {side} {symbol}", check.level, "draft"))
        return {
            "draft_id": draft.client_order_id,
            "exchange": exchange,
            "env": env,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "price": price,
            "quantity": quantity,
            "notional_krw": str(check.notional_krw) if check.notional_krw is not None else None,
            "market_price": str(market_price) if market_price is not None else None,
            "check": {"ok": check.ok, "level": check.level, "reasons": check.reasons},
        }

    async def order_submit(self, actor: str = "agent", draft_id: str = "") -> dict[str, Any]:
        drafts: dict[str, Any] = self.integrations.get("_drafts", {})
        if draft_id not in drafts:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "unknown draft_id; call order_draft first")
        draft, check, market_price = drafts[draft_id]
        if not check.ok:
            raise PhonePilotError("LIMIT_EXCEEDED", "; ".join(check.reasons))
        client = self._market(draft.exchange)
        notional = str(check.notional_krw) if check.notional_krw is not None else None
        side_ko = "매수" if draft.side == "buy" else "매도"
        req = ApprovalRequest(
            title="주문을 실제로 제출합니다" + (" (모의투자)" if draft.env == "mock" else " (실전)"),
            summary=f"{draft.exchange} {draft.symbol} {side_ko} {draft.order_type} 가격 {draft.price or '시장가'} 수량 {draft.quantity or '-'} 금액 약 {notional}원",
            decision=PolicyDecision(Level.CONFIRM_HIGH, "trading", check.reasons or ["order"]),
            client=actor,
            details={"market_price": str(market_price), "warnings": check.reasons},
            expected_amount=notional,
        )
        await self.approvals.request(req)
        token = client.gate.issue_token(draft)
        t0 = time.monotonic()
        try:
            result = await client.submit(draft, token)
        except PhonePilotError as e:
            self.timeline.add(TimelineEvent(actor, "order_submit", f"{draft.exchange} {side_ko} {draft.symbol}", "CONFIRM_HIGH", e.code))
            raise
        drafts.pop(draft_id, None)
        self.timeline.add(
            TimelineEvent(actor, "order_submit", f"{draft.exchange} {side_ko} {draft.symbol} {notional}원", "CONFIRM_HIGH", "submitted", elapsed_ms=int((time.monotonic() - t0) * 1000))
        )
        return {"submitted": True, "exchange": draft.exchange, "env": draft.env, "result": self.redactor.redact_obj(result)}

    async def order_cancel(self, actor: str = "agent", exchange: str = "upbit", order_id: str = "") -> dict[str, Any]:
        client = self._market(exchange)
        req = ApprovalRequest(
            title="주문 취소",
            summary=f"{exchange} 주문 {order_id} 취소",
            decision=PolicyDecision(Level.CONFIRM, "trading", ["cancel order"]),
            client=actor,
        )
        await self.approvals.request(req)
        token = client.gate.issue_cancel_token(exchange, order_id)
        result = await client.cancel(order_id, token)
        self.timeline.add(TimelineEvent(actor, "order_cancel", f"{exchange} {order_id}", "CONFIRM", "cancelled"))
        return {"cancelled": True, "result": self.redactor.redact_obj(result)}
