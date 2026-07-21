from __future__ import annotations

"""설정 파일을 읽어서 파이썬 객체로 변환하는 모듈."""

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

    def __post_init__(self):
        # frozen=True dataclass라 self.x = ... 직접 대입은 FrozenInstanceError.
        # object.__setattr__로 우회 (2026-07-20: excluded_symbols=None으로
        # 생성될 때 FrozenInstanceError가 나던 잠재 버그를 테스트 중 발견 —
        # 지금까지 settings.yaml이 항상 excluded_symbols: 리스트를 명시해서
        # None 케이스가 실제로 발생한 적이 없었을 뿐이었음)
        if self.excluded_symbols is None:
            object.__setattr__(self, "excluded_symbols", [])


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


@dataclass(frozen=True)
class WebSocketConfig:
    """조건검색 WebSocket 연결 설정입니다."""

    enabled: bool
    url: str
    condition_seqs: list  # 동시 구독할 조건식 번호 목록
    max_symbols: int
    app_key: str
    secret_key: str
    swing_condition_seqs: list = field(default_factory=list)  # 스윙 전용 검색식 seq
    swing_condition_output: str = "data/swing_condition_symbols.json"


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


@dataclass(frozen=True)
class StorageConfig:
    state_file: str
    trade_log_file: str
    signal_log_file: str
    app_log_file: str
    save_minute_bars: bool
    minute_bars_dir: str


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
    )
