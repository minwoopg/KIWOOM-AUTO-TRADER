from __future__ import annotations

"""프로그램 시작점.

이 파일을 보면 프로그램 전체 흐름을 가장 빠르게 이해할 수 있습니다.
실행 순서는 아래와 같습니다.
1. .env 파일을 읽는다.
2. settings.yaml을 읽는다.
3. 브로커를 만든다.
4. 전략, 리스크 관리자, 서비스 객체를 만든다.
5. 무한루프를 돌면서 run_once()를 반복 실행한다.

주의:
- use_mock=true  -> 가짜 MockBroker 사용
- use_mock=false -> 실제 KiwoomBroker 사용

즉 '키움 모의투자 실연동'을 하려면
- use_mock: false
- broker.base_url: https://mockapi.kiwoom.com
처럼 설정해야 합니다.
"""

import os
import time
from pathlib import Path

from config.settings import Settings, load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_market_open


def load_dotenv(path: str = ".env") -> None:
    """`.env` 파일이 있으면 KEY=VALUE 형태를 환경변수로 로드합니다.

    왜 필요한가?
    - 앱키/시크릿키를 settings.yaml에 직접 적지 않기 위해서입니다.
    - 민감한 값은 .env 파일에 두고, 코드에서는 환경변수로 읽는 편이 안전합니다.
    """

    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_broker(settings: Settings):
    """설정에 따라 MockBroker 또는 KiwoomBroker를 생성합니다.

    사용 예:
    - 흐름만 테스트: use_mock=true
    - 키움 모의투자 실연동: use_mock=false + base_url=mockapi
    - 키움 실전: use_mock=false + base_url=api
    """

    if settings.broker.use_mock:
        return MockBroker()
    return KiwoomBroker(settings.broker)


def main() -> None:
    """애플리케이션 전체를 조립하고 실행 루프를 시작합니다."""

    load_dotenv()
    settings = load_settings()

    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    app_logger.info(f"loaded targets: {settings.targets}")

    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    state_store = JsonStateStore(settings.storage.state_file)

    broker = build_broker(settings)
    broker.authenticate()

    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk)
    trading_service = TradingService(
        settings=settings,
        broker=broker,
        strategy_router=strategy_router,
        regime_classifier=regime_classifier,
        risk_manager=risk_manager,
        app_logger=app_logger,
        trade_logger=trade_logger,
        state_store=state_store,
    )

    app_logger.info(
        "application started",
        extra={"broker": settings.broker.provider, "use_mock": settings.broker.use_mock},
    )

    # 무한루프이므로 실제 운영 전에는 종료 조건, 예외 복구 등을 더 넣어야 합니다.
    while True:
        try:
            # 실제 키움 브로커일 때는 장중에만 주문 루프를 돌리는 것이 안전합니다.
            # 다만 MockBroker 는 공부용이라 시간 상관없이 실행하게 둡니다.
            if is_market_open() or settings.broker.use_mock:
                trading_service.run_once()

            time.sleep(settings.trading.poll_interval_seconds)

        except KeyboardInterrupt:
            app_logger.info("application stopped by user")
            break

        except Exception as exc:
            app_logger.exception("unexpected error: %s", exc)

            error_message = str(exc)

            # 키움 API 요청 제한(429)에 걸리면 더 오래 쉰다.
            if "http=429" in error_message or "허용된 요청 개수를 초과" in error_message:
                app_logger.warning("rate limit detected, backing off for 180 seconds")
                time.sleep(180)
            else:
                time.sleep(settings.trading.poll_interval_seconds)


if __name__ == "__main__":
    main()
