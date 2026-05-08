from __future__ import annotations

"""전략 인터페이스."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from domain.models import MarketPrice, Position, Signal

if TYPE_CHECKING:
    from domain.market_regime.minute_analyzer import MinuteAnalysis


class Strategy(ABC):
    """모든 전략이 따라야 하는 공통 규약입니다."""

    @abstractmethod
    def generate_signal(
        self,
        market_price: MarketPrice,
        position: Position | None,
        minute_analysis=None,
    ) -> Signal:
        """현재 시세와 보유 포지션을 보고 BUY/SELL/HOLD를 결정합니다.

        Parameters
        ----------
        market_price     : 현재가 및 일봉 기반 지표
        position         : 보유 포지션 (미보유 시 None)
        minute_analysis  : 분봉 분석 결과 (단타 전용, 없으면 None)
        """
        raise NotImplementedError
