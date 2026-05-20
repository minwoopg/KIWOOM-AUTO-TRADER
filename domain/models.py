from __future__ import annotations

"""도메인 모델 모음."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(str, Enum):
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

    # 일봉 기반 보조 지표 (선택적)
    indicator_rsi: float | None = None
    indicator_rsi_direction: int = 0            # +1 상승, -1 하락, 0 보합
    indicator_rsi_signal: float | None = None   # RSI의 9일 EMA (Signal선)
    indicator_rsi_signal_cross: int = 0         # +1 골든크로스, -1 데드크로스, 0 보합
    indicator_macd: float | None = None
    indicator_macd_signal: float | None = None
    indicator_macd_hist_direction: int = 0      # +1 확대, -1 축소, 0 보합
    indicator_volume_surge: bool = False
    indicator_price_above_ma5: bool = False
    indicator_volume_exhaustion: bool = False   # 매물 고갈: 거래량 < 평균 70%
    indicator_volume_buying: bool = False       # 세력 유입: 거래량 > 평균 130%


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_price: int


@dataclass(frozen=True)
class AccountBalance:
    cash: int
    total_asset: int
    positions: list[Position]


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    order_type: str = "market"
    price: Optional[int] = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: OrderSide
    requested_quantity: int
    accepted: bool
    message: str
    timestamp: datetime


@dataclass(frozen=True)
class Signal:
    type: SignalType
    reason: str


@dataclass(frozen=True)
class PriceBar:
    date: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


@dataclass(frozen=True)
class WeeklyBar:
    date: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


@dataclass(frozen=True)
class MinuteBar:
    cntr_tm: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    acc_volume: int


class MarketRegime(str, Enum):
    """장세 분류 결과입니다.

    BULLISH  : 상승장      → BreakoutStrategy (추격 매수)
    REBOUND  : 바닥권 반등 → BottomStrategy   (바닥 매수)  ← 신규
    SIDEWAYS : 횡보장      → HoldStrategy     (완전 관망)
    BEARISH  : 하락장      → HoldStrategy     (완전 관망)
    UNKNOWN  : 데이터 부족 → HoldStrategy     (보수적 관망)
    """

    BULLISH  = "BULLISH"
    REBOUND  = "REBOUND"
    SIDEWAYS = "SIDEWAYS"
    BEARISH  = "BEARISH"
    UNKNOWN  = "UNKNOWN"


@dataclass
class RuntimeState:
    bought_symbols_today: set[str] = field(default_factory=set)
    last_order_id_by_symbol: dict[str, str] = field(default_factory=dict)
    last_sold_at_by_symbol: dict[str, str] = field(default_factory=dict)
