from __future__ import annotations

"""전략 인터페이스.

전략이 바뀌더라도 TradingService는 이 인터페이스만 알면 됩니다.
즉, 서비스는 구체 전략이 무엇인지 몰라도 됩니다.
"""

from abc import ABC, abstractmethod

from domain.models import MarketPrice, Position, Signal


class Strategy(ABC):
    """모든 전략이 따라야 하는 공통 규약입니다."""

    @abstractmethod
    def generate_signal(self, market_price: MarketPrice, position: Position | None) -> Signal:
        """현재 시세와 보유 포지션을 보고 BUY/SELL/HOLD를 결정합니다."""

        raise NotImplementedError
