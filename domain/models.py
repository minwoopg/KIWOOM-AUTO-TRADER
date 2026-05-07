from __future__ import annotations

"""도메인 모델 모음.

이 파일에는 자동매매 프로그램이 내부적으로 사용하는 표준 객체를 정의합니다.
증권사 응답(JSON)을 코드 전체에서 직접 쓰지 않고, 이런 내부 모델로 바꿔서 사용하면
브로커가 바뀌어도 다른 코드가 덜 흔들립니다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class SignalType(str, Enum):
    """전략이 낼 수 있는 최종 판단 결과입니다."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(str, Enum):
    """주문의 방향을 나타냅니다."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class MarketPrice:
    """현재 시세와 기준 가격 정보를 담는 객체입니다.

    indicator_* 필드는 일봉 기반 지표값으로,
    TradingService가 classify() 결과와 함께 채워서 전략에 전달합니다.
    값이 없을 경우 None으로 두면 전략에서 기본 동작(단순 돌파)을 유지합니다.
    """

    symbol: str
    current_price: int
    reference_price: int
    previous_close: int
    timestamp: datetime

    # 일봉 기반 보조 지표 (선택적)
    indicator_rsi: float | None = None
    indicator_macd: float | None = None        # MACD 라인값
    indicator_macd_signal: float | None = None # 시그널 라인값
    indicator_volume_surge: bool = False       # 거래량 급증 여부


@dataclass(frozen=True)
class Position:
    """한 종목의 보유 수량과 평균단가를 표현합니다."""

    symbol: str
    quantity: int
    average_price: int


@dataclass(frozen=True)
class AccountBalance:
    """계좌 현금과 보유 종목 목록을 함께 나타냅니다."""

    cash: int
    total_asset: int
    positions: list[Position]


@dataclass(frozen=True)
class OrderRequest:
    """브로커에 주문을 넣기 위한 내부 표준 요청 객체입니다."""

    symbol: str
    side: OrderSide
    quantity: int
    order_type: str = "market"
    price: Optional[int] = None


@dataclass(frozen=True)
class OrderResult:
    """브로커가 주문 요청에 응답한 결과를 표현합니다."""

    order_id: str
    symbol: str
    side: OrderSide
    requested_quantity: int
    accepted: bool
    message: str
    timestamp: datetime


@dataclass(frozen=True)
class Signal:
    """전략 판단의 결과와 이유를 함께 담습니다."""

    type: SignalType
    reason: str


@dataclass(frozen=True)
class PriceBar:
    """일봉 하나를 표현하는 객체입니다.

    장세 분류기(MarketRegimeClassifier)가 이동평균, RSI 등을
    계산할 때 사용합니다.
    """

    date: str           # 'YYYYMMDD'
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


class MarketRegime(str, Enum):
    """장세 분류 결과입니다.

    BULLISH  : 상승장 → 돌파 전략 실행
    SIDEWAYS : 횡보장 → 신규 매수 중단, 기존 보유만 관리
    BEARISH  : 하락장 → 신규 매수 중단, 손절 우선
    UNKNOWN  : 데이터 부족 등으로 판단 불가 → 보수적으로 횡보 취급
    """

    BULLISH = "BULLISH"
    SIDEWAYS = "SIDEWAYS"
    BEARISH = "BEARISH"
    UNKNOWN = "UNKNOWN"


@dataclass
class RuntimeState:
    """프로그램 실행 중 유지해야 하는 상태를 담습니다.

    여기의 상태는 DB 대신 JSON 파일로 저장됩니다.
    첫 버전에서는 이 정도만 있어도 충분합니다.
    """

    bought_symbols_today: set[str] = field(default_factory=set)
    last_order_id_by_symbol: dict[str, str] = field(default_factory=dict)
