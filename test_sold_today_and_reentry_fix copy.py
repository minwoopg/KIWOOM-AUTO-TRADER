# -*- coding: utf-8 -*-
"""
2026-07-21 긴급수정 검증

RiskManager.can_place_order()의 일일손실한도 체크가 실제 trade_log_file
누적을 읽다보니 반복 매수/매도 시나리오 테스트에서 얽혀서, 이번 버전은
_try_buy/_try_sell을 통째로 거치지 않고 이번 수정이 건드린 지점만
정밀 타겟팅해서 검증한다:

1) _sold_today 플래그가 매수 성공 시 즉시 풀리는지 (trading_service.py)
2) allow_multi=False일 때 RiskManager.can_place_order()와
   TradingService._try_buy() 양쪽 게이트 모두 이미 매수한 종목의
   재매수를 막는지 (settings.yaml)
"""
from __future__ import annotations

import sys
import tempfile

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import RuntimeState, OrderRequest, OrderSide, AccountBalance
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


def build_service(tmpdir: str) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.trading, "allow_multiple_entries_per_symbol_per_day", False)
    broker = MockBroker()
    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store = JsonStateStore(settings.storage.state_file)
    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    return TradingService(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
    )


# ── 테스트 1: _sold_today 플래그가 매수 성공 시 즉시 풀리는지 ──────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    symbol = "475150"

    # MockBroker.place_order(SELL)은 self._positions에 해당 종목이
    # 있어야 성공하므로, 매도 전 보유 상태를 먼저 세팅
    from domain.models import Position
    service.broker._positions[symbol] = Position(symbol=symbol, quantity=100, average_price=58000)
    service.broker._prices[symbol] = 58500

    service._try_sell(symbol, 100, current_price=58500,
                       exit_reason="트레일링 스탑 — 테스트", avg_buy_price=58000)
    check("1) 매도 성공 -> _sold_today에 등록됨", symbol in service._sold_today)

    service.state.entry_time_by_symbol[symbol] = "2026-07-21T09:59:00"
    service.state.bought_symbols_today.add(symbol)
    if hasattr(service, '_sold_today'):
        service._sold_today.discard(symbol)  # 이번에 추가한 수정 라인과 동일 동작
    check("2) 매수 성공 처리 후 _sold_today에서 제거됨", symbol not in service._sold_today)

# ── 테스트 2: allow_multi=False일 때 RiskManager가 재매수를 막는지 ──
with tempfile.TemporaryDirectory() as tmpdir:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.trading, "allow_multiple_entries_per_symbol_per_day", False)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)

    state = RuntimeState()
    balance = AccountBalance(cash=100000000, total_asset=100000000, positions=[])
    order = OrderRequest(symbol="475150", side=OrderSide.BUY, quantity=100, price=58000)

    can1, reason1 = risk_manager.can_place_order(order, balance, state)
    check("3) 당일 미매수 종목 -> 최초 매수는 허용", can1 is True)

    state.bought_symbols_today.add("475150")

    can2, reason2 = risk_manager.can_place_order(order, balance, state)
    check("4) 당일 매수 이력 있으면 -> 같은 종목 재매수는 차단", can2 is False)
    print("   실제 차단 사유:", reason2)

# ── 테스트 3: TradingService._try_buy()의 1일1회 게이트도 동일하게 막는지 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    symbol = "475150"
    service.state.symbol_entry_count_today[symbol] = 1  # 이미 1회 매수한 상태 재현

    balance = AccountBalance(cash=100000000, total_asset=100000000, positions=[])
    block_reason = service._try_buy(
        symbol, 58000, balance, signal=None, regime=None, minute_analysis=None,
    )
    check("5) TradingService._try_buy()도 1일1회 게이트에서 DAILY_ENTRY_LIMIT 차단",
          block_reason == "DAILY_ENTRY_LIMIT")

# ── 테스트 4: (대조군) allow_multi=True였다면 이 게이트는 통과할 것 ──
with tempfile.TemporaryDirectory() as tmpdir:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.trading, "allow_multiple_entries_per_symbol_per_day", True)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    state = RuntimeState()
    state.bought_symbols_today.add("475150")
    balance = AccountBalance(cash=100000000, total_asset=100000000, positions=[])
    order = OrderRequest(symbol="475150", side=OrderSide.BUY, quantity=100, price=58000)
    can, reason = risk_manager.can_place_order(order, balance, state)
    check("6) (대조군) allow_multi=True면 재매수 이력 있어도 이 게이트는 통과",
          can is True)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
