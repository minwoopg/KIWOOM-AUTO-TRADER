from __future__ import annotations

"""전략 라우터.

장세(MarketRegime)와 매매 모드(단타/스윙)에 따라 전략 객체를 반환합니다.

단타(DAY):
    BULLISH  → BreakoutStrategy
    그 외    → HoldStrategy

스윙(SWING):
    BULLISH  → SwingStrategy
    그 외    → HoldStrategy
"""

from config.settings import StrategyConfig, SwingConfig
from domain.models import MarketRegime
from domain.strategy.base import Strategy
from domain.strategy.breakout_strategy import BreakoutStrategy
from domain.strategy.hold_strategy import HoldStrategy
from domain.strategy.swing_strategy import SwingStrategy


class StrategyRouter:
    """장세 + 모드에 맞는 전략을 반환하는 라우터입니다."""

    def __init__(self, day_config: StrategyConfig, swing_config: SwingConfig) -> None:
        self._breakout      = BreakoutStrategy(day_config)
        self._swing         = SwingStrategy(swing_config)
        self._hold_sideways = HoldStrategy(day_config, regime_label="횡보장")
        self._hold_bearish  = HoldStrategy(day_config, regime_label="하락장")
        self._hold_unknown  = HoldStrategy(day_config, regime_label="장세불명")

    def select(self, regime: MarketRegime, is_swing: bool = False) -> Strategy:
        """장세와 매매 모드에 맞는 전략 객체를 반환합니다.

        Parameters
        ----------
        regime   : 장세 분류 결과
        is_swing : True면 스윙 모드, False면 단타 모드
        """
        if regime == MarketRegime.BULLISH:
            return self._swing if is_swing else self._breakout

        if regime == MarketRegime.BEARISH:
            return self._hold_bearish
        if regime == MarketRegime.UNKNOWN:
            return self._hold_unknown
        return self._hold_sideways
