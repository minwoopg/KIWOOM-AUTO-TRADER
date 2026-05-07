from __future__ import annotations

"""설정 파일을 읽어서 파이썬 객체로 변환하는 모듈.

이 파일의 목적은 `settings.yaml` 내용을 프로그램이 쓰기 쉬운 형태로 바꾸는 것입니다.
나중에 자바로 이식할 때도 거의 같은 구조의 설정 클래스를 만들 수 있습니다.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# ${ENV_NAME} 형태의 문자열을 찾기 위한 정규표현식입니다.
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class AppConfig:
    """애플리케이션 자체 정보와 로그 레벨을 보관합니다."""

    name: str
    env: str
    log_level: str


@dataclass(frozen=True)
class BrokerConfig:
    """브로커(증권사) 연결에 필요한 설정을 보관합니다.

    현재는 키움증권 REST API 기준 필드를 두었습니다.
    실제 연동 단계에서는 공식 문서에 맞춰 필드를 조금 더 늘릴 수 있습니다.
    """

    provider: str
    use_mock: bool
    base_url: str
    app_key: str
    secret_key: str
    account_number: str
    is_paper_trading: bool


@dataclass(frozen=True)
class TradingConfig:
    """주문 주기, 조회 주기, 매수 금액, 최대 보유 수 등 운영 설정을 보관합니다."""

    poll_interval_seconds: int
    balance_refresh_seconds: int
    price_refresh_seconds: int
    order_cash_per_trade: int
    max_positions: int
    allow_multiple_entries_per_symbol_per_day: bool
    force_exit_before_market_close_minutes: int


@dataclass(frozen=True)
class StrategyConfig:
    """전략이 사용할 수치 파라미터를 보관합니다."""

    name: str
    breakout_threshold_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    reference_price_type: str


@dataclass(frozen=True)
class MarketRegimeConfig:
    """장세 분류기에 사용할 파라미터를 보관합니다.

    short_ma_days   : 단기 이동평균 기간 (기본 5일)
    long_ma_days    : 장기 이동평균 기간 (기본 20일)
    history_days    : 일봉 히스토리 요청 일수 (long_ma_days보다 커야 함)
    rsi_period      : RSI 계산 기간 (기본 14일)
    rsi_overbought  : RSI 과매수 기준 (기본 70)
    rsi_oversold    : RSI 과매도 기준 (기본 30)
    history_refresh_seconds : 일봉 캐시 갱신 주기 (기본 3600 = 1시간)
    """

    short_ma_days: int
    long_ma_days: int
    history_days: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    history_refresh_seconds: int


@dataclass(frozen=True)
class RiskConfig:
    """리스크 관리에 필요한 제한값을 보관합니다."""

    max_daily_loss_amount: int
    max_order_amount: int
    min_cash_buffer: int


@dataclass(frozen=True)
class StorageConfig:
    """상태 파일, 거래 로그, 앱 로그의 저장 경로를 보관합니다."""

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


# 이 함수는 문자열 안의 ${ENV_NAME} 값을 실제 환경변수 값으로 바꿉니다.
def _substitute_env(value: Any) -> Any:
    """환경변수 치환을 재귀적으로 수행합니다.

    dict, list, str를 모두 순회하면서 `${...}` 패턴을 실제 OS 환경변수 값으로 바꿉니다.
    예를 들어 `${KIWOOM_APP_KEY}`는 실제 `os.environ["KIWOOM_APP_KEY"]` 값으로 치환됩니다.
    """

    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            env_key = match.group(1)
            return os.getenv(env_key, "")

        return ENV_VAR_PATTERN.sub(replace, value)

    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_substitute_env(item) for item in value]

    return value


# 프로그램 시작 시 가장 먼저 호출되는 설정 로더입니다.
def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    """YAML 파일을 읽어서 Settings 객체로 변환합니다."""

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
    )
