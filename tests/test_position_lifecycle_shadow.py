# -*- coding: utf-8 -*-
"""
포지션 5단계 상태머신(shadow) 단위테스트 (2026-07-22)

domain/position/lifecycle.py의 PositionStateMachine 자체 로직과,
TradingService에 shadow로 배선된 부분을 각각 검증한다. shadow
모드이므로 실제 매매 판정에는 영향이 없어야 함 — 배선 테스트는
그것도 함께 확인한다.
"""
from __future__ import annotations

import sys
import tempfile

sys.path.insert(0, ".")

from domain.position.lifecycle import PositionLifecycle, PositionStateMachine
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


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: PositionStateMachine 자체 로직 (도메인 모듈 단독 테스트)
# ══════════════════════════════════════════════════════════════

# ── 1) 초기 상태는 FLAT ─────────────────────────────────────────
psm = PositionStateMachine()
check("1) 신규 종목 초기 상태 = FLAT", psm.get(symbol).lifecycle == PositionLifecycle.FLAT)

# ── 2) 매수 정상 흐름: FLAT -> BUY_PENDING -> OPEN ──────────────
psm = PositionStateMachine()
psm.on_buy_requested(symbol, 100, "order-1")
check("2) 매수 요청 -> BUY_PENDING", psm.get(symbol).lifecycle == PositionLifecycle.BUY_PENDING)
psm.on_buy_result(symbol, accepted=True, broker_quantity=100)
check("   매수 체결 -> OPEN", psm.get(symbol).lifecycle == PositionLifecycle.OPEN)
check("   known_quantity 갱신됨", psm.get(symbol).known_quantity == 100)

# ── 3) 매수 거부: BUY_PENDING -> FLAT(원래 미보유였으면) ─────────
psm = PositionStateMachine()
psm.on_buy_requested(symbol, 100, "order-2")
psm.on_buy_result(symbol, accepted=False, broker_quantity=0)
check("3) 매수 거부, 원래 미보유 -> FLAT 복귀", psm.get(symbol).lifecycle == PositionLifecycle.FLAT)
check("   last_error 기록됨", psm.get(symbol).last_error == "BUY_REJECTED")

# ── 4) 매도 정상 흐름: OPEN -> SELL_PENDING -> FLAT(전량체결) ────
psm = PositionStateMachine()
psm.sync_from_broker(symbol, 100)  # 최초 OPEN 상태로 세팅
psm.on_sell_requested(symbol, 100, "order-3")
check("4) 매도 요청 -> SELL_PENDING", psm.get(symbol).lifecycle == PositionLifecycle.SELL_PENDING)
psm.on_sell_result(symbol, accepted=True, broker_quantity=0)  # 다음 폴링에서 잔고 0 확인
check("   전량체결(잔고0) -> FLAT", psm.get(symbol).lifecycle == PositionLifecycle.FLAT)

# ── 5) 매도 미반영(API 지연): SELL_PENDING 유지 (7.4절 원조 문제 대응) ──
psm = PositionStateMachine()
psm.sync_from_broker(symbol, 100)
psm.on_sell_requested(symbol, 100, "order-4")
psm.on_sell_result(symbol, accepted=True, broker_quantity=100)  # 매도전과 동일 수량 = 미반영
check("5) 매도 접수됐지만 API 미반영(수량 동일) -> SELL_PENDING 유지",
      psm.get(symbol).lifecycle == PositionLifecycle.SELL_PENDING)

# ── 6) 매도 부분체결: SELL_PENDING -> OPEN(잔여수량) ─────────────
psm = PositionStateMachine()
psm.sync_from_broker(symbol, 100)
psm.on_sell_requested(symbol, 100, "order-5")
psm.on_sell_result(symbol, accepted=True, broker_quantity=40)  # 60주만 체결, 40주 잔존
check("6) 부분체결 -> OPEN(잔여수량)으로 복귀", psm.get(symbol).lifecycle == PositionLifecycle.OPEN)
check("   known_quantity가 잔여수량(40)으로 갱신", psm.get(symbol).known_quantity == 40)
check("   last_error = PARTIAL_FILL", psm.get(symbol).last_error == "PARTIAL_FILL")

