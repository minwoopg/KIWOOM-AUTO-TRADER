# -*- coding: utf-8 -*-
"""
보유/미보유 종목 폴링 간격 차등 적용 검증 (2026-07-22)

기존엔 보유/미보유 구분 없이 종목마다 무조건 1.0초씩 sleep했는데,
보유종목 사이는 held_symbol_poll_gap_seconds(짧게), 미보유종목 사이는
entry_poll_gap_seconds(기존과 동일)로 나눠 적용하도록 바꿨다. 이 테스트는
run_once()를 실제로 실행하며 asyncio.sleep에 어떤 인자가 몇 번 전달됐는지
추적해서, 의도한 대로 간격이 적용되는지 확인한다.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import Position
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


async def run_scenario(held_symbols_in_targets: set[str]):
    """targets 5개 중 held_symbols_in_targets에 해당하는 종목만 보유 중으로 설정하고
    run_once()를 실행하며 asyncio.sleep 호출 인자를 기록해서 반환한다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = build_minimal_settings(tmpdir)
        object.__setattr__(settings.trading, "held_symbol_poll_gap_seconds", 0.2)
        object.__setattr__(settings.trading, "entry_poll_gap_seconds", 1.0)

        broker = MockBroker()
        for sym in held_symbols_in_targets:
            broker._positions[sym] = Position(symbol=sym, quantity=10, average_price=10000)
            broker._prices[sym] = 10000

        app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
        trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
        signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
        state_store = JsonStateStore(settings.storage.state_file)
        strategy_router = StrategyRouter(settings.strategy)
        regime_classifier = MarketRegimeClassifier(settings.market_regime)
        risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
        service = TradingService(
            settings=settings, broker=broker, strategy_router=strategy_router,
            regime_classifier=regime_classifier, risk_manager=risk_manager,
            app_logger=app_logger, trade_logger=trade_logger,
            signal_logger=signal_logger, state_store=state_store,
        )

        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def tracking_sleep(seconds):
            sleep_calls.append(seconds)
            await real_sleep(0)  # 실제로는 기다리지 않음(테스트 속도)

        asyncio.sleep = tracking_sleep
        try:
            await service.run_once()
        finally:
            asyncio.sleep = real_sleep

        return sleep_calls, settings.targets


async def main() -> int:
    # ── 시나리오 1: 보유종목 전혀 없음 -> 전부 entry_poll_gap(1.0) ──
    sleep_calls, targets = await run_scenario(held_symbols_in_targets=set())
    check("1) 보유종목 0개 -> 모든 sleep이 entry_poll_gap(1.0)",
          all(abs(s - 1.0) < 1e-9 for s in sleep_calls))
    check(f"   sleep 호출 횟수 = targets-1 = {len(targets)-1}",
          len(sleep_calls) == len(targets) - 1)

    # ── 시나리오 2: 첫 두 종목이 보유 -> 그 사이 sleep은 held_gap(0.2) ──
    held = {targets[0], targets[1]}
    sleep_calls, targets = await run_scenario(held_symbols_in_targets=held)
    # 정렬 후: [보유1, 보유2, 미보유1, 미보유2, 미보유3]
    # sleep 호출은 순서대로: (보유1->보유2)=0.2, (보유2->미보유1)=1.0(직전이 미보유아님 주의),
    # 실제로는 "직전 종목이 held였는가"로 판단하므로 보유2->미보유1 전환도 0.2여야 함
    check("2) 보유종목 2개일 때 sleep 호출 수는 여전히 targets-1",
          len(sleep_calls) == len(targets) - 1)
    check("   첫 sleep(보유1->보유2 구간)은 held_gap(0.2)",
          abs(sleep_calls[0] - 0.2) < 1e-9)
    # 두번째 sleep은 "직전 종목(보유2)이 held였는가" 기준이므로 0.2여야 함
    check("   두번째 sleep(직전이 보유종목이므로)도 held_gap(0.2)",
          abs(sleep_calls[1] - 0.2) < 1e-9)
    # 이후(미보유->미보유) 구간은 entry_gap(1.0)
    check("   나머지 sleep(미보유->미보유 구간)은 entry_gap(1.0)",
          all(abs(s - 1.0) < 1e-9 for s in sleep_calls[2:]))

    # ── 시나리오 3: 보유종목이 held_gap=0으로 설정되면 sleep 자체를 건너뜀 ──
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = build_minimal_settings(tmpdir)
        object.__setattr__(settings.trading, "held_symbol_poll_gap_seconds", 0.0)
        object.__setattr__(settings.trading, "entry_poll_gap_seconds", 1.0)
        broker = MockBroker()
        held_sym = settings.targets[0]
        broker._positions[held_sym] = Position(symbol=held_sym, quantity=10, average_price=10000)
        broker._prices[held_sym] = 10000
        app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
        trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
        signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
        state_store = JsonStateStore(settings.storage.state_file)
        strategy_router = StrategyRouter(settings.strategy)
        regime_classifier = MarketRegimeClassifier(settings.market_regime)
        risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
        service = TradingService(
            settings=settings, broker=broker, strategy_router=strategy_router,
            regime_classifier=regime_classifier, risk_manager=risk_manager,
            app_logger=app_logger, trade_logger=trade_logger,
            signal_logger=signal_logger, state_store=state_store,
        )
        sleep_calls = []
        real_sleep = asyncio.sleep
        async def tracking_sleep(seconds):
            sleep_calls.append(seconds)
            await real_sleep(0)
        asyncio.sleep = tracking_sleep
        try:
            await service.run_once()
        finally:
            asyncio.sleep = real_sleep
        check("3) held_gap=0.0 -> 그 구간은 sleep 아예 스킵(0이 리스트에 없음)",
              0.0 not in sleep_calls)

    print()
    print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
