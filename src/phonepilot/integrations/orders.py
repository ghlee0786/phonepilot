"""Order safety core (PRD 13.4, 17.3-17.4).

Every order goes through the same pipeline:

1. `OrderDraft` - an immutable-by-convention description of the economic intent.
2. `OrderGate.check()` - allow-list, per-order/daily limits, price deviation, minimum size.
3. A human approves in the GUI, which then calls `OrderGate.issue_token()`.
4. The exchange client calls `OrderGate.consume_token()` *before* any network I/O.
5. `OrderGate.record_fill()` books the amount into the daily `LimitsLedger`.

Withdrawals and transfers are deliberately not representable: a draft can only buy or sell.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from phonepilot.errors import PhonePilotError
from phonepilot.security.secrets import Secret

try:  # Windows has no system tz database; `tzdata` may be missing.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    KST: tzinfo = ZoneInfo("Asia/Seoul")
except (ImportError, ZoneInfoNotFoundError):  # Korea has no DST since 1988, so a fixed offset is exact.
    KST = timezone(timedelta(hours=9), "KST")

Exchange = Literal["upbit", "kis"]
Env = Literal["real", "mock"]
Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]
Level = Literal["ok", "warn", "block"]

EXCHANGES = ("upbit", "kis")
ENVS = ("real", "mock")
SIDES = ("buy", "sell")
ORDER_TYPES = ("limit", "market")

TOKEN_TTL_S = 60.0
CHECK_TTL_S = 120.0


def kst_today(now: datetime | None = None) -> date:
    """Current calendar date in Asia/Seoul."""
    return (now or datetime.now(timezone.utc)).astimezone(KST).date()


def _dec(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        value = repr(value)  # avoid binary float artefacts
    try:
        d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise PhonePilotError("POLICY_BLOCKED", f"{name} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise PhonePilotError("POLICY_BLOCKED", f"{name} must be a positive finite number")
    return d


def _canon(d: object) -> str | None:
    return None if d is None else format(Decimal(str(d)).normalize(), "f")


# --------------------------------------------------------------------------- draft


@dataclass
class OrderDraft:
    """What the human is asked to approve. Only buy/sell exist - no withdraw/transfer."""

    exchange: Exchange
    env: Env
    symbol: str
    side: Side
    order_type: OrderType
    price: Decimal | None = None
    quantity: Decimal | None = None
    notional_krw: Decimal | None = None
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Structural validation (raises POLICY_BLOCKED). Re-run after any mutation."""
        if self.exchange not in EXCHANGES:
            raise PhonePilotError("POLICY_BLOCKED", f"unsupported exchange {self.exchange!r}")
        if self.env not in ENVS:
            raise PhonePilotError("POLICY_BLOCKED", f"unsupported env {self.env!r}")
        if self.side not in SIDES:
            raise PhonePilotError("POLICY_BLOCKED", f"unsupported side {self.side!r}; only buy/sell exist")
        if self.order_type not in ORDER_TYPES:
            raise PhonePilotError("POLICY_BLOCKED", f"unsupported order type {self.order_type!r}")
        self.symbol = str(self.symbol).strip().upper()
        if not self.symbol:
            raise PhonePilotError("POLICY_BLOCKED", "symbol is required")
        self.price = _dec(self.price, "price")
        self.quantity = _dec(self.quantity, "quantity")
        self.notional_krw = _dec(self.notional_krw, "notional_krw")
        if self.order_type == "limit":
            if self.price is None or self.quantity is None:
                raise PhonePilotError("POLICY_BLOCKED", "limit orders need price and quantity")
            if self.notional_krw is not None:
                raise PhonePilotError("POLICY_BLOCKED", "limit orders take price x quantity, not notional_krw")
        else:
            if self.price is not None:
                raise PhonePilotError("POLICY_BLOCKED", "market orders must not carry a price")
            if self.side == "sell" and (self.quantity is None or self.notional_krw is not None):
                raise PhonePilotError("POLICY_BLOCKED", "market sells need quantity only")
            if self.side == "buy" and (self.quantity is None) == (self.notional_krw is None):
                raise PhonePilotError("POLICY_BLOCKED", "market buys need exactly one of quantity or notional_krw")

    def economic_fields(self) -> dict[str, str | None]:
        return {
            "client_order_id": self.client_order_id,
            "env": self.env,
            "exchange": self.exchange,
            "notional_krw": _canon(self.notional_krw),
            "order_type": self.order_type,
            "price": _canon(self.price),
            "quantity": _canon(self.quantity),
            "side": self.side,
            "symbol": str(self.symbol).strip().upper(),
        }

    @property
    def draft_hash(self) -> str:
        """sha256 over canonical JSON of the economic fields (any change invalidates approval)."""
        blob = json.dumps(self.economic_fields(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def estimate_notional(self, market_price: Decimal | None = None) -> Decimal | None:
        """KRW amount of the order: price x qty, the explicit notional, or market price x qty."""
        if self.order_type == "limit" and self.price is not None and self.quantity is not None:
            return self.price * self.quantity
        if self.notional_krw is not None:
            return self.notional_krw
        if self.quantity is not None and market_price is not None and market_price > 0:
            return market_price * self.quantity
        return None


# --------------------------------------------------------------------------- limits


@dataclass
class OrderLimits:
    """User-configured limits (PRD 13.4). Only the GUI may raise them (FR-HIL-010)."""

    order_max_krw: Decimal = Decimal(100_000)
    order_daily_krw: Decimal = Decimal(300_000)
    order_daily_count: int = 10
    allowed_symbols: list[str] = field(default_factory=list)  # empty = everything blocked
    price_warn_pct: Decimal = Decimal(5)
    price_block_pct: Decimal = Decimal(15)
    spread_warn_pct: Decimal = Decimal(1)
    min_order_krw: dict[str, Decimal] = field(default_factory=lambda: {"upbit": Decimal(5000)})

    def __post_init__(self) -> None:
        for name in ("order_max_krw", "order_daily_krw", "price_warn_pct", "price_block_pct", "spread_warn_pct"):
            setattr(self, name, Decimal(str(getattr(self, name))))
        self.min_order_krw = {k: Decimal(str(v)) for k, v in self.min_order_krw.items()}
        self.allowed_symbols = [s.strip().upper() for s in self.allowed_symbols]

    def min_order_for(self, exchange: str) -> Decimal:
        return self.min_order_krw.get(exchange, Decimal(0))


class LimitsLedger:
    """Today's (KST) order total and count.

    Only the aggregate numbers and the date are persisted (FR-HIL-011) so that restarting the
    app does not reset the daily limit. No order details are ever written.
    """

    def __init__(self, path: Path | str | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path) if path else None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.day: date = kst_today(self._now())
        self.total_krw = Decimal(0)
        self.count = 0
        self._load()

    def _today(self) -> date:
        return kst_today(self._now())

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if date.fromisoformat(data["date"]) == self.day:
                self.total_krw = Decimal(str(data["total_krw"]))
                self.count = int(data["count"])
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            pass  # corrupt state behaves like a missing file

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"date": self.day.isoformat(), "total_krw": str(self.total_krw), "count": self.count}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.path)

    def _roll(self) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.total_krw, self.count = today, Decimal(0), 0
            self._save()

    def snapshot(self) -> tuple[Decimal, int]:
        """(total_krw, count) for today, resetting first if the KST date changed."""
        with self._lock:
            self._roll()
            return self.total_krw, self.count

    def add(self, notional_krw: Decimal) -> None:
        with self._lock:
            self._roll()
            self.total_krw += notional_krw
            self.count += 1
            self._save()


