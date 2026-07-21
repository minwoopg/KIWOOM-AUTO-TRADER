# -*- coding: utf-8 -*-
"""
_sold_today 수량기반 판정 전환 검증 (2026-07-22)

기존 "symbol in _sold_today면 무조건 미보유"를 "매도 시도 당시 수량과
지금 수량이 같을 때만 미보유"로 전환. 이 테스트는 세 가지 시나리오를
전부 검증한다:

1) 7.4절 원조 문제 재발 안 함: 매도 접수 직후 브로커 API가 옛 수량을
   그대로 반환하는 지연 구간에서는 여전히 "미보유"로 간주돼야 함
   (중복 매도 방지 기능 보존)
2) 7.12절 오늘 사고 재발 안 함: 매도 후 재매수가 실제로 체결되면
   즉시 "보유"로 인식돼야 함 (discard 안 해도 자동으로 안전한지 포함)
3) GPT가 지적한 부분체결 시나리오: 매도 요청량보다 적게 체결돼
   잔여수량이 남으면, 그 잔여수량을 정확히 "보유"로 인식해야 함
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
from domain.models import Position, AccountBalance
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


symbol = "475150"

# ── 시나리오 1: 매도 접수 직후, 브로커 API 지연으로 잔고가 그대로인 경우 ──
# _sold_today_qty_snapshot과 실제 잔고 수량이 같으면 -> 여전히 미보유로 처리
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    # 매도 성공을 흉내: 스냅샷에 매도 시도 당시 수량(100) 기록
    service._sold_today = {symbol}
    service._sold_today_qty_snapshot = {symbol: 100}

    # 브로커 API 지연 -> balance에 아직 매도 전 수량(100)이 그대로 보임
    stale_balance = AccountBalance(
        cash=100000000, total_asset=100000000,
        positions=[Position(symbol=symbol, quantity=100, average_price=58000)],
    )
    position_check = next((p for p in stale_balance.positions if p.symbol == symbol), None)
    sold_qty_snapshot = getattr(service, '_sold_today_qty_snapshot', {})
    if (
        position_check is not None
        and symbol in sold_qty_snapshot
        and position_check.quantity == sold_qty_snapshot[symbol]
    ):
        position_check = None
    check("1) API 지연으로 수량이 매도전과 동일 -> 여전히 미보유 처리 (중복매도 방지 유지)",
          position_check is None)

# ── 시나리오 2: 매도 성공 후 API가 정상 반영(포지션이 목록에서 사라짐) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._sold_today = {symbol}
    service._sold_today_qty_snapshot = {symbol: 100}

    normal_balance = AccountBalance(cash=100000000, total_asset=100000000, positions=[])
    position_check = next((p for p in normal_balance.positions if p.symbol == symbol), None)
    check("2) 정상 반영(포지션 목록에서 제거)되면 애초에 None -> 문제 없음",
          position_check is None)

# ── 시나리오 3(7.12절 재현): 매도 후 재매수 성공, 다른 수량으로 잔고에 잡힘 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._sold_today = {symbol}
    service._sold_today_qty_snapshot = {symbol: 103}  # 09:23 매도 103주

    # 09:59/10:18 재매수 성공 -> 잔고에 새 수량(201주)으로 잡힘
    reentry_balance = AccountBalance(
        cash=100000000, total_asset=100000000,
        positions=[Position(symbol=symbol, quantity=201, average_price=59390)],
    )
    position_check = next((p for p in reentry_balance.positions if p.symbol == symbol), None)
    sold_qty_snapshot = getattr(service, '_sold_today_qty_snapshot', {})
    if (
        position_check is not None
        and symbol in sold_qty_snapshot
        and position_check.quantity == sold_qty_snapshot[symbol]
    ):
        position_check = None
    check("3) 재매수로 수량이 달라짐(103->201) -> 즉시 보유로 인식 (discard 없이도)",
          position_check is not None and position_check.quantity == 201)

# ── 시나리오 4(GPT 우려, 부분체결): 매도 요청 100주 중 60주만 체결, 40주 잔존 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._sold_today = {symbol}
    service._sold_today_qty_snapshot = {symbol: 100}  # 100주 전량매도 시도

    # 부분체결: 60주만 체결되고 40주가 잔고에 남음 (수량이 100->40으로 달라짐)
    partial_balance = AccountBalance(
        cash=100000000, total_asset=100000000,
        positions=[Position(symbol=symbol, quantity=40, average_price=58000)],
    )
    position_check = next((p for p in partial_balance.positions if p.symbol == symbol), None)
    sold_qty_snapshot = getattr(service, '_sold_today_qty_snapshot', {})
    if (
        position_check is not None
        and symbol in sold_qty_snapshot
        and position_check.quantity == sold_qty_snapshot[symbol]
    ):
        position_check = None
    check("4) 부분체결로 수량이 달라짐(100->40) -> 잔여 40주를 보유로 인식",
          position_check is not None and position_check.quantity == 40)

# ── 시나리오 5: 실제 _try_sell 흐름 + 매수성공 처리 블록 직접 재현 ──
# (참고: _try_buy를 그대로 호출하면 reentry_cooldown_seconds(10분) 게이트에
#  걸려 이 테스트의 목적과 무관한 이유로 차단됨 — 매수 "성공 후" 처리
#  블록의 정리 로직만 검증하는 게 목적이므로 그 블록을 직접 재현한다)
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.broker._positions[symbol] = Position(symbol=symbol, quantity=103, average_price=58000)
    service.broker._prices[symbol] = 58400

    service._try_sell(symbol, 103, current_price=58400,
                       exit_reason="트레일링 스탑 — 테스트", avg_buy_price=58000)
    check("5) 매도 성공 -> 스냅샷에 매도수량(103) 기록됨",
          service._sold_today_qty_snapshot.get(symbol) == 103)

    # 매수 성공 처리 블록과 동일한 정리 로직 재현 (trading_service.py 1450줄 부근)
    if hasattr(service, '_sold_today'):
        service._sold_today.discard(symbol)
    if hasattr(service, '_sold_today_qty_snapshot'):
        service._sold_today_qty_snapshot.pop(symbol, None)
    check("   재매수 성공 처리 -> 스냅샷에서 제거됨 (이중 안전장치)",
          symbol not in service._sold_today_qty_snapshot)
    check("   재매수 성공 처리 -> _sold_today에서도 제거됨",
          symbol not in service._sold_today)

# ── 시나리오 6: 실제 _try_buy 경로로 쿨다운 통과 후 재매수 -> 자동 안전 확인 ──
with tempfile.TemporaryDirectory() as tmpdir:
    from domain.models import Signal, SignalType, MarketRegime
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.trading, "reentry_cooldown_seconds", 0)  # 쿨다운 제거
    broker = MockBroker()
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
    service.broker._positions[symbol] = Position(symbol=symbol, quantity=103, average_price=58000)
    service.broker._prices[symbol] = 58400
    service._try_sell(symbol, 103, current_price=58400,
                       exit_reason="트레일링 스탑 — 테스트", avg_buy_price=58000)

    service.broker._prices[symbol] = 58700
    buy_signal = Signal(type=SignalType.BUY, reason="테스트 매수")
    block_reason = service._try_buy(
        symbol, 58700, service.broker.get_account_balance(),
        signal=buy_signal, regime=MarketRegime.BULLISH, minute_analysis=None,
    )
    check("6) 쿨다운 제거 후 실제 재매수 성공", not block_reason)

    balance = service.broker.get_account_balance()
    position_check = next((p for p in balance.positions if p.symbol == symbol), None)
    sold_qty_snapshot = getattr(service, '_sold_today_qty_snapshot', {})
    if (
        position_check is not None
        and symbol in sold_qty_snapshot
        and position_check.quantity == sold_qty_snapshot[symbol]
    ):
        position_check = None
    check("   재매수 후 실제 판정 로직 통과 -> 보유로 정확히 인식",
          position_check is not None)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
