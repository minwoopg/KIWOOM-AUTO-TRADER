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
    indicator_rsi_direction: int = 0           # +1 상승, -1 하락, 0 보합
    indicator_macd: float | None = None
    indicator_macd_signal: float | None = None
    indicator_macd_hist_direction: int = 0     # +1 히스토그램 확대, -1 축소, 0 보합
    indicator_volume_surge: bool = False
    indicator_price_above_ma5: bool = False    # 현재가 > 5일 이동평균


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
    # 2026-08-14 (1P0.8-P0.1, 319400 실측 P0 사고 대응): place_order()가
    # 브로커 응답 자체를 받지 못한 경우(타임아웃/connection 오류 등)
    # True. 이때 accepted는 항상 False이지만, "서버가 명시적으로
    # 거부함"(예: HTTP 429)과는 성격이 다릅니다 — 실제로는 주문이
    # 브로커에 접수됐을 수도 있으므로, 호출부가 즉시 이전 상태로
    # 롤백하고 재시도를 허용하면 중복 주문 사고로 이어질 위험이
    # 있습니다. 기본값 False는 기존 accepted=True/False 두 값만
    # 쓰던 모든 브로커 구현체(MockBroker 등)와 100% 하위호환됩니다.
    is_ambiguous: bool = False


class BrokerOrderStatus(str, Enum):
    """ka10075(미체결조회)/ka10076(체결조회)를 종합해 판정한, 한
    주문의 read-only 상태입니다.

    2026-08-14/08-18 (1P0.8-B.1 실측, 1P0.8-B.2 모델 설계, 08-18 GPT
    리뷰 반영 강화): 이 값은 "실측으로 확인된 시그니처"만 반영합니다
    — 추측으로 상태를 넓히지 않습니다(프로젝트 전반의 fail-closed
    원칙과 동일). `ord_stt` 문자열 하나만으로 판정하지 않고, 실측
    fixture에서 함께 관찰된 수량 조합까지 정확히 일치해야만
    OPEN/FILLED를 반환합니다 — 그래야 미실측 부분체결이 `ord_stt`만
    같다는 이유로 잘못 OPEN/FILLED로 새는 것을 막을 수 있습니다.

    - OPEN   : ka10075 `oso`에 있고 `ord_stt == "접수"` **그리고**
      `cntr_qty == 0` **그리고** `oso_qty == ord_qty`(세 조건 모두
      실측 signature와 정확히 일치)이며 ka10076 `cntr`엔 전혀 없음.
      8/18 실측(005930/13557, 지정가 미체결: ord_qty=1/oso_qty=1/
      cntr_qty=0)으로 확인. 이 수량 조합이 아니면(예: `ord_stt`는
      "접수"지만 `cntr_qty > 0`인 경우) UNKNOWN.
    - FILLED : ka10076 `cntr`에 해당 주문번호가 있고 `ord_stt ==
      "체결"` **그리고** `ord_qty == cntr_qty` **그리고**
      `oso_qty == 0`(세 조건 모두 실측 signature와 정확히 일치).
      8/14 실측(005930/157897, 009150/163276, 전량체결: 각각
      ord_qty==cntr_qty, oso_qty=0)으로 확인. 이 수량 조합이
      아니면(예: `ord_stt`는 "체결"이지만 `cntr_qty < ord_qty`인
      경우) UNKNOWN.
    - UNKNOWN: 위 두 경우에 해당하지 않는 전부 — (a) `oso`/`cntr`
      둘 다에 흔적이 없음(8/18 실측: 취소 후 시그니처. 단, "취소됨"
      하나만을 의미하지 않음 — 오래된 체결이 조회 범위 밖으로
      벗어났거나 애초에 잘못된 order_id일 수도 있음. 이 상태에서
      "취소 확정"으로 해석하려면 클라이언트가 보유한 "이 order_id로
      주문을 냈다"는 사전 지식과 결합해야 함 — CHANGELOG의 "1P0.8-B.1
      실측 3차" 참고), (b) `oso`/`cntr`에는 매칭되지만 `ord_stt`나
      수량 조합이 위 실측 signature와 다른 값(부분체결/정정 등으로
      추정되나 아직 실측 확인된 바 없어 의도적으로 fail-close),
      (c) order_id 자체가 빈 값/전부 0이라 애초에 매칭을 시도하지
      않은 경우.
    """

    OPEN = "OPEN"
    FILLED = "FILLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BrokerOrder:
    """한 주문에 대해 "지금 다시 물어본" read-only 조회 스냅샷입니다.

    `OrderResult`(place_order() 호출 직후의 "제출 응답")와는 다른
    객체입니다 — 이건 이미 제출된 주문번호를 ka10075/ka10076으로
    나중에 다시 조회한 결과입니다. 1P0.8-B.2에서 신설, 아직
    `Broker` 인터페이스에는 연결되지 않았습니다(1P0.8-C에서
    `get_order_status()`/`get_open_orders()` 추가 예정 — 그 전까지는
    `infra/broker/kiwoom_order_status.py`의 순수 함수로만 존재).

    수량/가격 필드는 판정 불가능하거나 원본에 없으면 `None`입니다 —
    0으로 채우면 "실제로 0이었다"와 "몰라서 못 채웠다"를 구분할 수
    없어 의도적으로 `Optional`로 뒀습니다.
    """

    order_id: str
    symbol: str
    status: BrokerOrderStatus
    side: OrderSide | None = None
    # 2026-08-18 (GPT 리뷰 반영): requested_quantity/open_quantity/
    # filled_quantity는 oso/cntr 중 매칭된 항목의 원본 수량을 status와
    # 무관하게(UNKNOWN이어도) 그대로 담습니다 — 디버깅/로그 목적의
    # 참고값입니다. 오직 filled_price만 status가 실제로 FILLED로
    # "확정"됐을 때만 채웁니다(가격은 체결이 확정됐다는 의미를
    # 내포하므로 더 보수적으로 취급). status 필드 자체가 최종 판정을
    # 나타내므로, 호출부는 이 수치 필드들이 아니라 반드시 status를
    # 보고 분기해야 합니다.
    requested_quantity: int | None = None   # ord_qty
    open_quantity: int | None = None        # oso_qty (매칭된 항목의 원본값, status 무관)
    filled_quantity: int | None = None      # cntr_qty (매칭된 항목의 원본값, status 무관)
    filled_price: int | None = None         # cntr_pric — status가 FILLED로 확정됐을 때만 채워짐
    order_type_raw: str | None = None       # trde_tp 원문("보통"/"시장가" 등), 정규화하지 않고 그대로 보존
    # 디버깅/미확인 상태(UNKNOWN) 재현용 — 매칭된 원본 레코드를
    # 그대로 보존합니다(매칭 안 되면 둘 다 None).
    raw_oso_entry: dict | None = None
    raw_cntr_entry: dict | None = None


