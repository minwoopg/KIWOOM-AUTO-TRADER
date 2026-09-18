from __future__ import annotations

"""설정 파일을 읽어서 파이썬 객체로 변환하는 모듈."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class AppConfig:
    name: str
    env: str
    log_level: str


@dataclass(frozen=True)
class BrokerConfig:
    provider: str
    use_mock: bool
    base_url: str
    app_key: str
    secret_key: str
    account_number: str
    is_paper_trading: bool
    # 2026-09-18 (우선순위1 1차: 체결조회 증거 독립 저장): 계좌를
    # 구분하는 **비민감** 식별자(예: "acct-a") — 실계좌번호(account_number)
    # 자체는 아니며, 관측 저장소의 dedup·커버리지 집계 키에만 쓰입니다.
    # 기본값 ""(빈 문자열)은 "라벨 미설정" 상태이며, 이 경우 관측
    # 기능만 비활성화됩니다(기동 자체를 막지 않음 — 아래
    # `observation_enabled` 참고). settings.yaml에 이 키가 없어도
    # 하위호환으로 정상 로드됩니다(기존 설정 파일 변경 불필요).
    account_scope_id: str = ""

    @property
    def observation_enabled(self) -> bool:
        """`account_scope_id`가 비어있지 않을 때만 체결조회 증거
        관측 기능이 활성화됩니다. 이 값이 False라고 해서 프로그램
        기동 자체를 막지 않습니다 — 매매 로직과 무관한 부가 계측
        기능이므로, 라벨이 없으면 그 계측만 꺼지고 커버리지는
        "0%"가 아니라 "계측 비활성"으로 별도 표시되어야 합니다."""

        return bool(self.account_scope_id.strip())


@dataclass(frozen=True)
class TradingConfig:
    poll_interval_seconds: int
    balance_refresh_seconds: int
    price_refresh_seconds: int
    order_cash_per_trade: int
    max_positions: int
    allow_multiple_entries_per_symbol_per_day: bool
    force_exit_before_market_close_minutes: int
    reentry_cooldown_seconds: int
    trail_loss_cooldown_count: int = 1  # 트레일링 손실 N회 → 60분 쿨다운
    excluded_symbols: list = None       # 신규매수 영구 차단 종목
    force_exit_cushion_pct: float = 0.3  # 이월방지 강제청산 — 이 수익률 이상이면 이월 허용
    max_entries_per_symbol_per_day: int = 3  # 2026-07-16: allow_multiple_entries=True일 때도
                                               # 종목당 진입 횟수 상한 (무제한 재진입으로 인한
                                               # 단일종목 과집중 방지 — 7/15 475150 사례:
                                               # 7회 재진입, 41,766,000원 투입)
    # ── 보유종목 사이 폴링 간격 (2026-07-22) ──────────────────────
    # 기존엔 보유/미보유 구분 없이 종목마다 1.0초씩 sleep해서, 보유종목이
    # 여러 개면(최대 max_positions=5) 뒤쪽 보유종목의 손절판단이 그만큼
    # 늦어졌음(GPT 검토 지적, 7.11절 우선순위 정렬로 1차 완화했으나
    # 보유종목끼리는 여전히 1초씩 대기). API 레이트리밋 우려로 완전히
    # 0으로는 두지 않고, 미보유종목(entry_poll_gap_seconds, 기존 1.0초
    # 유지)보다 짧게 별도 설정. 기본값을 기존과 동일한 1.0으로 둬서
    # 하위호환 유지 — settings.yaml에서 명시적으로 낮춰야 실제로 빨라짐.
    held_symbol_poll_gap_seconds: float = 1.0
    entry_poll_gap_seconds: float = 1.0
    # 2026-09-15 (180초 감시 공백 대응 3단계 — 관측 경로 연결): 잔고
    # 장애(429 등) 중 청산 후보를 관측할 때, 캐시된 시세를 얼마나
    # 오래된 것까지 신뢰하고 evaluate_exit_candidate()를 호출할지의
    # 상한(초). 이 값을 넘으면 계산 자체를 시도하지 않고 "평가 보류
    # (stale_price)"로만 기록한다. 초기 설정값이며, 실측 후 조정 대상.
    exit_candidate_outage_max_price_age_seconds: int = 120
    # 2026-09-15 (같은 단계): 잔고 재시도 대기(기존 180초 백오프) 중
    # observe_exit_candidates_during_outage()를 반복 호출하는 간격(초).
    # 전체 대기 시간(trading_loop()가 넘기는 값, 기본 180초) 자체는
    # 이번 단계에서 바꾸지 않는다 — 그 180초를 한 번에 자지 않고 이
    # 간격으로 쪼개 관측 기회를 유지할 뿐이다. 초기 설정값이며, 실측
    # 후 조정 대상.
    balance_outage_observe_interval_seconds: float = 15.0
    # 2026-09-15 (180초 감시 공백 대응 3단계 3차 보완 — GPT 재검토
    # 1번 지적 반영): _get_market_price_with_cache()가 시세 재조회에
    # 실패한 뒤, 같은 종목에 대해 다시 브로커를 호출하기 전 최소한
    # 기다리는 시간(초). 이게 없으면 잔고 장애 재시도 대기 중
    # 관측 간격(예: 15초)마다 시세 API를 계속 재호출해 429를 반복
    # 유발한다(재현: 60/75/90초 세 번 모두 재호출·429). price_refresh_
    # seconds(기본 60)와 같은 기본값을 둬 기존 재조회 빈도 자체는
    # 바꾸지 않되, 실패가 감지된 이후에만 추가로 억제한다. 초기
    # 설정값이며, 실측 후 조정 대상.
    market_price_retry_backoff_seconds: float = 60.0
    # 2026-09-15 (독립 잔고 재시도 설계, GPT 재검토 반영): trading_loop()가
    # 잔고 API 429를 감지하면 더 이상 180초를 통째로 자지 않고, 이
    # 범위(최소~최대) 안에서 실패마다 2배씩 늘어나는(성공 시 최소로
    # 리셋) monotonic 절대시각 기준 백오프로 독립 재시도한다
    # (TradingService.enter_balance_outage()/handle_balance_outage_tick()
    # 참고). 최댓값(180초)은 기존 wait_out_balance_outage()가 쓰던 총
    # 대기시간과 동일해 "최악의 경우 오늘보다 회복이 느려지지 않는다"는
    # 조건을 만족한다. 최솟값(30초)은 이 API 계열에 이미 안전하다고
    # 받아들여진 ORDER_STATUS_QUERY_MIN_INTERVAL_SEC과 같은 값을
    # 재사용했다(2026-09-14 설계 문서 v2 §3 제안값). 초기 설정값이며,
    # 실측 후 조정 대상 — "안전성이 입증된 값"이라는 뜻은 아니다.
    balance_retry_backoff_min_seconds: float = 30.0
    balance_retry_backoff_max_seconds: float = 180.0

    def __post_init__(self):
        # frozen=True dataclass라 self.x = ... 직접 대입은 FrozenInstanceError.
        # object.__setattr__로 우회 (2026-07-20: excluded_symbols=None으로
        # 생성될 때 FrozenInstanceError가 나던 잠재 버그를 테스트 중 발견 —
        # 지금까지 settings.yaml이 항상 excluded_symbols: 리스트를 명시해서
        # None 케이스가 실제로 발생한 적이 없었을 뿐이었음)
        if self.excluded_symbols is None:
            object.__setattr__(self, "excluded_symbols", [])

        # 2026-09-15 (독립 잔고 재시도 설계, GPT 재검토 반영): "양수·
        # 유한값·최솟값≤최댓값 검증 추가" 지적 반영 — 잘못된 설정값이
        # (예: 음수, 0, inf, min>max) 조용히 통과해 재시도 백오프가
        # 전혀 늘지 않거나 즉시 무한대가 되는 것을 시작 시점에 막는다.
        for field_name in ("balance_retry_backoff_min_seconds", "balance_retry_backoff_max_seconds"):
            value = getattr(self, field_name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(
                    f"TradingConfig.{field_name}는 양의 유한한 숫자여야 합니다 (받은 값: {value!r})"
                )
        if self.balance_retry_backoff_min_seconds > self.balance_retry_backoff_max_seconds:
            raise ValueError(
                "TradingConfig.balance_retry_backoff_min_seconds는 "
                "balance_retry_backoff_max_seconds보다 클 수 없습니다 "
                f"(min={self.balance_retry_backoff_min_seconds}, "
                f"max={self.balance_retry_backoff_max_seconds})"
            )


@dataclass(frozen=True)
class StrategyConfig:
    """매매 전략 파라미터를 보관합니다."""

    name: str
    breakout_threshold_pct: float
    take_profit_pct: float        # 안전망 익절 기준
    stop_loss_pct: float
    reference_price_type: str
    trailing_stop_pct: float      # 최고가 대비 하락 기준
    trailing_start_pct: float     # 트레일링 시작 최소 수익률
    trend_reversal_rsi: float     # 추세 꺾임 감지 RSI 기준
    symbol_min_score_override: dict = None  # {종목코드: 최소진입점수} 개별 종목 진입문턱 상향
    disable_score3_buy: bool = False               # 3점("보수적 진입") 매수 비활성화
    low_upside_guard_enabled: bool = False          # 상승여력 부족 시 5점 미만 차단
    min_upside_to_recent_high_pct: float = 1.0      # 이 값 미만이면 low_upside_guard 발동
    low_upside_guard_apply_to_pattern_d: bool = False  # 패턴D(갭눌림)에도 상승여력 게이트 적용
    require_confirmation_for_score5: bool = False   # 5점 진입도 거래량급증/V자/PR/반등spike 중 최소 1개 요구
    slow_v_enabled: bool = False   # 2026-07-16: 느린 V자 탐지는 구현했으나 34일 백테스트
                                     # 결과 전 구간 마이너스(5분 승률25%/-0.64%)라 실거래
                                     # 반영은 비활성. 감지 자체(로그/분석용)는 계속 동작.


@dataclass(frozen=True)
class MarketRegimeConfig:
    """장세 분류기에 사용할 파라미터를 보관합니다."""

    short_ma_days: int
    long_ma_days: int
    history_days: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    history_refresh_seconds: int
    macd_fast: int
    macd_slow: int
    macd_signal: int
    volume_surge_ratio: float
    minute_tick_scope: int
    minute_bar_count: int
    minute_refresh_seconds: int
    min_trading_value: int
    pullback_min_pct: float
    pullback_max_pct: float
    change_rate_min: float
    change_rate_max: float
    rebound_min_pct: float
    gap_pullback_min_pct: float
    gap_pullback_max_pct: float
    # V자 반등 감지 파라미터
    v_bottom_lookback: int
    v_low_min_age: int
    v_low_max_age: int
    v_drop_threshold_pct: float
    v_rebound_threshold_pct: float
    v_max_rebound_pct: float
    v_volume_ratio: float
    v_min_bar_amount: int
    v_bottom_spike_ratio: float
    v_ma5_slope_bars: int
    # 느린 V자(Slow V) 반등 감지 파라미터 (2026-07-16)
    slow_v_bottom_lookback: int = 150
    slow_v_low_min_age: int = 9
    slow_v_low_max_age: int = 120
    slow_v_drop_threshold_pct: float = -2.5
    slow_v_rebound_threshold_pct: float = 0.5
    slow_v_max_rebound_pct: float = 15.0
    slow_v_volume_ratio: float = 1.0
    # 2026-07-28 (GPT 코드리뷰 지적, stale 분봉 안전장치 2단계): API
    # 호출이 예외 없이 성공해도 반환된 최신 봉이 실제로 신선한지
    # (오늘 날짜인지, 너무 오래된 봉이 아닌지)는 별도로 확인해야 함
    # — 재현 확인: API가 정상적으로 과거(전거래일) 봉을 반환해도
    # 예외가 없다는 이유만으로 신선하다고 오판정되고 있었음. 1분봉
    # 기준 120초를 보수적인 기본값으로 사용(장중 지연·재시도를
    # 고려한 여유). tick_scope가 커지면 이 값도 비례해서 늘려야
    # 함(예: 3분봉이면 최소 180초 이상).
    minute_bar_max_age_seconds: int = 120
    # 2026-07-28 (5차 GPT 코드리뷰 지적): 신규 진입 판단에 쓰기 위한
    # 최소 분봉 개수. minute_bar_count(60)와 동일하게 보수적으로
    # 설정 — 재현 확인: 이 검증이 없으면 API가 분봉을 1개만
    # 반환해도(또는 60개 전부 동일 timestamp라도) age 조건만
    # 통과하면 entry_safe=True가 되어, MinuteAnalyzer.analyze()가
    # None을 반환하는데도(또는 사실상 무의미한 분석 결과로) 신규
    # 매수가 나갈 수 있었음.
    minute_bar_min_count_for_entry: int = 60
    # 2026-07-28 (GPT 코드리뷰 지적 5번): 분봉 조회 실패/빈응답이
    # 연속될 때, 성공 캐시 시각(minute_refresh_seconds)과 무관하게
    # 이 시간 동안은 재시도하지 않음 — 매 폴링마다 실패하는 API를
    # 계속 두드리는 것을 방지(이전 HTTP 429 재발 방지 목적).
    minute_fetch_backoff_seconds: int = 20

    def __post_init__(self):
        # 2026-07-28 (6차 GPT 코드리뷰 지적 6번, "1B Safety Closure"):
        # 이번 라운드에서 신설한 분봉 안전 설정들(minute_bar_count,
        # minute_bar_min_count_for_entry, minute_bar_max_age_seconds,
        # minute_fetch_backoff_seconds)이 서로 모순되거나 비상식적인
        # 값으로 설정되면(예: 최소진입개수가 요청개수보다 큼) 안전
        # 장치 자체가 항상 차단되거나 항상 무력화되는 상황이 생길 수
        # 있음 — 설정 로드 시점에 즉시 검증해 조용한 오설정을 방지.
        if self.minute_bar_count <= 0:
            raise ValueError(
                f"market_regime.minute_bar_count는 0보다 커야 합니다 "
                f"(현재: {self.minute_bar_count})"
            )
        if not (1 <= self.minute_bar_min_count_for_entry <= self.minute_bar_count):
            raise ValueError(
                f"market_regime.minute_bar_min_count_for_entry는 "
                f"1 이상 minute_bar_count({self.minute_bar_count}) 이하여야 합니다 "
                f"(현재: {self.minute_bar_min_count_for_entry})"
            )
        if self.minute_bar_max_age_seconds <= 0:
            raise ValueError(
                f"market_regime.minute_bar_max_age_seconds는 0보다 커야 합니다 "
                f"(현재: {self.minute_bar_max_age_seconds})"
            )
        if self.minute_fetch_backoff_seconds < 0:
            raise ValueError(
                f"market_regime.minute_fetch_backoff_seconds는 0 이상이어야 합니다 "
                f"(현재: {self.minute_fetch_backoff_seconds})"
            )


@dataclass(frozen=True)
class WebSocketConfig:
    """조건검색 WebSocket 연결 설정입니다."""

    enabled: bool
    url: str
    condition_seqs: list  # 동시 구독할 조건식 번호 목록
    max_symbols: int
    app_key: str
    secret_key: str


@dataclass(frozen=True)
class RiskConfig:
    max_daily_loss_amount: int
    max_consecutive_losses: int
    max_order_amount: int
    min_cash_buffer: int


@dataclass(frozen=True)
class EntryWatchConfig:
    """매수 후 실패한 V자를 빠르게 정리하는 entry watch 설정입니다."""
    enabled: bool
    watch_minutes: int
    min_profit_pct: float
    fail_cut_pct: float
    fail_on_vwap_break: bool
    # ── VWAP 이탈 히스테리시스 (2026-07-22) ──────────────────────
    # 7/21 첫 실거래 4건 중 VWAP이탈청산 2건이 -4.83%/-0.43%로
    # 손실 컸음. 단일 폴링 시점 판단(price_above_vwap 한 번만 False)
    # 이라 노이즈에 취약 — 매수 직후 VWAP을 잠깐 밑돌았다가 반등하는
    # 흔한 패턴에서도 바로 청산됨. 연속 확인 횟수/이탈폭 하한/매수
    # 직후 유예시간 세 가지로 완화. 기본값은 기존 동작(즉시 1회
    # 이탈로 청산)과 동일하게 둬서 하위호환 유지 — yaml에서 명시
    # 조정해야 히스테리시스가 실제로 걸림.
    vwap_break_confirm_count: int = 1     # 연속 이탈 확인 횟수 (1=즉시청산, 기존과 동일)
    vwap_break_min_pct: float = 0.0       # VWAP 대비 이탈폭 하한(%). 0=이탈 즉시 카운트
    vwap_grace_seconds: int = 0           # 매수 후 이 시간 동안은 VWAP 청산 미적용


@dataclass
class KakaoConfig:
    access_token:  str = ""   # 카카오 액세스 토큰
    refresh_token: str = ""   # 리프레시 토큰 (자동 갱신용)
    rest_api_key:  str = ""   # REST API 키 (토큰 갱신용)
    # 2026-09-11: 카카오 앱의 "Client Secret 사용함"이 켜진 경우에만
    # 필요. 기존처럼 꺼져 있으면 빈 문자열로 두면 됨(하위호환).
    client_secret: str = ""
    # 2026-09-11: refresh로 새로 발급된 토큰을 재시작 후에도 이어 쓸 수
    # 있게 저장하는 경로. .env는 코드가 직접 고치지 않고, 이 파일(이미
    # .gitignore의 data/ 아래)에 원자적으로 저장·우선 로드함.
    token_state_file: str = "data/kakao_token_state.json"


@dataclass(frozen=True)
class StorageConfig:
    state_file: str
    trade_log_file: str
    signal_log_file: str
    app_log_file: str
    save_minute_bars: bool
    minute_bars_dir: str
    # entry_watch 반사실적(counterfactual) 비교 로그 (2026-07-22)
    entry_watch_shadow_log_file: str = "logs/entry_watch_shadow.csv"
    # 포지션 상태머신(shadow) 전이 로그 (2026-07-22)
    position_lifecycle_log_file: str = "logs/position_lifecycle.csv"
    # 진입 품질(MACD/VWAP) shadow 관측 전용 로그 (2026-08-05, 1E.5단계)
    # — legacy BUY 후보에만 기록, signal_log.csv와 별도로 관리해
    # 53MB급 signal_log.csv를 더 이상 키우지 않음.
    entry_quality_shadow_log_file: str = "logs/entry_quality_shadow.csv"
    # 1P0.8-E.1-A: durable tracked order journal (2026-08-20) — 실제
    # order_id가 확보된 BUY/SELL accepted 주문의 사실을 재시작 후에도
    # 잃지 않기 위한 원자적 JSON 저장소. state.json과 별개 파일(같은
    # data/ 디렉터리 관례를 따름) — infra/storage/tracked_order_journal.py
    # 참고. 이 라운드는 저장만 하고 startup 자동 복구는 하지 않음.
    tracked_order_journal_file: str = "data/tracked_order_journal.json"
    # Profitability Shadow v2 (2026-08-24): Low Upside skip 후보(F1/F2/F3)
    # 실시간 shadow 관측 로그 — entry_quality_guard_mode와 무관하게 항상
    # 기록됨(주문 차단 없음, 순수 관측).
    low_upside_shadow_log_file: str = "logs/low_upside_shadow.csv"
    # Profitability Shadow v2 (2026-08-24): entry_watch 최소수익미달청산
    # (5분 시점) 판단 순간의 feature snapshot 전용 로그 — VWAP이탈청산/
    # 급락청산과 섞이지 않도록 별도 파일로 분리.
    min_profit_extension_shadow_log_file: str = "logs/min_profit_extension_shadow.csv"
    # B01 (2026-09-11, 개선 체크리스트 0단계): 실행 기준선(run_id/git_sha/
    # git_dirty/effective_config_hash/모의·실전 구분/시작시각) 기록 전용
    # 신규 파일. trades.csv/signal_log.csv와는 완전히 분리된 새 파일이라
    # (infra/storage/run_baseline.py 참고) 기존 CSV 스키마에는 영향이
    # 전혀 없습니다 — 연결은 timestamp 시간범위 조인으로 처리합니다.
    run_baseline_log_file: str = "logs/run_baseline.csv"
    # S01 관측 1단계 (2026-09-11, GPT 5차 검토 반영): 잔고 조회
    # 성공/실패·캐시 나이 관측 전용 로그. 순수 관측이며 조회 주기/폴백
    # 판정에는 영향 없음 (infra/storage/logger.py의 BalanceFreshnessLogger
    # 참고).
    balance_freshness_log_file: str = "logs/balance_freshness.csv"
    # S02 관측 1단계 (2026-09-11, GPT 5차 검토 반영): entry_watch 정상
    # 창을 통째로 놓친 진입 건의 후보 관측 전용 로그. 이 로그 존재가
    # SELL 판정을 만들지 않음 (infra/storage/logger.py의
    # DelayedEvalCandidateLogger 참고).
    delayed_eval_candidate_log_file: str = "logs/delayed_eval_candidate.csv"
    # 180초 감시 공백 대응 3단계 (2026-09-15, 관측 경로 연결): 잔고
    # 장애(429 등) 재시도 대기 중 캐시된 시세·잔고만으로 계산한 청산
    # 후보(손절·트레일링) 관측 전용 로그. 주문 제출·체결 확정·
    # highest_price 갱신에는 전혀 관여하지 않음 (infra/storage/logger.py의
    # ExitCandidateOutageLogger, domain/strategy/exit_calc.py 참고).
    exit_candidate_outage_log_file: str = "logs/exit_candidate_outage.csv"
    # 2026-09-18 (우선순위1 1차: 체결조회 증거 독립 저장): get_order_status_
    # evidence() 조회 결과(성공/실패 모두)를 순수 관측 목적으로만 append하는
    # 로그. CSV가 아니라 JSON Lines(한 줄에 레코드 하나)입니다 — 조회
    # 하나당 매칭 행이 여러 개일 수 있어 고정 컬럼 스키마에 맞지 않기
    # 때문입니다. TrackedOrderJournalStore와 달리 이 파일은 안전한 시점에
    # 삭제되지 않습니다(영구 append-only) — infra/storage/
    # order_status_observation_store.py 참고. BUY/SELL/리스크 판정 어디에도
    # 관여하지 않습니다.
    order_status_observation_log_file: str = "logs/order_status_observations.jsonl"


@dataclass(frozen=True)
class ExperimentalConfig:
    """단계적 리팩터링을 위한 기능 플래그 (2026-07-27, GPT 설계 검토 반영).

    각 플래그는 "off" / "shadow" / "enforce" 세 상태를 지원합니다.
      off     : 새 로직을 아예 실행하지 않음 (완전 비활성)
      shadow  : 새 로직을 계산·로그만 하고 실제 매매 판정에는 영향 없음
      enforce : 새 로직이 실제 매매 판정에 반영됨

    안전 원칙: 반드시 shadow를 거쳐 충분한 데이터로 기존 로직과의
    차이를 검증한 뒤에만 enforce로 전환합니다. 각 리팩터링 단계는
    이 플래그로 감싸서, 예상치 못한 문제가 생기면 이 값만 되돌려도
    즉시 이전 동작으로 복귀할 수 있어야 합니다.
    """
    session_metrics_mode: str = "off"      # 1단계: 세션/롤링 데이터 의미 분리
    decision_engine_mode: str = "off"      # 2단계: DecisionEngine 추출
    position_lifecycle_mode: str = "off"   # 3단계: 체결 확인 상태머신 실제화
    reward_risk_guard_mode: str = "off"    # 4단계: 상승여력·손익비 하드 게이트
    candidate_ranking_mode: str = "off"    # 5단계: 후보 순위화
    trailing_breakeven_mode: str = "off"   # 6단계: 순본전 트레일링
    # 2026-08-05 (1E.5단계, GPT 코드리뷰 지시): MACD/VWAP 진입 품질
    # shadow 관측 — off/shadow만 지원, enforce는 이번 단계에서
    # 의도적으로 구현하지 않음(아래 __post_init__에서 별도 검증).
    entry_quality_guard_mode: str = "off"
    # 2026-08-28 (Candidate A Production Pilot v1, 민우님 명세+승인):
    # 이 프레임워크에서 enforce까지 구현되는 첫 플래그입니다. predicate
    # (upside_to_recent_high_pct<0.50 AND rebound_volume_spike is
    # False)는 domain/strategy/candidate_a_guard.py에 하드코딩되어
    # 있고 여기서는 바꿀 수 없습니다. entry_quality_guard_mode와
    # 달리 enforce 동작 자체는 이번 라운드에 구현되지만, 이번
    # 승인 범위는 "off"→"shadow" 배포까지입니다 — "enforce"로의
    # 전환은 설정 파일 값을 사람이 직접 바꾸는 별도 승인 단계로만
    # 이뤄집니다(코드 레벨에서 enforce를 막지 않음 — B단계 검증에서
    # 실제로 enforce가 정확히 동작하는지 테스트로 확인해야 하므로).
    #
    # 2026-08-28 (reclosure, 민우님 지적): 이 플래그는 위 클래스
    # docstring의 일반 계약("off=새 로직 아예 미실행")과 다르게
    # 동작합니다 — candidate_a_guard_mode는 **_try_buy()의 주문
    # 차단 gate 승격 상태만** 제어하고, low_upside_shadow.csv 관측
    # (_write_signal_log()의 would_skip_low_upside_no_spike 계산)은
    # 이 값이 "off"여도 legacy_buy_candidate 조건만 맞으면 항상
    # 그대로 기록됩니다(2026-08-24 Profitability Shadow v2 도입 때부터
    # 이 모드와 독립적이었음 — Candidate A가 이 관측 위에 gate를
    # 얹은 것뿐). "off"를 보고 "Candidate A 계산 자체가 꺼졌다"고
    # 오해하지 마세요.
    candidate_a_guard_mode: str = "off"

    def __post_init__(self) -> None:
        # 2026-07-27: YAML에서 off/on처럼 quote 없는 특정 단어는 YAML 1.1
        # 스펙상 boolean으로 자동 해석됨(off->False, on->True, yes/no 등도
        # 동일) — 실제로 이 버그를 이 자리에서 재현/확인함. 문자열이 아닌
        # 값이 들어오면 즉시 명확한 오류로 잡아서, 설정 파일에 따옴표
        # 누락이 있을 때 조용히 엉뚱한 값으로 동작하지 않도록 함.
        valid = {"off", "shadow", "enforce"}
        for field_name in (
            "session_metrics_mode", "decision_engine_mode", "position_lifecycle_mode",
            "reward_risk_guard_mode", "candidate_ranking_mode", "trailing_breakeven_mode",
            "entry_quality_guard_mode", "candidate_a_guard_mode",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or value not in valid:
                raise ValueError(
                    f"experimental.{field_name}는 {valid} 중 하나(문자열)여야 합니다: "
                    f"{value!r} (타입: {type(value).__name__}) — settings.yaml에서 "
                    f'따옴표를 빠뜨리면 "off"가 YAML boolean(False)으로 해석될 수 있습니다. '
                    f'반드시 "off"처럼 따옴표로 감싸세요.'
                )
        # 2026-08-05 (GPT 코드리뷰 지시): entry_quality_guard_mode는
        # 이번 1E.5단계에서 off/shadow만 지원 — enforce 동작(실제
        # BUY 차단)은 아직 구현하지 않았으므로, 설정 파일에 실수로
        # "enforce"가 들어가도 조용히 무시되는 대신 명확한 오류로
        # 막아서 "설정은 enforce인데 실제로는 shadow처럼 동작"하는
        # 혼동을 방지.
        if self.entry_quality_guard_mode == "enforce":
            raise ValueError(
                'experimental.entry_quality_guard_mode="enforce"는 아직 지원되지 '
                '않습니다(1E.5단계는 shadow 관측까지만 구현됨). "off" 또는 '
                '"shadow"를 사용하세요.'
            )


@dataclass(frozen=True)
class Settings:
    """프로그램 전체 설정을 한 번에 담는 최상위 객체입니다."""

    app: AppConfig
    broker: BrokerConfig
    targets: list[str]
    trading: TradingConfig
    strategy: StrategyConfig
    risk: RiskConfig
    market_regime: MarketRegimeConfig
    storage: StorageConfig
    websocket: WebSocketConfig
    entry_watch: EntryWatchConfig = None
    kakao: KakaoConfig = None
    # 2026-07-27 (GPT 코드리뷰): 기존엔 = None이라 load_settings()를
    # 거치지 않고 Settings(...)를 직접 생성하면(테스트 등) experimental
    # 이 None으로 남아, 다음 단계 코드가 settings.experimental.xxx_mode
    # 를 그대로 읽으면 AttributeError가 날 수 있었음(재현 확인). 매번
    # "settings.experimental or ExperimentalConfig()"를 호출부에서
    # 기억하게 하는 것보다 모델 자체가 항상 유효한 값을 보장하는 게
    # 안전 — default_factory로 전환하면 Settings를 어떤 방식으로
    # 생성하든(직접 생성/테스트 fixture/load_settings) 항상
    # ExperimentalConfig()(전부 off)가 채워짐.
    experimental: ExperimentalConfig = field(default_factory=ExperimentalConfig)


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")
        return ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item) for item in value]
    return value


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = _substitute_env(raw)

    kakao_raw = raw.get("kakao", {})
    entry_watch_raw = raw.get("entry_watch", {})
    experimental_raw = raw.get("experimental", {})
    return Settings(
        app=AppConfig(**raw["app"]),
        broker=BrokerConfig(**raw["broker"]),
        targets=raw["targets"]["symbols"],
        trading=TradingConfig(**raw["trading"]),
        strategy=StrategyConfig(**raw["strategy"]),
        risk=RiskConfig(**raw["risk"]),
        market_regime=MarketRegimeConfig(**raw["market_regime"]),
        storage=StorageConfig(**raw["storage"]),
        websocket=WebSocketConfig(**raw["websocket"]),
        entry_watch=EntryWatchConfig(**entry_watch_raw) if entry_watch_raw else None,
        kakao=KakaoConfig(**kakao_raw) if kakao_raw else KakaoConfig(),
        experimental=ExperimentalConfig(**experimental_raw) if experimental_raw else ExperimentalConfig(),
    )
