from __future__ import annotations

"""전략 라우터.

장세(MarketRegime)에 따라 전략 객체를 반환합니다.

BULLISH  → BreakoutStrategy (추격 매수)
REBOUND  → BottomStrategy   (바닥 매수)
그 외    → HoldStrategy     (완전 관망)
"""

from config.settings import StrategyConfig
from domain.models import MarketRegime
from domain.strategy.base import Strategy
from domain.strategy.breakout_strategy import BreakoutStrategy
from domain.strategy.bottom_strategy import BottomStrategy
from domain.strategy.hold_strategy import HoldStrategy


class StrategyRouter:
    def __init__(self, config: StrategyConfig) -> None:
        self._breakout      = BreakoutStrategy(config)
        self._bottom        = BottomStrategy(config)
        self._hold_sideways = HoldStrategy(config, regime_label="횡보장")
        self._hold_bearish  = HoldStrategy(config, regime_label="하락장")
        self._hold_unknown  = HoldStrategy(config, regime_label="장세불명")

    def select(self, regime: MarketRegime) -> Strategy:
        """장세에 맞는 전략 객체를 반환합니다."""
        if regime == MarketRegime.BULLISH:
            return self._breakout
        if regime == MarketRegime.REBOUND:
            return self._bottom
        if regime == MarketRegime.BEARISH:
            return self._hold_bearish
        if regime == MarketRegime.UNKNOWN:
            return self._hold_unknown
        return self._hold_sideways