@dataclass(frozen=True)
class Signal:
    """전략 판단의 결과와 이유를 함께 담습니다."""

    type: SignalType
    reason: str
    # 2026-07-28 (6차 GPT 코드리뷰 지적 5번, "1B Safety Closure"):
    # 이 신호가 minute_analysis(분봉 지표: VWAP/MA5 등)를 실제로
    # 참조해서 나온 것인지 표시. True면 신선한(entry_safe=True)
    # 분봉 데이터가 있을 때만 이 신호를 신뢰할 수 있다는 뜻 —
    # stale 상태에서 지표 기반 SELL(추세 꺾임 등)을 억제하고, 가격
    # 기반 hard-risk SELL(고정 손절/트레일링/안전망 익절처럼
    # current_price·average_price·highest_price만으로 계산되는
    # 신호)은 계속 허용하기 위한 구분. 기본값 False — 기존 대부분의
    # 신호(가격 기반 또는 BUY/HOLD)는 이 구분과 무관.
    requires_fresh_minute_data: bool = False


@dataclass(frozen=True)
class PriceBar:
    """일봉 하나를 표현하는 객체입니다."""

    date: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


@dataclass(frozen=True)
class WeeklyBar:
    """주봉 하나를 표현하는 객체입니다."""

    date: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


@dataclass(frozen=True)
class MinuteBar:
    """분봉 하나를 표현하는 객체입니다.

    VWAP, 분봉 저점 상승, 눌림목 판단에 사용합니다.
    cntr_tm: 체결시간 'YYYYMMDDHHmmSS'
    """

    cntr_tm: str        # 체결시간
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    acc_volume: int     # 누적 거래량 (VWAP 계산용)


class MarketRegime(str, Enum):
    """장세 분류 결과입니다.

    BULLISH  : 강한 상승장 → BreakoutStrategy (A/B/C 매수 전부 허용)
    NEUTRAL  : 애매한 장세 → NeutralStrategy  (B반등 + C눌림목만 허용)
    REBOUND  : 바닥권 반등 → BottomStrategy   (바닥 매수)
    SIDEWAYS : 횡보장      → HoldStrategy     (완전 관망)
    BEARISH  : 하락장      → HoldStrategy     (완전 관망)
    UNKNOWN  : 데이터 부족 → HoldStrategy     (보수적 관망)
    """

    BULLISH  = "BULLISH"
    NEUTRAL  = "NEUTRAL"
    REBOUND  = "REBOUND"
    SIDEWAYS = "SIDEWAYS"
    BEARISH  = "BEARISH"
    UNKNOWN  = "UNKNOWN"


@dataclass
class RuntimeState:
    """프로그램 실행 중 유지해야 하는 상태를 담습니다.

    여기의 상태는 DB 대신 JSON 파일로 저장됩니다.
    첫 버전에서는 이 정도만 있어도 충분합니다.
    """

    bought_symbols_today: set[str] = field(default_factory=set)
    last_order_id_by_symbol: dict[str, str] = field(default_factory=dict)
    # 종목별 마지막 매도 시각 (핑퐁 차단용) — ISO 문자열로 저장
    last_sold_at_by_symbol: dict[str, str] = field(default_factory=dict)
    # 종목별 매수 진입 시각 (hold_minutes 계산용) — ISO 문자열로 저장
    entry_time_by_symbol: dict[str, str] = field(default_factory=dict)
    # 연속 손절 횟수 (일일 손실한도 연동)
    consecutive_losses: int = 0
    # 종목별 매수 후 최고가 추적 (entry watch용)
    peak_price_by_symbol: dict[str, int] = field(default_factory=dict)
    # 당일 종목별 손실 횟수 (재진입 제한용)
    symbol_loss_count_today: dict[str, int] = field(default_factory=dict)
    # 당일 종목별 매수 진입 횟수 (1일 1회 제한용)
    symbol_entry_count_today: dict[str, int] = field(default_factory=dict)
    # 종목별 손절 발생 시각 (ISO 문자열, 30분 금지용)
    symbol_stoploss_at: dict[str, str] = field(default_factory=dict)
    # 종목별 트레일링 손실 발생 시각 목록 (60분 금지용)
    symbol_trail_loss_at: dict[str, list] = field(default_factory=dict)
    # NEUTRAL 손절 발생 종목 — 당일 재진입 완전 금지
    symbol_block_today: set[str] = field(default_factory=set)
    # entry_watch VWAP 연속 이탈 카운터 (히스테리시스용, 2026-07-22)
    # 종목이 VWAP 위로 회복하면 이 값은 0으로 리셋됨
    vwap_break_streak_by_symbol: dict[str, int] = field(default_factory=dict)
