from __future__ import annotations

"""실제 증권사 연결 없이 전체 흐름을 테스트하기 위한 가짜 브로커.

이 클래스는 매우 중요합니다.
실제 API를 붙이기 전에 프로그램 구조가 맞는지 검증할 수 있기 때문입니다.
"""

from datetime import datetime
from itertools import count

from domain.models import AccountBalance, MarketPrice, OrderRequest, OrderResult, Position
from infra.broker.base import Broker


class MockBroker(Broker):
    """메모리 안에서만 동작하는 테스트용 브로커입니다."""

    def __init__(self) -> None:
        """초기 현금, 초기 가격, 주문 번호 시퀀스를 준비합니다."""

        self._seq = count(1)
        self._cash = 1_000_000
        self._positions: dict[str, Position] = {}
        self._prices = {
            "005930": 71000,
            "000660": 185000,
        }

    def authenticate(self) -> None:
        """가짜 브로커라서 실제 인증은 하지 않습니다."""

        return None

    def get_market_price(self, symbol: str) -> MarketPrice:
        """미리 넣어둔 가짜 가격으로 현재가 객체를 만들어 돌려줍니다."""

        current = self._prices.get(symbol, 10000)
        return MarketPrice(
            symbol=symbol,
            current_price=current,
            reference_price=current,
            previous_close=int(current * 0.985),
            timestamp=datetime.now(),
        )

    def get_account_balance(self) -> AccountBalance:
        """현재 현금과 보유 포지션을 반환합니다."""

        return AccountBalance(cash=self._cash, total_asset=self._cash, positions=list(self._positions.values()))

    def place_order(self, order: OrderRequest) -> OrderResult:
        """매수/매도를 메모리 상에서 흉내 냅니다."""

        order_id = f"MOCK-{next(self._seq):06d}"

        if order.side.value == "BUY":
            current_price = self._prices.get(order.symbol, 10000)
            cost = current_price * order.quantity
            if self._cash < cost:
                return OrderResult(order_id, order.symbol, order.side, order.quantity, False, "insufficient cash", datetime.now())
            self._cash -= cost
            self._positions[order.symbol] = Position(order.symbol, order.quantity, current_price)
        else:
            position = self._positions.get(order.symbol)
            if position is None:
                return OrderResult(order_id, order.symbol, order.side, order.quantity, False, "no position", datetime.now())
            sell_price = self._prices.get(order.symbol, position.average_price)
            self._cash += sell_price * order.quantity
            self._positions.pop(order.symbol, None)

        return OrderResult(order_id, order.symbol, order.side, order.quantity, True, "accepted", datetime.now())
