from __future__ import annotations

"""주문 전 마지막 안전장치 역할을 하는 모듈.

전략이 BUY를 냈다고 해서 바로 주문하지 않고,
리스크 규칙에 위배되지 않는지 여기서 한번 더 점검합니다.
"""

from config.settings import RiskConfig, TradingConfig
from domain.models import AccountBalance, OrderRequest, RuntimeState


class RiskManager:
    """주문 가능 여부를 검사하는 간단한 리스크 관리자입니다."""

    def __init__(self, trading_config: TradingConfig, risk_config: RiskConfig) -> None:
        """운영 제한값을 보관합니다."""

        self.trading_config = trading_config
        self.risk_config = risk_config

    def can_place_order(self, order: OrderRequest, balance: AccountBalance, state: RuntimeState) -> tuple[bool, str]:
        """주문이 가능한지 검사하고, 불가능하면 이유를 함께 돌려줍니다."""

        # 종목당 하루 1회 진입 제한
        if not self.trading_config.allow_multiple_entries_per_symbol_per_day:
            if order.symbol in state.bought_symbols_today:
                return False, "already bought today"

        # 최대 보유 종목 수 제한
        current_symbols = {position.symbol for position in balance.positions}
        if order.symbol not in current_symbols and len(current_symbols) >= self.trading_config.max_positions:
            return False, "max positions exceeded"

        # 최소 현금 버퍼 유지
        estimated_amount = (order.price or 0) * order.quantity
        if balance.cash - estimated_amount < self.risk_config.min_cash_buffer:
            return False, "cash buffer would be violated"

        # 주문 금액 상한 제한
        if estimated_amount > self.risk_config.max_order_amount:
            return False, "order amount exceeds max_order_amount"

        return True, "ok"
