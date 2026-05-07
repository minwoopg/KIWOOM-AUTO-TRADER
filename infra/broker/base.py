from __future__ import annotations

"""브로커 인터페이스.

증권사가 바뀌어도 TradingService는 이 인터페이스만 믿고 동작합니다.
예를 들어 KiwoomBroker 대신 미래에 KisBroker, AlpacaBroker를 붙여도 됩니다.
"""

from abc import ABC, abstractmethod

from domain.models import AccountBalance, MarketPrice, OrderRequest, OrderResult


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

    @abstractmethod
    def get_daily_prices(self, symbol: str, days: int) -> list[PriceBar]:
        """한 종목의 일봉 히스토리를 조회합니다.

        Parameters
        ----------
        symbol : 종목코드 (예: '005930')
        days   : 가져올 일봉 수 (최신 기준 최근 N일)

        Returns
        -------
        list[PriceBar]
            오래된 날짜가 앞(index 0), 최신이 뒤(index -1)로 정렬됩니다.
        """

        raise NotImplementedError
