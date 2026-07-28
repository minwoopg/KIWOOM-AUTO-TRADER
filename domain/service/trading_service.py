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
import json
import time
from datetime import date, datetime
from pathlib import Path

from config.settings import Settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalyzer, MinuteAnalysis
from domain.models import AccountBalance, MarketRegime, OrderRequest, OrderSide, Signal, SignalType
from domain.position.lifecycle import PositionLifecycle, PositionStateMachine
from domain.risk.risk_manager import RiskManager
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.base import Broker
from infra.storage.daily_reporter import DailyReporter
from infra.storage.logger import (
    AppLogger, TradeCsvLogger, SignalCsvLogger, EntryWatchShadowLogger, PositionLifecycleLogger,
)
from infra.storage.minute_bar_saver import MinuteBarSaver
from infra.storage.skip_reason import classify_skip_reason, SkipReason
from infra.notify.kakao_notifier import KakaoNotifier, build_notifier
from domain.indicator.indicators import calc_atr, calc_bollinger, ATRResult, BollingerResult
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_market_open


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
        entry_watch_shadow_logger: "EntryWatchShadowLogger | None" = None,
        position_lifecycle_logger: "PositionLifecycleLogger | None" = None,
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
        # 2026-07-22: 선택적 인자 — 기존 생성 코드(main.py, 테스트)를
        # 안 건드리고 도입하기 위해 None이면 storage 설정에서 자동 생성
        self.entry_watch_shadow_logger = entry_watch_shadow_logger or EntryWatchShadowLogger(
            settings.storage.entry_watch_shadow_log_file
        )
        self.position_lifecycle_logger = position_lifecycle_logger or PositionLifecycleLogger(
            settings.storage.position_lifecycle_log_file
        )

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
        self._last_buy_signal_at: dict[str, datetime] = {}  # 종목별 마지막 BUY신호 시각
        self._symbol_to_condition: dict[str, str] = {}       # 종목 → 조건검색식 이름

        # [REGIME]/[MIN] 로그 중복 억제 (2026-07-14: app.log 200MB 급증 원인 —
        # 매 폴링(10초)마다 값이 살짝만 바뀌어도 무조건 재로깅되던 것을,
        # 실제로 라벨/점수가 바뀌었을 때 + 최소 간격으로만 남기도록 축소)
        self._last_regime_logged: dict[str, tuple[str, datetime]] = {}
        self._last_min_logged: dict[str, tuple[int, datetime]] = {}
        self._regime_log_heartbeat_sec = 300  # REGIME: 라벨 불변 시 5분마다 하트비트

        # 2026-07-20: 일별 상태(symbol_entry_count_today 등) 리셋이
        # main.py의 "장 시작 전 대기" 분기에서만 호출되고 있었음 — 프로세스가
        # 이미 실행 중이면(주말 내내 켜져있던 경우 등) 리셋이 전혀 안 됨.
        # 7/20(월) 실제 사례: 금요일 475150 진입 3회로 카운터가 이미 3에
        # 도달해 있었는데 주말 지나고도 리셋이 안 돼서, 월요일 매수 0건인데
        # MAX_ENTRIES_PER_DAY가 203회 차단되는 버그 발생. 프로세스 재시작
        # 타이밍에 의존하지 않도록, 폴링마다 날짜변경을 직접 체크하도록 변경.
        self._last_reset_date = None
        self._notifier: KakaoNotifier = build_notifier(settings)  # 카카오 알림
        # ATR/볼린저 계산용 일봉 캐시 (종목별 60개 유지)
        self._daily_bars_cache: dict[str, dict] = {}  # (미사용 — _update_indicators가 cached_daily_bars 재사용)
        self._last_indicators: dict[str, dict] = {}   # {symbol: {atr, bb}}

        # 보유 종목별 최고가 추적 (트레일링 스탑용)
        self._highest_price: dict[str, int] = loaded_highest

        # 동적 종목 목록 (조건검색 연동 시 갱신)
        self._dynamic_targets: list[str] | None = None

        # entry_watch counterfactual 추적 (2026-07-22)
        # {symbol: {trigger_at, trigger_type, entry_price, trigger_price,
        #           actual_pnl_pct, checkpoints_done: set[int]}}
        # 휘발성 관찰용 데이터라 state.json에 영속화하지 않음(다른 캐시와
        # 동일 패턴). 프로세스 재시작 시 진행 중이던 추적은 유실되지만,
        # 관찰 목적이라 손실 위험은 없음.
        self._entry_watch_shadow_tracking: dict[str, dict] = {}

        # ── 포지션 5단계 상태머신 (2026-07-22, shadow 모드) ─────────
        # GPT 검토(7.12/7.14절)에서 제안된 완전한 lifecycle 머신.
        # 아직 실제 매매 판정에는 쓰이지 않고, 기존 _sold_today_qty_snapshot
        # 판정과 병행 계산해 결과가 갈리는 경우만 로그로 남김(검증 단계).
        # 검증 완료 후 실제 교체 예정 — domain/position/lifecycle.py 참고.
        self._position_state_machine = PositionStateMachine(logger=self.position_lifecycle_logger)
        self._position_state_machine_initialized = False

        # 일일 리포트 생성기
        self._reporter = DailyReporter(
            trade_log_file=settings.storage.trade_log_file,
            report_dir=str(Path(settings.storage.trade_log_file).parent),
            signal_log_file=settings.storage.signal_log_file,
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
            slow_v_bottom_lookback=cfg.slow_v_bottom_lookback,
            slow_v_low_min_age=cfg.slow_v_low_min_age,
            slow_v_low_max_age=cfg.slow_v_low_max_age,
            slow_v_drop_threshold_pct=cfg.slow_v_drop_threshold_pct,
            slow_v_rebound_threshold_pct=cfg.slow_v_rebound_threshold_pct,
            slow_v_max_rebound_pct=cfg.slow_v_max_rebound_pct,
            slow_v_volume_ratio=cfg.slow_v_volume_ratio,
        )
        # 장세 판단 요약 (리포트용)
        self._regime_summary: dict[str, str] = {}
        # 장 마감 리포트가 이미 생성됐는지 여부 (중복 방지)
        self._report_generated_today: bool = False

        # 1분봉 저장기
        self._minute_saver: MinuteBarSaver | None = (
            MinuteBarSaver(settings.storage.minute_bars_dir)
            if getattr(settings.storage, 'save_minute_bars', False)
            else None
        )

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

    def update_targets(
        self,
        symbols: list[str],
        sym_to_cond: dict[str, str] | None = None,
    ) -> None:
        """조건검색 결과로 종목 목록을 동적으로 갱신합니다.

        보유 중인 종목은 조건검색 편출 여부와 무관하게 항상 포함합니다.
        """
        if sym_to_cond:
            self._symbol_to_condition.update(sym_to_cond)
        holding_symbols: list[str] = []
        try:
            balance = self._get_balance_with_cache()
            holding_symbols = [p.symbol for p in balance.positions]
        except Exception:
            pass

        merged = list(symbols)
        for sym in holding_symbols:
            if sym not in merged:
                merged.append(sym)
                self.app_logger.info(
                    f"[TARGET] {sym} | 조건검색 편출됐으나 보유 중 → 모니터링 유지"
                )

        self._dynamic_targets = merged

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

            held = [f"{p.symbol}({p.quantity}주)" for p in balance.positions]
            self.app_logger.info(
                f"account balance loaded from api | "
                f"cash={balance.cash:,} | positions={len(balance.positions)} | "
                f"held={held}"
            )
            return balance

        elapsed = (now - self.cached_balance_loaded_at).total_seconds()

        if elapsed >= self.settings.trading.balance_refresh_seconds:
            balance = self.broker.get_account_balance()
            self.cached_balance = balance
            self.cached_balance_loaded_at = now

            held = [f"{p.symbol}({p.quantity}주)" for p in balance.positions]
            self.app_logger.info(
                f"account balance loaded from api | "
                f"cash={balance.cash:,} | positions={len(balance.positions)} | "
                f"held={held}"
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

    def _log_regime_if_changed(self, symbol: str, label: str, reason: str) -> None:
        """[REGIME] 로그를 라벨 변경 시 + 5분 하트비트로만 남깁니다.

        기존엔 이 지점 3곳(일봉분류/급등강제/급락강제)이 매 폴링(10초)마다
        무조건 info 로그를 남겨서 app.log가 200MB까지 불어난 주 원인이었음.
        급등락 % 같은 세부값은 바뀌어도, 실제로 봐야 할 건 "장세 라벨이
        바뀌었는가"이므로 라벨 기준으로만 dedup.
        """
        now = datetime.now()
        last = self._last_regime_logged.get(symbol)
        changed = last is None or last[0] != label
        heartbeat_due = last is not None and (now - last[1]).total_seconds() >= self._regime_log_heartbeat_sec
        if changed or heartbeat_due:
            self.app_logger.info(f"[REGIME] {symbol} | {label} | {reason}")
            self._last_regime_logged[symbol] = (label, now)

    def _get_regime_with_cache(self, symbol: str, current_price: int = 0, prev_close: int = 0) -> tuple[MarketRegime, str]:
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

                self._log_regime_if_changed(symbol, regime.value, reason)
                self._regime_summary[symbol] = f"{regime.value} ({reason})"

            except Exception as exc:
                self.app_logger.warning(
                    f"[REGIME] {symbol} | 일봉 조회 실패({type(exc).__name__}: {exc}) — "
                    f"{'직전 캐시 유지' if symbol in self.cached_regime else 'UNKNOWN으로 처리'}"
                )

        # ── 당일 등락률 기반 장세 보정 ───────────────────────────
        # 일봉 로드와 분리: 일봉 데이터는 항상 로드하되,
        # 당일 급등락 시에는 일봉 분류 결과를 보정해서 반환합니다.
        if current_price > 0 and prev_close > 0:
            change_rate = (current_price - prev_close) / prev_close * 100
            if change_rate >= 2.0:
                reason = f"당일 급등 {change_rate:+.1f}% — BULLISH 강제 적용"
                self._log_regime_if_changed(symbol, "BULLISH", reason)
                self._regime_summary[symbol] = f"BULLISH ({reason})"
                return MarketRegime.BULLISH, reason
            elif change_rate <= -2.0:
                reason = f"당일 급락 {change_rate:+.1f}% — NEUTRAL 강제 적용"
                self._log_regime_if_changed(symbol, "NEUTRAL", reason)
                self._regime_summary[symbol] = f"NEUTRAL ({reason})"
                return MarketRegime.NEUTRAL, reason

        # 캐시 재사용
        cached = self.cached_regime.get(symbol, MarketRegime.UNKNOWN)
        return cached, "(캐시)"

    def _get_minute_analysis(
        self, symbol: str, prev_close: int
    ) -> tuple[MinuteAnalysis | None, bool, str]:
        """분봉 데이터를 가져와 2차 필터 분석 결과를 반환합니다. 결과는 캐시합니다.

        2026-07-27 (GPT 코드리뷰 지적, 안전성 긴급 수정): 기존엔
        get_minute_bars() 예외 시 오래된 캐시(cached_minute_bars)를
        그대로 분석에 넘기면서, 이 데이터가 "방금 조회한 신선한
        데이터"인지 "조회 실패로 어쩔 수 없이 쓴 오래된 캐시"인지
        호출부에 전혀 알리지 않았음 — 실운영에서 분봉 조회 실패 직후
        오래된 캐시 기반으로 신규 매수(039980)가 발생한 원인이 이
        구조. 반환값에 신선도 정보(is_fresh)와 사유(reason)를 함께
        담아, 호출부가 "이 데이터로 신규 진입을 해도 되는지"를
        명시적으로 판단할 수 있게 함.

        반환값:
            (analysis, is_fresh, reason)
            - analysis: 기존과 동일한 MinuteAnalysis | None
            - is_fresh: 이번 호출에서 API 조회에 성공했으면 True,
              실패해서 캐시로 대체했으면 False (첫 조회 자체가
              실패해 캐시도 없는 경우도 False)
            - reason: fresh가 False일 때의 사유 문자열
              ("MINUTE_DATA_UNAVAILABLE" | "STALE_MINUTE_DATA" | "")
              — 캐시가 아예 없으면 UNAVAILABLE, 오래된 캐시라도
              있으면 STALE.

        기존 동작(캐시를 분석에 사용하는 것 자체)은 그대로 유지 —
        보유 종목의 손절/트레일링 판단이 끊기지 않도록. 다만 이제
        신선도를 알 수 있으므로, 호출부가 미보유 종목의 신규 진입만
        선택적으로 차단할 수 있음.
        """
        now = datetime.now()
        loaded_at = self.cached_minute_bars_loaded_at.get(symbol)
        refresh_sec = self.settings.market_regime.minute_refresh_seconds

        need_refresh = (
            loaded_at is None
            or (now - loaded_at).total_seconds() >= refresh_sec
        )

        is_fresh = True
        stale_reason = ""

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

                # 1분봉 저장 (enabled 시)
                if self._minute_saver is not None and bars:
                    try:
                        self._minute_saver.save(symbol, bars)
                    except Exception as save_exc:
                        self.app_logger.debug(f"[MIN] {symbol} | 분봉 저장 실패: {save_exc}")
            except Exception as exc:
                self.app_logger.warning(f"[MIN] {symbol} | 분봉 조회 실패: {exc}")
                bars = self.cached_minute_bars.get(symbol, [])
                is_fresh = False
                stale_reason = (
                    "STALE_MINUTE_DATA" if bars else "MINUTE_DATA_UNAVAILABLE"
                )
        else:
            bars = self.cached_minute_bars.get(symbol, [])

        if not bars:
            return None, is_fresh, stale_reason

        analysis = self._minute_analyzer.analyze(bars, prev_close)
        return analysis, is_fresh, stale_reason

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
            # ── 매도 상세 로그 ──────────────────────────────────
            reason = signal.reason
            if position is not None:
                avg_p = position.average_price
                pnl   = (current_price - avg_p) / avg_p * 100
                if "추세 꺾임" in reason:
                    # 점수제 상세
                    import re as _re
                    score_m = _re.search(r'(\d)/5점', reason)
                    score_v = score_m.group(1) if score_m else '?'
                    conds = _re.search(r'— (.+?) \(', reason)
                    conds_v = conds.group(1) if conds else reason
                    ma = minute_analysis
                    vwap_ok  = ma.price_above_vwap if ma else None
                    ma5_ok   = ma.ma5_above_ma20 if ma else None
                    self.app_logger.info(
                        f"[SELL_SCORE] {symbol} "
                        f"profit={pnl:+.2f}% "
                        f"score={score_v}/5 "
                        f"조건=[{conds_v}] "
                        f"VWAP={'위' if vwap_ok else '아래'} "
                        f"MA5={'위' if ma5_ok else '아래'}"
                    )
                elif "트레일링" in reason:
                    # 트레일링 상세
                    import re as _re
                    trail_m = _re.search(r'폭 -([\d.]+)%', reason)
                    trail_v = trail_m.group(1) if trail_m else '?'
                    _high = self._highest_price.get(symbol, 0)
                    self.app_logger.info(
                        f"[TRAIL] {symbol} "
                        f"profit={pnl:+.2f}% "
                        f"high={_high:,}원 "
                        f"trail_band={trail_v}% "
                        f"stop={int(_high*(1-float(trail_v)/100)) if _high and trail_v != '?' else '?'}원"
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
        # 2026-07-20: 프로세스 재시작 타이밍에 의존하지 않는 날짜변경 감지.
        # main.py의 조건부 reset_daily_loss_counts() 호출과 별개로, 여기서
        # 매 폴링마다 직접 오늘 날짜를 확인해 확실하게 리셋되도록 보강.
        today = date.today()
        if self._last_reset_date != today:
            if self._last_reset_date is not None:
                self.app_logger.info(
                    f"[RESET] 날짜변경 감지 {self._last_reset_date} → {today} — 일별 상태 초기화"
                )
            self.reset_daily_loss_counts()
            self._last_reset_date = today

        balance = self._get_balance_with_cache()

        # ── 포지션 상태머신 shadow 동기화 + 불변조건 검사 (2026-07-22) ──
        # 아직 실제 매매 판정에는 관여하지 않음(shadow 모드). 매 폴링마다
        # PENDING이 아닌 종목은 브로커 잔고로 동기화하고, "잔고>0인데
        # 로컬상태=FLAT"인 불변조건 위반이 있는지 검사해 CRITICAL 로그만
        # 남김. 기존 _sold_today_qty_snapshot 기반 실제 판정과는 별개.
        self._sync_position_state_machine_shadow(balance)

        # ── 이월 포지션 강제청산 체크 ──────────────────────────
        # settings.yaml의 force_exit_before_market_close_minutes (기본 12분 전 = 14:48)
        # 14:40~14:50 사이에 수익 쿠션 없는 보유 포지션을 청산해 이월 방지
        await self._check_force_exit_overnight(balance)

        # ── 보유 종목 우선 처리 (2026-07-20) ──────────────────────
        # 기존엔 targets를 그냥 순서대로(조건검색 편입 순 등) 순회해서,
        # 보유 종목이 리스트 뒷쪽에 있으면 앞선 미보유 종목들의 무거운
        # 분석(장세판단/분봉2차필터)과 종목당 1초 sleep을 전부 거친 뒤에야
        # 손절/트레일링 판단을 받았음. 정상 손절 41건 중 30건(73%)이
        # 기준(-1.5%)보다 나쁘게 체결, 최악 -9.0%(7.5%p 괴리) 사례 확인.
        #
        # targets 자체를 두 그룹으로 나눠 보유 종목을 먼저 처리하는 것으로
        # 완화. 처리 로직(_process_symbol)은 완전히 동일하게 재사용 —
        # 순서만 바꿔서 보유 종목이 앞선 미보유 종목들의 sleep 누적을
        # 기다리지 않게 함. (완전 별도 async 루프로 분리하지 않은 이유:
        # state/balance 캐시를 두 태스크가 동시에 건드리면 경쟁 조건 위험)
        held_symbols = {p.symbol for p in balance.positions}
        ordered_targets = sorted(
            enumerate(self.targets),
            key=lambda pair: 0 if pair[1] in held_symbols else 1,
        )

        for order_idx, (_orig_i, symbol) in enumerate(ordered_targets):

            if symbol in self._excluded_symbols:
                continue

            if order_idx > 0:
                # 2026-07-22: 직전 종목이 보유종목이었으면 짧은 간격
                # (held_symbol_poll_gap_seconds), 미보유종목이었으면
                # 기존과 동일한 간격(entry_poll_gap_seconds) 적용.
                # "직전 종목 기준"인 이유: 보유종목 구간 내부(둘 다 held)
                # 뿐 아니라 보유→미보유 전환 경계도 자연스럽게 커버하려면
                # 이번에 처리할 종목이 아니라 방금 API를 호출한 종목의
                # 성격을 봐야 함.
                prev_symbol = ordered_targets[order_idx - 1][1]
                if prev_symbol in held_symbols:
                    gap = getattr(self.settings.trading, "held_symbol_poll_gap_seconds", 1.0)
                else:
                    gap = getattr(self.settings.trading, "entry_poll_gap_seconds", 1.0)
                if gap > 0:
                    await asyncio.sleep(gap)

            await self._process_symbol(symbol, balance)

        # ── entry_watch counterfactual 체크포인트 확인 (2026-07-22) ──
        # 청산된 종목은 balance.positions에 없어서 위 루프에서 가격을
        # 못 얻으므로, 추적 중인 종목만 별도로 가격 조회 후 체크.
        # targets에 없는 종목이 추적 대상일 수도 있으니(예: entry_watch
        # 청산 후 targets에서 자연히 빠진 경우) 별도 순회가 필요함.
        if self._entry_watch_shadow_tracking:
            for shadow_symbol in list(self._entry_watch_shadow_tracking.keys()):
                try:
                    mp = self._get_market_price_with_cache(shadow_symbol)
                    self._check_entry_watch_shadow_checkpoints(shadow_symbol, mp.current_price)
                except Exception as exc:
                    self.app_logger.warning(
                        f"[EW_SHADOW] {shadow_symbol} | 가격 조회 실패로 체크포인트 스킵: {exc}"
                    )

        self.state_store.save(self.state, self._highest_price)

    async def _process_symbol(self, symbol: str, balance) -> None:
        """종목 하나에 대해 시세 조회 → 장세/분봉분석 → 신호판단 → 주문을 수행합니다.

        run_once()의 순회 루프에서 종목마다 호출됩니다. 보유 종목 우선
        처리를 위해 2026-07-20에 run_once() 본문에서 분리했습니다.
        """
        try:
            position_check = next((p for p in balance.positions if p.symbol == symbol), None)
            # ── 매도 직후 지연 방어를 수량 비교 기반으로 전환 (2026-07-22) ──
            # 기존엔 "_sold_today에 있으면 무조건 미보유"였는데, 이게
            # 두 가지 실제 사고를 냈음(순서대로):
            #  1) 7/15: 강제청산 매도 접수 직후 브로커 API가 옛 잔고를
            #     반환하는 지연 구간에서 중복 매도 재시도 (7.4절)
            #  2) 7/21: 매도 성공 후 같은 날 재매수가 실제로 체결됐는데도
            #     플래그가 그대로 남아 3시간+ 손절판단 자체가 마비 (7.12절)
            # 두 경우 모두 "매도 시도 시점의 잔고 수량"과 "지금 잔고
            # 수량"을 비교하면 구분된다: 수량이 그대로면 아직 API 미반영
            # (진짜 지연 — 미보유로 간주 유지), 수량이 달라졌으면 브로커가
            # 이미 새 상태(체결완료 또는 재매수)를 반영한 것이므로 그
            # 잔고를 그대로 신뢰해야 함.
            sold_qty_snapshot = getattr(self, '_sold_today_qty_snapshot', {})
            if (
                position_check is not None
                and symbol in sold_qty_snapshot
                and position_check.quantity == sold_qty_snapshot[symbol]
            ):
                position_check = None
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
                    return
            else:
                self._unknown_count[symbol] = 0

            # BULLISH일 때만 분봉 2차 필터 적용
            minute_analysis = None
            minute_data_fresh = True
            minute_data_stale_reason = ""
            if regime in (MarketRegime.BULLISH, MarketRegime.NEUTRAL, MarketRegime.REBOUND):
                minute_analysis, minute_data_fresh, minute_data_stale_reason = (
                    self._get_minute_analysis(symbol, market_price.previous_close)
                )
                if minute_analysis:
                    score = minute_analysis.score()
                    last = self._last_min_logged.get(symbol)
                    now = datetime.now()
                    score_changed = last is None or last[0] != score
                    min_interval_passed = last is None or (now - last[1]).total_seconds() >= 30
                    # 2026-07-14: 기존엔 스로틀이 전혀 없어서 매 폴링(10초)마다
                    # 무조건 로깅 — app.log 최대 기여 태그(227k/997k줄)였음.
                    # 점수 변화 시 + 최소 30초 간격(다른 로그들과 동일 cadence)으로 축소.
                    if score_changed or min_interval_passed:
                        self.app_logger.info(
                            f"[MIN ] {symbol} | {score}/5 | {minute_analysis.summary()}"
                        )
                        self._last_min_logged[symbol] = (score, now)
                    # V자 실패 사유 로그 (감지 안 됐을 때, 분당 1회)
                    if not minute_analysis.is_v_rebound:
                        v_fails = getattr(self._minute_analyzer, '_last_v_fail_reasons', [])
                        if v_fails:
                            last_v_log = self.last_hold_log_at_by_symbol.get(
                                f"__vfail_{symbol}"
                            )
                            now_dt = datetime.now()
                            if last_v_log is None or (now_dt - last_v_log).total_seconds() >= 60:
                                self.app_logger.info(
                                    f"[V_FAIL] {symbol} | " + " / ".join(v_fails)
                                )
                                self.last_hold_log_at_by_symbol[f"__vfail_{symbol}"] = now_dt

            strategy = self.strategy_router.select(regime)
            position = next((p for p in balance.positions if p.symbol == symbol), None)
            # 당일 매도 완료 종목은 잔고 API 지연으로 잘못 보일 수 있으니 None 처리
            # (2026-07-03: position_check에만 적용돼 있던 걸 실제 판단용 position에도 적용
            #  — 064290이 14:48 매도 성공 후 5번 추가 매도 시도한 원인)
            # (2026-07-22: 수량 비교 기반으로 전환 — 위 _sold_today_qty_snapshot
            #  주석 참고. 매도 시도 당시 수량과 지금 수량이 같을 때만 미보유로 간주)
            _forced_none_by_sold_today = False
            if (
                position is not None
                and symbol in sold_qty_snapshot
                and position.quantity == sold_qty_snapshot[symbol]
            ):
                position = None
                _forced_none_by_sold_today = True

            if position is None and balance.positions and not _forced_none_by_sold_today:
                # symbol이 정확히 일치하는 포지션이 없을 때,
                # 비슷한(문자열 차이만 있는) 포지션이 있는지 확인해서
                # 공백/접두사 등 매칭 실패 원인을 즉시 드러냄
                close_matches = [
                    p.symbol for p in balance.positions
                    if symbol in p.symbol or p.symbol in symbol
                ]
                if close_matches:
                    self.app_logger.warning(
                        f"[POS_MISMATCH] {symbol} | "
                        f"정확히 일치하는 포지션 없음, 유사 symbol 발견: {close_matches} "
                        f"(repr: {[repr(s) for s in close_matches]})"
                    )

            # 보유 중인 경우 최고가 갱신 (트레일링 스탑 + entry watch 공용)
            if position is not None:
                current = market_price.current_price
                if current > self._highest_price.get(symbol, 0):
                    self._highest_price[symbol] = current
                # entry watch용 peak_price 갱신
                if current > self.state.peak_price_by_symbol.get(symbol, 0):
                    self.state.peak_price_by_symbol[symbol] = current
            else:
                self._highest_price.pop(symbol, None)
                # 청산 후 다음 진입에 옛 카운터가 이어지지 않도록 리셋
                self.state.vwap_break_streak_by_symbol.pop(symbol, None)

            highest_price = self._highest_price.get(symbol, 0)
            # 볼린저 %B — 상단 돌파 시 진입 문턱 상향에 사용 (2026-07-02)
            _bb_cached = self._last_indicators.get(symbol, {}).get('bb')
            _bb_pb = getattr(_bb_cached, 'percent_b', None) if _bb_cached is not None else None

            # ── entry_watch: 정규 전략보다 먼저 체크 ──────────────
            # 매수 후 watch_minutes(+1분 버퍼) 이내에서만 작동. SELL을
            # 내면 정규 전략(손절/트레일링) 호출 자체를 건너뜀.
            signal = self._check_entry_watch(
                symbol, position, market_price.current_price, minute_analysis,
            )
            if signal is None:
                signal = strategy.generate_signal(
                    market_price, position, minute_analysis, highest_price,
                    bb_percent_b=_bb_pb,
                )

            # 2026-07-27 (GPT 코드리뷰 지적, 안전성 긴급 수정): 미보유
            # 종목에서 분봉 데이터가 신선하지 않은데(조회 실패로 오래된
            # 캐시를 쓴 경우) BUY 신호가 나오면 강제로 HOLD로 덮어씀 —
            # 실운영에서 분봉 조회 실패 직후 오래된 캐시 기반 신규
            # 매수(039980)가 발생했던 문제의 재발 방지. 보유 종목
            # (position is not None)의 손절/트레일링 SELL은 이 검사와
            # 무관하게 그대로 동작 — 위험 축소 행동을 막으면 안 되므로.
            if (
                position is None
                and not minute_data_fresh
                and signal.type == SignalType.BUY
            ):
                signal = Signal(type=SignalType.HOLD, reason=minute_data_stale_reason)

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

            # ── BUY 신호면 주문 시도 후 결과를 signal_log에 반영 ──
            order_block_reason = ""
            final_decision = signal.type.value
            if signal.type == SignalType.BUY:
                block = self._try_buy(
                    symbol, market_price.current_price, balance,
                    signal=signal, regime=regime,
                    minute_analysis=minute_analysis,
                )
                if block:
                    order_block_reason = block
                    final_decision = "BLOCKED"

            # ── SELL 신호 처리 ───────────────────────────────
            if signal.type == SignalType.SELL and position is not None:
                self._try_sell(
                    symbol, position.quantity, market_price.current_price,
                    exit_reason=signal.reason,
                    avg_buy_price=position.average_price,
                )

            # ── ATR / 볼린저 계산 (로그 전용, 일봉 캐시 사용) ──
            self._update_indicators(symbol, market_price.current_price)

            # ── 시그널 로그 기록 (BUY/HOLD/SELL 불문 전체) ──
            self._write_signal_log(
                symbol=symbol,
                price=market_price.current_price,
                regime=regime,
                signal=signal,
                minute_analysis=minute_analysis,
                final_decision=final_decision,
                order_block_reason=order_block_reason,
            )

        except Exception as exc:
            self.app_logger.exception(
                f"[ERROR] {symbol} | 종목 처리 중 예외 발생, 다음 종목으로 계속합니다: {exc}"
            )
            return

    def reset_daily_loss_counts(self) -> None:
        """당일 손실 횟수를 초기화합니다. 매일 최초 1회(날짜변경 감지 시) 호출됩니다."""
        self.state.symbol_loss_count_today.clear()
        self.state.symbol_stoploss_at.clear()
        self.state.symbol_trail_loss_at.clear()
        self.state.symbol_entry_count_today.clear()
        self.state.symbol_block_today.clear()
        # 2026-07-20: StateReconciler.reconcile()이 프로세스 시작 시 1회만
        # 호출되어 bought_symbols_today/consecutive_losses도 동일한 유형의
        # "주말 넘어가면 초기화 누락" 버그에 노출되어 있었음 — 여기로 통합.
        self.state.bought_symbols_today.clear()
        self.state.consecutive_losses = 0
        if hasattr(self, '_sold_today'):
            self._sold_today.clear()  # 당일 매도 완료 종목 초기화
        if hasattr(self, '_sold_today_qty_snapshot'):
            self._sold_today_qty_snapshot.clear()
        self._excluded_symbols.clear()  # 당일 제외 종목(매매제한 등) 초기화 — 익일 재시도 허용
        self.app_logger.info("[RESET] 당일 종목별 손실/진입 카운트 초기화 완료")

    def _update_indicators(self, symbol: str, current_price: int) -> None:
        """
        ATR / 볼린저 지표를 계산해서 _last_indicators에 캐시합니다.

        regime 판정에서 이미 받아온 cached_daily_bars를 재사용합니다
        (별도 API 호출 없음 — 중복 호출로 인한 429 방지).
        """
        try:
            # regime 판정용으로 이미 로드된 일봉 캐시를 그대로 재사용
            bars = self.cached_daily_bars.get(symbol)
            if not bars or len(bars) < 20:
                self.app_logger.warning(
                    f"[INDICATOR] {symbol} 일봉 데이터 부족 "
                    f"({len(bars) if bars else 0}개, 최소 20개 필요) — "
                    f"ATR/볼린저 계산 불가 (regime 캐시 대기 중)"
                )
                return

            h = [b.high_price for b in bars]
            l = [b.low_price  for b in bars]
            c = [b.close_price for b in bars]

            atr = calc_atr(h, l, c, period=14, current_price=current_price)
            bb  = calc_bollinger(c, current_price=current_price, period=20)

            self._last_indicators[symbol] = {'atr': atr, 'bb': bb}

        except Exception as e:
            self.app_logger.warning(
                f"[INDICATOR] {symbol} 계산 실패: {type(e).__name__}: {e}"
            )

    async def _check_force_exit_overnight(self, balance) -> None:
        """
        장 마감 전 이월 방지 강제청산.

        14:40(force_exit_before_market_close_minutes=20) 기준:
          - 수익 쿠션 없는 포지션(수익률 < +0.3%) → 즉시 청산
          - 수익 쿠션 있는 포지션(수익률 >= +0.3%) → 이월 허용

        이 로직은 매 폴링(10초)마다 호출되지만,
        이미 청산 처리된 포지션은 balance 갱신 후 사라지므로 중복 청산 없음.
        """
        now = datetime.now()
        force_exit_minutes = getattr(
            self.settings.trading,
            'force_exit_before_market_close_minutes',
            12,
        )
        # 강제청산 시작 시각 계산 (15:30 - N분)
        close_hour, close_min = 15, 30
        force_min_total = close_hour * 60 + close_min - force_exit_minutes
        force_h, force_m = divmod(force_min_total, 60)

        now_min_total = now.hour * 60 + now.minute
        is_force_exit_window = (
            force_min_total <= now_min_total < close_hour * 60 + close_min
        )
        if not is_force_exit_window:
            return

        cushion_threshold = 0.3  # +0.3% 이상이면 이월 허용

        for pos in balance.positions:
            symbol = pos.symbol
            avg = pos.average_price
            if avg <= 0:
                continue
            # 2026-07-15: 기존 정규 매도 경로(595/667라인)는 _sold_today로
            # "매도 접수 직후 balance 미반영으로 인한 중복 매도 시도"를
            # 막고 있었는데, 이 강제청산 경로만 그 체크가 빠져 있었음.
            # 7/15 사례: 475150 664주 강제청산 접수 후, 모의투자 체결 반영
            # 지연으로 다음 폴링에도 포지션이 그대로 보여 11회 연속
            # "매도가능수량 부족" 재시도/실패가 발생. 동일 메커니즘 재사용.
            # (2026-07-22: 수량 비교 기반으로 전환. 수량이 매도 시도 당시와
            # 같을 때만 "아직 미반영"으로 보고 skip — 부분체결로 수량이
            # 줄었다면 잔여수량에 대해 다시 강제청산을 시도해야 하므로)
            sold_qty_snapshot = getattr(self, '_sold_today_qty_snapshot', {})
            if symbol in sold_qty_snapshot and pos.quantity == sold_qty_snapshot[symbol]:
                continue
            try:
                market_price = self._get_market_price_with_cache(symbol)
                current = market_price.current_price
            except Exception:
                continue

            pnl_pct = (current - avg) / avg * 100

            if pnl_pct >= cushion_threshold:
                self.app_logger.info(
                    f"[OVERNIGHT] {symbol} | 수익 쿠션 {pnl_pct:+.2f}% "
                    f"→ 이월 허용 (기준 +{cushion_threshold}%)"
                )
                continue

            self.app_logger.warning(
                f"[OVERNIGHT] {symbol} | 수익 쿠션 없음 {pnl_pct:+.2f}% "
                f"→ 이월 방지 강제청산 ({now.strftime('%H:%M')})"
            )
            self._try_sell(
                symbol=symbol,
                quantity=pos.quantity,
                current_price=current,
                exit_reason=(
                    f"이월 방지 강제청산 — "
                    f"{force_h:02d}:{force_m:02d} 이후 수익 쿠션 없음 ({pnl_pct:+.2f}%)"
                ),
                avg_buy_price=avg,
            )
            await asyncio.sleep(0.5)

    def _check_entry_watch(
        self,
        symbol: str,
        position,
        current_price: int,
        minute_analysis,
    ) -> "Signal | None":
        """
        매수 직후 watch_minutes 동안 실패한 진입(V자 등)을 빠르게 정리합니다.

        정규 전략(손절/트레일링)보다 먼저 체크하며, 다음 중 하나라도
        해당하면 즉시 SELL 신호를 반환합니다:
          1) 관찰 기간 중 fail_cut_pct 이하로 급락
          2) fail_on_vwap_break=True이고 VWAP 이탈
          3) watch_minutes(+1분 버퍼) 경과 시점에도 min_profit_pct 미달

        watch_minutes(+버퍼)를 초과하면 더 이상 관여하지 않고 None을
        반환해 정규 전략에 판단을 위임합니다. position이 없거나
        entry_watch가 비활성화된 경우도 None을 반환합니다.
        """
        ew = getattr(self.settings, "entry_watch", None)
        if ew is None or not ew.enabled:
            return None
        if position is None:
            return None

        entry_time_str = self.state.entry_time_by_symbol.get(symbol, "")
        if not entry_time_str:
            return None
        try:
            entry_dt = datetime.fromisoformat(entry_time_str)
        except ValueError:
            return None

        elapsed_min = (datetime.now() - entry_dt).total_seconds() / 60
        # 관찰 윈도우(+1분 버퍼) 초과 시 정규 전략에 위임
        if elapsed_min > ew.watch_minutes + 1:
            return None

        avg = position.average_price
        if avg <= 0:
            return None
        pnl_pct = (current_price - avg) / avg * 100

        # 1) 급락 즉시 청산
        if pnl_pct <= ew.fail_cut_pct:
            self._start_entry_watch_shadow_tracking(
                symbol, position, avg, current_price, ew, "급락청산",
            )
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"entry_watch 급락청산 — 매수 후 {elapsed_min:.1f}분, "
                    f"수익률 {pnl_pct:+.2f}% (기준 {ew.fail_cut_pct:+.1f}% 이하)"
                ),
            )

        # 2) VWAP 이탈 청산 (히스테리시스 적용, 2026-07-22)
        # 7/21 사고: 단일 폴링 시점 판단이라 노이즈에 취약했음
        # (VWAP이탈청산 2건이 -4.83%/-0.43%). 유예시간 → 이탈폭 하한
        # → 연속 확인 순으로 세 단계 필터를 거쳐야 청산됨. 세 값 모두
        # 기본값(0, 0.0, 1)이면 기존과 동일하게 즉시 청산.
        if ew.fail_on_vwap_break and minute_analysis is not None:
            grace_sec = getattr(ew, "vwap_grace_seconds", 0)
            in_grace_period = (elapsed_min * 60) < grace_sec

            vwap = minute_analysis.vwap
            below_vwap = not minute_analysis.price_above_vwap
            min_break_pct = getattr(ew, "vwap_break_min_pct", 0.0)
            vwap_gap_pct = (
                (current_price - vwap) / vwap * 100 if vwap > 0 else 0.0
            )
            breaks_threshold = below_vwap and vwap_gap_pct <= -abs(min_break_pct)

            if in_grace_period or not breaks_threshold:
                # 유예시간 중이거나 이탈폭 기준 미달 → VWAP 위로 회복한
                # 것과 동일하게 취급, 연속 카운터 리셋
                self.state.vwap_break_streak_by_symbol[symbol] = 0
            else:
                streak = self.state.vwap_break_streak_by_symbol.get(symbol, 0) + 1
                self.state.vwap_break_streak_by_symbol[symbol] = streak
                confirm_count = getattr(ew, "vwap_break_confirm_count", 1)
                if streak >= confirm_count:
                    self._start_entry_watch_shadow_tracking(
                        symbol, position, avg, current_price, ew, "VWAP이탈청산",
                    )
                    return Signal(
                        type=SignalType.SELL,
                        reason=(
                            f"entry_watch VWAP이탈청산 — 매수 후 {elapsed_min:.1f}분, "
                            f"수익률 {pnl_pct:+.2f}%, VWAP {vwap:,.0f}원 아래 "
                            f"({vwap_gap_pct:+.2f}%, {streak}/{confirm_count}회 연속 확인)"
                        ),
                    )

        # 3) watch_minutes 경과 시점에 최소수익 미달 청산
        if elapsed_min >= ew.watch_minutes and pnl_pct < ew.min_profit_pct:
            self._start_entry_watch_shadow_tracking(
                symbol, position, avg, current_price, ew, "최소수익미달청산",
            )
            return Signal(
                type=SignalType.SELL,
                reason=(
                    f"entry_watch 최소수익미달청산 — 매수 후 {elapsed_min:.1f}분, "
                    f"수익률 {pnl_pct:+.2f}% (기준 {ew.min_profit_pct:+.1f}% 미달)"
                ),
            )

        return None

    def _start_entry_watch_shadow_tracking(
        self, symbol: str, position, entry_price: int, trigger_price: int,
        ew, trigger_type: str,
    ) -> None:
        """entry_watch가 청산한 종목의 counterfactual 추적을 시작합니다.

        "entry_watch가 개입 안 했다면 어떻게 됐을지"를 실제로 계속
        관찰해서 5/10/20분 체크포인트마다 기록합니다. 같은 종목이 이미
        추적 중이면(예: 짧은 시간 내 재진입 후 다시 청산) 기존 추적을
        덮어씁니다 — 가장 최근 개입의 효과를 보는 게 목적이므로.
        """
        if entry_price <= 0:
            return
        # 방어적 초기화 — __init__을 거치지 않고 만들어진 인스턴스(단위테스트
        # 등에서 TradingService.__new__로 속성만 채워 쓰는 경우) 대응
        if not hasattr(self, "_entry_watch_shadow_tracking"):
            self._entry_watch_shadow_tracking: dict[str, dict] = {}
        actual_pnl_pct = (trigger_price - entry_price) / entry_price * 100
        self._entry_watch_shadow_tracking[symbol] = {
            "trigger_at": datetime.now(),
            "trigger_type": trigger_type,
            "entry_price": entry_price,
            "trigger_price": trigger_price,
            "actual_pnl_pct": actual_pnl_pct,
            "checkpoints_done": set(),
        }

    def _check_entry_watch_shadow_checkpoints(self, symbol: str, current_price: int) -> None:
        """추적 중인 종목의 체크포인트(5/10/20분) 도달 여부를 확인하고 기록합니다.

        매 폴링마다 호출됩니다. 이 종목이 추적 대상이 아니면 즉시 반환.
        모든 체크포인트(20분)를 다 기록하면 추적을 종료합니다. 종목이
        재매수되어 실제 보유 중이어도 추적 자체는 별도로 계속됩니다 —
        "그때 안 팔았다면"이라는 가정 자체가 재매수와 무관한 반사실적
        질문이기 때문입니다.
        """
        tracking = self._entry_watch_shadow_tracking.get(symbol)
        if tracking is None:
            return

        checkpoints = (5, 10, 20)
        elapsed_min = (datetime.now() - tracking["trigger_at"]).total_seconds() / 60
        entry_price = tracking["entry_price"]

        for cp in checkpoints:
            if cp in tracking["checkpoints_done"]:
                continue
            if elapsed_min < cp:
                continue

            counterfactual_pnl_pct = (
                (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
            )
            effect_pct = counterfactual_pnl_pct - tracking["actual_pnl_pct"]

            self.entry_watch_shadow_logger.append({
                "trigger_at": tracking["trigger_at"].isoformat(),
                "symbol": symbol,
                "trigger_type": tracking["trigger_type"],
                "entry_price": entry_price,
                "trigger_price": tracking["trigger_price"],
                "actual_pnl_pct": round(tracking["actual_pnl_pct"], 3),
                "checkpoint_min": cp,
                "checkpoint_price": current_price,
                "counterfactual_pnl_pct": round(counterfactual_pnl_pct, 3),
                "entry_watch_effect_pct": round(effect_pct, 3),
            })
            tracking["checkpoints_done"].add(cp)

        if tracking["checkpoints_done"] >= set(checkpoints):
            self._entry_watch_shadow_tracking.pop(symbol, None)

    def _sync_position_state_machine_shadow(self, balance: AccountBalance) -> None:
        """포지션 상태머신(shadow)을 브로커 잔고로 동기화하고 불변조건을 검사합니다.

        shadow 모드 — 여기서 나온 판정은 실제 매매 로직에 아직 반영되지
        않습니다. 목적은 두 가지: (1) 실제 운영 데이터로 상태머신 자체가
        올바르게 동작하는지 검증, (2) POSITION_STATE_MISMATCH가 실제로
        얼마나 자주 발생하는지 관찰.

        PENDING 중인 종목은 건드리지 않음 — on_buy_result/on_sell_result가
        명시적으로 전이시키는 게 원칙이므로, 여기서 강제로 잔고 동기화하면
        PENDING 로직과 충돌함.
        """
        psm = self._position_state_machine

        # 2026-07-24 (12차 수정, GPT 코드리뷰): acknowledge_error()가
        # 실전에서 실질적으로 호출 불가능한 문제 — 이 프로세스는 계속
        # 도는 백그라운드 asyncio 프로세스라 REPL/디버거 연결 없이는
        # 실행 중에 이 메서드를 부를 방법이 없었음. 사람이 사용할 수
        # 있는 유일한 접점(파일 시스템)을 통해 명령을 전달하는 가장
        # 단순한 방식으로 연결 — commands/ack_error_{symbol}.json
        # 파일이 있으면 다음 폴링에서 읽어 처리하고 즉시 삭제(중복
        # 처리 방지). 파일 형식:
        #   {"broker_quantity": 100, "note": "HTS 직접 확인, 실제 100주"}
        self._process_pending_ack_error_commands()

        # 프로세스(또는 이번 shadow 기능) 최초 실행 시 브로커 실제 잔고
        # 기준으로 초기화 — state.json 등 영속화된 옛 상태를 신뢰하지 않음
        if not self._position_state_machine_initialized:
            for pos in balance.positions:
                psm.sync_from_broker(pos.symbol, pos.quantity)
            self._position_state_machine_initialized = True
            return

        broker_qty_by_symbol = {p.symbol: p.quantity for p in balance.positions}

        # targets + 이미 상태머신이 알고 있는 종목 전체를 대상으로 검사
        # (targets에서 빠졌어도 이전에 보유했던 종목은 계속 추적해야 함)
        symbols_to_check = set(self.targets) | set(psm._states.keys()) | set(broker_qty_by_symbol.keys())

        for symbol in symbols_to_check:
            broker_qty = broker_qty_by_symbol.get(symbol, 0)
            state = psm.get(symbol)

            # 불변조건 검사 — PENDING 상태는 정상적으로 일시 불일치가
            # 날 수 있으므로 검사 대상에서 제외
            violation = psm.check_invariant(symbol, broker_qty)
            if violation:
                self.app_logger.critical(f"[POSITION_STATE_MISMATCH][SHADOW] {violation}")

            # PENDING이 아닌 종목만 잔고로 재동기화 (다음 폴링을 위한 갱신)
            if state.lifecycle == PositionLifecycle.SELL_PENDING:
                # 매도 요청 다음 폴링 — 브로커 잔고를 다시 조회해 실제로
                # 전량/부분/미체결인지 판정 (on_sell_result 내부 로직)
                psm.on_sell_result(symbol, accepted=True, broker_quantity=broker_qty)
            elif state.lifecycle == PositionLifecycle.BUY_PENDING:
                # 2026-07-23: 매도와 대칭 — 매수 요청 다음 폴링에서
                # 브로커 잔고를 다시 조회해 실제로 체결됐는지 확인.
                # 기존엔 이 분기가 없어서 BUY_PENDING을 그냥 지나쳤음
                # (매수는 _try_buy 안에서 즉시 확정했었으므로).
                psm.confirm_buy_from_broker(symbol, broker_qty)
            else:
                psm.sync_from_broker(symbol, broker_qty)

            # 2026-07-24 (9차 수정, GPT 코드리뷰): 이상치(예상 밖 수량)
            # 감지 시 ERROR로 전이되는데, 이게 position_lifecycle.csv
            # 에만 남으면 놓치기 쉬움 — POSITION_STATE_MISMATCH와
            # 동일하게 app.log에도 CRITICAL로 노출해 실시간으로
            # 눈에 띄게 함. 사람이 실제 계좌를 확인해야 하는 상황.
            # ERROR는 sync_from_broker()에서도 자동 복구되지 않고
            # 계속 유지되므로(위 수정 참고), 새로 진입했을 때뿐 아니라
            # 이미 ERROR 상태로 남아있는 매 폴링에도 반복 알림 —
            # 사람이 확인하기 전까지 계속 눈에 띄어야 하므로.
            if psm.get(symbol).lifecycle == PositionLifecycle.ERROR:
                self.app_logger.critical(
                    f"[POSITION_STATE_ERROR][SHADOW] {symbol} | "
                    f"{psm.get(symbol).last_error} | broker_qty={broker_qty} — "
                    f"실제 계좌 상태를 확인하세요"
                )

    def _process_pending_ack_error_commands(self) -> None:
        """commands/ack_error_{symbol}.json 파일을 확인해 ERROR 상태를 정정합니다.

        2026-07-24 (12차 수정, GPT 코드리뷰): PositionStateMachine.
        acknowledge_error()는 실제 매매 로직과 무관하게 관찰용
        상태를 정정하는 안전한 메서드지만, 이 프로세스가 계속 도는
        백그라운드 asyncio 프로세스라 REPL/디버거 연결 없이는 실행
        중에 호출할 방법이 없었음. 파일 시스템을 통한 명령 전달로
        연결.

        사용법 (PowerShell):
            $body = @{ broker_quantity = 100; note = "HTS 직접 확인" } | ConvertTo-Json
            $body | Out-File -Encoding utf8 commands\\ack_error_475150.json

        파일이 있으면 다음 폴링(최대 poll_interval_seconds 이내)에서
        읽어 처리하고 즉시 삭제합니다 — 중복 처리 방지. 처리 결과는
        app.log와 position_lifecycle.csv에 모두 남습니다. broker_
        quantity가 음수이거나 note가 없으면 처리하지 않고 오류
        로그만 남긴 뒤 파일을 삭제합니다(같은 잘못된 파일이 매
        폴링마다 반복 실패하는 것을 막기 위해 — 대신 오류 사유를
        app.log에서 확인 가능).
        """
        commands_dir = Path("commands")
        if not commands_dir.is_dir():
            return
        # 2026-07-24 (13차 수정): Path.glob()은 파일시스템 순회 순서에
        # 의존하며 정렬을 보장하지 않음 — 여러 명령 파일이 동시에
        # 있을 때 처리/로그 순서가 예측 불가능해지는 것을 막기 위해
        # 정렬해서 순회(파일명은 종목코드가 포함돼 있어 정렬하면
        # 자연스럽게 종목코드순으로 처리됨).
        for cmd_file in sorted(commands_dir.glob("ack_error_*.json")):
            symbol = cmd_file.stem[len("ack_error_"):]
            try:
                payload = json.loads(cmd_file.read_text(encoding="utf-8"))
                broker_quantity = int(payload["broker_quantity"])
                note = str(payload["note"])
                self._position_state_machine.acknowledge_error(symbol, broker_quantity, note)
                self.app_logger.warning(
                    f"[ACK_ERROR_COMMAND] {symbol} | 파일 명령으로 ERROR 해제 — "
                    f"broker_quantity={broker_quantity}, note={note!r}"
                )
            except Exception as exc:
                # 2026-07-24 (13차 수정, GPT 코드리뷰): 기존엔
                # (OSError, JSONDecodeError, KeyError, ValueError,
                # TypeError)만 나열해서 잡았음 — finally가 있어서
                # "파일이 삭제된다"는 보장은 됐지만, 나열 안 된 예외
                # 타입이 나오면 이 함수 자체가 예외를 던지며 중단돼
                # for 루프 뒤에 남은 다른 명령 파일들은 이번 폴링에서
                # 처리가 안 되고, run_once() 전체가 이 폴링 사이클을
                # 통째로 실패할 위험이 있었음. acknowledge_error()가
                # 앞으로 새로운 예외 타입을 던지게 바뀌거나
                # PositionStateMachine 내부 구현이 달라져도 이 함수
                # 하나 때문에 전체 루프가 끊기면 안 되므로, Exception
                # 전체(KeyboardInterrupt/SystemExit는 BaseException만
                # 상속해 여기 안 걸림, 의도적으로 유지)로 범위를 넓힘.
                self.app_logger.error(
                    f"[ACK_ERROR_COMMAND] {symbol} | 명령 파일 처리 실패, 무시하고 "
                    f"삭제합니다: {exc} — 파일을 다시 만들어 재시도하세요"
                )
            finally:
                cmd_file.unlink(missing_ok=True)

    def _run_end_of_day_tasks(self, now: datetime) -> None:
        """장 마감 후 작업 (리포트/검증). run_once 밖에서도 호출 가능."""
        if now.hour == 15 and now.minute >= 20 and not self._report_generated_today:
            self.app_logger.info("━" * 45)
            self.app_logger.info("  🔔 장 마감 (15:20) — 매매 종료")
            self.app_logger.info("  보유 포지션은 다음날로 이월됩니다.")
            self.app_logger.info("━" * 45)
            self._generate_daily_report()
            self._report_generated_today = True
            self.app_logger.info("[REPORT] 일일 리포트 생성 완료")
            self._validate_logs_today(now.date())
            self._run_signal_analysis_today(now.date())
            self._run_trade_analysis_today(now.date())
            self._run_indicator_analysis_today(now.date())
            self._run_replay_today(now.date())
            self._run_bb_block_impact_today(now.date())

    def _run_signal_analysis_today(self, target_date) -> None:
        """장 마감 후 시그널 분석을 자동 실행합니다."""
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "analyze_signal_log.py", date_str],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[ANALYSIS] 시그널 분석 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[ANALYSIS] 시그널 분석 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[ANALYSIS] 시그널 분석 오류: {exc}")

    def _run_trade_analysis_today(self, target_date) -> None:
        """장 마감 후 거래 분석을 자동 실행합니다."""
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "analyze_trades.py", date_str],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[ANALYSIS] 거래 분석 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[ANALYSIS] 거래 분석 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[ANALYSIS] 거래 분석 오류: {exc}")

    def _run_indicator_analysis_today(self, target_date) -> None:
        """장 마감 후 ATR/볼린저 지표 분석을 자동 실행합니다."""
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "analyze_indicators.py", date_str],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[ANALYSIS] 지표(ATR/볼린저) 분석 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[ANALYSIS] 지표 분석 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[ANALYSIS] 지표 분석 오류: {exc}")

    def _run_bb_block_impact_today(self, target_date) -> None:
        """장 마감 후 볼린저 상단돌파 차단 가상 성과 분석을 자동 실행합니다."""
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "analyze_bb_block_impact.py", date_str],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[ANALYSIS] 볼린저 차단 영향 분석 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[ANALYSIS] 볼린저 차단 영향 분석 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[ANALYSIS] 볼린저 차단 영향 분석 오류: {exc}")

    def _run_replay_today(self, target_date) -> None:
        """장 마감 후 리플레이를 자동 실행합니다."""
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "replay_runner.py", date_str],
                capture_output=True, text=True, timeout=120,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[REPLAY] 리플레이 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[REPLAY] 리플레이 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[REPLAY] 리플레이 실행 오류: {exc}")

    def _validate_logs_today(self, target_date) -> None:
        """장 마감 후 로그 품질을 자동 검증하고 결과를 app.log에 기록합니다."""
        # signal_log / trades.csv 검증
        try:
            from validate_logs import check_signal_log, check_trades_log
            e1, w1 = check_signal_log(target_date)
            e2, w2 = check_trades_log(target_date)
            total_errors   = e1 + e2
            total_warnings = w1 + w2
            if total_errors == 0 and total_warnings == 0:
                self.app_logger.info("[VALIDATE] 로그 품질 검사 통과 ✅")
            else:
                self.app_logger.warning(
                    f"[VALIDATE] 로그 품질 검사 — 오류 {total_errors}건 / 경고 {total_warnings}건"
                )
        except Exception as exc:
            self.app_logger.warning(f"[VALIDATE] 로그 품질 검사 실패: {exc}")

        # 1분봉 저장 품질 검증
        try:
            from validate_minute_bars import validate as validate_bars
            report = validate_bars(target_date)
            has_error = "❌" in report
            has_warn  = "⚠️" in report
            if not has_error and not has_warn:
                self.app_logger.info("[VALIDATE] 1분봉 품질 검사 통과 ✅")
            elif has_error:
                self.app_logger.warning("[VALIDATE] 1분봉 품질 검사 — 오류 발견 (reports/ 확인)")
            else:
                self.app_logger.warning("[VALIDATE] 1분봉 품질 검사 — 경고 발견 (reports/ 확인)")
            # 결과를 reports/에 저장
            from pathlib import Path
            Path("reports").mkdir(exist_ok=True)
            Path(f"reports/minute_bar_quality_{target_date.strftime('%Y%m%d')}.txt").write_text(
                report, encoding="utf-8"
            )
        except Exception as exc:
            self.app_logger.warning(f"[VALIDATE] 1분봉 품질 검사 실패: {exc}")

    def _try_buy(
        self,
        symbol: str,
        current_price: int,
        balance: AccountBalance,
        signal=None,
        regime=None,
        minute_analysis=None,
    ) -> str:
        """매수 주문 가능 여부를 검사한 뒤 실제 주문을 시도합니다.
        반환값: 차단 사유 문자열 (차단 없으면 빈 문자열)
        """

        # ── excluded_symbols 차단 ───────────────────────────────
        excluded = getattr(self.settings.trading, 'excluded_symbols', [])
        if symbol in excluded:
            self.app_logger.info(
                f"[EXCL] {symbol} | excluded_symbols 차단"
            )
            # signal_log 기록은 호출부(_try_buy 호출 지점)에서
            # order_block_reason="EXCLUDED_SYMBOL"로 한 번만 남긴다.
            # (2026-07-09: 여기서 한 번 더 기록하던 걸 제거 — 동일 이벤트가
            #  SKIP_EXCLUDED_SYMBOL / EXCLUDED_SYMBOL 두 줄로 중복 집계되던 버그)
            return "EXCLUDED_SYMBOL"

        # ── 동일 종목 1일 1회 진입 제한 (allow_multi=False일 때만) ────
        allow_multi = getattr(
            self.settings.trading,
            'allow_multiple_entries_per_symbol_per_day',
            True,
        )
        if not allow_multi:
            entry_cnt = self.state.symbol_entry_count_today.get(symbol, 0)
            if entry_cnt >= 1:
                self.app_logger.info(
                    f"[ENTRY_LIMIT] {symbol} | 당일 {entry_cnt}회 진입 완료 "
                    f"— 1일 1회 제한으로 신규매수 차단"
                )
                return "DAILY_ENTRY_LIMIT"

        # ── 종목당 일일 최대 진입 횟수 상한 (2026-07-16) ────────────
        # allow_multi=True(현재값)라도 재진입 자체는 무제한이 아니어야 함.
        # 7/15 475150 사례: 손실 없이 계속 상승하는 종목에 재진입이
        # 반복되어(7회) 41,766,000원(order_cash_per_trade x7)이 하루 만에
        # 한 종목에 쏠림 — 손실 2회 게이트는 "손실이 나야" 작동하므로
        # 계속 수익 중인 추격매수는 못 막았음. 상한을 걸어 과집중 방지.
        max_entries = getattr(
            self.settings.trading,
            'max_entries_per_symbol_per_day',
            3,
        )
        entry_cnt = self.state.symbol_entry_count_today.get(symbol, 0)
        if entry_cnt >= max_entries:
            self.app_logger.info(
                f"[ENTRY_LIMIT] {symbol} | 당일 {entry_cnt}회 진입 완료 "
                f"— 일일 최대 {max_entries}회 상한으로 신규매수 차단"
            )
            return "MAX_ENTRIES_PER_DAY"

        # ── 시간대 제한 — 14:50 이후 신규매수 차단 ─────────────
        _now = datetime.now()
        if _now.hour > 14 or (_now.hour == 14 and _now.minute >= 50):
            self.app_logger.info(
                f"[BLOCK] {symbol} | 14:50 이후 신규매수 차단 "
                f"({_now.strftime('%H:%M')})"
            )
            return "AFTER_1450"

        # ── BUY 신호 종목별 쿨다운 (10분) ────────────────────────
        last_buy_sig = self._last_buy_signal_at.get(symbol)
        if last_buy_sig:
            elapsed_sig = (datetime.now() - last_buy_sig).total_seconds()
            if elapsed_sig < 600:  # 10분
                remaining_sig = int(600 - elapsed_sig)
                self.app_logger.debug(
                    f"[BUY_COOL] {symbol} | BUY 신호 쿨다운 중 ({remaining_sig}초 남음)"
                )
                # 2026-07-22: bare return(=None 반환)이었음 — 다른 차단
                # 사유들은 전부 문자열을 반환하는데 이것만 None이라 호출부
                # signal_log 기록에서 사유가 빈 값으로 남았을 것(GPT 검토로
                # 발견). 다른 사유들과 일관되게 명시적 문자열로 정정.
                return "BUY_SIGNAL_COOLDOWN"

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
                return "REENTRY_COOLDOWN"

        # ── 종목별 재진입 제한 체크 ──────────────────────────────
        now_dt = datetime.now()

        # 0) NEUTRAL 손절 발생 → 당일 매수 완전 금지
        if symbol in self.state.symbol_block_today:
            self.app_logger.info(
                f"[SYMBOL_BLOCK_TODAY] {symbol} "
                f"reason=NEUTRAL_STOPLOSS_BLOCK "
                f"block_buy=true"
            )
            return "NEUTRAL_STOPLOSS_BLOCK"

        # 1) 당일 손실 2회 이상 → 당일 매수 금지
        loss_cnt = self.state.symbol_loss_count_today.get(symbol, 0)
        if loss_cnt >= 2:
            self.app_logger.info(
                f"[SYMBOL_BLOCK_TODAY] {symbol} "
                f"reason=DAILY_LOSS_{loss_cnt} "
                f"loss_count_today={loss_cnt} "
                f"block_buy=true"
            )
            return "DAILY_LOSS_LIMIT"

        # 2) 손절 발생 → 30분 매수 금지
        stoploss_str = self.state.symbol_stoploss_at.get(symbol)
        if stoploss_str:
            elapsed_sl = (now_dt - datetime.fromisoformat(stoploss_str)).total_seconds()
            if elapsed_sl < 1800:  # 30분
                remaining_sl = int(1800 - elapsed_sl)
                self.app_logger.info(
                    f"[SYMBOL_COOLDOWN] {symbol} "
                    f"reason=STOPLOSS "
                    f"cooldown_until={(now_dt + __import__('datetime').timedelta(seconds=remaining_sl)).strftime('%H:%M')} "
                    f"loss_count_today={loss_cnt} "
                    f"block_buy=true"
                )
                return "STOPLOSS_COOLDOWN"

        # 3) 트레일링 손실 2회 이상 → 60분 매수 금지
        trail_list = self.state.symbol_trail_loss_at.get(symbol, [])
        recent_trail = [
            t for t in trail_list
            if (now_dt - datetime.fromisoformat(t)).total_seconds() < 3600
        ]
        trail_loss_threshold = getattr(
            self.settings.trading, 'trail_loss_cooldown_count', 2
        )
        if len(recent_trail) >= trail_loss_threshold:
            oldest = min(datetime.fromisoformat(t) for t in recent_trail)
            remaining_tr = int(3600 - (now_dt - oldest).total_seconds())
            self.app_logger.info(
                f"[SYMBOL_COOLDOWN] {symbol} "
                f"reason=TRAILING_LOSS_{len(recent_trail)} "
                f"cooldown_until={(now_dt + __import__('datetime').timedelta(seconds=remaining_tr)).strftime('%H:%M')} "
                f"loss_count_today={loss_cnt} "
                f"block_buy=true"
            )
            return "TRAIL_LOSS_COOLDOWN"

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
            # 2026-07-22: 여기 반환값이 "STOPLOSS_COOLDOWN"으로 잘못
            # 고정되어 있었음 — 실제 사유(최대보유종목수)와 무관한 문자열이라
            # signal_analysis의 BLOCKED 사유 분포를 왜곡시켜 왔음(GPT 검토로
            # 발견). SkipReason.MAX_POSITIONS로 정정.
            from infra.storage.skip_reason import SkipReason
            return SkipReason.MAX_POSITIONS
        quantity = max(1, self.settings.trading.order_cash_per_trade // current_price)
        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            price=current_price,
        )

        can_order, reason = self.risk_manager.can_place_order(order, live_balance, self.state)

        if not can_order:
            self.app_logger.warning(
                f"[BLOCK] {symbol} | 매수 조건 충족했지만 주문 미실행 | 사유: {reason}"
            )
            # 2026-07-22: 여기 반환값이 "TRAIL_LOSS_COOLDOWN"으로 무조건
            # 고정되어 있었음. RiskManager.can_place_order()는 이미
            # SkipReason(ALREADY_HOLDING/MAX_POSITIONS/RISK_LIMIT/
            # DAILY_LOSS_LIMIT 등) 상수로 정확한 사유를 반환하는데, 그
            # reason을 무시하고 항상 같은 문자열로 덮어써서 이 경로를 거친
            # BLOCKED 사유가 전부 "트레일링 손실 쿨다운"으로 잘못 집계되고
            # 있었음(GPT 검토로 발견 — signal_analysis 리포트 왜곡 원인).
            # reason을 그대로 반환하도록 정정.
            return reason

        # ── 포지션 상태머신 shadow 통지 (2026-07-22→23) ──────────────
        # BUY_PENDING → OPEN 실체결 확인: 이전엔 accepted=True이면
        # 여기서 바로 OPEN으로 확정했는데(요청 수량을 그대로 신뢰),
        # 이제 accepted 여부만 알리고 BUY_PENDING을 유지 — 실제 체결
        # 확인은 다음 폴링의 confirm_buy_from_broker()가 담당(SELL과
        # 대칭 구조, GPT 제안 반영).
        self._position_state_machine.on_buy_requested(symbol, order.quantity, "pending")
        result = self.broker.place_order(order)
        self._position_state_machine.on_buy_result(symbol, result.accepted)

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
            self._last_buy_signal_at[symbol] = datetime.now()

            # ── _sold_today 플래그 즉시 해제 (2026-07-21 긴급수정) ──────
            # _sold_today는 "매도 접수 직후 브로커 API 잔고 미반영으로 인한
            # 중복 매도 시도"를 막기 위한 임시 플래그인데(7.4절 참고),
            # 날짜가 바뀔 때만 초기화되도록 되어 있어 같은 날 재매수가
            # 성공한 뒤에도 계속 남아있었음. _process_symbol()의 position
            # 판정 로직이 "symbol in _sold_today면 무조건 position=None"으로
            # 처리하기 때문에, 재매수로 실제 보유 중인데도 전략이 계속
            # "미보유"로 오판 — 손절/트레일링 판단(보유 중 분기) 자체가
            # 전혀 실행되지 않는 상태가 됨.
            # 7/21 475150 사례: 09:23 트레일링 익절(103주) → 09:59/10:18
            # 재매수(201주) 성공 → _sold_today는 그대로 남아 있어서 이후
            # 계속 매수신호(BUY)만 내고 손절이 3시간 넘게 아예 판단되지
            # 않음. 손절가(-1.5%)를 훨씬 지나 -4.48%까지 방치됨.
            # 재매수가 성공했다는 건 브로커가 이미 새 포지션을 인지했다는
            # 뜻이므로, 이 시점에 바로 플래그를 지워도 원래 목적(직후 짧은
            # 지연 방어)에는 영향이 없음.
            # (2026-07-22: 수량기반 판정으로 전환하면서 이 discard가 없어도
            # 자동으로 안전해짐 — 재매수로 수량이 스냅샷과 달라지면 판정
            # 로직이 알아서 실제 잔고를 신뢰함. 다만 이중 안전장치 겸
            # 스냅샷 dict가 무한히 쌓이지 않도록 여기서도 정리.)
            if hasattr(self, '_sold_today'):
                self._sold_today.discard(symbol)
            if hasattr(self, '_sold_today_qty_snapshot'):
                self._sold_today_qty_snapshot.pop(symbol, None)

            # ── 볼린저 상단 돌파 매수 경고 (2026-06-30 추가) ──────────
            # 볼린저 %B > 1.0(상단 돌파)에서의 매수는 추격매수 위험이 높음.
            # 차단하지 않고 경고만 남겨 추적(어떤 결과로 이어지는지 데이터 수집).
            _ind = self._last_indicators.get(symbol, {})
            _bb  = _ind.get("bb")
            if _bb is not None and getattr(_bb, "percent_b", None) is not None:
                if _bb.percent_b > 1.0:
                    self.app_logger.warning(
                        f"[BB_WARN] {symbol} | 볼린저 상단 돌파 매수 "
                        f"(%B={_bb.percent_b:.2f}) — 추격매수 위험 구간"
                    )
                elif _bb.percent_b >= 0.8:
                    self.app_logger.info(
                        f"[BB_WARN] {symbol} | 볼린저 상단 근처 매수 "
                        f"(%B={_bb.percent_b:.2f})"
                    )
            # 1일 1회 진입 제한용 카운터
            self.state.symbol_entry_count_today[symbol] = (
                self.state.symbol_entry_count_today.get(symbol, 0) + 1
            )

            # 주문 성공 후 잔고 캐시 무효화
            self.cached_balance = None
            self.cached_balance_loaded_at = None

            self.app_logger.info(
                f"[ORDER] {symbol} | 매수 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
            self._notifier.send(
                f"🟢 [매수] {symbol}\n"
                f"가격: {current_price:,}원 | 수량: {quantity}주\n"
                f"금액: {current_price * quantity:,}원\n"
                f"장세: {regime.value if regime else '-'} | 점수: {signal.reason[:30]}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매수 주문 실패 | 사유: {result.message}"
            )
            # ── 영구적 실패 사유는 당일 재시도 차단 (2026-07-06) ──────
            # RC4007(매매제한 종목) 등은 재시도해도 항상 실패함.
            # 252670이 09:00~15:11까지 계속 재시도되며 실패 163건을 만든 원인.
            # 일시적 사유(수량부족, 네트워크 등)는 재시도 여지를 남기기 위해
            # 제외하고, 명백히 영구적인 사유만 골라서 차단한다.
            _permanent_fail_keywords = [
                "매매제한", "RC4007",       # 매매제한 종목
                "거래정지", "관리종목",       # 거래정지/관리종목
                "상장폐지",                  # 상장폐지
            ]
            _msg = result.message or ""
            if any(kw in _msg for kw in _permanent_fail_keywords):
                if symbol not in self._excluded_symbols:
                    self._excluded_symbols.add(symbol)
                    self.app_logger.warning(
                        f"[EXCL] {symbol} | 영구적 매수 실패 사유로 당일 재시도 차단 "
                        f"— {_msg}"
                    )

    def _try_sell(
        self,
        symbol: str,
        quantity: int,
        current_price: int = 0,
        exit_reason: str = "",
        avg_buy_price: int = 0,
    ) -> None:
        """매도 주문을 생성하고 브로커로 전달합니다."""
        order = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=quantity)
        # ── 포지션 상태머신 shadow 통지 (2026-07-22) ────────────────
        # on_sell_result는 여기서 바로 호출하지 않음 — "체결됐는지"는
        # 다음 폴링에서 브로커 잔고를 다시 조회해야 알 수 있으므로,
        # 실제 판정 흐름과 동일하게 _sync_position_state_machine_shadow가
        # 다음 폴링에서 처리하도록 SELL_PENDING만 표시.
        self._position_state_machine.on_sell_requested(symbol, quantity, "pending")
        result = self.broker.place_order(order)
        if not result.accepted:
            self._position_state_machine.on_sell_result(symbol, accepted=False, broker_quantity=0)

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
            context={
                "exit_reason": exit_reason,
                "hold_minutes": hold_minutes,
                "avg_buy_price": avg_buy_price,
                "condition_name": self._symbol_to_condition.get(symbol, ""),
            },
        )

        if result.accepted:
            # 주문 성공 후 잔고 캐시 무효화
            self.cached_balance = None
            self.cached_balance_loaded_at = None

            # 매도 완료 종목 기록 — 모의투자 API가 즉시 수량을 반영 안 할 류에
            # 다음 포링 사이클에서 같은 종목을 또 매도 시도하는 것을 방지
            if not hasattr(self, '_sold_today'):
                self._sold_today: set[str] = set()
            self._sold_today.add(symbol)

            # ── 수량 스냅샷 기록 (2026-07-22) ──────────────────────
            # 매도가 실제로 반영되면 브로커는 해당 종목을 잔고 목록에서
            # 아예 제거한다(quantity=0으로 남기지 않음 — MockBroker와
            # 실제 키움 API 모두 이 방식). 따라서 다음 폴링에서 여전히
            # "매도 시도 당시와 같은 수량"으로 잔고에 남아있다면 그건
            # API가 아직 매도를 반영하지 못한 것(진짜 지연), 수량이
            # 달라졌다면(=신규 포지션) 재매수가 체결된 것이므로 그
            # 잔고를 그대로 신뢰해야 함 — quantity 인자가 곧 매도 시도
            # 당시 보유수량(이 시스템은 항상 전량매도만 함).
            if not hasattr(self, '_sold_today_qty_snapshot'):
                self._sold_today_qty_snapshot: dict[str, int] = {}
            self._sold_today_qty_snapshot[symbol] = quantity

            # 매도 시각 기록 → 재진입 쿨다운에 사용
            now_iso = datetime.now().isoformat()
            self.state.last_sold_at_by_symbol[symbol] = now_iso
            self.state.entry_time_by_symbol.pop(symbol, None)

            # ── 재진입 제한 state 업데이트 ──────────────
            avg_p = avg_buy_price if avg_buy_price > 0 else 0
            sold_price = current_price
            is_loss = avg_p > 0 and sold_price < avg_p
            is_stoploss = "손절" in exit_reason
            is_trail_loss = "트레일링" in exit_reason and is_loss

            if is_loss:
                cnt = self.state.symbol_loss_count_today.get(symbol, 0) + 1
                self.state.symbol_loss_count_today[symbol] = cnt
                # ── 전역 연속손절 카운터는 '새로운 종목'의 손실만 반영 ──
                # 2026-06-15: 한 종목 반복매매(같은 종목 2회 이상 손실)가
                # 전역 max_consecutive_losses를 소진시켜 계좌 전체가
                # 차단되는 문제 발생(005930 3연속 손절 → 전체 매수 중단).
                # cnt==1(해당 종목 첫 손실)일 때만 전역 카운터 증가.
                # 같은 종목 반복 손실은 symbol_loss_count_today(종목별
                # DAILY_LOSS_LIMIT, 한도 2)로 별도 차단됨.
                if cnt == 1:
                    self.state.consecutive_losses += 1
                self.app_logger.info(
                    f"[LOSS_CNT] {symbol} | 당일 손실 {cnt}회 "
                    f"| 연속손절(전역) {self.state.consecutive_losses}회 "
                    f"| 수익률 {(sold_price - avg_p) / avg_p * 100 if avg_p else 0:+.2f}%"
                )
            else:
                # 수익 매도 시 연속손절 초기화
                if self.state.consecutive_losses > 0:
                    self.app_logger.info(
                        f"[CONSEC_RESET] {symbol} | 수익 매도 → 연속손절 초기화"
                    )
                self.state.consecutive_losses = 0
            if is_stoploss:
                self.state.symbol_stoploss_at[symbol] = now_iso
                cooldown_until = (datetime.now() + __import__('datetime').timedelta(seconds=1800)).strftime('%H:%M')
                self.app_logger.info(
                    f"[SYMBOL_COOLDOWN] {symbol} "
                    f"reason=STOPLOSS "
                    f"cooldown_until={cooldown_until} "
                    f"loss_count={self.state.symbol_loss_count_today.get(symbol, 0)}"
                )
                # ── NEUTRAL 손절 → 당일 재진입 완전 금지 ──────────
                is_neutral_stoploss = "[중립]" in exit_reason
                if is_neutral_stoploss:
                    self.state.symbol_block_today.add(symbol)
                    self.app_logger.info(
                        f"[SYMBOL_BLOCK_TODAY] {symbol} "
                        f"reason=NEUTRAL_STOPLOSS — "
                        f"NEUTRAL 손절 발생 → 당일 재진입 금지"
                    )
            if is_trail_loss:
                trail_list = self.state.symbol_trail_loss_at.get(symbol, [])
                trail_list.append(now_iso)
                # 최근 60분 이내 기록만 유지
                now_dt = datetime.now()
                trail_list = [
                    t for t in trail_list
                    if (now_dt - datetime.fromisoformat(t)).total_seconds() < 3600
                ]
                self.state.symbol_trail_loss_at[symbol] = trail_list
                if len(trail_list) >= 2:
                    cooldown_until = (datetime.now() + __import__('datetime').timedelta(seconds=3600)).strftime('%H:%M')
                    self.app_logger.info(
                        f"[SYMBOL_COOLDOWN] {symbol} "
                        f"reason=TRAILING_LOSS_{len(trail_list)} "
                        f"cooldown_until={cooldown_until} "
                        f"loss_count={self.state.symbol_loss_count_today.get(symbol, 0)}"
                    )

            self.app_logger.info(
                f"[ORDER] {symbol} | 매도 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
            # ── 카카오 알림 ──────────────────────────────
            if avg_buy_price > 0:
                pnl_pct = (current_price - avg_buy_price) / avg_buy_price * 100
                pnl_amt = int((current_price - avg_buy_price) * quantity)
                icon = "🔴" if pnl_amt < 0 else "🟡"
                self._notifier.send(
                    f"{icon} [매도] {symbol}\n"
                    f"가격: {current_price:,}원 | 수량: {quantity}주\n"
                    f"수익률: {pnl_pct:+.2f}% | 손익: {pnl_amt:+,}원\n"
                    f"사유: {exit_reason[:40]}"
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
            ctx["rebound_volume_ratio"]   = ma.rebound_volume_ratio
            ctx["change_rate_pct"]        = round(ma.change_rate_pct, 2)
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
        final_decision: str = "",
        order_block_reason: str = "",
        atr_result=None,
        bb_result=None,
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
            "skip_reason": classify_skip_reason(signal.reason, signal.type.value),
            "final_decision":    final_decision or signal.type.value,
            "order_block_reason": order_block_reason,
            "condition_name": self._symbol_to_condition.get(symbol, ""),
        }
        if minute_analysis is not None:
            ma = minute_analysis
            if ma.is_v_rebound:          patterns.append("V")
            if ma.is_pulldown_recovery:  patterns.append("PR")
            if ma.is_valid_change_rate:  patterns.append("A")
            if ma.is_valid_rebound:      patterns.append("B")
            if ma.is_valid_pulldown:     patterns.append("C")
            # 갭D: breakout_strategy의 cond_gap_pullback 결과를 signal.reason으로 판단
            if signal and "[갭D]" in signal.reason: patterns.append("D")
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
                "rebound_volume_ratio": ma.rebound_volume_ratio,
                "change_rate_pct": round(ma.change_rate_pct, 2),
                "v_bottom_spike":       ma.v_bottom_spike,
                "upside_to_recent_high_pct": ma.upside_to_recent_high_pct,
                "ma5_above_ma20":      ma.ma5_above_ma20,
                "v_fail_reason":       ma.v_fail_reason if not ma.is_v_rebound else "",
            })
        else:
            row["detected_patterns"] = "-"
        # ATR / 볼린저 (로그 전용)
        atr = atr_result or self._last_indicators.get(symbol, {}).get('atr')
        bb  = bb_result  or self._last_indicators.get(symbol, {}).get('bb')
        row.update({
            "atr_14":          round(atr.atr, 2) if atr else "",
            "atr_14_pct":      round(atr.atr_pct, 3) if atr else "",
            "bb_percent_b":    round(bb.percent_b, 4) if bb else "",
            "bb_bandwidth_pct": round(bb.bandwidth_pct, 2) if bb else "",
            "bb_position":     bb.position if bb else "",
        })
        self.signal_logger.append(row)