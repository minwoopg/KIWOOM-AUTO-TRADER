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
import shutil
import time
from pathlib import Path

from config.settings import Settings, load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_reconciler import StateReconciler
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_market_open, seconds_until_market_open


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


def build_trading_service(settings, broker, app_logger, trade_logger, signal_logger, state_store):
    strategy_router   = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager      = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    return TradingService(
        settings=settings,
        broker=broker,
        strategy_router=strategy_router,
        regime_classifier=regime_classifier,
        risk_manager=risk_manager,
        app_logger=app_logger,
        trade_logger=trade_logger,
        signal_logger=signal_logger,
        state_store=state_store,
    )


# ── REST 루프 ────────────────────────────────────────────────────

async def trading_loop(trading_service: TradingService, settings: Settings, app_logger) -> None:
    """REST API 기반 매매 루프 (asyncio 버전)."""

    # ── 장 시작 전 대기 ──────────────────────────────────────
    wait_sec = seconds_until_market_open()
    if wait_sec > 0:
        wait_min = int(wait_sec // 60)
        app_logger.info(
            f"application ready — 장 시작까지 {wait_min}분 대기 중 (09:00 시작)"
        )
        await asyncio.sleep(wait_sec)
        app_logger.info("장 시작 — 매매 루프 시작")
        trading_service.reset_daily_loss_counts()
    else:
        app_logger.info("application started (장중 실행)")

    poll = settings.trading.poll_interval_seconds

    while True:
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            if is_market_open() or settings.broker.use_mock:
                await trading_service.run_once()
            else:
                # 장 외 시간 — 대기 메시지 (분 단위로 한 번)
                if now.second < poll:
                    app_logger.info(
                        f"[WAIT] 장 외 시간 ({now.strftime('%H:%M')}) — "
                        f"09:00 장 시작까지 대기 중"
                    )
                # 리포트는 15:25 이후 생성 (마지막 체결 기록 완료 후)
                if now.hour > 15 or (now.hour == 15 and now.minute >= 25):
                    trading_service._run_end_of_day_tasks(now)
            await asyncio.sleep(poll)

        except (asyncio.CancelledError, KeyboardInterrupt):
            app_logger.info("application stopped by user")
            break

        except Exception as exc:
            app_logger.exception("unexpected error: %s", exc)
            msg = str(exc)
            if "http=429" in msg or "허용된 요청 개수를 초과" in msg:
                app_logger.warning("rate limit detected, backing off for 180 seconds")
                await asyncio.sleep(180)
            else:
                await asyncio.sleep(poll)


# ── 메인 ────────────────────────────────────────────────────────

async def async_main() -> None:
    load_dotenv()

    # 구버전 .pyc 캐시가 남아 AttributeError를 일으키는 것을 방지합니다.
    # 업데이트 후 첫 실행 시 자동으로 재컴파일됩니다.
    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    settings = load_settings()

    app_logger   = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger  = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store  = JsonStateStore(settings.storage.state_file)

    broker = build_broker(settings)
    broker.authenticate()

    # ── 시작 시 state.json과 실제 잔고 동기화 ──────────────
    try:
        balance_init = broker.get_account_balance()
        reconciler   = StateReconciler(app_logger)
        state, highest_price = state_store.load()
        state, highest_price = reconciler.reconcile(state, highest_price, balance_init)
        state_store.save(state, highest_price)
        app_logger.info("[RECONCILE] state.json 동기화 완료")
    except Exception as e:
        app_logger.warning(f"[RECONCILE] 시작 시 state 동기화 실패: {e}")

    trading_service = build_trading_service(
        settings, broker, app_logger, trade_logger, signal_logger, state_store
    )

    # ── WebSocket 조건검색 활성화 여부 ───────────────────────────
    if settings.websocket.enabled:
        from infra.websocket.condition_watcher import ConditionWatcher
        from infra.websocket.real_token import fetch_real_token

        # 수동 고정 종목 (settings.yaml의 targets)
        manual_symbols = settings.targets

        def on_symbols_changed(symbols: list[str]) -> None:
            # 자동 제외된 종목은 재편입 차단
            excluded = trading_service.get_excluded_symbols()
            filtered = [s for s in symbols if s not in excluded]
            combined = list(dict.fromkeys(manual_symbols + filtered))
            limited  = combined[:settings.websocket.max_symbols]
            trading_service.update_targets(limited)
            blocked = excluded & set(symbols)
            if blocked:
                app_logger.info(f"[COND] 제외 종목 재편입 차단: {blocked}")
            app_logger.info(f"[COND] 종목 목록 갱신: {limited}")

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

        seqs_str = ", ".join(str(s) for s in settings.websocket.condition_seqs)
        app_logger.info(
            f"[COND] 조건검색 모드 활성화 "
            f"(조건식 번호: {seqs_str})"
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
