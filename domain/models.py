from __future__ import annotations

"""도메인 모델 모음.

이 파일에는 자동매매 프로그램이 내부적으로 사용하는 표준 객체를 정의합니다.
증권사 응답(JSON)을 코드 전체에서 직접 쓰지 않고, 이런 내부 모델로 바꿔서 사용하면
브로커가 바뀌어도 다른 코드가 덜 흔들립니다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


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
    """현재 시세와 기준 가격 정보를 담는 객체입니다."""

    symbol: str
    current_price: int
    reference_price: int
    previous_close: int
    timestamp: datetime


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


@dataclass
class RuntimeState:
    """프로그램 실행 중 유지해야 하는 상태를 담습니다.

    여기의 상태는 DB 대신 JSON 파일로 저장됩니다.
    첫 버전에서는 이 정도만 있어도 충분합니다.
    """

    bought_symbols_today: set[str] = field(default_factory=set)
    last_order_id_by_symbol: dict[str, str] = field(default_factory=dict)
