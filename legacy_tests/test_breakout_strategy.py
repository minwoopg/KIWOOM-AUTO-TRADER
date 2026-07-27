from domain.models import MarketPrice, Position, SignalType
from domain.strategy.breakout_strategy import BreakoutStrategy
from config.settings import StrategyConfig
from datetime import datetime


class TestBreakoutStrategy:
    """돌파 전략의 핵심 동작을 점검하는 테스트입니다."""

    def _build_strategy(self) -> BreakoutStrategy:
        """테스트용 전략 객체를 생성합니다."""

        return BreakoutStrategy(
            StrategyConfig(
                name="breakout",
                breakout_threshold_pct=1.0,
                take_profit_pct=2.0,
                stop_loss_pct=1.5,
                reference_price_type="previous_close",
            )
        )

    def test_buy_signal_when_price_breaks_threshold(self) -> None:
        """기준가를 돌파하면 BUY가 나오는지 확인합니다."""

        strategy = self._build_strategy()
        market_price = MarketPrice(
            symbol="005930",
            current_price=1010,
            reference_price=1000,
            previous_close=1000,
            timestamp=datetime.now(),
        )

        signal = strategy.generate_signal(market_price, None)
        assert signal.type == SignalType.BUY

    def test_sell_signal_when_take_profit_is_reached(self) -> None:
        """익절 가격에 도달하면 SELL이 나오는지 확인합니다."""

        strategy = self._build_strategy()
        market_price = MarketPrice(
            symbol="005930",
            current_price=1030,
            reference_price=1000,
            previous_close=1000,
            timestamp=datetime.now(),
        )
        position = Position(symbol="005930", quantity=1, average_price=1000)

        signal = strategy.generate_signal(market_price, position)
        assert signal.type == SignalType.SELL
