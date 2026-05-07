from __future__ import annotations

"""전략 라우터 (Strategy Router).

장세 분류 결과(MarketRegime)를 받아서 알맞은 전략 객체를 반환합니다.

나중에 전략을 추가/변경하려면 이 파일의 select() 메서드만 수정하면 됩니다.
TradingService나 다른 코드는 건드릴 필요가 없습니다.

현재 라우팅 규칙:
    BULLISH  → BreakoutStrategy (기준가 돌파 + 익절/손절)
    SIDEWAYS → HoldStrategy     (신규 매수 없음, 보유분만 관리)
    BEARISH  → HoldStrategy     (동일, 로그에만 '하락장' 표시)
    UNKNOWN  → HoldStrategy     (데이터 부족 → 보수적으로 관망)
"""

from config.settings import StrategyConfig
from domain.models import MarketRegime
from domain.strategy.base import Strategy
from domain.strategy.breakout_strategy import BreakoutStrategy
from domain.strategy.hold_strategy import HoldStrategy


class StrategyRouter:
    """장세에 맞는 전략을 골라주는 라우터입니다."""

    def __init__(self, config: StrategyConfig) -> None:
        # 전략 객체를 미리 만들어두고 재사용합니다 (매번 생성하지 않음)
        self._breakout = BreakoutStrategy(config)
        self._hold_sideways = HoldStrategy(config, regime_label="횡보장")
        self._hold_bearish  = HoldStrategy(config, regime_label="하락장")
        self._hold_unknown  = HoldStrategy(config, regime_label="장세불명")

    def select(self, regime: MarketRegime) -> Strategy:
        """장세 분류 결과에 맞는 전략 객체를 반환합니다."""

        if regime == MarketRegime.BULLISH:
            return self._breakout
        if regime == MarketRegime.BEARISH:
            return self._hold_bearish
        if regime == MarketRegime.UNKNOWN:
            return self._hold_unknown
        # SIDEWAYS (및 그 외 모든 경우)
        return self._hold_sideways
