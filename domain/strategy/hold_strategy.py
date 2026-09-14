from __future__ import annotations

"""횡보장/하락장 전용 소극적 전략.

이 전략은 신규 매수를 하지 않습니다.
보유 중인 종목에 대해서만 익절/손절 판단을 수행합니다.

장세 분류기가 SIDEWAYS 또는 BEARISH를 반환할 때 StrategyRouter가 이 전략을 선택합니다.
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy
from domain.strategy.exit_calc import TrailingParams, calc_stop_loss


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

    def trailing_params(self) -> TrailingParams | None:
        # 2026-09-14 (구현 지시서 §2.5): HoldStrategy는 원본부터 트레일링이
        # 없습니다(highest_price 미사용, 익절/손절만 판단) — Strategy
        # 기본값(None)과 동일하지만, "트레일링이 없다"는 사실을 이
        # 파일만 보고도 알 수 있도록 명시적으로 오버라이드합니다.
        return None

    def generate_signal(self, market_price: MarketPrice, position: Position | None, minute_analysis=None, highest_price: int = 0, **kwargs) -> Signal:
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
        # 2026-09-14 (180초 감시 공백 대응 — 구현 지시서 1번): 손절
        # 계산을 exit_calc.py의 순수 함수로 추출 — HoldStrategy는
        # 트레일링이 없으므로(highest_price 미사용) calc_trailing_stop()은
        # 쓰지 않습니다. 기존 평가 순서(익절 먼저, 손절 나중)는 그대로.
        stop_loss = calc_stop_loss(average_price, current_price, self.config.stop_loss_pct)

        if current_price >= take_profit_price:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"{self.regime_label}이지만 익절 목표 {take_profit_price:,}원 도달 — 매도합니다"
                ),
            )

        if stop_loss.triggered:
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"{self.regime_label} + 손절 기준 {stop_loss.stop_loss_price:,}원 하회 — 손절합니다"
                ),
            )

        return Signal(
            type=SignalType.HOLD,
            reason=(
                f"{self.regime_label} — 익절 {take_profit_price:,}원 / 손절 {stop_loss.stop_loss_price:,}원 "
                f"사이에서 유지합니다"
            ),
        )
