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
    max_day_positions: int
    max_swing_positions: int
    allow_multiple_entries_per_symbol_per_day: bool
    force_exit_before_market_close_minutes: int
    reentry_cooldown_seconds: int


@dataclass(frozen=True)
class StrategyConfig:
    """단타 전략 파라미터를 보관합니다."""

    name: str
    breakout_threshold_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    reference_price_type: str


@dataclass(frozen=True)
class SwingConfig:
    """스윙 전략 파라미터를 보관합니다.

    take_profit_pct      : 익절 기준 (기본 12%)
    stop_loss_pct        : 손절 기준 (기본 5%)
    weekly_history_weeks : 주봉 조회 주수
    weekly_refresh_seconds : 주봉 캐시 갱신 주기 (초)
    short_ma_weeks       : 단기 이동평균 기간 (주)
    long_ma_weeks        : 장기 이동평균 기간 (주)
    """

    take_profit_pct: float
    stop_loss_pct: float
    weekly_history_weeks: int
    weekly_refresh_seconds: int
    short_ma_weeks: int
    long_ma_weeks: int


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
    macd_fast: int
    macd_slow: int
    macd_signal: int
    volume_surge_ratio: float


@dataclass(frozen=True)
class WebSocketConfig:
    """조건검색 WebSocket 연결 설정입니다.

    enabled       : false면 WebSocket 없이 기존 방식(수동 종목)으로 동작
    url           : WebSocket 서버 URL (실전: wss://api.kiwoom.com:10000/...)
    condition_seq : 사용할 조건검색식 번호 (HTS에서 확인)
    max_symbols   : 조건검색으로 편입 가능한 최대 종목 수
    app_key       : 조건검색용 실전 계좌 앱키 (모의투자와 별도)
    secret_key    : 조건검색용 실전 계좌 시크릿키
    """

    enabled: bool
    url: str
    condition_seq: int
    max_symbols: int
    app_key: str
    secret_key: str


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
    day_symbols: list[str]
    swing_symbols: list[str]
    trading: TradingConfig
    strategy: StrategyConfig
    swing: SwingConfig
    risk: RiskConfig
    market_regime: MarketRegimeConfig
    storage: StorageConfig
    websocket: WebSocketConfig

    @property
    def targets(self) -> list[str]:
        """단타 + 스윙 종목을 합친 전체 감시 목록입니다."""
        return list(dict.fromkeys(self.day_symbols + self.swing_symbols))


# 이 함수는 문자열 안의 ${ENV_NAME} 값을 실제 환경변수 값으로 바꿉니다.
def _substitute_env(value: Any) -> Any:
    """환경변수 치환을 재귀적으로 수행합니다."""

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


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    """YAML 파일을 읽어서 Settings 객체로 변환합니다."""

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = _substitute_env(raw)

    return Settings(
        app=AppConfig(**raw["app"]),
        broker=BrokerConfig(**raw["broker"]),
        day_symbols=raw["targets"].get("day_symbols", []),
        swing_symbols=raw["targets"].get("swing_symbols", []),
        trading=TradingConfig(**raw["trading"]),
        strategy=StrategyConfig(**raw["strategy"]),
        swing=SwingConfig(**raw["swing"]),
        risk=RiskConfig(**raw["risk"]),
        market_regime=MarketRegimeConfig(**raw["market_regime"]),
        storage=StorageConfig(**raw["storage"]),
        websocket=WebSocketConfig(**raw["websocket"]),
    )
