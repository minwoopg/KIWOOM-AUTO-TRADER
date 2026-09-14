from __future__ import annotations

"""전략 인터페이스."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from domain.models import MarketPrice, Position, Signal
from domain.strategy.exit_calc import TrailingParams

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
        highest_price: int = 0,
        **kwargs,
    ) -> Signal:
        """현재 시세와 보유 포지션을 보고 BUY/SELL/HOLD를 결정합니다.

        Parameters
        ----------
        market_price     : 현재가 및 일봉 기반 지표
        position         : 보유 포지션 (미보유 시 None)
        minute_analysis  : 분봉 분석 결과 (단타 전용, 없으면 None)
        highest_price    : 보유 중 기록한 최고가 (트레일링 스탑용)
        """
        raise NotImplementedError

    def trailing_params(self) -> TrailingParams | None:
        """이 전략이 트레일링 스탑에 실제로 쓰는 시작 배율·구간표.

        2026-09-14 (180초 감시 공백 대응 — 구현 지시서 §2.5, GPT
        재검토 반영): 잔고 장애 관측 경로(`exit_calc.
        evaluate_exit_candidate()`)가 `generate_signal()` 전체를
        부르지 않고도 정상 전략과 같은 트레일링 숫자를 쓸 수 있도록
        노출하는 훅입니다. 기본값은 `None`(트레일링 없음) —
        `HoldStrategy`처럼 트레일링을 쓰지 않는 전략은 오버라이드하지
        않아도 됩니다. 트레일링을 쓰는 전략은 이 메서드가 반환하는
        값을 `generate_signal()` 내부에서도 그대로 읽어써야 합니다
        (숫자를 두 곳에 따로 적지 않기 위함 — 각 구현체 참고).
        """
        return None
