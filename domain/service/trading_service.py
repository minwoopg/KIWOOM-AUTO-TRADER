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

import asyncio
import time
from datetime import datetime
from pathlib import Path

from config.settings import Settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalyzer, MinuteAnalysis
from domain.models import AccountBalance, MarketRegime, OrderRequest, OrderSide, Signal, SignalType
from domain.risk.risk_manager import RiskManager
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.base import Broker
from infra.storage.daily_reporter import DailyReporter
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

        # 일봉 히스토리 캐시
        self.cached_daily_bars: dict[str, list] = {}
        self.cached_daily_bars_loaded_at: dict[str, datetime] = {}

        # 주봉 히스토리 캐시 (스윙 전략용)
        self.cached_weekly_bars: dict[str, list] = {}
        self.cached_weekly_bars_loaded_at: dict[str, datetime] = {}

        # 분봉 캐시 (단타 2차 필터용)
        self.cached_minute_bars: dict[str, list] = {}
        self.cached_minute_bars_loaded_at: dict[str, datetime] = {}

        # 장세 분류 결과 캐시
        self.cached_regime: dict[str, MarketRegime] = {}

        # HOLD 로그 throttle
        self.last_hold_log_at_by_symbol: dict[str, datetime] = {}

        # 동적 종목 목록 (조건검색 연동 시 갱신)
        self._dynamic_day_symbols: list[str] | None = None
        self._dynamic_swing_symbols: list[str] | None = None

        # 일일 리포트 생성기
        self._reporter = DailyReporter(
            trade_log_file=settings.storage.trade_log_file,
            report_dir=str(Path(settings.storage.trade_log_file).parent),
        )
        cfg = settings.market_regime
        self._minute_analyzer = MinuteAnalyzer(
            min_trading_value=cfg.min_trading_value,
            pullback_min_pct=cfg.pullback_min_pct,
            pullback_max_pct=cfg.pullback_max_pct,
            change_rate_min=cfg.change_rate_min,
            change_rate_max=cfg.change_rate_max,
            rebound_min_pct=cfg.rebound_min_pct,
        )
        # 장세 판단 요약 (리포트용)
        self._regime_summary: dict[str, str] = {}
        # 장 마감 리포트가 이미 생성됐는지 여부 (중복 방지)
        self._report_generated_today: bool = False

        # UNKNOWN 연속 횟수 카운터 (비정상 종목 자동 제외용)
        self._unknown_count: dict[str, int] = {}
        # 자동 제외된 종목 목록
        self._excluded_symbols: set[str] = set()

    @property
    def targets(self) -> list[str]:
        """현재 감시 중인 전체 종목 목록입니다."""
        day   = self._dynamic_day_symbols   if self._dynamic_day_symbols   is not None else self.settings.day_symbols
        swing = self._dynamic_swing_symbols if self._dynamic_swing_symbols is not None else self.settings.swing_symbols
        return list(dict.fromkeys(day + swing))

    def update_targets(self, day_symbols: list[str], swing_symbols: list[str] | None = None) -> None:
        """조건검색 결과로 단타/스윙 종목 목록을 동적으로 갱신합니다."""
        self._dynamic_day_symbols = day_symbols
        if swing_symbols is not None:
            self._dynamic_swing_symbols = swing_symbols

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
                # 리포트용 장세 요약 갱신
                self._regime_summary[symbol] = f"{regime.value} ({reason})"
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

    def _get_minute_analysis(self, symbol: str, prev_close: int) -> MinuteAnalysis | None:
        """분봉 데이터를 가져와 2차 필터 분석 결과를 반환합니다. 결과는 캐시합니다."""
        now = datetime.now()
        loaded_at = self.cached_minute_bars_loaded_at.get(symbol)
        refresh_sec = self.settings.market_regime.minute_refresh_seconds

        need_refresh = (
            loaded_at is None
            or (now - loaded_at).total_seconds() >= refresh_sec
        )

        if need_refresh:
            try:
                cfg = self.settings.market_regime
                bars = self.broker.get_minute_bars(
                    symbol,
                    tick_scope=cfg.minute_tick_scope,
                    count=cfg.minute_bar_count,
                )
                self.cached_minute_bars[symbol] = bars
                self.cached_minute_bars_loaded_at[symbol] = now
            except Exception as exc:
                self.app_logger.warning(f"[MIN] {symbol} | 분봉 조회 실패: {exc}")
                bars = self.cached_minute_bars.get(symbol, [])
        else:
            bars = self.cached_minute_bars.get(symbol, [])

        if not bars:
            return None

        return self._minute_analyzer.analyze(bars, prev_close)

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
        rsi_direction = self.regime_classifier._calc_rsi_direction(closes, cfg.rsi_period)
        macd_line, signal_line = self.regime_classifier._calc_macd(
            closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
        )
        macd_hist_direction = self.regime_classifier._calc_macd_hist_direction(
            closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
        )
        volume_surge = self.regime_classifier._is_volume_surge(volumes, cfg.volume_surge_ratio)
        ma5 = self.regime_classifier._calc_ma(closes, 5)
        price_above_ma5 = closes[-1] > ma5

        return MarketPrice(
            symbol=market_price.symbol,
            current_price=market_price.current_price,
            reference_price=market_price.reference_price,
            previous_close=market_price.previous_close,
            timestamp=market_price.timestamp,
            indicator_rsi=rsi,
            indicator_rsi_direction=rsi_direction,
            indicator_macd=macd_line,
            indicator_macd_signal=signal_line,
            indicator_macd_hist_direction=macd_hist_direction,
            indicator_volume_surge=volume_surge,
            indicator_price_above_ma5=price_above_ma5,
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

    async def run_once(self) -> None:
        """자동매매 루프를 한 번 실행합니다."""
        balance = self._get_balance_with_cache()

        for i, symbol in enumerate(self.targets):

            if symbol in self._excluded_symbols:
                continue

            if i > 0:
                await asyncio.sleep(1.0)  # asyncio 환경에서 안전한 딜레이

            # 스윙 종목 여부 확인
            is_swing = symbol in self.settings.swing_symbols

            regime, _ = self._get_regime_with_cache(symbol)

            if regime == MarketRegime.UNKNOWN:
                self._unknown_count[symbol] = self._unknown_count.get(symbol, 0) + 1
                if self._unknown_count[symbol] >= 3:
                    self._excluded_symbols.add(symbol)
                    self.app_logger.warning(
                        f"[EXCL] {symbol} | UNKNOWN 3회 연속 — 감시 대상에서 제외합니다"
                    )
                    continue
            else:
                self._unknown_count[symbol] = 0

            market_price = self._get_market_price_with_cache(symbol)
            market_price = self._attach_indicators(market_price, symbol)

            # ── 단타 종목 + BULLISH일 때만 분봉 2차 필터 적용 ──────
            minute_analysis = None
            if not is_swing and regime == MarketRegime.BULLISH:
                minute_analysis = self._get_minute_analysis(
                    symbol, market_price.previous_close
                )
                if minute_analysis:
                    self.app_logger.info(
                        f"[MIN ] {symbol} | {minute_analysis.score()}/5 | {minute_analysis.summary()}"
                    )

            # 스윙 종목은 is_swing=True로 SwingStrategy 선택
            strategy = self.strategy_router.select(regime, is_swing=is_swing)
            position = next((p for p in balance.positions if p.symbol == symbol), None)
            signal   = strategy.generate_signal(market_price, position, minute_analysis)

            self._log_signal_decision(symbol, signal, market_price.current_price, regime)

            if signal.type == SignalType.BUY:
                self._try_buy(symbol, market_price.current_price, balance, is_swing=is_swing)
            elif signal.type == SignalType.SELL and position is not None:
                self._try_sell(symbol, position.quantity)

        if is_near_market_close(self.settings.trading.force_exit_before_market_close_minutes):
            self.app_logger.info("near market close: force exit started")

            latest_balance = self.broker.get_account_balance()
            self.cached_balance = latest_balance
            self.cached_balance_loaded_at = datetime.now()

            for position in latest_balance.positions:
                # 스윙 종목은 오버나이트 허용 — 강제청산 제외
                if position.symbol in self.settings.swing_symbols:
                    self.app_logger.info(
                        f"[SWING] {position.symbol} | 스윙 종목 — 장 마감 강제청산 제외"
                    )
                    continue
                self._try_sell(position.symbol, position.quantity)

            # 장 마감 리포트 생성 (하루 1회)
            if not self._report_generated_today:
                self._generate_daily_report()
                self._report_generated_today = True

        self.state_store.save(self.state)

    def _try_buy(self, symbol: str, current_price: int, balance: AccountBalance, is_swing: bool = False) -> None:
        """매수 주문 가능 여부를 검사한 뒤 실제 주문을 시도합니다."""

        # ── 재진입 쿨다운 체크 ────────────────────────────────────
        cooldown_sec = self.settings.trading.reentry_cooldown_seconds
        last_sold_str = self.state.last_sold_at_by_symbol.get(symbol)
        if last_sold_str:
            last_sold_at = datetime.fromisoformat(last_sold_str)
            elapsed = (datetime.now() - last_sold_at).total_seconds()
            remaining = int(cooldown_sec - elapsed)
            if elapsed < cooldown_sec:
                self.app_logger.info(
                    f"[COOL ] {symbol} | 매도 후 재진입 쿨다운 중 "
                    f"({remaining}초 남음 / 총 {cooldown_sec}초)"
                )
                return

        # ── 단타/스윙 포지션 수 체크 ─────────────────────────────
        mode_label = "스윙" if is_swing else "단타"
        held_symbols = {p.symbol for p in balance.positions}
        if is_swing:
            swing_held = len([s for s in held_symbols if s in self.settings.swing_symbols])
            if swing_held >= self.settings.trading.max_swing_positions:
                self.app_logger.info(
                    f"[BLOCK] {symbol} | [{mode_label}] 최대 보유 종목 수 초과 "
                    f"({swing_held}/{self.settings.trading.max_swing_positions})"
                )
                return
        else:
            day_held = len([s for s in held_symbols if s in self.settings.day_symbols or
                           s not in self.settings.swing_symbols])
            if day_held >= self.settings.trading.max_day_positions:
                self.app_logger.info(
                    f"[BLOCK] {symbol} | [{mode_label}] 최대 보유 종목 수 초과 "
                    f"({day_held}/{self.settings.trading.max_day_positions})"
                )
                return
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

            # 매도 시각 기록 → 재진입 쿨다운에 사용
            self.state.last_sold_at_by_symbol[symbol] = datetime.now().isoformat()

            self.app_logger.info(
                f"[ORDER] {symbol} | 매도 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매도 주문 실패 | 사유: {result.message}"
            )

    def _generate_daily_report(self) -> None:
        """장 마감 후 일일 리포트를 생성하고 로그에 출력합니다."""
        try:
            report = self._reporter.generate(regime_summary=self._regime_summary)
            self.app_logger.info("=" * 45)
            for line in report.splitlines():
                self.app_logger.info(line)
            self.app_logger.info("=" * 45)
        except Exception as exc:
            self.app_logger.warning(f"[REPORT] 리포트 생성 실패: {exc}")

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