# --------------------------------------------------------------------------- gate


@dataclass
class CheckResult:
    ok: bool
    level: Level
    reasons: list[str]
    notional_krw: Decimal | None
    deviation_pct: Decimal | None = None
    daily_remaining_krw: Decimal | None = None
    daily_remaining_count: int | None = None


@dataclass
class _Token:
    subject_hash: str
    expires_at: float


class OrderGate:
    """Limit checks + single-use approval tokens bound to a draft hash."""

    def __init__(
        self,
        limits: OrderLimits | None = None,
        ledger: LimitsLedger | None = None,
        token_ttl_s: float = TOKEN_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits or OrderLimits()
        self.ledger = ledger or LimitsLedger()
        self.token_ttl_s = token_ttl_s
        self._clock = clock
        self._tokens: dict[str, _Token] = {}
        self._checked: dict[str, tuple[CheckResult, float]] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        return self._clock() if self._clock else time.monotonic()

    # -- checks --------------------------------------------------------------

    def check(self, draft: OrderDraft, market_price: Decimal, *, spread_pct: Decimal | None = None) -> CheckResult:
        """Evaluate a draft against the limits. Never raises for rule violations."""
        draft.validate()
        lim = self.limits
        blocks: list[str] = []
        warns: list[str] = []
        mp = Decimal(str(market_price)) if market_price is not None else Decimal(0)
        if not mp.is_finite() or mp <= 0:
            blocks.append("market_price_unavailable: 현재가를 확인할 수 없음")
            mp = Decimal(0)

        if draft.symbol not in lim.allowed_symbols:
            blocks.append(f"symbol_not_allowed: {draft.symbol} 은(는) 허용 종목 목록에 없음")

        notional = draft.estimate_notional(mp or None)
        total, count = self.ledger.snapshot()
        if notional is None:
            blocks.append("notional_unknown: 주문 금액을 계산할 수 없음")
        else:
            if notional > lim.order_max_krw:
                blocks.append(f"order_max_exceeded: {notional:,.0f} > 건당 한도 {lim.order_max_krw:,.0f}")
            if total + notional > lim.order_daily_krw:
                blocks.append(
                    f"daily_amount_exceeded: 오늘 {total:,.0f} + {notional:,.0f} > 일일 한도 {lim.order_daily_krw:,.0f}"
                )
            min_krw = lim.min_order_for(draft.exchange)
            if notional < min_krw:
                blocks.append(f"below_min_order: {notional:,.0f} < 최소 주문 금액 {min_krw:,.0f}")
        if count + 1 > lim.order_daily_count:
            blocks.append(f"daily_count_exceeded: 오늘 {count}건, 일일 한도 {lim.order_daily_count}건")

        deviation: Decimal | None = None
        if draft.order_type == "limit" and draft.price is not None and mp > 0:
            deviation = abs(draft.price - mp) / mp * 100
            if deviation > lim.price_block_pct:
                blocks.append(f"price_deviation_blocked: 현재가 대비 {deviation:.2f}% > {lim.price_block_pct}%")
            elif deviation > lim.price_warn_pct:
                warns.append(f"price_deviation_warning: 현재가 대비 {deviation:.2f}% > {lim.price_warn_pct}%")
        if draft.order_type == "market" and spread_pct is not None and Decimal(str(spread_pct)) > lim.spread_warn_pct:
            warns.append(f"wide_spread_warning: 호가 스프레드 {Decimal(str(spread_pct)):.2f}% > {lim.spread_warn_pct}%")

        level: Level = "block" if blocks else ("warn" if warns else "ok")
        result = CheckResult(
            ok=not blocks,
            level=level,
            reasons=blocks + warns,
            notional_krw=notional,
            deviation_pct=deviation,
            daily_remaining_krw=max(lim.order_daily_krw - total, Decimal(0)),
            daily_remaining_count=max(lim.order_daily_count - count, 0),
        )
        with self._lock:
            self._checked[draft.draft_hash] = (result, self._now())
        return result

    # -- approval tokens -----------------------------------------------------

    def _purge(self, now: float) -> None:
        for tok in [t for t, v in self._tokens.items() if v.expires_at <= now]:
            del self._tokens[tok]
        for h in [h for h, (_, at) in self._checked.items() if now - at > CHECK_TTL_S + self.token_ttl_s]:
            del self._checked[h]

    def _issue(self, subject_hash: str) -> str:
        now = self._now()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            self._tokens[token] = _Token(subject_hash, now + self.token_ttl_s)
        return token

    def _consume(self, subject_hash: str, token: str | None) -> None:
        now = self._now()
        with self._lock:
            entry = self._tokens.pop(token, None) if token else None  # single use: burned on any attempt
        if entry is None:
            raise PhonePilotError("APPROVAL_DENIED", "approval token missing, unknown or already used")
        if entry.expires_at <= now:
            raise PhonePilotError("APPROVAL_DENIED", "approval token expired")
        if not hmac.compare_digest(entry.subject_hash, subject_hash):
            raise PhonePilotError("APPROVAL_DENIED", "approval token does not match this order (draft changed)")

    def issue_token(self, draft: OrderDraft) -> str:
        """Call ONLY after a human approved this exact draft in the approval window."""
        h = draft.draft_hash
        now = self._now()
        with self._lock:
            checked = self._checked.get(h)
        if checked is None or now - checked[1] > CHECK_TTL_S:
            raise PhonePilotError("POLICY_BLOCKED", "draft must pass OrderGate.check() right before approval")
        if not checked[0].ok:
            raise PhonePilotError("LIMIT_EXCEEDED", "; ".join(checked[0].reasons))
        return self._issue(h)

    def consume_token(self, draft: OrderDraft, token: str | None) -> None:
        """Validate and burn the token; re-checks daily limits against the ledger."""
        self._consume(draft.draft_hash, token)
        with self._lock:
            checked = self._checked.get(draft.draft_hash)
        notional = checked[0].notional_krw if checked else draft.estimate_notional()
        total, count = self.ledger.snapshot()
        if count + 1 > self.limits.order_daily_count or (
            notional is not None and total + notional > self.limits.order_daily_krw
        ):
            raise PhonePilotError("LIMIT_EXCEEDED", "daily limit reached since approval")

    @staticmethod
    def _cancel_hash(exchange: str, order_id: str) -> str:
        blob = json.dumps({"action": "cancel", "exchange": exchange, "order_id": order_id}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def issue_cancel_token(self, exchange: str, order_id: str) -> str:
        """Call ONLY after a human approved cancelling this order."""
        return self._issue(self._cancel_hash(exchange, order_id))

    def consume_cancel_token(self, exchange: str, order_id: str, token: str | None) -> None:
        self._consume(self._cancel_hash(exchange, order_id), token)

    # -- bookkeeping ---------------------------------------------------------

    def record_fill(self, draft: OrderDraft, notional_krw: Decimal | None = None) -> None:
        """Book a submitted order (filled or pending - PRD counts both) into today's usage."""
        with self._lock:
            checked = self._checked.pop(draft.draft_hash, None)
        amount = notional_krw or (checked[0].notional_krw if checked else None) or draft.estimate_notional()
        self.ledger.add(amount or Decimal(0))


# --------------------------------------------------------------------------- shared helpers for clients


def require_allowed_host(secret: Secret, host: str, *, strict: bool = True) -> None:
    """Refuse to use `secret` for `host` unless its meta allows it.

    strict=False tolerates an empty allow-list (e.g. an access key id that is not itself secret).
    """
    allowed = [h.strip().lower() for h in secret.meta.allowed_hosts]
    if not allowed and not strict:
        return
    if host.lower() not in allowed:
        raise PhonePilotError("SECRET_SCOPE_DENIED", f"{secret.name} is not allowed for host {host}")


def redact(text: object, secrets_: Iterable[Secret], limit: int = 200) -> str:
    """Truncate a server message and drop it entirely if it echoes any secret."""
    s = str(text)[:limit]
    if any(sec.contained_in(str(text)) for sec in secrets_):
        return "(message redacted: contained secret material)"
    return s
