from __future__ import annotations

"""단순 돌파 전략.

이 전략은 아래 규칙으로 동작합니다.
1. 보유 종목이 없으면:
   - 기준가 대비 일정 비율 이상 상승했을 때 BUY
   - 아니면 HOLD
2. 보유 종목이 있으면:
   - 평균단가 대비 익절 구간이면 SELL
   - 평균단가 대비 손절 구간이면 SELL
   - 아니면 HOLD
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class BreakoutStrategy(Strategy):
    """기준가 돌파 + 익절/손절 기반의 단순 전략입니다."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def generate_signal(self, market_price: MarketPrice, position: Position | None) -> Signal:
        """현재 시세와 보유 상태를 보고 BUY / SELL / HOLD 신호를 생성합니다."""

        current_price = market_price.current_price

        # 보유 중이 아니라면 신규 진입 판단
        if position is None:
            reference_price = market_price.reference_price
            breakout_price = int(reference_price * (1 + self.config.breakout_threshold_pct / 100))

            if current_price >= breakout_price:
                return Signal(
                    type=SignalType.BUY,
                    reason=(
                        f"기준가 {reference_price:,}원 대비 "
                        f"{self.config.breakout_threshold_pct:.1f}% 돌파 조건을 충족했습니다"
                    ),
                )

            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"기준가 {reference_price:,}원 대비 "
                    f"돌파 기준 {breakout_price:,}원에 아직 도달하지 않아 관망합니다"
                ),
            )

        # 보유 중이라면 익절/손절 판단
        average_price = position.average_price
        take_profit_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        stop_loss_price = int(average_price * (1 - self.config.stop_loss_pct / 100))

        if current_price >= take_profit_price:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"평균단가 {average_price:,}원 대비 "
                    f"익절 목표 {take_profit_price:,}원에 도달했습니다"
                ),
            )

        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"평균단가 {average_price:,}원 대비 "
                    f"손절 기준 {stop_loss_price:,}원 아래로 내려왔습니다"
                ),
            )

        return Signal(
            type=SignalType.HOLD,
            reason=(
                f"보유 중이지만 익절 목표 {take_profit_price:,}원과 "
                f"손절 기준 {stop_loss_price:,}원 사이에 있어 유지합니다"
            ),
        )