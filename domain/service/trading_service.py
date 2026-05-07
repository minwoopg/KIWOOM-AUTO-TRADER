from __future__ import annotations

"""자동매매의 중심 흐름을 담당하는 서비스.

이 서비스가 하는 일은 다음과 같습니다.
1. 계좌 정보를 읽는다.
2. 관심 종목 시세를 읽는다.
3. 전략 판단을 수행한다.
4. 리스크 검사를 한다.
5. 주문을 넣는다.
6. 로그와 상태를 저장한다.

이번 버전에서는 API 호출 수를 줄이기 위해
계좌 정보와 현재가를 일정 시간 동안 캐시해서 재사용합니다.
"""

import time
from datetime import datetime

from config.settings import Settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, MarketRegime, OrderRequest, OrderSide, Signal, SignalType
from domain.risk.risk_manager import RiskManager
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.base import Broker
from infra.storage.logger import AppLogger, TradeCsvLogger
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_near_market_close


class TradingService:
    """1회 실행 사이클(run_once)을 담당하는 애플리케이션 서비스입니다."""

    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        strategy_router: StrategyRouter,
        regime_classifier: MarketRegimeClassifier,
        risk_manager: RiskManager,
        app_logger: AppLogger,
        trade_logger: TradeCsvLogger,
        state_store: JsonStateStore,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.strategy_router = strategy_router
        self.regime_classifier = regime_classifier
        self.risk_manager = risk_manager
        self.app_logger = app_logger
        self.trade_logger = trade_logger
        self.state_store = state_store

        self.state = self.state_store.load()

        # 계좌/현재가 캐시
        self.cached_balance: AccountBalance | None = None
        self.cached_balance_loaded_at: datetime | None = None
        self.cached_market_prices: dict[str, object] = {}
        self.cached_market_price_loaded_at: dict[str, datetime] = {}

        # 일봉 히스토리 캐시 (종목별, 하루 1~2회 갱신)
        self.cached_daily_bars: dict[str, list] = {}
        self.cached_daily_bars_loaded_at: dict[str, datetime] = {}

        # 장세 분류 결과 캐시 (일봉과 같은 주기로 갱신)
        self.cached_regime: dict[str, MarketRegime] = {}

        # HOLD 로그 throttle
        self.last_hold_log_at_by_symbol: dict[str, datetime] = {}

    def _get_balance_with_cache(self) -> AccountBalance:
        """계좌 조회를 매번 하지 않고 일정 시간 동안 캐시를 재사용합니다."""
        now = datetime.now()

        if self.cached_balance is None or self.cached_balance_loaded_at is None:
            balance = self.broker.get_account_balance()
            self.cached_balance = balance
            self.cached_balance_loaded_at = now

            self.app_logger.info(
                "account balance loaded from api",
                extra={"cash": balance.cash, "positions": len(balance.positions)},
            )
            return balance

        elapsed = (now - self.cached_balance_loaded_at).total_seconds()

        if elapsed >= self.settings.trading.balance_refresh_seconds:
            balance = self.broker.get_account_balance()
            self.cached_balance = balance
            self.cached_balance_loaded_at = now

            self.app_logger.info(
                "account balance loaded from api",
                extra={"cash": balance.cash, "positions": len(balance.positions)},
            )
            return balance

        self.app_logger.info(
            "account balance loaded from cache",
            extra={"cash": self.cached_balance.cash, "positions": len(self.cached_balance.positions)},
        )
        return self.cached_balance

    def _get_market_price_with_cache(self, symbol: str):
        """현재가 조회를 종목별로 일정 시간 동안 캐시 재사용하도록 처리합니다.

        추가 개선:
        - API 호출 실패 시, 기존 캐시가 있으면 그 값을 그대로 사용합니다.
        - 기존 캐시도 없을 때만 예외를 다시 올립니다.
        """
        now = datetime.now()

        cached_price = self.cached_market_prices.get(symbol)
        cached_loaded_at = self.cached_market_price_loaded_at.get(symbol)

        # 아직 한 번도 조회하지 않았다면 즉시 API 호출
        if cached_price is None or cached_loaded_at is None:
            try:
                market_price = self.broker.get_market_price(symbol)
                self.cached_market_prices[symbol] = market_price
                self.cached_market_price_loaded_at[symbol] = now

                self.app_logger.info(
                    "market price loaded from api",
                    extra={"symbol": symbol, "current_price": market_price.current_price},
                )
                return market_price
            except Exception as exc:
                self.app_logger.warning(
                    f"[WARN ] {symbol} | 현재가 조회에 실패했고 사용할 캐시도 없습니다. 사유: {exc}"
                )
                raise

        elapsed = (now - cached_loaded_at).total_seconds()

        # 설정한 주기 이상 지났으면 다시 API 호출 시도
        if elapsed >= self.settings.trading.price_refresh_seconds:
            try:
                market_price = self.broker.get_market_price(symbol)
                self.cached_market_prices[symbol] = market_price
                self.cached_market_price_loaded_at[symbol] = now

                self.app_logger.info(
                    "market price loaded from api",
                    extra={"symbol": symbol, "current_price": market_price.current_price},
                )
                return market_price
            except Exception as exc:  
                # API 실패 시 기존 캐시를 유지하고 판단은 계속 진행
                self.app_logger.warning(
                    f"[WARN ] {symbol} | 현재가 재조회에 실패하여 직전 캐시값을 사용합니다. 사유: {exc}"
                )
                return cached_price

        # 아직 주기가 안 지났으면 캐시값 재사용
        self.app_logger.info(
            "market price loaded from cache",
            extra={"symbol": symbol, "current_price": cached_price.current_price},
        )
        return cached_price

    def _get_regime_with_cache(self, symbol: str) -> tuple[MarketRegime, str]:
        """일봉 히스토리를 가져와 장세를 분류합니다. 결과는 캐시합니다.

        일봉 데이터는 자주 바뀌지 않으므로 history_refresh_seconds 주기로만 갱신합니다.
        기본값 3600초(1시간)로 설정되어 있어 429 부담이 거의 없습니다.
        """
        now = datetime.now()
        loaded_at = self.cached_daily_bars_loaded_at.get(symbol)
        refresh_sec = self.settings.market_regime.history_refresh_seconds

        need_refresh = (
            loaded_at is None
            or (now - loaded_at).total_seconds() >= refresh_sec
        )

        if need_refresh:
            try:
                bars = self.broker.get_daily_prices(
                    symbol, self.settings.market_regime.history_days
                )
                self.cached_daily_bars[symbol] = bars
                self.cached_daily_bars_loaded_at[symbol] = now

                regime, reason = self.regime_classifier.classify(bars)
                self.cached_regime[symbol] = regime

                self.app_logger.info(
                    f"[REGIME] {symbol} | {regime.value} | {reason}"
                )
                return regime, reason

            except Exception as exc:
                self.app_logger.warning(
                    f"[REGIME] {symbol} | 일봉 조회 실패({type(exc).__name__}: {exc}) — "
                    f"{'직전 캐시 유지' if symbol in self.cached_regime else 'UNKNOWN으로 처리'}"
                )
                cached = self.cached_regime.get(symbol, MarketRegime.UNKNOWN)
                return cached, "일봉 조회 실패 — 직전 장세 판단 유지"

        # 캐시 재사용
        cached = self.cached_regime.get(symbol, MarketRegime.UNKNOWN)
        return cached, "(캐시)"

    def _attach_indicators(self, market_price, symbol: str):
        """캐시된 일봉 데이터로 지표값을 계산해 MarketPrice에 주입합니다.

        추가 API 호출 없이 이미 캐시된 일봉을 재사용합니다.
        일봉 캐시가 없으면 지표값 없이 그대로 반환합니다.
        """
        from domain.models import MarketPrice

        bars = self.cached_daily_bars.get(symbol)
        if not bars:
            return market_price

        closes  = [bar.close_price for bar in bars]
        volumes = [bar.volume for bar in bars]
        cfg = self.settings.market_regime

        rsi = self.regime_classifier._calc_rsi(closes, cfg.rsi_period)
        macd_line, signal_line = self.regime_classifier._calc_macd(
            closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
        )
        volume_surge = self.regime_classifier._is_volume_surge(volumes, cfg.volume_surge_ratio)

        return MarketPrice(
            symbol=market_price.symbol,
            current_price=market_price.current_price,
            reference_price=market_price.reference_price,
            previous_close=market_price.previous_close,
            timestamp=market_price.timestamp,
            indicator_rsi=rsi,
            indicator_macd=macd_line,
            indicator_macd_signal=signal_line,
            indicator_volume_surge=volume_surge,
        )

    def _log_signal_decision(self, symbol: str, signal: Signal, current_price: int, regime: MarketRegime) -> None:
        """전략 판단 결과를 사람이 읽기 쉬운 문장으로 로그에 남깁니다."""
        regime_tag = f"[{regime.value}]"

        if signal.type == SignalType.BUY:
            self.app_logger.info(
                f"[BUY ] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
            )
            return

        if signal.type == SignalType.SELL:
            self.app_logger.info(
                f"[SELL] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
            )
            return

        if signal.type == SignalType.HOLD:
            now = datetime.now()
            last_logged_at = self.last_hold_log_at_by_symbol.get(symbol)
            if last_logged_at is None or (now - last_logged_at).total_seconds() >= 30:
                self.app_logger.info(
                    f"[HOLD] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
                )
                self.last_hold_log_at_by_symbol[symbol] = now

    def run_once(self) -> None:
        """자동매매 루프를 한 번 실행합니다."""
        balance = self._get_balance_with_cache()

        for i, symbol in enumerate(self.settings.targets):

            # 두 번째 종목부터는 API 과호출 방지를 위해 잠깐 대기합니다.
            # 키움 ka10001/ka10086 연속 호출 시 429 방지용입니다.
            if i > 0:
                time.sleep(1.0)

            # 일봉(장세) → 현재가 순서로 호출합니다.
            # 일봉은 캐시가 있으면 API를 전혀 호출하지 않으므로
            # 첫 실행 이후에는 부담이 없습니다.
            regime, _ = self._get_regime_with_cache(symbol)
            market_price = self._get_market_price_with_cache(symbol)

            # 일봉에서 계산한 지표값을 MarketPrice에 주입합니다.
            # 전략이 RSI/MACD/거래량을 직접 참조할 수 있게 됩니다.
            market_price = self._attach_indicators(market_price, symbol)

            strategy = self.strategy_router.select(regime)
            position = next((p for p in balance.positions if p.symbol == symbol), None)
            signal = strategy.generate_signal(market_price, position)

            self._log_signal_decision(symbol, signal, market_price.current_price, regime)

            if signal.type == SignalType.BUY:
                self._try_buy(symbol, market_price.current_price, balance)
            elif signal.type == SignalType.SELL and position is not None:
                self._try_sell(symbol, position.quantity)

        if is_near_market_close(self.settings.trading.force_exit_before_market_close_minutes):
            self.app_logger.info("near market close: force exit started")

            latest_balance = self.broker.get_account_balance()
            self.cached_balance = latest_balance
            self.cached_balance_loaded_at = datetime.now()

            for position in latest_balance.positions:
                self._try_sell(position.symbol, position.quantity)

        self.state_store.save(self.state)

    def _try_buy(self, symbol: str, current_price: int, balance: AccountBalance) -> None:
        """매수 주문 가능 여부를 검사한 뒤 실제 주문을 시도합니다."""
        quantity = max(1, self.settings.trading.order_cash_per_trade // current_price)
        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            price=current_price,
        )

        can_order, reason = self.risk_manager.can_place_order(order, balance, self.state)

        if not can_order:
            self.app_logger.warning(
                f"[BLOCK] {symbol} | 매수 조건 충족했지만 주문 미실행 | 사유: {reason}"
            )
            return

        result = self.broker.place_order(order)

        self._write_trade_log(
            order.symbol,
            order.side.value,
            quantity,
            result.accepted,
            result.message,
            result.order_id,
        )

        if result.accepted:
            self.state.bought_symbols_today.add(symbol)
            self.state.last_order_id_by_symbol[symbol] = result.order_id

            # 주문 성공 후 잔고 캐시 무효화
            self.cached_balance = None
            self.cached_balance_loaded_at = None

            self.app_logger.info(
                f"[ORDER] {symbol} | 매수 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매수 주문 실패 | 사유: {result.message}"
            )

    def _try_sell(self, symbol: str, quantity: int) -> None:
        """매도 주문을 생성하고 브로커로 전달합니다."""
        order = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=quantity)
        result = self.broker.place_order(order)

        self._write_trade_log(
            order.symbol,
            order.side.value,
            quantity,
            result.accepted,
            result.message,
            result.order_id,
        )

        if result.accepted:
            # 주문 성공 후 잔고 캐시 무효화
            self.cached_balance = None
            self.cached_balance_loaded_at = None

            self.app_logger.info(
                f"[ORDER] {symbol} | 매도 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매도 주문 실패 | 사유: {result.message}"
            )

    def _write_trade_log(
        self,
        symbol: str,
        side: str,
        quantity: int,
        accepted: bool,
        message: str,
        order_id: str,
    ) -> None:
        """거래 로그 CSV에 한 줄을 추가합니다."""
        self.trade_logger.append(
            {
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "accepted": accepted,
                "message": message,
                "order_id": order_id,
            }
        )