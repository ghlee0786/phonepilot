---
name: phonepilot-market-review
description: Read balances and quotes from Upbit / KIS through PhonePilot's official-API integration and, on request, prepare an order draft that the user must approve in the PhonePilot window. Not investment advice; no automated trading, no withdrawals.
---

# 투자 조회·승인형 주문 (워크플로 D)

1. 조회: `account_balances(exchange)`, `market_quote(exchange, symbols)`, `account_open_orders(exchange)`. 요약은 사실·수치 중심으로, "투자 자문이 아님"을 밝힌다.
2. 주문은 사용자가 명시적으로 요청할 때만: `order_draft(...)` → 결과의 `check`(허용 종목, 건당·일일 한도, 가격 괴리)를 보여주고 → 사용자가 원하면 `order_submit(draft_id)`. PhonePilot 창에서 사용자가 금액을 직접 입력해 승인해야 제출된다.
3. `ORDER_STATE_UNKNOWN`이면 재제출하지 말고 `account_open_orders`로 확인한다.
4. 조건부 자동 주문·반복 매매·출금·송금은 하지 않는다. KIS는 기본 모의투자다.
