from __future__ import annotations

"""프로그램 시작점.

실행 순서:
1. .env / settings.yaml 로드
2. 브로커 인증
3. TradingService 조립
4. websocket.enabled=true면 ConditionWatcher도 함께 실행
   - WebSocket 루프와 REST 루프를 asyncio로 병렬 실행
5. websocket.enabled=false면 기존 방식(수동 종목)으로 동작
"""

import asyncio
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
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_broker(settings: Settings):
    if settings.broker.use_mock:
        return MockBroker()
    return KiwoomBroker(settings.broker)


def build_trading_service(settings, broker, app_logger, trade_logger, state_store):
    strategy_router   = StrategyRouter(settings.strategy, settings.swing)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager      = RiskManager(settings.trading, settings.risk)
    return TradingService(
        settings=settings,
        broker=broker,
        strategy_router=strategy_router,
        regime_classifier=regime_classifier,
        risk_manager=risk_manager,
        app_logger=app_logger,
        trade_logger=trade_logger,
        state_store=state_store,
    )


# ── REST 루프 ────────────────────────────────────────────────────

async def trading_loop(trading_service: TradingService, settings: Settings, app_logger) -> None:
    """REST API 기반 매매 루프 (asyncio 버전)."""
    app_logger.info("application started")
    poll = settings.trading.poll_interval_seconds

    while True:
        try:
            if is_market_open() or settings.broker.use_mock:
                await trading_service.run_once()
            await asyncio.sleep(poll)

        except (asyncio.CancelledError, KeyboardInterrupt):
            app_logger.info("application stopped by user")
            break

        except Exception as exc:
            app_logger.exception("unexpected error: %s", exc)
            msg = str(exc)
            if "http=429" in msg or "허용된 요청 개수를 초과" in msg:
                from utils.time_utils import is_near_market_close
                # 장 마감 20분 이내면 백오프를 짧게 → 강제청산 타이밍 놓치지 않음
                if is_near_market_close(20):
                    app_logger.warning("rate limit detected near market close, backing off for 10 seconds")
                    await asyncio.sleep(10)
                else:
                    app_logger.warning("rate limit detected, backing off for 180 seconds")
                    await asyncio.sleep(180)
            else:
                await asyncio.sleep(poll)


# ── 메인 ────────────────────────────────────────────────────────

async def async_main() -> None:
    load_dotenv()
    settings = load_settings()

    app_logger   = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    state_store  = JsonStateStore(settings.storage.state_file)

    broker = build_broker(settings)
    broker.authenticate()

    trading_service = build_trading_service(
        settings, broker, app_logger, trade_logger, state_store
    )

    # ── WebSocket 조건검색 활성화 여부 ───────────────────────────
    if settings.websocket.enabled:
        from infra.websocket.condition_watcher import ConditionWatcher
        from infra.websocket.real_token import fetch_real_token

        # 수동 고정 종목 (settings.yaml의 targets)
        manual_day_symbols   = settings.day_symbols
        manual_swing_symbols = settings.swing_symbols

        def on_symbols_changed(symbols: list[str]) -> None:
            # 수동 고정 종목을 앞에 두고 조건검색 종목을 뒤에 붙입니다.
            # max_symbols 제한이 걸려도 수동 종목이 먼저 보장됩니다.
            combined_day = list(dict.fromkeys(manual_day_symbols + symbols))
            limited_day  = combined_day[:settings.websocket.max_symbols]
            trading_service.update_targets(limited_day, manual_swing_symbols)
            app_logger.info(
                f"[COND] 단타 종목 갱신: {limited_day} / 스윙 종목: {manual_swing_symbols}"
            )

        # 조건검색은 실전 계좌 토큰으로 별도 발급
        app_logger.info("[COND] 실전 계좌 토큰 발급 중...")
        real_token = fetch_real_token(
            app_key=settings.websocket.app_key,
            secret_key=settings.websocket.secret_key,
        )
        app_logger.info("[COND] 실전 계좌 토큰 발급 완료")

        watcher = ConditionWatcher(
            config=settings.websocket,
            token=real_token,
            on_symbols_changed=on_symbols_changed,
        )

        app_logger.info(
            f"[COND] 조건검색 모드 활성화 "
            f"(조건식 번호: {settings.websocket.condition_seq})"
        )
        app_logger.info("[COND] 종목은 조건검색으로 자동 설정됩니다")

        await asyncio.gather(
            trading_loop(trading_service, settings, app_logger),
            watcher.start(),
        )

    else:
        # 기존 방식: settings.yaml의 targets 그대로 사용
        app_logger.info(f"loaded targets: {settings.targets}")
        await trading_loop(trading_service, settings, app_logger)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
