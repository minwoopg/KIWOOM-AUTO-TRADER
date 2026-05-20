from __future__ import annotations

"""설정 파일을 읽어서 파이썬 객체로 변환하는 모듈."""

import os
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class WebSocketConfig:
    """조건검색 WebSocket 연결 설정입니다."""

    enabled: bool
    url: str
    condition_seq: int
    max_symbols: int
    app_key: str
    secret_key: str


@dataclass(frozen=True)
class RiskConfig:
    max_daily_loss_amount: int
    max_order_amount: int
    min_cash_buffer: int


@dataclass(frozen=True)
class StorageConfig:
    state_file: str
    trade_log_file: str
    app_log_file: str


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
    )
