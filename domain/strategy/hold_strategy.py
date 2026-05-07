from __future__ import annotations

"""횡보장/하락장 전용 소극적 전략.

이 전략은 신규 매수를 하지 않습니다.
보유 중인 종목에 대해서만 익절/손절 판단을 수행합니다.

장세 분류기가 SIDEWAYS 또는 BEARISH를 반환할 때 StrategyRouter가 이 전략을 선택합니다.
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class HoldStrategy(Strategy):
    """신규 매수 없이 보유 포지션만 관리하는 전략입니다."""

    def __init__(self, config: StrategyConfig, regime_label: str = "횡보/하락장") -> None:
        """
        Parameters
        ----------
        config       : 익절/손절 기준값 (BreakoutStrategy와 동일한 설정을 재사용)
        regime_label : 로그에 표시할 장세 이름 (예: "횡보장", "하락장")
        """
        self.config = config
        self.regime_label = regime_label

    def generate_signal(self, market_price: MarketPrice, position: Position | None) -> Signal:
        """보유 중이면 익절/손절 판단, 미보유면 무조건 HOLD."""

        current_price = market_price.current_price

        # 미보유 → 장세가 좋지 않으니 신규 매수 안 함
        if position is None:
            return Signal(
                type=SignalType.HOLD,
                reason=f"{self.regime_label} — 신규 매수를 보류합니다",
            )

        # 보유 중 → 익절/손절은 장세와 무관하게 항상 판단
        average_price = position.average_price
        take_profit_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        stop_loss_price = int(average_price * (1 - self.config.stop_loss_pct / 100))

        if current_price >= take_profit_price:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"{self.regime_label}이지만 익절 목표 {take_profit_price:,}원 도달 — 매도합니다"
                ),
            )

        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"{self.regime_label} + 손절 기준 {stop_loss_price:,}원 하회 — 손절합니다"
                ),
            )

        return Signal(
            type=SignalType.HOLD,
            reason=(
                f"{self.regime_label} — 익절 {take_profit_price:,}원 / 손절 {stop_loss_price:,}원 "
                f"사이에서 유지합니다"
            ),
        )
