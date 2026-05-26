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
from infra.storage.logger import AppLogger, TradeCsvLogger, SignalCsvLogger
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
        signal_logger: SignalCsvLogger,
        state_store: JsonStateStore,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.strategy_router = strategy_router
        self.regime_classifier = regime_classifier
        self.risk_manager = risk_manager
        self.app_logger = app_logger
        self.trade_logger = trade_logger
        self.signal_logger = signal_logger
        self.state_store = state_store

        self.state, loaded_highest = self.state_store.load()

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

        # 보유 종목별 최고가 추적 (트레일링 스탑용)
        self._highest_price: dict[str, int] = loaded_highest

        # 동적 종목 목록 (조건검색 연동 시 갱신)
        self._dynamic_targets: list[str] | None = None

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
            v_bottom_lookback=cfg.v_bottom_lookback,
            v_low_min_age=cfg.v_low_min_age,
            v_low_max_age=cfg.v_low_max_age,
            v_drop_threshold_pct=cfg.v_drop_threshold_pct,
            v_rebound_threshold_pct=cfg.v_rebound_threshold_pct,
            v_max_rebound_pct=cfg.v_max_rebound_pct,
            v_volume_ratio=cfg.v_volume_ratio,
            v_min_bar_amount=cfg.v_min_bar_amount,
            v_bottom_spike_ratio=cfg.v_bottom_spike_ratio,
            v_ma5_slope_bars=cfg.v_ma5_slope_bars,
        )
        # 장세 판단 요약 (리포트용)
        self._regime_summary: dict[str, str] = {}
        # 장 마감 리포트가 이미 생성됐는지 여부 (중복 방지)
        self._report_generated_today: bool = False

        # UNKNOWN 연속 횟수 카운터 (비정상 종목 자동 제외용)
        self._unknown_count: dict[str, int] = {}
        self._low_volume_count: dict[str, int] = {}   # 거래대금 부족 카운트
        # 자동 제외된 종목 목록
        self._excluded_symbols: set[str] = set()

    @property
    def targets(self) -> list[str]:
        """현재 감시 중인 전체 종목 목록입니다."""
        if self._dynamic_targets is not None:
            return self._dynamic_targets
        return self.settings.targets

    def update_targets(self, symbols: list[str]) -> None:
        """조건검색 결과로 종목 목록을 동적으로 갱신합니다."""
        self._dynamic_targets = symbols

    def get_excluded_symbols(self) -> set[str]:
        """자동 제외된 종목 목록을 반환합니다."""
        return self._excluded_symbols.copy()

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

        self.app_logger.debug(
            "account balance loaded from cache",
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
        self.app_logger.debug(
            "market price loaded from cache",
        )
        return cached_price

    def _get_regime_with_cache(self, symbol: str, current_price: int = 0, prev_close: int = 0) -> tuple[MarketRegime, str]:
        """일봉 히스토리를 가져와 장세를 분류합니다. 결과는 캐시합니다.

        일봉 데이터는 자주 바뀌지 않으므로 history_refresh_seconds 주기로만 갱신합니다.
        기본값 3600초(1시간)로 설정되어 있어 429 부담이 거의 없습니다.
        """
        # ── 당일 등락률 기반 장세 보정 ───────────────────────────
        if current_price > 0 and prev_close > 0:
            change_rate = (current_price - prev_close) / prev_close * 100
            if change_rate >= 2.0:
                reason = f"당일 급등 {change_rate:+.1f}% — BULLISH 강제 적용"
                self.app_logger.info(f"[REGIME] {symbol} | BULLISH | {reason}")
                self._regime_summary[symbol] = f"BULLISH ({reason})"
                return MarketRegime.BULLISH, reason
            elif change_rate <= -2.0:
                reason = f"당일 급락 {change_rate:+.1f}% — NEUTRAL 강제 적용"
                self.app_logger.info(f"[REGIME] {symbol} | NEUTRAL | {reason}")
                self._regime_summary[symbol] = f"NEUTRAL ({reason})"
                return MarketRegime.NEUTRAL, reason

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

    def _log_signal_decision(
        self,
        symbol: str,
        signal: Signal,
        current_price: int,
        regime: MarketRegime,
        position=None,
        minute_analysis=None,
    ) -> None:
        """전략 판단 결과를 로그에 남깁니다.

        로그 태그 종류:
            [BUY     ] 매수 신호
            [SELL    ] 매도 신호
            [HOLD_POS] 보유 중 유지 (익절/손절 진행상황 표시)
            [NEAR_TP ] 익절가 근접 경고
            [NEAR_SL ] 손절가 근접 경고
            [BLOCK   ] 분봉 필터 차단 (매수 시도했지만 차단)
            [HOLD    ] 일반 홀딩 (미보유, 장세/조건 미충족)
        """
        regime_tag = f"[{regime.value}]"
        now = datetime.now()

        # ── 매수 신호 ────────────────────────────────────────────
        if signal.type == SignalType.BUY:
            self.app_logger.info(
                f"[BUY     ] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
            )
            return

        # ── 매도 신호 ────────────────────────────────────────────
        if signal.type == SignalType.SELL:
            self.app_logger.info(
                f"[SELL    ] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
            )
            return

        # ── HOLD 처리 ─────────────────────────────────────────────
        if signal.type == SignalType.HOLD:

            # 보유 중인 종목 → HOLD_POS로 분리
            if position is not None:
                avg = position.average_price
                tp  = int(avg * (1 + self.settings.strategy.take_profit_pct / 100))
                sl  = int(avg * (1 - self.settings.strategy.stop_loss_pct / 100))
                pnl_pct = (current_price - avg) / avg * 100
                tp_remain = (tp - current_price) / current_price * 100
                sl_remain = (current_price - sl) / current_price * 100

                # 익절가 2% 이내 근접 경고
                if 0 < tp_remain <= 2.0:
                    self.app_logger.info(
                        f"[NEAR_TP ] {symbol} | 현재가 {current_price:,}원 | "
                        f"익절 {tp:,}원까지 +{tp_remain:.2f}% 남음 ⚠️"
                    )

                # 손절가 1% 이내 근접 경고
                if 0 < sl_remain <= 1.0:
                    self.app_logger.info(
                        f"[NEAR_SL ] {symbol} | 현재가 {current_price:,}원 | "
                        f"손절 {sl:,}원까지 -{sl_remain:.2f}% 남음 ⚠️"
                    )

                # 보유 종목 상태 로그 (30초마다)
                last_logged_at = self.last_hold_log_at_by_symbol.get(symbol)
                if last_logged_at is None or (now - last_logged_at).total_seconds() >= 30:
                    self.app_logger.info(
                        f"[HOLD_POS] {symbol} | 현재가 {current_price:,}원 ({pnl_pct:+.1f}%) | "
                        f"익절 {tp:,}원(+{tp_remain:.1f}% 남음) / 손절 {sl:,}원"
                    )
                    self.last_hold_log_at_by_symbol[symbol] = now
                return

            # 미보유 종목 — 분봉 필터 차단 여부 구분
            # signal.reason에 "진입 조건 미충족" 또는 "눌림목" 등 차단 사유가 있으면 BLOCK
            block_keywords = ["진입 조건 미충족", "눌림목 구간", "거래대금 부족"]
            is_block = any(kw in signal.reason for kw in block_keywords)

            if is_block and minute_analysis is not None:
                last_logged_at = self.last_hold_log_at_by_symbol.get(symbol)
                if last_logged_at is None or (now - last_logged_at).total_seconds() >= 30:
                    self.app_logger.info(
                        f"[BLOCK   ] {regime_tag} {symbol} | "
                        f"분봉 {minute_analysis.score()}/5 | {signal.reason}"
                    )
                    self.last_hold_log_at_by_symbol[symbol] = now
                return

            # 일반 HOLD (30초마다)
            last_logged_at = self.last_hold_log_at_by_symbol.get(symbol)
            if last_logged_at is None or (now - last_logged_at).total_seconds() >= 30:
                self.app_logger.info(
                    f"[HOLD    ] {regime_tag} {symbol} | 현재가 {current_price:,}원 | {signal.reason}"
                )
                self.last_hold_log_at_by_symbol[symbol] = now

    async def run_once(self) -> None:
        """자동매매 루프를 한 번 실행합니다."""
        balance = self._get_balance_with_cache()

        for i, symbol in enumerate(self.targets):

            if symbol in self._excluded_symbols:
                continue

            if i > 0:
                await asyncio.sleep(1.0)

            try:
                position_check = next((p for p in balance.positions if p.symbol == symbol), None)
                if position_check is not None:
                    try:
                        market_price = self.broker.get_market_price(symbol)
                        self.cached_market_prices[symbol] = market_price
                        self.cached_market_price_loaded_at[symbol] = datetime.now()
                    except Exception:
                        market_price = self._get_market_price_with_cache(symbol)
                else:
                    market_price = self._get_market_price_with_cache(symbol)
    
                market_price = self._attach_indicators(market_price, symbol)
    
                # 장세 판단 — 현재가/전일 종가 전달해서 당일 급등/급락 감지
                regime, _ = self._get_regime_with_cache(
                    symbol,
                    current_price=market_price.current_price,
                    prev_close=market_price.previous_close,
                )
    
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
    
                # BULLISH일 때만 분봉 2차 필터 적용
                minute_analysis = None
                if regime in (MarketRegime.BULLISH, MarketRegime.NEUTRAL, MarketRegime.REBOUND):
                    minute_analysis = self._get_minute_analysis(
                        symbol, market_price.previous_close
                    )
                    if minute_analysis:
                        self.app_logger.info(
                            f"[MIN ] {symbol} | {minute_analysis.score()}/5 | {minute_analysis.summary()}"
                        )
    
                strategy = self.strategy_router.select(regime)
                position = next((p for p in balance.positions if p.symbol == symbol), None)
    
                # 보유 중인 경우 최고가 갱신
                if position is not None:
                    current = market_price.current_price
                    if current > self._highest_price.get(symbol, 0):
                        self._highest_price[symbol] = current
                else:
                    self._highest_price.pop(symbol, None)
    
                highest_price = self._highest_price.get(symbol, 0)
                signal = strategy.generate_signal(market_price, position, minute_analysis, highest_price)
    
                self._log_signal_decision(
                    symbol, signal, market_price.current_price,
                    regime, position, minute_analysis
                )
    
                # 거래대금 부족 3회 연속이면 자동 제외
                if (
                    signal.type == SignalType.HOLD
                    and minute_analysis is not None
                    and not minute_analysis.is_valid_trading_value
                ):
                    self._low_volume_count[symbol] = self._low_volume_count.get(symbol, 0) + 1
                    if self._low_volume_count[symbol] >= 3:
                        self._excluded_symbols.add(symbol)
                        self.app_logger.warning(
                            f"[EXCL] {symbol} | 거래대금 부족 3회 연속 "
                            f"({minute_analysis.trading_value//100_000_000}억) — 감시 대상에서 제외합니다"
                        )
                else:
                    # 거래대금 충분하면 카운트 초기화
                    self._low_volume_count[symbol] = 0
    
                # ── 시그널 로그 기록 (BUY/HOLD 불문 전체) ──────────
                self._write_signal_log(
                    symbol=symbol,
                    price=market_price.current_price,
                    regime=regime,
                    signal=signal,
                    minute_analysis=minute_analysis,
                )

                if signal.type == SignalType.BUY:
                    self._try_buy(
                        symbol, market_price.current_price, balance,
                        signal=signal, regime=regime,
                        minute_analysis=minute_analysis,
                    )
                elif signal.type == SignalType.SELL and position is not None:
                    self._try_sell(
                        symbol, position.quantity, market_price.current_price,
                        exit_reason=signal.reason,
                    )

            except Exception as exc:
                self.app_logger.exception(
                    f"[ERROR] {symbol} | 종목 처리 중 예외 발생, 다음 종목으로 계속합니다: {exc}"
                )
                continue

        if is_near_market_close(self.settings.trading.force_exit_before_market_close_minutes):
            self.app_logger.info("near market close: force exit started")

            try:
                latest_balance = self.broker.get_account_balance()
                self.cached_balance = latest_balance
                self.cached_balance_loaded_at = datetime.now()

                api_positions = {p.symbol: p for p in latest_balance.positions}

                # 키움 모의투자는 당일 체결 종목을 잔고 API에 즉시 반영하지 않을 수 있습니다.
                # 캐시된 포지션을 보조 수단으로 활용하여 누락 없이 청산합니다.
                cached_positions = {
                    p.symbol: p
                    for p in (self.cached_balance.positions if self.cached_balance else [])
                }
                merged = {**cached_positions, **api_positions}  # API 결과 우선

                if not merged:
                    self.app_logger.info("[FORCE_EXIT] 청산할 보유 종목이 없습니다.")
                else:
                    for symbol, position in merged.items():
                        # 현재가를 조회하여 price=0 기록을 방지합니다.
                        try:
                            mp = self.broker.get_market_price(symbol)
                            current_price = mp.current_price
                        except Exception:
                            current_price = self.cached_market_prices.get(symbol)
                            current_price = current_price.current_price if current_price else 0

                        self.app_logger.info(
                            f"[FORCE_EXIT] {symbol} | {position.quantity}주 | 현재가 {current_price:,}원"
                        )
                        self._try_sell(
                            symbol, position.quantity, current_price,
                            exit_reason="FORCE_EXIT",
                        )

            except Exception as exc:
                self.app_logger.warning(f"[FORCE_EXIT] 강제청산 중 오류 발생: {exc}")

            # 장 마감 리포트 생성 (하루 1회 — 강제청산 성공 여부와 무관하게 생성)
            if not self._report_generated_today:
                self._generate_daily_report()
                self._report_generated_today = True

        self.state_store.save(self.state, self._highest_price)

    def _try_buy(
        self,
        symbol: str,
        current_price: int,
        balance: AccountBalance,
        signal=None,
        regime=None,
        minute_analysis=None,
    ) -> None:
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

        # ── 최대 보유 종목 수 체크 ────────────────────────────────
        try:
            live_balance = self.broker.get_account_balance()
        except Exception:
            live_balance = balance
        held_count = len(live_balance.positions)
        if held_count >= self.settings.trading.max_positions:
            self.app_logger.info(
                f"[BLOCK] {symbol} | 최대 보유 종목 수 초과 "
                f"({held_count}/{self.settings.trading.max_positions})"
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

        # ── 매수 컨텍스트 구성 ────────────────────────────────────
        ctx = self._build_trade_context(
            side="BUY", signal=signal, regime=regime,
            minute_analysis=minute_analysis, current_price=current_price,
        )
        self._write_trade_log(
            order.symbol,
            order.side.value,
            quantity,
            result.accepted,
            result.message,
            result.order_id,
            price=current_price,
            context=ctx,
        )

        if result.accepted:
            # 매수 시각 기록 → entry_watch 용
            self.state.entry_time_by_symbol[symbol] = datetime.now().isoformat()
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

    def _try_sell(
        self,
        symbol: str,
        quantity: int,
        current_price: int = 0,
        exit_reason: str = "",
    ) -> None:
        """매도 주문을 생성하고 브로커로 전달합니다."""
        order = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=quantity)
        result = self.broker.place_order(order)

        # 보유 시간 계산
        hold_minutes = ""
        entry_time_str = self.state.entry_time_by_symbol.get(symbol, "")
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                hold_minutes = round((datetime.now() - entry_dt).total_seconds() / 60, 1)
            except ValueError:
                pass

        self._write_trade_log(
            order.symbol,
            order.side.value,
            quantity,
            result.accepted,
            result.message,
            result.order_id,
            price=current_price,
            context={"exit_reason": exit_reason, "hold_minutes": hold_minutes},
        )

        if result.accepted:
            # 주문 성공 후 잔고 캐시 무효화
            self.cached_balance = None
            self.cached_balance_loaded_at = None

            # 매도 시각 기록 → 재진입 쿨다운에 사용
            self.state.last_sold_at_by_symbol[symbol] = datetime.now().isoformat()
            self.state.entry_time_by_symbol.pop(symbol, None)

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

    def _build_trade_context(
        self,
        side: str,
        signal=None,
        regime=None,
        minute_analysis=None,
        current_price: int = 0,
    ) -> dict:
        """trades.csv 컨텍스트 필드를 구성합니다."""
        ctx: dict = {}
        if side != "BUY" or signal is None:
            return ctx
        ctx["entry_strategy"] = self.settings.strategy.name
        ctx["market_regime"] = regime.value if regime else ""
        # 점수 파싱 (signal.reason에 'N/8' 형태로 포함)
        import re
        m = re.search(r'(\d+)/8', signal.reason)
        ctx["entry_score"] = m.group(1) if m else ""
        ctx["entry_reason"] = signal.reason[:120]
        if minute_analysis is not None:
            ma = minute_analysis
            ctx["is_v_rebound"]          = ma.is_v_rebound
            ctx["is_pulldown_recovery"]   = ma.is_pulldown_recovery
            ctx["v_drop_pct"]             = round(ma.v_drop_pct, 2)
            ctx["v_rise_pct"]             = round(ma.v_rise_pct, 2)
            ctx["v_low_age"]              = ma.v_bottom_k
            ctx["current_vs_vwap_pct"]    = round(
                (current_price - ma.vwap) / ma.vwap * 100, 2
            ) if ma.vwap > 0 else ""
            ctx["volume_ratio"]           = round(ma.v_volume_ratio, 2)
            ctx["bar_amount"]             = ma.trading_value
            ctx["rebound_volume_spike"]   = ma.rebound_volume_spike
            ctx["v_bottom_spike"]         = ma.v_bottom_spike
            ctx["upside_to_recent_high_pct"] = ma.upside_to_recent_high_pct
        return ctx

    def _write_trade_log(
        self,
        symbol: str,
        side: str,
        quantity: int,
        accepted: bool,
        message: str,
        order_id: str,
        price: int = 0,
        context: dict | None = None,
    ) -> None:
        """거래 로그 CSV에 한 줄을 추가합니다."""
        row = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "accepted": accepted,
            "message": message,
            "order_id": order_id,
        }
        if context:
            row.update(context)
        self.trade_logger.append(row)

    def _write_signal_log(
        self,
        symbol: str,
        price: int,
        regime,
        signal,
        minute_analysis=None,
    ) -> None:
        """시그널 판단 결과를 signal_log.csv에 기록합니다.

        BUY뿐 아니라 HOLD/SKIP도 모두 기록합니다.
        """
        import re
        m = re.search(r'(\d+)/8', signal.reason)
        score = m.group(1) if m else ""

        patterns = []
        row: dict = {
            "timestamp": datetime.now().isoformat(),
            "symbol":    symbol,
            "price":     price,
            "regime":    regime.value if regime else "",
            "score":     score,
            "signal":    signal.type.value,
            "skip_reason": "" if signal.type.value == "BUY" else signal.reason[:120],
        }
        if minute_analysis is not None:
            ma = minute_analysis
            if ma.is_v_rebound:          patterns.append("V")
            if ma.is_pulldown_recovery:  patterns.append("PR")
            if ma.is_valid_change_rate:  patterns.append("A")
            if ma.is_valid_rebound:      patterns.append("B")
            if ma.is_valid_pulldown:     patterns.append("C")
            row.update({
                "detected_patterns":   "/".join(patterns) if patterns else "-",
                "is_v_rebound":        ma.is_v_rebound,
                "is_pulldown_recovery": ma.is_pulldown_recovery,
                "v_drop_pct":          round(ma.v_drop_pct, 2),
                "v_rise_pct":          round(ma.v_rise_pct, 2),
                "v_low_age":           ma.v_bottom_k,
                "current_vs_vwap_pct": round(
                    (price - ma.vwap) / ma.vwap * 100, 2
                ) if ma.vwap > 0 else "",
                "volume_ratio":        round(ma.v_volume_ratio, 2),
                "bar_amount":          ma.trading_value,
                "rebound_volume_spike": ma.rebound_volume_spike,
                "v_bottom_spike":       ma.v_bottom_spike,
                "upside_to_recent_high_pct": ma.upside_to_recent_high_pct,
                "ma5_above_ma20":      ma.ma5_above_ma20,
            })
        else:
            row["detected_patterns"] = "-"
        self.signal_logger.append(row)