# ── 7) 매도 거부: SELL_PENDING -> OPEN(원래 수량 유지) ───────────
psm = PositionStateMachine()
psm.sync_from_broker(symbol, 100)
psm.on_sell_requested(symbol, 100, "order-6")
psm.on_sell_result(symbol, accepted=False, broker_quantity=0)
check("7) 매도 거부 -> OPEN 복귀(여전히 보유중)", psm.get(symbol).lifecycle == PositionLifecycle.OPEN)
check("   last_error = SELL_REJECTED", psm.get(symbol).last_error == "SELL_REJECTED")

# ── 8) 불변조건 검사: 잔고 있는데 FLAT -> 위반 감지 (7.12절 사고 유형) ──
psm = PositionStateMachine()
# lifecycle은 기본 FLAT인 채로 두고, 브로커 잔고만 있는 상황을 흉내
violation = psm.check_invariant(symbol, broker_quantity=201)
check("8) 잔고 있는데 로컬상태 FLAT -> 위반 감지됨", violation is not None)
check("   위반 메시지에 종목코드 포함", symbol in violation)

# ── 9) 불변조건 검사: PENDING 중에는 위반 아님 (정상적 일시 불일치) ──
psm = PositionStateMachine()
psm.on_buy_requested(symbol, 100, "order-7")  # BUY_PENDING (아직 lifecycle=BUY_PENDING, FLAT 아님)
violation = psm.check_invariant(symbol, broker_quantity=0)
check("9) BUY_PENDING 중 잔고0 -> 위반 아님(정상)", violation is None)

# ── 10) 불변조건 검사: OPEN 상태에서 잔고 일치 -> 위반 아님 ──────
psm = PositionStateMachine()
psm.sync_from_broker(symbol, 100)
violation = psm.check_invariant(symbol, broker_quantity=100)
check("10) OPEN 상태에서 실제 잔고와 일치 -> 위반 아님", violation is None)

print()
print("── 2부: TradingService shadow 배선 검증 ──")


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


# ── 11) TradingService 생성 시 상태머신이 초기화되는지 ───────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    check("11) TradingService에 _position_state_machine 존재",
          hasattr(service, "_position_state_machine"))
    check("    아직 초기화 안 됨(_position_state_machine_initialized=False)",
          service._position_state_machine_initialized is False)

# ── 12) _try_sell 호출 시 상태머신에 SELL_PENDING 통지되는지 ─────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.broker._positions[symbol] = Position(symbol=symbol, quantity=100, average_price=58000)
    service.broker._prices[symbol] = 58400
    # 상태머신을 미리 OPEN으로 세팅 (실제로는 sync에서 자동으로 됨)
    service._position_state_machine.sync_from_broker(symbol, 100)

    service._try_sell(symbol, 100, current_price=58400,
                       exit_reason="트레일링 스탑 — 테스트", avg_buy_price=58000)
    check("12) _try_sell 호출 -> 상태머신도 SELL_PENDING으로 전이",
          service._position_state_machine.get(symbol).lifecycle == PositionLifecycle.SELL_PENDING)

# ── 13) shadow 모드가 실제 매매 판정(기존 로직)에 영향 없는지 ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.broker._positions[symbol] = Position(symbol=symbol, quantity=100, average_price=58000)
    service.broker._prices[symbol] = 58400
    service._sold_today = {symbol}
    service._sold_today_qty_snapshot = {symbol: 100}

    # 기존 로직으로 position 판정 (실제 _process_symbol과 동일 로직)
    balance = service.broker.get_account_balance()
    position_check = next((p for p in balance.positions if p.symbol == symbol), None)
    sold_qty_snapshot = getattr(service, '_sold_today_qty_snapshot', {})
    if (
        position_check is not None
        and symbol in sold_qty_snapshot
        and position_check.quantity == sold_qty_snapshot[symbol]
    ):
        position_check = None

    # 상태머신 shadow 동기화를 실행해도 위 판정 결과가 그대로인지 확인
    service._sync_position_state_machine_shadow(balance)
    position_check_after = next((p for p in balance.positions if p.symbol == symbol), None)
    if (
        position_check_after is not None
        and symbol in sold_qty_snapshot
        and position_check_after.quantity == sold_qty_snapshot[symbol]
    ):
        position_check_after = None

    check("13) shadow 동기화 실행 전후 기존 판정 결과 동일(shadow가 실판정에 영향 없음)",
          (position_check is None) == (position_check_after is None))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
