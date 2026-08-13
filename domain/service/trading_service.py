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
from domain.market_regime.minute_analyzer import MinuteAnalyzer, MinuteAnalysis, MinuteDataResult
from domain.market_regime.session_metrics import merge_session_bars, build_session_metrics, format_session_metrics_log_line
from domain.models import AccountBalance, MarketRegime, OrderRequest, OrderResult, OrderSide, Signal, SignalType
from domain.position.lifecycle import PositionLifecycle, PositionStateMachine
from domain.risk.risk_manager import RiskManager
from domain.strategy.strategy_router import StrategyRouter
from domain.strategy.entry_quality_shadow import evaluate_vwap_shadow
from infra.broker.base import Broker
from infra.storage.daily_reporter import DailyReporter
from infra.storage.logger import (
    AppLogger, TradeCsvLogger, SignalCsvLogger, EntryWatchShadowLogger, PositionLifecycleLogger,
    EntryQualityShadowLogger,
)
from infra.storage.minute_bar_saver import MinuteBarSaver
from infra.storage.skip_reason import classify_skip_reason, SkipReason
from infra.notify.kakao_notifier import KakaoNotifier, build_notifier
from domain.indicator.indicators import calc_atr, calc_bollinger, ATRResult, BollingerResult
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_market_open, now_kst, parse_kst_bar_timestamp


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
        entry_quality_shadow_logger: "EntryQualityShadowLogger | None" = None,
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
        # 2026-08-05 (1E.5단계): 동일 패턴 — None이면 storage 설정에서
        # 자동 생성. entry_quality_guard_mode="off"일 때는 이 로거가
        # 존재해도 실제로 append_if_new()가 호출되지 않으므로(호출부
        # 자체가 mode를 먼저 확인) 파일이 새로 만들어지지 않음.
        self.entry_quality_shadow_logger = entry_quality_shadow_logger or EntryQualityShadowLogger(
            settings.storage.entry_quality_shadow_log_file
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


        # 분봉 캐시 (단타 2차 필터용)
        self.cached_minute_bars: dict[str, list] = {}
        self.cached_minute_bars_loaded_at: dict[str, datetime] = {}
        # 2026-07-28 (GPT 코드리뷰 지적 5번): 성공 캐시 시각(loaded_at)
        # 과 별개로 "마지막 실패/빈응답/stale 시각"을 추적 — 실패가
        # 연속될 때 매 폴링(10초)마다 API를 계속 두드리지 않고 짧은
        # 백오프를 두기 위함(이전 HTTP 429 재발 방지 목적).
        self.cached_minute_bars_failed_at: dict[str, datetime] = {}

        # 2026-07-28 (1C단계, session_metrics_mode="shadow" 전용):
        # 종목별 당일 세션 상태 — {symbol: SessionState}. SessionState
        # 는 session_date(어느 거래일 것인지) + 필터를 통과한 봉만
        # 담긴 딕셔너리 + 필터링 카운터를 함께 보관 — merge_session_
        # bars()가 날짜가 바뀌면 자동으로 새 세션을 시작하므로,
        # reset_daily_loss_counts() 호출이 누락되어도 전일 세션이
        # 새 거래일 계산에 섞이지 않음(2차 GPT 코드리뷰 지적 3번).
        # 기존 cached_minute_bars(최근 60개 롤링, 매 갱신마다 통째로
        # 교체됨)와는 완전히 별개. session_metrics_mode가 "off"
        # (기본값)면 이 저장소는 채워지지 않고 완전히 비어있는 채로
        # 유지됨(불필요한 메모리 사용 없음).
        self._session_state_by_symbol: dict = {}
        # 2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 1단계):
        # _update_session_metrics_shadow()가 매번 build_session_
        # metrics()를 호출해 계산한 결과를 여기 캐시 — VWAP shadow
        # 관측(_write_signal_log)에서 같은 계산을 또 하지 않고
        # 이 최신값을 그대로 재사용. 로그 자체는 60초 스로틀이
        # 걸려도 이 캐시는 매 폴링마다 최신 상태로 갱신됨(로그
        # 출력 여부와 무관하게 metrics 계산 자체는 항상 일어나므로).
        # 일일 초기화(reset_daily_loss_counts) 시 함께 clear.
        self._latest_session_metrics_by_symbol: dict = {}

        # 장세 분류 결과 캐시
        self.cached_regime: dict[str, MarketRegime] = {}

        # HOLD 로그 throttle
        self.last_hold_log_at_by_symbol: dict[str, datetime] = {}
        self._last_buy_signal_at: dict[str, datetime] = {}  # 종목별 마지막 BUY신호 시각
        # 2026-08-05 (3차 GPT 코드리뷰 지적 P1, 재현 확인): 기존
        # entry_quality_shadow.csv의 actual_order_submitted가
        # final_decision=="BUY"로만 계산되고 있었는데, _try_buy()
        # 내부에서 실제로 broker.place_order()를 호출한 뒤 결과가
        # result.accepted=False(브로커가 거부)여도 명시적 block
        # 사유를 반환하지 않아서(암묵적으로 None 반환) final_
        # decision은 여전히 "BUY"로 남는 것을 확인 — "주문을
        # 시도했다"와 "브로커가 실제로 접수했다"가 이 필드
        # 하나로는 구분이 안 됐음.
        #
        # _try_buy()의 반환 시그니처(차단 사유 문자열 또는 빈
        # 문자열)는 test_order_block_reason.py 등 기존 회귀가 직접
        # 비교하므로 그대로 유지하고, 대신 place_order()를 실제로
        # 호출했을 때의 OrderResult를 이 저장소에 별도로 기록 —
        # _write_signal_log() 호출 시점에 여기서 조회해 order_
        # attempted/order_accepted/order_id/order_message를 정확히
        # 채움. 종목당 최근 1건만 필요하므로 매 시도마다 덮어씀.
        self._last_order_attempt_by_symbol: dict[str, OrderResult] = {}
        # 2026-08-05 (GPT 코드리뷰 지적, 5번): 기존엔 _symbol_to_
        # condition(단수형)을 update()로 별도 누적해서, _symbol_to_
        # conditions(복수형)는 스냅샷 교체로 고쳤는데도 단수형 대표
        # 값에는 편출된 종목의 과거 조건식명이 계속 남아 두 필드가
        # 서로 모순되는 상황(condition_name=옛값, condition_names=
        # 빈 문자열)이 생길 수 있었음. 이제 단수형은 별도 저장소로
        # 관리하지 않고, 필요할 때마다 복수형 스냅샷에서 결정적으로
        # 파생시킴(_representative_condition_name() 메서드 참고) —
        # 항상 같은 시점의 데이터에서 나오므로 모순이 생길 수 없음.
        self._symbol_to_conditions: dict[str, tuple[str, ...]] = {}
        # 2026-08-05 (GPT 코드리뷰 지적, 1번): 종목별로 조건식 출처가
        # CNSRREQ 초기 조회로 확정된 것인지(True), 실시간 이벤트로만
        # 알려져 불확실한지(False)를 추적 — ConditionWatcher.symbol_
        # condition_source_reliable을 그대로 스냅샷 교체.
        self._symbol_condition_source_reliable: dict[str, bool] = {}

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
        # 1P0.4: 종목별 마지막 강제 매도 시각(최소 간격 적용용)
        self._last_forced_sell_at: dict[str, datetime] = {}
        # 1P0.6: 강제 매도가 브로커에 거부된 연속 횟수(종목별)
        self._forced_sell_failures: dict[str, int] = {}
        # 1P0.7: SELL accepted~완전 청산 확정 사이 대기 중인 side-effect 컨텍스트
        self._pending_sell_side_effects: dict[str, dict] = {}

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
        sym_to_conditions: dict[str, tuple[str, ...]] | None = None,
        sym_to_reliable: dict[str, bool] | None = None,
    ) -> None:
        """조건검색 결과로 종목 목록을 동적으로 갱신합니다.

        보유 중인 종목은 조건검색 편출 여부와 무관하게 항상 포함합니다.

        2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 1단계):
        sym_to_conditions(전체 조건식 목록)는 반드시 매번 통째로
        교체해야 함 — update()를 쓰면 이번 폴링에 편출된 종목의
        과거 조건식 이름이 dict에 그대로 남아, "지금 이 종목이
        눌림목 조건식에 편입돼 있는가"라는 질문에 거짓 True를
        답하게 됨(재현 확인: 1회차 눌림목 편입 후 2회차 매핑에서
        완전히 빠져도, update() 방식이면 과거 값이 남아있음).

        2026-08-05 (2차 GPT 코드리뷰 지적, 5번): sym_to_cond(단수형
        대표값) 파라미터는 하위 호환을 위해 시그니처에는 남기되
        더 이상 별도 저장소에 update()로 누적하지 않음 — 대표
        `condition_name`이 필요할 때는 _representative_condition_
        name()이 sym_to_conditions 스냅샷에서 그때그때 결정적으로
        파생시킴. 이렇게 해야 "condition_name=옛 조건식, condition_
        names=빈 문자열"처럼 두 필드가 서로 모순되는 상황이 생기지
        않음(같은 시점의 같은 데이터에서 파생되므로).

        sym_to_conditions가 None으로 전달되면(예: 조건검색 자체가
        비활성이거나 이번 폴링에 결과가 없는 경우) 기존 저장소를
        비우지 않고 그대로 유지 — "결과가 없다"와 "전부 편출됐다"
        는 다른 신호이므로, 호출부가 명시적으로 빈 dict({})를
        넘겨야 실제로 전량 편출로 처리됨. sym_to_reliable도 동일
        원칙.
        """
        if sym_to_conditions is not None:
            self._symbol_to_conditions = dict(sym_to_conditions)
        if sym_to_reliable is not None:
            self._symbol_condition_source_reliable = dict(sym_to_reliable)
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

    def _representative_condition_name(self, symbol: str) -> str:
        """종목의 대표 조건식 이름을 현재 스냅샷에서 결정적으로 파생시킵니다.

        2026-08-05 (GPT 코드리뷰 지적, 5번): 기존엔 이 값을 별도
        저장소(_symbol_to_condition)에 update()로 누적해서, 편출된
        종목의 과거 조건식명이 잔존해 "condition_name=옛 조건식,
        condition_names=빈 문자열"처럼 두 필드가 서로 모순되는
        상황이 생길 수 있었음. 이제 항상 _symbol_to_conditions
        (같은 시점의 스냅샷)에서 파생시키므로 모순 자체가 구조적
        으로 불가능함 — 대표값을 정렬 순서로 고정해 호출마다
        같은 종목에 대해 항상 같은 대표값이 나오도록 함(결정적).
        """
        names = self._symbol_to_conditions.get(symbol, ())
        if not names:
            return ""
        return sorted(names)[0]

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

    def _get_minute_analysis(self, symbol: str, prev_close: int) -> MinuteDataResult:
        """분봉 데이터를 가져와 2차 필터 분석 결과를 반환합니다. 결과는 캐시합니다.

        2026-07-27~28 (GPT 코드리뷰 지적, 안전성 긴급 수정 3단계):
        1~2단계에서 API 예외/빈 응답/과거 봉의 "최초 호출" 케이스는
        막았으나, 3차 재검토로 다음 문제들이 추가로 재현됨:

        (c) 캐시 재사용 경로(need_refresh=False) 우회 — 1회차에서
            과거 봉으로 entry_safe=False가 나와도, 60초 캐시 구간
            안의 2회차 호출은 신선도 재검증 없이 source가 초기값
            "LIVE"로 남아 entry_safe=True가 되던 치명적 버그(재현:
            동일 latest_bar_timestamp인데 1회차 False, 2회차 True).
        (d) datetime.now()(naive, 시스템 로컬시각)와 KST 봉
            timestamp를 그대로 비교 — UTC 서버 환경 재현 시
            age_seconds=-32400(-9시간)인데도 entry_safe=True로
            판정되던 버그.

        이번 수정의 핵심 원칙:
        - 신선도 검증(_evaluate_bar_freshness)을 단일 헬퍼로 뽑아,
          "API 응답 직후"와 "캐시 재사용 시" 양쪽에서 동일하게 재검증
          — 두 경로의 판정 기준이 어긋날 수 없도록 함.
        - now_kst()/parse_kst_bar_timestamp()로 KST timezone-aware
          비교만 사용 — naive datetime 비교를 완전히 제거.
        - 캐시(cached_minute_bars/cached_minute_bars_loaded_at)는
          신선도 검증을 통과했을 때만 갱신 — 과거 봉/파싱실패
          응답으로 "성공 캐시 시각"이 갱신되지 않도록 함.
        - 새 API 응답의 최신 봉이 기존 캐시의 최신 봉보다 오래됐으면
          기존 캐시를 덮어쓰지 않음(더 최신인 캐시 보호).
        - 실패/빈 응답 시각(cached_minute_bars_failed_at)을 성공
          캐시 시각과 분리 추적해 짧은 백오프를 적용 — 실패가
          연속될 때 10초 폴링마다 API를 계속 두드리지 않도록 함
          (이전 HTTP 429 재발 방지).

        기존 동작(캐시를 분석에 사용하는 것 자체)은 그대로 유지 —
        보유 종목의 손절/트레일링 판단이 끊기지 않도록. entry_safe
        만으로 호출부가 신규 진입 여부를 판단.
        """
        now = now_kst()
        loaded_at = self.cached_minute_bars_loaded_at.get(symbol)
        failed_at = self.cached_minute_bars_failed_at.get(symbol)
        refresh_sec = self.settings.market_regime.minute_refresh_seconds
        backoff_sec = getattr(
            self.settings.market_regime, "minute_fetch_backoff_seconds", 20
        )

        need_refresh = (
            loaded_at is None
            or (now - loaded_at).total_seconds() >= refresh_sec
        )
        # 2026-07-28 (GPT 지적 5번): 직전 시도가 실패/빈응답이었다면
        # 성공 캐시 시각(loaded_at)과 무관하게 backoff_sec가 지나기
        # 전에는 재시도하지 않음 — 매 폴링(10초)마다 실패하는 API를
        # 계속 두드리는 것을 방지(HTTP 429 재발 방지 목적).
        backoff_active = False
        if need_refresh and failed_at is not None:
            if (now - failed_at).total_seconds() < backoff_sec:
                need_refresh = False
                backoff_active = True

        if need_refresh:
            try:
                cfg = self.settings.market_regime
                new_bars = self.broker.get_minute_bars(
                    symbol,
                    tick_scope=cfg.minute_tick_scope,
                    count=cfg.minute_bar_count,
                )

                if not new_bars:
                    self.app_logger.warning(
                        f"[MIN_STALE] {symbol} | API가 빈 분봉을 반환 — "
                        f"기존 캐시 유지, 신규진입 차단"
                    )
                    self.cached_minute_bars_failed_at[symbol] = now
                    bars = self.cached_minute_bars.get(symbol, [])
                    source = "EMPTY" if not bars else "CACHE_STALE"
                    reason = "MINUTE_DATA_UNAVAILABLE" if not bars else "STALE_MINUTE_DATA"
                    # 2026-07-28 (4차 GPT 코드리뷰 지적, 세션 오염
                    # 재현 확인): 빈 응답 자체는 세션에 넣을 새 데이터가
                    # 없음 — 기존 캐시(이미 구조 검증을 통과한 데이터)
                    # 가 있으면 그걸 다시 세션에 반영해도 안전(동일
                    # timestamp는 merge_session_bars가 교체하므로 중복
                    # 걱정 없음), 없으면 세션에 아무것도 안 넣음.
                    session_ingest_bars = bars if bars else []
                else:
                    # 2026-07-28 (6차 GPT 코드리뷰 지적, "1B Safety
                    # Closure"): 이전엔 구조 검증(_evaluate_bar_
                    # freshness, OHLC 포함) 전에 이미 1분봉 저장
                    # (_minute_saver.save)이 실행되고 있었음 — 검증
                    # 실패/이상 데이터가 정상 리플레이용 CSV에
                    # 조용히 섞여 들어갈 수 있었던 문제. 검증을
                    # 저장보다 먼저 하도록 순서 변경.
                    fresh_ok, fresh_dt, fresh_age, fresh_reason_code, fresh_detail = (
                        self._evaluate_bar_freshness(new_bars, now)
                    )
                    if not fresh_ok:
                        self.app_logger.warning(
                            f"[MIN_STALE] {symbol} | {fresh_detail} — 신규진입 차단 "
                            f"(성공 캐시 시각은 갱신하지 않음)"
                        )
                        self.cached_minute_bars_failed_at[symbol] = now
                        # 신선도/구조 검증 실패 응답으로는 캐시/loaded_at을
                        # 절대 갱신하지 않음(GPT 지적 2·3번) — 기존 캐시가
                        # 있으면 그걸 그대로 분석에 사용, 없으면 이번에
                        # 받은(오래된/이상한) new_bars로라도 분석 시도
                        # (보유종목 판단이 끊기지 않도록 — 단, analyze()
                        # 자체가 이 이상 데이터로 예외를 낼 수 있으므로
                        # 아래에서 try/except로 감쌈).
                        existing_bars = self.cached_minute_bars.get(symbol, [])
                        bars = existing_bars if existing_bars else new_bars
                        source = "LIVE_OLD_BAR"
                        reason = fresh_reason_code or "STALE_MINUTE_DATA"
                        # 거부된 응답을 별도 경로(rejected)로 남김 —
                        # 정상 minute_bars 저장 CSV에는 절대 섞지 않음.
                        self._save_rejected_minute_bars(symbol, new_bars, reason, fresh_detail)
                        # 2026-07-28 (4차 GPT 코드리뷰 지적, 세션 오염
                        # 재현 확인): 구조 검증에 실패한 new_bars(예:
                        # OHLC가 0인 60개)가 그대로 bars에 담겨 세션에
                        # 전달되고 있었음(재현: session_bar_count=41,
                        # 저장된 high_price 집합이 전부 {0}). 세션에는
                        # "구조 검증을 통과한 적이 있는" existing_bars
                        # (있다면)만 반영 — new_bars(이번에 검증 실패한
                        # 원본)는 절대 세션에 넣지 않음.
                        session_ingest_bars = existing_bars if existing_bars else []
                    else:
                        # 2026-07-28 (GPT 지적 3번): 새 응답의 최신 봉이
                        # 기존 캐시의 최신 봉보다 오래되면 기존(더 최신인)
                        # 캐시를 덮어쓰지 않음.
                        existing_bars = self.cached_minute_bars.get(symbol, [])
                        existing_latest_dt = None
                        if existing_bars:
                            existing_latest_dt = parse_kst_bar_timestamp(
                                existing_bars[-1].cntr_tm
                            )
                        if existing_latest_dt is not None and fresh_dt is not None and fresh_dt < existing_latest_dt:
                            # 2026-07-28 (5차 GPT 코드리뷰 지적 4번): 새
                            # 응답이 기존 캐시보다 오래됐지만 그 자체는
                            # max_age 이내(fresh_ok=True)인 경우 —
                            # REGRESSED_MINUTE_RESPONSE로 명시하고
                            # 신규 진입 차단(entry_safe=False, 보수적
                            # 확정 정책). analysis는 기존(더 신선한)
                            # 캐시로 계속 제공해 보유종목 판단 유지.
                            self.app_logger.warning(
                                f"[MIN_STALE] {symbol} | 새 응답(최신봉={fresh_dt})이 "
                                f"기존 캐시(최신봉={existing_latest_dt})보다 오래됨 "
                                f"(REGRESSED_MINUTE_RESPONSE) — 기존 캐시로 분석은 "
                                f"계속하되 이번 사이클 신규진입은 차단"
                            )
                            self.cached_minute_bars_failed_at[symbol] = now
                            bars = existing_bars
                            source = "CACHE_FRESH"
                            reason = "REGRESSED_MINUTE_RESPONSE"
                            self._save_rejected_minute_bars(
                                symbol, new_bars, reason,
                                f"새응답 최신봉={fresh_dt} < 기존캐시 최신봉={existing_latest_dt}",
                            )
                            # 퇴행된(더 과거) 응답도 세션에는 안 넣음 —
                            # 기존(더 신선한) 캐시만 다시 반영.
                            session_ingest_bars = existing_bars if existing_bars else []
                        else:
                            # 2026-07-28 (6차 GPT 코드리뷰 지적): 구조
                            # 검증을 통과한 응답만 성공 캐시/loaded_at을
                            # 갱신하고, 정상 리플레이용 CSV에도 이
                            # 지점에서만 저장 — 검증 전 저장하던 순서를
                            # 여기로 이동.
                            self.cached_minute_bars[symbol] = new_bars
                            self.cached_minute_bars_loaded_at[symbol] = now
                            self.cached_minute_bars_failed_at.pop(symbol, None)
                            bars = new_bars
                            source = "LIVE"
                            reason = ""
                            # 2026-07-28 (4차 GPT 코드리뷰 지적): 구조
                            # 검증을 실제로 통과한 이 new_bars만 세션에
                            # 반영 — 유일하게 "새로 검증된 데이터"이므로
                            # session_ingest_bars = new_bars.
                            session_ingest_bars = new_bars

                            if self._minute_saver is not None:
                                try:
                                    self._minute_saver.save(symbol, new_bars)
                                except Exception as save_exc:
                                    self.app_logger.debug(
                                        f"[MIN] {symbol} | 분봉 저장 실패: {save_exc}"
                                    )
            except Exception as exc:
                self.app_logger.warning(f"[MIN] {symbol} | 분봉 조회 실패: {exc}")
                self.cached_minute_bars_failed_at[symbol] = now
                bars = self.cached_minute_bars.get(symbol, [])
                source = "CACHE_STALE" if bars else "UNAVAILABLE"
                reason = "STALE_MINUTE_DATA" if bars else "MINUTE_DATA_UNAVAILABLE"
                # 조회 자체가 예외를 던진 경우도 세션에 새로 반영할
                # 데이터가 없음 — 기존(구조 검증을 통과했던) 캐시만
                # 다시 반영.
                session_ingest_bars = bars if bars else []
        else:
            # 2026-07-28 (GPT 지적 1번, 가장 심각한 버그): 캐시 재사용
            # 경로에서도 매번 최신 timestamp의 날짜·age·파싱 여부를
            # 재검증 — "캐시니까 신선하다"고 가정하지 않음. 아래
            # _evaluate_bar_freshness()는 API 응답 직후 검증과 완전히
            # 동일한 로직을 재사용.
            bars = self.cached_minute_bars.get(symbol, [])
            if not bars:
                source, reason = "UNAVAILABLE", "MINUTE_DATA_UNAVAILABLE"
                if backoff_active:
                    self.app_logger.debug(
                        f"[MIN_STALE] {symbol} | 직전 실패 후 백오프 구간이라 "
                        f"재시도 스킵, 캐시도 없어 UNAVAILABLE"
                    )
            else:
                fresh_ok, _, _, fresh_reason_code, fresh_detail = (
                    self._evaluate_bar_freshness(bars, now)
                )
                if fresh_ok:
                    source, reason = "CACHE_FRESH", ""
                else:
                    source, reason = "CACHE_STALE", (fresh_reason_code or "STALE_MINUTE_DATA")
            # 2026-07-28 (4차 GPT 코드리뷰 지적): 캐시 재사용 경로는
            # cached_minute_bars 자체가 "구조 검증을 통과했던 시점의
            # 데이터"이므로(567번째 줄 부근에서 fresh_ok일 때만 캐시가
            # 갱신됨), 여기서 다시 검증할 필요 없이 bars 그대로 세션에
            # 반영해도 안전 — 다만 이 경로에서 fresh_ok가 False(예:
            # max_age 초과)이면 "신선하지 않다"는 의미이지 "구조가
            # 잘못됐다"는 의미가 아니므로, 세션에는 여전히 반영해도
            # 됨(세션 목적 자체가 "당일 전체 누적"이라 오래된 데이터도
            # 유효한 세션 데이터임 — 신선도와 세션 소속 여부는 별개 개념).
            session_ingest_bars = bars if bars else []

        if not bars:
            return MinuteDataResult(
                analysis=None, entry_safe=False, source=source or "UNAVAILABLE",
                reason=reason or "MINUTE_DATA_UNAVAILABLE",
                latest_bar_timestamp=None, age_seconds=None,
            )

        # 2026-07-28 (6차 GPT 코드리뷰 지적, "1B Safety Closure"):
        # MinuteAnalyzer.analyze()가 내부적으로 예외를 던질 수 있음
        # (재현: OHLC가 0인 봉으로 day_high=0 나눗셈 -> ZeroDivisionError
        # 가 _process_symbol()까지 그대로 전파돼 해당 폴링에서 나머지
        # 모든 종목 처리가 중단될 위험이 있었음). 이제 위에서 구조
        # 검증을 먼저 통과시키므로 정상 경로에서는 이 예외가 이론상
        # 발생하지 않아야 하지만, analyzer 로직 자체의 다른 버그나
        # 예상 못한 입력에 대비해 이 호출 자체도 방어적으로 감쌈 —
        # 예외가 나도 종목 처리 루프 전체를 막지 않고, 이 종목만
        # MINUTE_ANALYSIS_ERROR로 안전하게 차단.
        try:
            analysis = self._minute_analyzer.analyze(bars, prev_close)
        except Exception as analyze_exc:
            self.app_logger.error(
                f"[MIN_STALE] {symbol} | MinuteAnalyzer.analyze() 예외 발생"
                f"(무시하고 계속 진행, 신규진입 차단): {analyze_exc}"
            )
            analysis = None
            if reason == "":
                reason = "MINUTE_ANALYSIS_ERROR"

        latest_ts = bars[-1].cntr_tm if bars else None
        latest_dt = parse_kst_bar_timestamp(latest_ts)
        age_seconds = (now - latest_dt).total_seconds() if latest_dt is not None else None

        # 2026-07-28 (5차 GPT 코드리뷰 지적): entry_safe 최종 조건에
        # analysis is not None을 포함하지 않아서, 진입 품질 검증
        # (_evaluate_bar_freshness)을 통과한 source/reason이어도
        # MinuteAnalyzer.analyze()가 내부적으로 None을 반환하는
        # 경우(예: 봉 개수가 v_low_max_age+1보다 적은 등 analyzer
        # 자체 최소요건 미달)까지 entry_safe=True로 새는 우회가
        # 있었음(재현 확인). analysis가 없으면 무조건 차단.
        if analysis is None:
            entry_safe = False
            if reason == "":
                reason = "MINUTE_ANALYSIS_UNAVAILABLE"
        else:
            entry_safe = source in ("LIVE", "CACHE_FRESH") and reason == ""

        # 2026-07-28 (1C단계, session_metrics_mode="shadow" 전용):
        # 세션 지표를 계산·로그만 하고 이 함수의 반환값(analysis,
        # entry_safe, source, reason 등)에는 절대 영향을 주지 않음 —
        # 이 블록을 통째로 지워도 위의 return 문 결과가 바이트
        # 단위로 동일해야 한다는 게 "shadow"의 핵심 안전 조건. 예외가
        # 나도 fail-open(경고만 남기고 무시) — 신규 진입/청산 판단을
        # 절대 막지 않음.
        #
        # 2026-07-28 (2차 GPT 코드리뷰 지적 4번): 이미 위에서 계산해둔
        # analysis를 그대로 전달 — shadow 함수 내부가 MinuteAnalyzer.
        # analyze()를 다시 호출하면 안 됨(MinuteAnalyzer는 _last_
        # v_fail_reasons 같은 내부 상태를 바꾸는 상태성 객체라, 재호출
        # 자체가 "shadow는 상태를 안 바꾼다"는 원칙을 어김 — 재현
        # 확인: off는 analyze() 1회, shadow는 2회 호출되고 있었음).
        #
        # 2026-07-28 (4차 GPT 코드리뷰 지적, 세션 오염 재현 확인):
        # 여기서 bars(분석용, 구조 검증에 실패해도 보유종목 판단을
        # 위해 오염된 데이터를 그대로 담을 수 있음)를 그대로 세션에
        # 넘기면 안 됨 — session_ingest_bars(구조 검증을 실제로
        # 통과한 데이터만, 위에서 각 경로별로 명시적으로 결정됨)를
        # 대신 전달. 재현 확인: high=0/low=0인 60개 응답을 넣으면
        # entry_safe=False로 안전하게 차단되면서도 세션 저장소에는
        # 그 오염된 60개가 그대로 들어가고 있었음(session_bar_count
        # =41, 저장된 high_price가 전부 {0}) — session_ingest_bars
        # 분리로 이 경로 자체를 차단.
        try:
            self._update_session_metrics_shadow(symbol, session_ingest_bars, analysis)
        except Exception as shadow_exc:
            self.app_logger.warning(
                f"[SESSION_SHADOW] {symbol} | shadow 계산 실패(무시): {shadow_exc}"
            )

        return MinuteDataResult(
            analysis=analysis, entry_safe=entry_safe, source=source,
            reason=reason, latest_bar_timestamp=latest_ts, age_seconds=age_seconds,
        )

    def _update_session_metrics_shadow(self, symbol: str, bars: list, analysis) -> None:
        """세션 지표를 shadow 모드로 계산하고 관찰 로그만 남깁니다.

        2026-07-28 (1C단계): session_metrics_mode가 "shadow"가
        아니면 즉시 반환 — "off"(기본값)에서는 세션 누적 자체가
        전혀 쌓이지 않아 메모리·CPU 부담이 없음. 이 함수는 로그
        출력 외에 어떤 반환값도 없고(None), 호출부(_get_minute_
        analysis)가 이 함수의 성공/실패와 무관하게 항상 동일한
        MinuteDataResult를 반환하도록 fail-open으로 감싸져 있음.

        2026-07-28 (2차 GPT 코드리뷰 지적 4번): analysis를 인자로
        전달받아 사용 — 이 함수 내부에서 MinuteAnalyzer.analyze()를
        다시 호출하지 않음. legacy_vwap/day_high/day_low는 전달받은
        analysis에서 그대로 읽음.

        2026-07-28 (2차 GPT 코드리뷰 지적 3번): 세션 상태를 종목별
        SessionState로 관리 — merge_session_bars()가 날짜 변경을
        자체적으로 감지해 자동 초기화하므로, reset_daily_loss_
        counts() 호출이 누락돼도 전일 세션이 새 거래일 계산에 섞이지
        않음.

        로그 스로틀: 매 폴링(10초)마다 로그를 남기면 기존 [MIN]
        로그와 마찬가지로 app.log 용량 문제가 재발할 수 있으므로,
        60초 간격으로 제한(기존 [V_FAIL]/[MIN_STALE] 로그와 동일
        cadence 원칙).
        """
        mode = getattr(self.settings.experimental, "session_metrics_mode", "off")
        if mode != "shadow":
            return
        if not bars:
            return

        target_date = now_kst().strftime("%Y%m%d")
        existing_state = self._session_state_by_symbol.get(symbol)
        new_state = merge_session_bars(existing_state, bars, target_date)
        self._session_state_by_symbol[symbol] = new_state

        metrics = build_session_metrics(new_state)
        # 2026-08-05 (GPT 코드리뷰 지적): 로그 스로틀 체크보다 먼저
        # 캐시를 갱신 — 아래 60초 스로틀은 "로그 출력"만 제한하지,
        # metrics 계산 자체는 매 폴링마다 일어나므로 이 캐시는 항상
        # 최신 상태를 유지함(VWAP shadow가 스로틀된 로그 주기와
        # 무관하게 최신 세션 값을 쓸 수 있도록).
        self._latest_session_metrics_by_symbol[symbol] = metrics

        last_log = self.last_hold_log_at_by_symbol.get(f"__session_shadow_{symbol}")
        now_dt = datetime.now()
        if last_log is not None and (now_dt - last_log).total_seconds() < 60:
            return
        self.last_hold_log_at_by_symbol[f"__session_shadow_{symbol}"] = now_dt

        legacy_vwap = analysis.vwap if analysis is not None else None
        legacy_day_high = analysis.day_high if analysis is not None else None
        legacy_day_low = analysis.day_low if analysis is not None else None

        self.app_logger.info(
            format_session_metrics_log_line(
                symbol, metrics, legacy_vwap, legacy_day_high, legacy_day_low,
            )
        )

    def _save_rejected_minute_bars(
        self, symbol: str, bars: list, reason: str, detail: str,
    ) -> None:
        """구조/신선도 검증에 실패한 분봉 응답의 진단 메타데이터를 별도 경로에 기록합니다.

        2026-07-28 (6차 GPT 코드리뷰 지적, "1B Safety Closure"):
        검증 실패·퇴행·과거 응답이 정상 리플레이용 minute_bars CSV에
        조용히 섞이면 안 됨(1A 단계에서 이미 저장된 분봉이 "당시
        실제 입력"이라는 전제로 fixture를 만든 적이 있었는데, 그
        전제가 깨질 수 있는 문제). 정상 저장(_minute_saver.save)은
        검증을 통과한 데이터에만 실행되도록 이미 위에서 순서를
        옮겼고, 이 함수는 거부 사실을 참고용으로 별도 기록.

        2026-07-28 (7차 GPT 코드리뷰 지적, 표현 정정): "거부 응답을
        별도 경로에 저장"이라는 이전 표현이 부정확했음 — 실제로는
        분봉 데이터 전체(OHLCV)가 아니라 reason/detail/bar_count/
        first_ts/last_ts 같은 진단 메타데이터 한 줄만 기록함. 정상
        리플레이 CSV를 오염시키지 않는다는 안전성 목표는 메타데이터
        로그만으로도 달성되지만, 거부된 분봉의 실제 OHLCV 값을 사후
        분석하려면(예: 어떤 값이 왜 이상했는지 원본을 다시 봐야 하는
        경우) 이 로그만으로는 부족함 — 필요해지면 별도 JSON/CSV로
        원본 봉 데이터까지 저장하는 확장을 고려.

        저장 실패는 fail-open — 예외가 나도 신규 진입 차단 로직에
        영향을 주지 않음.
        """
        if self._minute_saver is None or not bars:
            return
        try:
            reject_dir = Path(self.settings.storage.minute_bars_dir) / "rejected"
            reject_dir.mkdir(parents=True, exist_ok=True)
            today_str = now_kst().strftime("%Y%m%d")
            reject_path = reject_dir / f"{symbol}_{today_str}.log"
            with open(reject_path, "a", encoding="utf-8") as f:
                f.write(
                    f"{now_kst().isoformat()} | reason={reason} | detail={detail} | "
                    f"bar_count={len(bars)} | first_ts={bars[0].cntr_tm if bars else None} | "
                    f"last_ts={bars[-1].cntr_tm if bars else None}\n"
                )
        except Exception as exc:
            self.app_logger.debug(f"[MIN] {symbol} | 거부 응답 기록 실패(무시): {exc}")

    def _evaluate_bar_freshness(
        self, bars: list, now: datetime,
    ) -> tuple[bool, datetime | None, float | None, str, str]:
        """분봉 리스트가 신규 진입에 쓸 만큼 신선하고 품질이 충분한지 판정합니다.

        2026-07-28 (GPT 지적 1번): API 응답 직후 검증과 캐시 재사용
        시 검증이 서로 다른 기준을 쓰면 우회가 생기므로, 단일 헬퍼로
        통합해 양쪽에서 동일하게 호출. KST timezone-aware 비교만
        사용(GPT 지적 4번) — naive datetime 비교 금지.

        2026-07-28 (5차 GPT 코드리뷰 지적, 진입 품질 검증): 최소
        분봉 개수, timestamp 파싱 가능성, 엄격한 오름차순(중복 불허)
        검증 추가.

        2026-07-28 (6차 GPT 코드리뷰 지적, "1B Safety Closure"):
        OHLC 구조 자체가 깨진 봉(high=0, low=0 등)을 검사하지
        않아서, 신선도·개수·정렬 검증을 전부 통과해도 MinuteAnalyzer
        .analyze() 내부에서 0으로 나누는 ZeroDivisionError가 그대로
        _process_symbol()까지 전파되던 문제를 재현 확인(open=58000,
        high=0, low=0, close=58000인 60개 봉 — fresh_ok=True인데
        analyze()가 day_high=0으로 나누다가 예외). OHLC 검증을
        신선도 검증의 일부로 추가 — 하나라도 위반하면 즉시 차단.

        이 함수는 "진단"이 아니라 "진입 안전장치"입니다 — 기존
        infra/broker/minute_bar_diagnostics.py의 진단 로거(raw_
        order_violation_count 등)는 관찰 전용이라 이 안전장치와는
        별개로 계속 관찰만 함(1B단계 원칙 유지).

        판정 기준(하나라도 실패하면 False, 검사 순서대로):
        1. 분봉 개수 >= minute_bar_min_count_for_entry
        2. 전체 cntr_tm 파싱 가능
        3. timestamp 엄격한 오름차순(중복/역순/뒤섞임 전부 차단)
        4. OHLC 구조 정상: open/high/low/close 모두 > 0,
           low <= open <= high, low <= close <= high,
           volume >= 0, acc_volume >= 0 (전체 봉에 대해 검사)
        5. 최신 봉 age_seconds가 -5초 이상, max_age_seconds 이하

        반환값: (fresh_ok, latest_dt, age_seconds, reason_code, detail_message)
        reason_code는 MinuteDataResult.reason에 그대로 쓰일 명시적
        코드("" | STALE_MINUTE_DATA | INVALID_MINUTE_OHLC 등).
        """
        max_age_sec = getattr(
            self.settings.market_regime, "minute_bar_max_age_seconds", 120
        )
        min_count = getattr(
            self.settings.market_regime, "minute_bar_min_count_for_entry", 60
        )

        # 1) 최소 개수 확인
        if len(bars) < min_count:
            return False, None, None, "STALE_MINUTE_DATA", (
                f"분봉 개수 부족({len(bars)}개 < 최소 {min_count}개)"
            )

        # 2) 전체 timestamp 파싱 가능 여부 + 3) 엄격한 오름차순(중복 불허)
        parsed = [parse_kst_bar_timestamp(b.cntr_tm) for b in bars]
        if any(dt is None for dt in parsed):
            invalid_count = sum(1 for dt in parsed if dt is None)
            return False, None, None, "STALE_MINUTE_DATA", (
                f"분봉 중 {invalid_count}개 timestamp 파싱 실패"
            )
        if not all(parsed[i] < parsed[i + 1] for i in range(len(parsed) - 1)):
            return False, None, None, "STALE_MINUTE_DATA", (
                "분봉 timestamp가 엄격한 오름차순이 아님(중복 또는 뒤섞임)"
            )

        # 4) OHLC 구조 검증 (2026-07-28, 6차 GPT 코드리뷰 지적)
        for idx, b in enumerate(bars):
            if b.open_price <= 0 or b.high_price <= 0 or b.low_price <= 0 or b.close_price <= 0:
                return False, None, None, "INVALID_MINUTE_OHLC", (
                    f"{idx}번째 봉({b.cntr_tm})의 OHLC에 0 이하 값 존재 "
                    f"(open={b.open_price}, high={b.high_price}, "
                    f"low={b.low_price}, close={b.close_price})"
                )
            if not (b.low_price <= b.open_price <= b.high_price):
                return False, None, None, "INVALID_MINUTE_OHLC", (
                    f"{idx}번째 봉({b.cntr_tm})의 open이 low~high 범위 밖 "
                    f"(low={b.low_price}, open={b.open_price}, high={b.high_price})"
                )
            if not (b.low_price <= b.close_price <= b.high_price):
                return False, None, None, "INVALID_MINUTE_OHLC", (
                    f"{idx}번째 봉({b.cntr_tm})의 close가 low~high 범위 밖 "
                    f"(low={b.low_price}, close={b.close_price}, high={b.high_price})"
                )
            if b.volume < 0 or b.acc_volume < 0:
                return False, None, None, "INVALID_MINUTE_OHLC", (
                    f"{idx}번째 봉({b.cntr_tm})의 거래량이 음수 "
                    f"(volume={b.volume}, acc_volume={b.acc_volume})"
                )

        # 2026-07-28 (버그 수정): OHLC 검증 블록을 추가하며 이 계산이
        # 실수로 누락되어 NameError가 났던 것을 재발견·복구.
        latest_ts = bars[-1].cntr_tm
        latest_dt = parsed[-1]
        age_seconds = (now - latest_dt).total_seconds()

        # 2026-07-28 (GPT 지적 4번): age_seconds < -5인 미래 봉도
        # entry_safe=False — 시계 오차(수 초)는 허용하되, 명백히
        # 미래인 timestamp는 데이터 이상으로 간주.
        if age_seconds < -5:
            return False, latest_dt, age_seconds, "STALE_MINUTE_DATA", (
                f"최신 봉({latest_ts})이 미래 시각(age={age_seconds:.0f}s)"
            )

        if age_seconds > max_age_sec:
            return False, latest_dt, age_seconds, "STALE_MINUTE_DATA", (
                f"최신 봉({latest_ts}) age={age_seconds:.0f}s > {max_age_sec}s"
            )

        return True, latest_dt, age_seconds, "", ""

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
            minute_data_entry_safe = True
            minute_data_reason = ""
            # 2026-08-04 (GPT 코드리뷰 지시, MACD shadow 관측):
            # minute_result는 regime이 BULLISH/NEUTRAL/REBOUND일
            # 때만 정의되므로(BEARISH/UNKNOWN 경로는 이 블록 자체를
            # 안 탐), _write_signal_log()에 latest_bar_timestamp를
            # 안전하게 넘기기 위해 별도 변수로 미리 초기화.
            latest_bar_timestamp = None
            if regime in (MarketRegime.BULLISH, MarketRegime.NEUTRAL, MarketRegime.REBOUND):
                minute_result = self._get_minute_analysis(symbol, market_price.previous_close)
                minute_analysis = minute_result.analysis
                minute_data_entry_safe = minute_result.entry_safe
                minute_data_reason = minute_result.reason
                latest_bar_timestamp = minute_result.latest_bar_timestamp
                # 2026-07-28 (6차 GPT 코드리뷰 지적 5번, "1B Safety
                # Closure"): 기존엔 minute_analysis가 존재하기만
                # 하면(캐시로 얻은 stale 데이터라도) [MIN]/[V_FAIL]
                # 로그와 그 아래(1234번째 줄 부근)의 low_volume_count
                # /자동제외 카운터가 entry_safe 여부와 무관하게 갱신
                # 됐음 — stale 데이터로 관측 상태(점수 로그, V자 실패
                # 누적, 거래대금 부족 누적)가 오염될 수 있었음. 이제
                # entry_safe인 경우에만 이 블록 전체를 실행하고,
                # stale이면 [MIN_STALE] 한 줄만 남기고 상태 갱신은
                # 건너뜀.
                if minute_analysis and minute_data_entry_safe:
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
                elif not minute_data_entry_safe:
                    last_stale_log = self.last_hold_log_at_by_symbol.get(f"__minstale_{symbol}")
                    now_dt = datetime.now()
                    if last_stale_log is None or (now_dt - last_stale_log).total_seconds() >= 60:
                        self.app_logger.info(
                            f"[MIN_STALE] {symbol} | reason={minute_data_reason} "
                            f"— 상태 갱신 없이 관찰만 함"
                        )
                        self.last_hold_log_at_by_symbol[f"__minstale_{symbol}"] = now_dt

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
            #
            # 2026-07-28 (7차 GPT 코드리뷰 지적, "1B Safety Closure"
            # 후속): _check_entry_watch()는 정규 strategy.generate_
            # signal()보다 먼저 실행되는 별도 경로인데, 여기에는
            # minute_analysis를 무조건 그대로 넘기고 있었음 —
            # entry_watch 내부의 VWAP 이탈 청산이 requires_fresh_
            # minute_data 표시 없이 SELL을 반환하고, stale 데이터로도
            # vwap_break_streak_by_symbol을 증가시키던 우회를 재현
            # 확인(entry_time 2분 전 + stale + price_above_vwap=False
            # 조합에서 실제로 SELL 신호와 streak=1이 발생).
            #
            # entry_watch의 급락청산(fail_cut_pct)·시간초과청산
            # (watch_minutes)은 가격/시간 기반이라 stale이어도 계속
            # 허용해야 하므로 _check_entry_watch() 호출 자체는 그대로
            # 유지 — 대신 VWAP 판단에만 쓰이는 minute_analysis를
            # entry_safe일 때만 전달하고, stale이면 None을 넘겨서
            # 함수 내부의 "if ... and minute_analysis is not None"
            # 조건 자체가 거짓이 되도록 함(1531번째 줄 부근). 이러면
            # VWAP 판단 로직 자체가 실행되지 않아 streak도 갱신되지
            # 않음 — GPT 권장대로 "판단 자체를 실행하지 않는" 구조.
            entry_watch_minute_analysis = (
                minute_analysis if minute_data_entry_safe else None
            )
            if not minute_data_entry_safe:
                # stale 구간에서는 VWAP 연속 이탈 확인이 이어지지
                # 않도록 명시적으로 리셋(보수적 정책, GPT 권장).
                self.state.vwap_break_streak_by_symbol.pop(symbol, None)

            signal = self._check_entry_watch(
                symbol, position, market_price.current_price, entry_watch_minute_analysis,
            )
            if signal is None:
                signal = strategy.generate_signal(
                    market_price, position, minute_analysis, highest_price,
                    bb_percent_b=_bb_pb,
                )

            # 2026-07-27~28 (GPT 코드리뷰 지적, 안전성 긴급 수정): 미보유
            # 종목에서 분봉 데이터가 신규진입에 안전하지 않으면(조회
            # 실패로 오래된 캐시를 썼거나, API가 빈 응답을 줬거나,
            # API는 성공했지만 반환된 최신 봉이 과거 데이터인 경우 모두
            # 포함) BUY 신호가 나와도 강제로 HOLD로 덮어씀 — 실운영에서
            # 분봉 조회 실패 직후 오래된 캐시 기반 신규 매수(039980)가
            # 발생했던 문제의 재발 방지, 이후 빈 응답·과거봉 우회
            # 경로도 재현 확인 후 함께 차단. 보유 종목(position is not
            # None)의 손절/트레일링 SELL은 이 검사와 무관하게 그대로
            # 동작 — 위험 축소 행동을 막으면 안 되므로.
            if (
                position is None
                and not minute_data_entry_safe
                and signal.type == SignalType.BUY
            ):
                signal = Signal(type=SignalType.HOLD, reason=minute_data_reason)

            # 2026-07-28 (6차 GPT 코드리뷰 지적 5번, "1B Safety
            # Closure"): 보유 종목의 SELL 신호 중 minute_analysis
            # (VWAP/MA5 등 분봉 지표)를 실제로 참조한 것은
            # requires_fresh_minute_data=True로 표시되어 있음(각
            # 전략의 "추세 꺾임" 신호). 분봉 데이터가 stale이면 이
            # 판단 자체를 신뢰할 수 없으므로 HOLD로 전환 — 단, 고정
            # 손절/트레일링 스탑/안전망 익절처럼 현재가·평균단가·
            # 최고가만으로 계산되는 hard-risk SELL(requires_fresh_
            # minute_data=False, 기본값)은 이 검사와 무관하게 계속
            # 허용 — 위험 축소 행동을 stale 데이터를 이유로 막으면
            # 안 되므로.
            if (
                position is not None
                and signal.type == SignalType.SELL
                and getattr(signal, "requires_fresh_minute_data", False)
                and not minute_data_entry_safe
            ):
                self.app_logger.info(
                    f"[MIN_STALE] {symbol} | 지표 기반 SELL({signal.reason}) 차단 — "
                    f"분봉 데이터 stale(reason={minute_data_reason}), HOLD로 대기"
                )
                signal = Signal(
                    type=SignalType.HOLD,
                    reason=f"지표기반 SELL 보류(stale) — {minute_data_reason}",
                )

            self._log_signal_decision(
                symbol, signal, market_price.current_price,
                regime, position, minute_analysis
            )

            # 거래대금 부족 3회 연속이면 자동 제외
            # 2026-07-28 (6차 GPT 코드리뷰 지적 5번, "1B Safety
            # Closure"): entry_safe 여부와 무관하게 minute_analysis가
            # 있기만 하면 이 카운터가 갱신됐음 — stale 데이터로 우연히
            # trading_value가 낮게 계산되면 실제로는 판단할 수 없는
            # 상황인데도 자동제외 카운트가 올라갈 수 있었음. entry_safe
            # 인 경우에만 카운터를 갱신·초기화(스킵 시엔 그대로 유지 —
            # stale 사이클이 카운트를 임의로 리셋하지도, 올리지도 않음).
            if minute_data_entry_safe:
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
                # 2026-08-05 (3차 GPT 코드리뷰 지적 P1): _try_buy()
                # 호출 직전에 이 종목의 이전 폴링 주문시도 기록을
                # 명시적으로 지움 — 이번 폴링에서 place_order()가
                # 실제로 호출됐는지(order_attempted)를, "저장소에
                # 값이 있다"가 아니라 "이번 호출 이후에 값이 새로
                # 생겼다"로 정확히 판단하기 위함. _try_buy() 내부의
                # 조기 반환(EXCLUDED_SYMBOL 등)들은 place_order()
                # 자체를 호출하지 않으므로, 그 경로에서는 이 값이
                # 계속 비어있는 게 맞음.
                self._last_order_attempt_by_symbol.pop(symbol, None)
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
                market_price=market_price,
                latest_bar_timestamp=latest_bar_timestamp,
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
        # 2026-07-28 (1C단계): 세션 누적 분봉도 날짜 변경 시 초기화 —
        # 전날 세션 데이터가 새 거래일의 session_vwap/session_high/
        # session_low 계산에 섞여 들어가면 안 되므로.
        self._session_state_by_symbol.clear()
        self._latest_session_metrics_by_symbol.clear()
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
                force=True,   # 1P0.2: 이월 방지는 반드시 나가야 하는 경로
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
                        # 2026-07-28 (7차 GPT 코드리뷰 지적): 이 SELL은
                        # minute_analysis(VWAP)를 직접 참조하므로 지표
                        # 기반 신호 — requires_fresh_minute_data=True로
                        # 표시. 1차 방어(entry_watch_minute_analysis를
                        # entry_safe일 때만 전달)로 이미 stale 상황
                        #에서는 이 코드 지점 자체에 도달하지 않지만,
                        # 방어적으로 2차 표시를 추가.
                        requires_fresh_minute_data=True,
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
            # 1P0.4: 잔고 변화로 orphan 주문 해소 여부를 먼저 확인.
            # 1P0.6: 신호와 무관하게 매 폴링 감시.
            # (a) 차단이 오래 지속되면 즉시 드러냅니다 — decide_sell만
            #     의존하면 SELL 신호가 없는 동안 조용히 갇힙니다.
            _esc = psm.check_block_escalation(symbol)
            if _esc:
                self.app_logger.critical(f"[LIFECYCLE_STUCK] {symbol} {_esc}")
            # 2026-08-10 (1P0.7, GPT 코드리뷰): ERROR 자동 회복(900초)을
            # 제거했습니다. "시간이 지났다"는 원 주문이 terminal이라는
            # 증거가 아닙니다 — check_block_escalation()의
            # RECONCILIATION_REQUIRED로만 노출하고, 실제 해제는
            # acknowledge_error()로 사람이 확인 후 수행합니다.
            _orphan = psm.observe_for_orphan(symbol, broker_qty)
            if _orphan:
                self.app_logger.warning(f"[LIFECYCLE_ORPHAN] {symbol} 해소 | {_orphan}")
            violation = psm.check_invariant(symbol, broker_qty)
            if violation:
                self.app_logger.critical(f"[POSITION_STATE_MISMATCH][SHADOW] {violation}")

            # PENDING이 아닌 종목만 잔고로 재동기화 (다음 폴링을 위한 갱신)
            if state.lifecycle == PositionLifecycle.SELL_PENDING:
                # 매도 요청 다음 폴링 — 브로커 잔고를 다시 조회해 실제로
                # 전량/부분/미체결인지 판정 (on_sell_result 내부 로직)
                psm.on_sell_result(symbol, accepted=True, broker_quantity=broker_qty)
                # 1P0.7: 이 폴링에서 FLAT이 확정됐으면(완전 청산 확인)
                # 보류해뒀던 손실/쿨다운/알림 side effect를 지금 실행합니다.
                if psm.get(symbol).lifecycle == PositionLifecycle.FLAT:
                    self._apply_deferred_sell_side_effects(symbol)
                # 1P0.3: 브로커가 응답하지 않아 PENDING이 굳는 것을 방지.
                _resolved = psm.resolve_stale_pending(symbol, broker_qty)
                if _resolved:
                    self.app_logger.warning(f"[LIFECYCLE_TIMEOUT] {symbol} {_resolved}")
                    if psm.get(symbol).lifecycle == PositionLifecycle.FLAT:
                        self._apply_deferred_sell_side_effects(symbol)
            elif state.lifecycle == PositionLifecycle.BUY_PENDING:
                # 2026-07-23: 매도와 대칭 — 매수 요청 다음 폴링에서
                # 브로커 잔고를 다시 조회해 실제로 체결됐는지 확인.
                # 기존엔 이 분기가 없어서 BUY_PENDING을 그냥 지나쳤음
                # (매수는 _try_buy 안에서 즉시 확정했었으므로).
                psm.confirm_buy_from_broker(symbol, broker_qty)
                _resolved = psm.resolve_stale_pending(symbol, broker_qty)
                if _resolved:
                    self.app_logger.warning(f"[LIFECYCLE_TIMEOUT] {symbol} {_resolved}")
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
            self._run_shadow_analysis_today(now.date())
            self._export_daily_bundle_today(now.date())

    def _export_daily_bundle_today(self, target_date) -> None:
        """분석용 일일 번들(exports/bundle_YYYYMMDD.zip)을 생성합니다.

        2026-08-06 (1I단계): 분석 때마다 signal_log.csv(65MB,
        22만행)와 app.log(9MB) 전체를 올려야 했음. 해당 거래일
        몫만 잘라내면 실측 74MB → 0.5MB. **모든 리포트가 생성된
        뒤에 실행**해야 그날 리포트까지 함께 담기므로 마지막에 둠.
        """
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "export_daily_bundle.py", date_str],
                capture_output=True, text=True, timeout=300,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[EXPORT] 분석용 일일 번들 생성 완료 → exports/ 저장")
            else:
                self.app_logger.warning(f"[EXPORT] 번들 생성 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[EXPORT] 번들 생성 오류: {exc}")

    def _run_shadow_analysis_today(self, target_date) -> None:
        """장 마감 후 shadow 관측 데이터 리포트를 자동 생성합니다.

        2026-08-06 (1H단계): 1E(MACD)·1E.5~1E.7(VWAP) shadow가
        쌓는 데이터를 읽는 코드가 지금까지 전혀 없었음 — 기존
        리포트 6종은 shadow 필드를 참조하지 않아, 확인할 때마다
        즉석 스크립트를 써야 했음. 다른 분석과 동일하게
        subprocess로 실행하고 reports/에 저장하는 패턴을 따름.
        읽기 전용 후처리라 매매 판단에는 영향이 없음.
        """
        try:
            import subprocess, sys
            date_str = target_date.strftime("%Y-%m-%d")
            result = subprocess.run(
                [sys.executable, "analyze_shadow.py", date_str],
                capture_output=True, text=True, timeout=120,
                cwd=str(Path(__file__).resolve().parents[2]),
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                self.app_logger.info("[ANALYSIS] shadow 관측 분석 완료 → reports/ 저장")
            else:
                self.app_logger.warning(f"[ANALYSIS] shadow 관측 분석 실패:\n{result.stderr}")
        except Exception as exc:
            self.app_logger.warning(f"[ANALYSIS] shadow 관측 분석 오류: {exc}")

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

        # ── 1P0.2: 미결 주문 중 재매수 차단 ─────────────────────
        # 8/10 006360/017900: BUY 부분체결로 BUY_PENDING인 상태에서
        # 추가 주문이 겹쳐 UNEXPECTED_QUANTITY_INCREASE(ERROR)가
        # 발생했습니다. ERROR 상태에서의 매수도 함께 막습니다 —
        # 사람이 확인하지 않은 이상치 위에 새 포지션을 쌓으면 안 됩니다.
        # 1P0.3: order_block_reason에는 **파라미터 없는 code**만 넣습니다.
        # 상세(expected_final=174, observed=7 등)를 그대로 넣으면 값마다
        # 다른 사유가 되어 skip_reason 분포가 오염됩니다.
        _buy_block = self._position_state_machine.would_block_buy_detail(symbol)
        if _buy_block:
            _code, _detail = _buy_block
            self.app_logger.warning(
                f"[LIFECYCLE_BLOCK] {symbol} BUY 차단 | {_code} | {_detail}"
            )
            return _code

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
        # 2026-07-28 (6차 GPT 코드리뷰 지적 5번, "1B Safety Closure"):
        # 기존 datetime.now()(naive, 시스템 로컬시각)는 서버가 UTC로
        # 설정된 환경(AWS 등)에서 실제 KST 14:50과 어긋나는 시각을
        # 기준으로 판정했고, 테스트도 실행 시각(예: 컨테이너가 UTC
        # 23시대일 때)에 따라 우연히 걸리거나 안 걸리는 flaky 문제가
        # 있었음(1B.6절에서 재현 확인). now_kst()(1B.8에서 신설한
        # tzdata 비의존 고정 UTC+9)로 교체 — 이제 서버 로컬시각과
        # 무관하게 항상 정확한 KST 14:50 기준으로 판정.
        _now_kst = now_kst()
        if _now_kst.hour > 14 or (_now_kst.hour == 14 and _now_kst.minute >= 50):
            self.app_logger.info(
                f"[BLOCK] {symbol} | 14:50 이후 신규매수 차단 "
                f"({_now_kst.strftime('%H:%M:%S')} KST)"
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
        # 2026-08-05 (3차 GPT 코드리뷰 지적 P1): 브로커 응답을
        # 즉시 저장 — _write_signal_log()가 이 시점의 실제 접수
        # 여부(result.accepted)를 order_accepted로 정확히 반영할
        # 수 있도록. final_decision="BUY"라도 result.accepted가
        # False일 수 있음(브로커 거부) — 이전엔 이 구분이 전혀
        # 없었음.
        self._last_order_attempt_by_symbol[symbol] = result

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

    # 손절·강제청산으로 간주할 사유 키워드. 이 사유의 매도는 guard를
    # 우회합니다 — 막히면 손실이 무한정 커지기 때문입니다.
    FORCED_EXIT_KEYWORDS = ("손절", "강제청산", "이월 방지", "stop", "긴급")
    # 1P0.4: 강제 매도도 최소 간격을 둡니다(무제한 재시도 방지).
    FORCED_SELL_MIN_INTERVAL_SEC = 30

    @classmethod
    def _is_forced_exit_reason(cls, exit_reason: str) -> bool:
        text = str(exit_reason or "")
        return any(k in text for k in cls.FORCED_EXIT_KEYWORDS)

    # ── 1P0.2: 모든 SELL 경로의 단일 관문 ──────────────────────
    # 8/10 047040은 "매도가능수량 부족"을 16초 간격으로 11회 반복
    # 했습니다. 1P0.1은 lifecycle 상태만 고쳤을 뿐 `_try_sell` 호출
    # 자체를 막지 못했습니다 — 호출부마다 guard를 넣으면 새 경로가
    # 생길 때 빠지므로, `_try_sell`을 dispatcher로 만들어 **모든
    # 경로가 반드시 통과**하게 합니다.
    #
    # force=True는 하드 손절·강제청산처럼 반드시 나가야 하는 경로용
    # 우회구입니다. guard가 잘못 잠겨 청산이 막히는 상황을 피하기
    # 위한 안전판이며, 사용 시 반드시 로그에 남깁니다.
    def _try_sell(
        self,
        symbol: str,
        quantity: int,
        current_price: int = 0,
        exit_reason: str = "",
        avg_buy_price: int = 0,
        force: bool = False,
    ) -> None:
        """매도 결정을 상태머신 단일 진입점에 위임합니다 (1P0.5).

        1P0.2~1P0.4는 서비스 계층과 상태머신 양쪽에 차단 규칙이
        흩어져 있어 조합을 검증하기 어려웠습니다. 이제 판단은
        `decide_sell()` 한 곳에서만 하고, 여기서는 결과를 기록하고
        실행할지만 정합니다.
        """
        if not force and self._is_forced_exit_reason(exit_reason):
            force = True
        decision = self._position_state_machine.decide_sell(symbol, forced=force)

        if decision.decision.value == "RECONCILIATION_REQUIRED":
            # 2026-08-10 (1P0.7, GPT 코드리뷰): HARD block이 5분 이상
            # 지속됐다는 신호입니다. 매도를 허용하지 않습니다 — 원
            # 주문 상태를 모르는 채로 새 주문을 더 얹는 것보다, 사람이
            # 브로커에서 직접 확인하는 편이 안전합니다. 손절이어도
            # 여기서 막힙니다(의도된 동작).
            self.app_logger.critical(
                f"[RECONCILIATION_REQUIRED] {symbol} SELL 차단(장시간) | "
                f"{decision.code} | {decision.detail} | "
                f"요청수량={quantity} 사유={exit_reason} — 브로커에서 직접 확인 필요"
            )
            return
        if decision.decision.value == "BLOCKED":
            self.app_logger.warning(
                f"[LIFECYCLE_BLOCK] {symbol} SELL 차단 | {decision.code} | "
                f"{decision.detail} | 요청수량={quantity} 사유={exit_reason}"
            )
            return
        if decision.decision.value == "THROTTLED":
            self.app_logger.warning(
                f"[LIFECYCLE_FORCE_THROTTLE] {symbol} 강제매도 최소간격 미달 | "
                f"{decision.code} | 사유={exit_reason}"
            )
            return
        if decision.decision.value == "ALLOW_FORCED":
            self.app_logger.warning(
                f"[LIFECYCLE_FORCE] {symbol} 강제 매도 경로 | 사유={exit_reason}"
            )

        self._try_sell_unchecked(
            symbol, quantity, current_price, exit_reason, avg_buy_price,
        )

    def _try_sell_unchecked(
        self,
        symbol: str,
        quantity: int,
        current_price: int = 0,
        exit_reason: str = "",
        avg_buy_price: int = 0,
    ) -> None:
        """실제 매도 주문 발행. **직접 호출하지 마십시오** — guard를
        건너뜁니다. 반드시 `_try_sell()`을 통해 호출하십시오."""
        order = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=quantity)
        # ── 포지션 상태머신 shadow 통지 (2026-07-22) ────────────────
        # on_sell_result는 여기서 바로 호출하지 않음 — "체결됐는지"는
        # 다음 폴링에서 브로커 잔고를 다시 조회해야 알 수 있으므로,
        # 실제 판정 흐름과 동일하게 _sync_position_state_machine_shadow가
        # 다음 폴링에서 처리하도록 SELL_PENDING만 표시.
        self._position_state_machine.on_sell_requested(symbol, quantity, "pending")
        result = self.broker.place_order(order)
        if not result.accepted:
            self._position_state_machine.on_sell_result(
                symbol, accepted=False, broker_quantity=0,
                reject_reason=str(getattr(result, "message", "") or "")[:80],
            )
            # 1P0.6: 강제 매도(손절·강제청산)가 브로커에 거부되면
            # 시스템이 자체 해결할 수 없습니다 — 조용히 넘기지 않고
            # CRITICAL로 즉시 노출합니다.
            if self._is_forced_exit_reason(exit_reason):
                self._forced_sell_failures[symbol] = (
                    self._forced_sell_failures.get(symbol, 0) + 1
                )
                self.app_logger.critical(
                    f"[FORCED_SELL_FAILED] {symbol} 강제 매도 거부 "
                    f"(연속 {self._forced_sell_failures[symbol]}회) | "
                    f"사유={exit_reason} | 브로커={getattr(result, 'message', '')}"
                )
        else:
            self._forced_sell_failures.pop(symbol, None)

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
                "condition_name": self._representative_condition_name(symbol),
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

            # 2026-08-10 (1P0.7, GPT 코드리뷰, 재현 확인): accepted != full fill.
            # 047040에서 SELL accepted 후 343주가 남았던 실측 사례처럼, 부분체결이면
            # quantity(=요청 수량) 기준으로 손실/쿨다운/재진입차단/알림을 지금 계산하면
            # 안 됩니다 — 실제 포지션이 아직 살아있을 수 있습니다. side effect는 브로커가
            # 잔고 0(완전 청산)을 확인해줄 때까지 보류하고, 컨텍스트만 저장합니다.
            # sync 루프가 FLAT 확정을 감지하면 _apply_deferred_sell_side_effects()를
            # 호출합니다.
            self._pending_sell_side_effects[symbol] = {
                "exit_reason": exit_reason,
                "avg_buy_price": avg_buy_price,
                "current_price": current_price,
                "quantity": quantity,
                "recorded_at": datetime.now(),
            }
            self.app_logger.info(
                f"[ORDER] {symbol} | 매도 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매도 주문 실패 | 사유: {result.message}"
            )


    def _apply_deferred_sell_side_effects(self, symbol: str) -> None:
        """SELL이 완전 청산으로 확정됐을 때만 실행되는 후속 처리.

        2026-08-10 (1P0.7, GPT 코드리뷰): 손실 카운트·쿨다운·재진입
        차단·알림이 SELL `accepted` 시점에 즉시 실행돼, 047040처럼
        343주가 남은 부분체결에도 "완전 청산했다"고 전제한 계산이
        적용되고 있었습니다. sync 루프가 브로커 잔고 0(FLAT 확정)을
        본 시점에만 호출합니다.
        """
        ctx = self._pending_sell_side_effects.pop(symbol, None)
        if ctx is None:
            return
        exit_reason = ctx["exit_reason"]
        avg_buy_price = ctx["avg_buy_price"]
        current_price = ctx["current_price"]
        quantity = ctx["quantity"]

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
        market_price=None,
        latest_bar_timestamp=None,
    ) -> None:
        """시그널 판단 결과를 signal_log.csv에 기록합니다.

        BUY뿐 아니라 HOLD/SKIP도 모두 기록합니다.

        2026-08-04 (GPT 코드리뷰 지시, MACD shadow 관측 1단계):
        market_price(MACD 원시 지표를 담은 객체)를 받아 관측 필드를
        추가로 기록 — 신호 판단 로직(어떤 Signal이 반환됐는지)은
        전혀 바꾸지 않고, 순수하게 "만약 이랬다면"을 계산해서
        로그에만 남김.

        2026-08-04 (2차 GPT 코드리뷰 지적, 필드 재설계): 1차
        구현에서 두 가지 문제가 발견됨 —
        (a) "MACD 데드면 최소 5점 요구"(min-score-5, breakout_
            strategy.py에 이미 있는 chasing_overheated 확장판)와
            "MACD가 Signal 이하이면 점수와 무관하게 완전 차단"
            (hard gate, 원래 검증하려던 대상)을 would_be_blocked_
            if_macd_dead_required 필드 하나로 뭉뚱그렸음 — hard
            gate 기준에서는 True여야 할 "데드+6점" 케이스가 min5
            기준(6점이면 이미 통과)으로 계산되어 False로 나오고
            있었음.
        (b) legacy signal이 BUY가 아닌(HOLD 등) 행에서도 "차단됐을
            것"을 계산하고 있었음 — HOLD였던 판단을 "차단"이라고
            부르는 건 counterfactual의 정의 자체가 안 맞음(애초에
            매수 후보가 아니었던 것과, 매수 후보였는데 새 게이트가
            막은 것은 다른 질문).

        이제 두 가지 hard-vs-min5 필드로 명확히 분리하고, 두 필드
        모두 "legacy_buy_candidate(전략이 실제로 BUY를 반환한
        경우)"일 때만 계산 — HOLD 행은 MACD 상태만 기록하고 두
        차단 필드는 빈 값으로 남김.
        """
        import re
        m = re.search(r'(\d+)/8', signal.reason)
        score = m.group(1) if m else ""

        # 2026-08-05 (3차 GPT 코드리뷰 지적 P1): 이번 폴링에서
        # _try_buy()가 실제로 broker.place_order()를 호출했다면
        # (호출 직전에 이전 폴링 기록을 pop()으로 지워뒀으므로,
        # 여기 값이 있다는 건 정확히 "이번 폴링에서 새로 생긴
        # 기록"을 의미) 그 OrderResult를 그대로 가져와 order_
        # attempted/order_accepted/order_id/order_message에 반영.
        order_attempt = self._last_order_attempt_by_symbol.get(symbol)

        # ── MACD 상태 관측 (2026-08-04, 2차 개정) ────────────────
        # 원시값(macd/macd_signal)과 breakout_strategy.py의 실제
        # 계산식(macd > macd_signal)을 그대로 재사용 — 다른 계산식을
        # 쓰면 로그와 실제 동작이 미묘하게 어긋나는 위험이 있음.
        macd_val = ""
        macd_signal_val = ""
        macd_above_signal_val = ""
        macd_hist_dir_val = ""
        legacy_buy_candidate_val = ""
        chasing_overheated_applies_val = False
        chasing_overheated_condition_val = ""
        would_block_existing_chasing_gate_val = ""
        would_block_macd_dead_min_score5_val = ""
        would_block_macd_above_signal_required_val = ""
        latest_bar_timestamp_val = latest_bar_timestamp or ""

        has_macd = (
            market_price is not None
            and market_price.indicator_macd is not None
            and market_price.indicator_macd_signal is not None
        )
        if has_macd:
            macd_val = market_price.indicator_macd
            macd_signal_val = market_price.indicator_macd_signal
            macd_above_signal_val = market_price.indicator_macd > market_price.indicator_macd_signal
            macd_hist_dir_val = market_price.indicator_macd_hist_direction

        # legacy_buy_candidate: 전략이 이번 폴링에서 실제로 BUY를
        # 반환했는지(entry_watch/stale 데이터 차단 등 1B~1C 안전
        # 장치를 이미 전부 거친 이 함수 호출 시점의 signal 기준).
        legacy_buy_candidate_val = signal.type == SignalType.BUY

        # 2026-08-04 (GPT 코드리뷰 지적): chasing_overheated는
        # BreakoutStrategy(BULLISH 장세)에만 실제 존재하는 게이트 —
        # NEUTRAL 등 다른 전략에는 이 로직 자체가 없으므로, 그
        # 장세에서 계산하면 "적용되지도 않는 게이트가 발동했다"는
        # 거짓 신호가 됨. regime이 정확히 BULLISH일 때만 계산.
        chasing_overheated_applies_val = (regime == MarketRegime.BULLISH)

        # 2026-08-04 (2차 GPT 코드리뷰 지적, 재현 확인): 기존
        # chasing_overheated_val을 legacy_buy_candidate_val(신호가
        # BUY인 경우)일 때만 계산했었는데, 실제 BreakoutStrategy의
        # chasing_overheated 게이트가 진짜로 차단한 사례는 이미
        # signal=HOLD로 나옴 — "등락4%+MACD데드+4점"을 넣으면 전략
        # 자체가 HOLD를 반환하는데, 그 HOLD 행에서 legacy_buy_
        # candidate_val=False가 되어 chasing_overheated가 빈 값으로
        # 남고 있었음(재현 확인). 이러면 "기존 게이트가 실제로 몇
        # 건을 막았는가"를 이 필드로 전혀 집계할 수 없음.
        #
        # chasing_overheated_condition(조건 자체의 충족 여부)과
        # would_block_existing_chasing_gate(그 조건이 실제로 기존
        # 게이트를 발동시켰을지)를 BUY/HOLD와 무관하게 계산하도록
        # 분리 — legacy_buy_candidate 조건은 신규 가상 게이트
        # (would_block_macd_dead_min_score5 / would_block_macd_
        # above_signal_required)에만 그대로 유지.
        if chasing_overheated_applies_val and has_macd:
            change_rate_pct = (
                minute_analysis.change_rate_pct if minute_analysis is not None else None
            )
            chasing_overheated_condition_val = (
                minute_analysis is not None
                and change_rate_pct is not None
                and change_rate_pct >= 3.0
                and not macd_above_signal_val
            )
            if chasing_overheated_condition_val and score:
                would_block_existing_chasing_gate_val = int(score) < 5
            elif chasing_overheated_condition_val:
                # 조건은 충족했는데 score를 파싱 못 한 경우(8점
                # 체계 자체가 안 돌아간 특수 케이스) — 판단 불가이므로
                # "차단 안 됨(False)"으로 단정하지 않고 빈 값 유지.
                would_block_existing_chasing_gate_val = ""
            else:
                # 조건 자체가 거짓이면 기존 게이트가 발동할 여지가
                # 없으므로 False로 명시(빈 값이 아님 — "발동 안 함"
                # 이 명확한 사실이므로).
                would_block_existing_chasing_gate_val = False

        if legacy_buy_candidate_val and has_macd:
            # (1) hard gate: MACD가 Signal 이하이면 점수와 무관하게
            # 완전 차단됐을지 — 원래 검증 대상.
            would_block_macd_above_signal_required_val = not macd_above_signal_val

            # (2) min-score-5: "MACD 데드면 최소 5점 요구"(기존
            # chasing_overheated 확장판) — score가 파싱 안 되는
            # 경우(8점 체계 자체가 안 돌아간 특수 케이스)는 판단
            # 불가이므로 빈 값으로 남김.
            if not macd_above_signal_val and score:
                would_block_macd_dead_min_score5_val = int(score) < 5

        patterns = []
        row: dict = {
            # 2026-08-04 (GPT 코드리뷰 지적): datetime.now()는 시스템
            # 로컬 시각 — UTC 컨테이너 등에서는 latest_bar_timestamp
            # (KST 기준 분봉 시각)와 최대 9시간까지 어긋날 수 있음
            # (재현 확인). now_kst()로 통일하되, 기존 CSV의 timestamp
            # 컬럼 포맷(타임존 표기 없는 ISO 문자열)과의 호환을 위해
            # tzinfo는 제거하고 기록 — 값 자체는 KST 벽시계 시각.
            "timestamp": now_kst().replace(tzinfo=None).isoformat(),
            "symbol":    symbol,
            "price":     price,
            "regime":    regime.value if regime else "",
            "score":     score,
            "signal":    signal.type.value,
            "skip_reason": classify_skip_reason(signal.reason, signal.type.value),
            "final_decision":    final_decision or signal.type.value,
            "order_block_reason": order_block_reason,
            "condition_name": self._representative_condition_name(symbol),
            "macd": macd_val,
            "macd_signal": macd_signal_val,
            "macd_above_signal": macd_above_signal_val,
            "macd_hist_direction": macd_hist_dir_val,
            "legacy_buy_candidate": legacy_buy_candidate_val,
            "latest_bar_timestamp": latest_bar_timestamp_val,
            "chasing_overheated_applies": chasing_overheated_applies_val,
            "chasing_overheated_condition": chasing_overheated_condition_val,
            "would_block_existing_chasing_gate": would_block_existing_chasing_gate_val,
            "would_block_macd_dead_min_score5": would_block_macd_dead_min_score5_val,
            "would_block_macd_above_signal_required": would_block_macd_above_signal_required_val,
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

        # ── VWAP shadow 관측 (2026-08-05, 1E.5단계, GPT 코드리뷰 지시) ──
        # entry_quality_guard_mode가 "off"(기본값)면 계산 자체를
        # 건너뜀 — MACD shadow(1E단계)와 달리 이건 신규 기능이라
        # 명시적으로 shadow를 켜야 계산됨. "shadow"일 때만 evaluate_
        # vwap_shadow()를 호출하고, 그 결과를 signal_log.csv(상태값
        # 요약)와 entry_quality_shadow.csv(legacy BUY 후보 전용,
        # 상세 8개 would_block_*)에 각각 기록. Signal이나 주문 결과는
        # 이 블록이 절대 건드리지 않음 — 이미 위에서 row가 전부
        # 완성된 뒤 관측치만 추가하는 순수 로깅 단계.
        guard_mode = getattr(self.settings.experimental, "entry_quality_guard_mode", "off")
        if guard_mode == "shadow":
            condition_names = self._symbol_to_conditions.get(symbol, ())
            # 2026-08-05 (2차 GPT 코드리뷰 지적 1번): 딕셔너리에
            # 아예 없는 경우도 "신뢰 불가"로 취급해야 함(get()의
            # 기본값을 False로 명시) — "이 종목에 대해 아무 정보도
            # 없다"와 "신뢰 가능하다고 확인됐다"를 혼동하면 안 됨.
            condition_source_reliable = self._symbol_condition_source_reliable.get(symbol, False)
            session_metrics = self._latest_session_metrics_by_symbol.get(symbol)
            # 2026-08-05: 세션 값이 오늘 거래일 것이 아니면(예: 어제
            # 계산된 값이 자정을 넘겨 그대로 남아있는 극단적 상황)
            # 사용하지 않음 — merge_session_bars()가 날짜 변경 시
            # 자동으로 새 세션을 시작하므로 이 경로는 실제로는 거의
            # 발생하지 않지만, 방어적으로 재확인.
            if session_metrics is not None and session_metrics.session_date != now_kst().strftime("%Y%m%d"):
                session_metrics = None

            assessment = evaluate_vwap_shadow(
                legacy_buy_candidate=legacy_buy_candidate_val,
                current_price=price,
                minute_analysis=minute_analysis,
                condition_names=condition_names,
                condition_source_reliable=condition_source_reliable,
                session_metrics=session_metrics,
            )

            row.update({
                "is_pr": assessment.is_pr,
                "is_c": assessment.is_c,
                "is_pullback_condition": assessment.is_pullback_condition,
                "condition_names": "|".join(assessment.condition_names),
                "rolling_vwap": assessment.rolling_vwap if assessment.rolling_vwap is not None else "",
                "rolling_vwap_distance_pct": (
                    round(assessment.rolling_vwap_distance_pct, 2)
                    if assessment.rolling_vwap_distance_pct is not None else ""
                ),
                "session_vwap": assessment.session_vwap if assessment.session_vwap is not None else "",
                "session_vwap_distance_pct": (
                    round(assessment.session_vwap_distance_pct, 2)
                    if assessment.session_vwap_distance_pct is not None else ""
                ),
                "session_metrics_ready": assessment.session_metrics_ready,
                "session_readiness_reason": assessment.session_readiness_reason,
            })

            # 전용 CSV는 legacy BUY 후보에만 기록(GPT 지시) — 같은
            # 분봉·같은 패턴·같은 점수의 중복 폴링은 로거 내부에서
            # 자동으로 걸러짐(append_if_new의 키 기준).
            if legacy_buy_candidate_val:
                self.entry_quality_shadow_logger.append_if_new({
                    "timestamp": now_kst().replace(tzinfo=None).isoformat(),
                    "symbol": symbol,
                    "latest_bar_timestamp": latest_bar_timestamp_val,
                    "detected_patterns": row.get("detected_patterns", "-"),
                    "score": score,
                    "regime": regime.value if regime else "",
                    "condition_name": self._representative_condition_name(symbol),
                    "condition_names": "|".join(assessment.condition_names),
                    "condition_source_reliable": condition_source_reliable,
                    # 2026-08-05 (2차 GPT 코드리뷰 지적 3번): 실제
                    # 진입 기준값 — legacy_buy_candidate=True라도
                    # 실제 주문됐는지, 기존 규칙(DAILY_ENTRY_LIMIT/
                    # AFTER_1450/RISK_LIMIT 등)으로 이미 차단된
                    # 후보인지 이 필드들로 구분 가능. current_price
                    # 가 있어야 5·10·20분 후 수익률도 다른 로그와
                    # 복잡한 조인 없이 직접 계산 가능.
                    "current_price": price,
                    "legacy_reason": signal.reason,
                    "final_decision": final_decision or signal.type.value,
                    "order_block_reason": order_block_reason,
                    "order_attempted": order_attempt is not None,
                    "order_accepted": order_attempt.accepted if order_attempt is not None else "",
                    "order_id": order_attempt.order_id if order_attempt is not None else "",
                    "order_message": order_attempt.message if order_attempt is not None else "",
                    "macd": macd_val,
                    "macd_signal": macd_signal_val,
                    "macd_above_signal": macd_above_signal_val,
                    "would_block_macd_dead_min_score5": would_block_macd_dead_min_score5_val,
                    "would_block_macd_above_signal_required": would_block_macd_above_signal_required_val,
                    "is_pr": assessment.is_pr,
                    "is_c": assessment.is_c,
                    "is_pullback_condition": assessment.is_pullback_condition,
                    "is_pr_or_pullback_condition": assessment.is_pr_or_pullback_condition,
                    "rolling_vwap": assessment.rolling_vwap if assessment.rolling_vwap is not None else "",
                    "rolling_vwap_distance_pct": (
                        round(assessment.rolling_vwap_distance_pct, 4)
                        if assessment.rolling_vwap_distance_pct is not None else ""
                    ),
                    "session_vwap": assessment.session_vwap if assessment.session_vwap is not None else "",
                    "session_vwap_distance_pct": (
                        round(assessment.session_vwap_distance_pct, 4)
                        if assessment.session_vwap_distance_pct is not None else ""
                    ),
                    "session_metrics_ready": assessment.session_metrics_ready,
                    "session_readiness_reason": assessment.session_readiness_reason,
                    "session_gate_eligible": assessment.session_gate_eligible,
                    "would_block_pr_only_rolling_vwap": assessment.would_block_pr_only_rolling_vwap,
                    "would_block_c_or_pr_rolling_vwap": assessment.would_block_c_or_pr_rolling_vwap,
                    "would_block_pullback_condition_rolling_vwap": (
                        assessment.would_block_pullback_condition_rolling_vwap
                    ),
                    "would_block_pr_or_pullback_condition_rolling_vwap": (
                        assessment.would_block_pr_or_pullback_condition_rolling_vwap
                    ),
                    "would_block_pr_only_session_vwap": assessment.would_block_pr_only_session_vwap,
                    "would_block_c_or_pr_session_vwap": assessment.would_block_c_or_pr_session_vwap,
                    "would_block_pullback_condition_session_vwap": (
                        assessment.would_block_pullback_condition_session_vwap
                    ),
                    "would_block_pr_or_pullback_condition_session_vwap": (
                        assessment.would_block_pr_or_pullback_condition_session_vwap
                    ),
                })

        self.signal_logger.append(row)