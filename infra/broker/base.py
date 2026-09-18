from __future__ import annotations

"""브로커 인터페이스.

증권사가 바뀌어도 TradingService는 이 인터페이스만 믿고 동작합니다.
예를 들어 KiwoomBroker 대신 미래에 KisBroker, AlpacaBroker를 붙여도 됩니다.
"""

from abc import ABC, abstractmethod

from domain.models import (
    AccountBalance, BrokerOrder, MarketPrice, OrderRequest, OrderResult,
    OrderStatusEvidence, PriceBar, WeeklyBar, MinuteBar,
)


class Broker(ABC):
    """증권사 연동 클래스가 반드시 구현해야 하는 공통 인터페이스입니다."""

    @abstractmethod
    def authenticate(self) -> None:
        """브로커 인증을 수행합니다."""

        raise NotImplementedError

    @abstractmethod
    def get_market_price(self, symbol: str) -> MarketPrice:
        """한 종목의 현재가를 조회합니다."""

        raise NotImplementedError

    @abstractmethod
    def get_account_balance(self) -> AccountBalance:
        """계좌 현금과 보유 종목을 조회합니다."""

        raise NotImplementedError

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:
        """주문을 브로커에 전달합니다."""

        raise NotImplementedError

    def get_order_status(self, order_id: str, symbol: str) -> BrokerOrder:
        """Read execution state. Unsupported brokers must not imply a fill."""
        raise NotImplementedError

    def get_open_orders(self, symbol: str) -> list[BrokerOrder]:
        """Read outstanding orders. Unsupported is different from empty."""
        raise NotImplementedError

    def get_order_status_evidence(self, order_id: str, symbol: str) -> OrderStatusEvidence:
        """`get_order_status()`와 같은 판정을 반환하되, 원본 매칭 행
        전체를 함께 담습니다(우선순위1 1차: 체결조회 증거 독립 저장).

        2026-09-18: 기본 구현은 `get_order_status()`를 그대로 호출해
        `OrderStatusEvidence(broker_order=..., matched_cntr_entries=[],
        matched_oso_entries=[])`로 감싸기만 합니다 — 즉 이 메서드를
        오버라이드하지 않는 모든 `Broker` 구현(`MockBroker`, 테스트용
        브로커 포함)은 **추가 작업 없이 자동으로 이 메서드를 지원**하며,
        기존 `get_order_status()`가 예외를 던지면 이 기본 구현도
        동일하게 그 예외를 그대로 전파합니다(API 오류 처리 방식은
        전혀 바뀌지 않음). 원본 매칭 행 전체를 실제로 보존하려면
        `KiwoomBroker`처럼 이 메서드를 오버라이드해야 합니다.
        """

        return OrderStatusEvidence(broker_order=self.get_order_status(order_id, symbol))

    @abstractmethod
    def get_daily_prices(self, symbol: str, days: int) -> list[PriceBar]:
        """한 종목의 일봉 히스토리를 조회합니다."""
        raise NotImplementedError

    @abstractmethod
    def get_weekly_prices(self, symbol: str, weeks: int) -> list[WeeklyBar]:
        """한 종목의 주봉 히스토리를 조회합니다."""
        raise NotImplementedError

    @abstractmethod
    def get_minute_bars(self, symbol: str, tick_scope: int = 3, count: int = 40) -> list[MinuteBar]:
        """한 종목의 분봉 데이터를 조회합니다.

        Parameters
        ----------
        symbol     : 종목코드
        tick_scope : 분봉 단위 (1/3/5/10/15/30/45/60)
        count      : 가져올 봉 수 (최신 기준)

        Returns
        -------
        list[MinuteBar]
            오래된 시간이 앞(index 0), 최신이 뒤(index -1)로 정렬됩니다.
        """
        raise NotImplementedError
