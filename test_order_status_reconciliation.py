# -*- coding: utf-8 -*-
"""1P0.8-D.1: Tracked Order Reconciliation 회귀 테스트.

민우님이 C.1 closure 승인과 함께 확정한 D.1 범위를 그대로 검증합니다:

    조회 대상 = 실제 추적 가능한 order_id가 있는 세 경우만
        1. BUY_PENDING  + 실제 pending_order_id
        2. SELL_PENDING + 실제 pending_order_id
        3. orphan_order_id 존재 + 실제 order_id

    제외 = None/""/"pending"/"UNKNOWN_ORDER_ID", ERROR(ambiguous
    placement), 일반 OPEN/FLAT.

    조회 결과별 행동:
        OPEN    → 유지(상태 변경 없음)
        UNKNOWN → 유지(상태 변경 없음)
        조회 예외 → 유지(상태 변경 없음, 로그만)
        FILLED  → 계좌 잔고와 "동시에" 일치할 때만 기존 공식 전이
                   메서드(confirm_buy_from_broker/on_sell_result/
                   observe_for_orphan)로 확정. 불일치면
                   ORDER_STATUS_BALANCE_MISMATCH 로그만 남기고 유지.

    side-effect(_apply_first_fill_buy_side_effects/
    _apply_deferred_sell_side_effects)는 이 라운드가 직접 호출하지
    않고, 기존 _sync_position_state_machine_shadow()의 중앙화된
    실행 지점이 자연히 감지해 정확히 1회 실행해야 합니다.

이 파일은 domain/service/trading_service.py의
TradingService._reconcile_tracked_order_status()를 검증합니다.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import (
    AccountBalance, BrokerOrder, BrokerOrderStatus, OrderSide, Position,
)
from domain.position.lifecycle import PositionLifecycle as L, is_trackable_order_id
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


class _ScriptedOrderStatusBroker(MockBroker):
    """get_order_status()의 반환값/예외를 테스트에서 직접 지정할 수 있는
    브로커 더블. 호출 인자를 전부 기록해 호출 횟수/대상을 검증합니다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.get_order_status_calls: list[tuple[str, str]] = []
        self._next: object | None = None

    def script(self, result_or_exc) -> None:
        self._next = result_or_exc

    def get_order_status(self, order_id: str, symbol: str) -> BrokerOrder:
        self.get_order_status_calls.append((order_id, symbol))
        if isinstance(self._next, BaseException):
            raise self._next
        if self._next is not None:
            return self._next
        return super().get_order_status(order_id, symbol)


def _build_service(broker) -> TradingService:
    tmpdir = tempfile.mkdtemp()
    settings = build_minimal_settings(tmpdir)
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


def _filled(order_id, symbol, side=OrderSide.BUY, qty=10) -> BrokerOrder:
    return BrokerOrder(
        order_id=order_id, symbol=symbol, status=BrokerOrderStatus.FILLED,
        side=side, requested_quantity=qty, open_quantity=0,
        filled_quantity=qty, filled_price=10000,
    )


def _open(order_id, symbol) -> BrokerOrder:
    return BrokerOrder(order_id=order_id, symbol=symbol, status=BrokerOrderStatus.OPEN)


def _unknown(order_id, symbol) -> BrokerOrder:
    return BrokerOrder(order_id=order_id, symbol=symbol, status=BrokerOrderStatus.UNKNOWN)


def _unsupported(order_id, symbol, status="PARTIALLY_FILLED") -> BrokerOrder:
    """2026-08-19 (커밋 전 보강, 민우님 GPT 리뷰): `BrokerOrderStatus`는
    현재 OPEN/FILLED/UNKNOWN 셋뿐이라 아직 실존하지 않는 상태값이지만,
    이후 실측으로 PARTIALLY_FILLED/CANCELLED/REJECTED 등이 enum에
    추가됐을 때도 `_reconcile_tracked_order_status()`가 그걸 FILLED로
    오판하지 않는지(forward fail-close) 확인하기 위한 synthetic
    값입니다. `BrokerOrder`는 `frozen=True`이지만 런타임 타입 강제는
    없으므로 `status`에 enum 밖의 순수 문자열을 직접 넣습니다.
    """
    return BrokerOrder(order_id=order_id, symbol=symbol, status=status)


_OLD_PENDING = datetime.now() - timedelta(seconds=999)  # 나이 제한 우회용


def _setup_buy_pending(svc, symbol, order_id, base_qty, requested_qty,
                        pending_since=None):
    psm = svc._position_state_machine
    psm.on_buy_requested(symbol, requested_qty, "pending")
    state = psm.get(symbol)
    state.base_quantity_before_order = base_qty
    state.known_quantity = base_qty
    state.expected_final_quantity = base_qty + requested_qty
    psm.confirm_pending_order_id(symbol, order_id)
    state.pending_since = pending_since or _OLD_PENDING
    return state


def _setup_sell_pending(svc, symbol, order_id, sell_base_qty,
                         pending_since=None):
    psm = svc._position_state_machine
    psm.on_sell_requested(symbol, sell_base_qty, "pending")
    state = psm.get(symbol)
    state.sell_base_quantity = sell_base_qty
    state.known_quantity = sell_base_qty
    psm.confirm_pending_order_id(symbol, order_id)
    state.pending_since = pending_since or _OLD_PENDING
    return state


def _setup_sell_orphan(svc, symbol, order_id, known_qty, lifecycle=L.OPEN):
    psm = svc._position_state_machine
    state = psm.get(symbol)
    state.lifecycle = lifecycle
    state.known_quantity = known_qty
    state.orphan_order_id = order_id
    state.orphan_since = datetime.now()
    state.orphan_expected_delta = -known_qty
    return state


def _setup_buy_orphan(svc, symbol, order_id, known_qty, target_qty,
                       lifecycle=L.OPEN):
    psm = svc._position_state_machine
    state = psm.get(symbol)
    state.lifecycle = lifecycle
    state.known_quantity = known_qty
    state.expected_final_quantity = target_qty
    state.orphan_order_id = order_id
    state.orphan_since = datetime.now()
    state.orphan_expected_delta = target_qty - known_qty
    return state


# ══════════════════════════════════════════════════════════════
# 0. is_trackable_order_id() — sentinel/placeholder 판별
# ══════════════════════════════════════════════════════════════
check("0-1) None -> False", is_trackable_order_id(None) is False)
check("0-2) 빈 문자열 -> False", is_trackable_order_id("") is False)
check("0-3) 공백만 -> False", is_trackable_order_id("   ") is False)
check("0-4) 'pending' placeholder -> False", is_trackable_order_id("pending") is False)
check("0-5) 'UNKNOWN_ORDER_ID' sentinel -> False",
      is_trackable_order_id("UNKNOWN_ORDER_ID") is False)
check("0-6) 실제 주문번호 -> True", is_trackable_order_id("0157897") is True)
check("0-7) 앞뒤 공백 있는 실제 번호도 True(strip 후 판정)",
      is_trackable_order_id("  157897  ") is True)


# ══════════════════════════════════════════════════════════════
# 1. BUY_PENDING 조회 결과별 행동
# ══════════════════════════════════════════════════════════════
broker1 = _ScriptedOrderStatusBroker()
svc1 = _build_service(broker1)
sym1 = "005930"
_setup_buy_pending(svc1, sym1, "700001", base_qty=0, requested_qty=10)
broker1.script(_open("700001", sym1))
svc1._reconcile_tracked_order_status(sym1, broker_qty=0)
check("1-1) BUY_PENDING + OPEN -> BUY_PENDING 유지",
      svc1._position_state_machine.get(sym1).lifecycle == L.BUY_PENDING)
check("1-2) BUY_PENDING + OPEN -> get_order_status 1회 호출",
      len(broker1.get_order_status_calls) == 1)

broker2 = _ScriptedOrderStatusBroker()
svc2 = _build_service(broker2)
sym2 = "005930"
_setup_buy_pending(svc2, sym2, "700002", base_qty=0, requested_qty=10)
broker2.script(_filled("700002", sym2, qty=10))
svc2._reconcile_tracked_order_status(sym2, broker_qty=10)  # 잔고도 목표(10) 일치
state2 = svc2._position_state_machine.get(sym2)
check("2-1) BUY_PENDING + FILLED + 잔고 일치 -> OPEN 확정",
      state2.lifecycle == L.OPEN)
check("2-2) OPEN 확정 시 known_quantity=broker_qty",
      state2.known_quantity == 10)
check("2-3) OPEN 확정 시 pending_order_id 정리됨",
      state2.pending_order_id is None)

broker3 = _ScriptedOrderStatusBroker()
svc3 = _build_service(broker3)
sym3 = "005930"
_setup_buy_pending(svc3, sym3, "700003", base_qty=0, requested_qty=10)
broker3.script(_filled("700003", sym3, qty=10))
svc3._reconcile_tracked_order_status(sym3, broker_qty=4)  # 잔고는 아직 4주(불일치)
state3 = svc3._position_state_machine.get(sym3)
check("3-1) BUY_PENDING + FILLED + 잔고 불일치 -> BUY_PENDING 유지",
      state3.lifecycle == L.BUY_PENDING)
check("3-2) 불일치 시 pending_order_id 보존(추적 정보 유지)",
      state3.pending_order_id == "700003")


# ══════════════════════════════════════════════════════════════
# 2. SELL_PENDING 조회 결과별 행동
# ══════════════════════════════════════════════════════════════
broker4 = _ScriptedOrderStatusBroker()
svc4 = _build_service(broker4)
sym4 = "005930"
_setup_sell_pending(svc4, sym4, "700004", sell_base_qty=50)
broker4.script(_open("700004", sym4))
svc4._reconcile_tracked_order_status(sym4, broker_qty=50)
check("4-1) SELL_PENDING + OPEN -> SELL_PENDING 유지",
      svc4._position_state_machine.get(sym4).lifecycle == L.SELL_PENDING)

broker5 = _ScriptedOrderStatusBroker()
svc5 = _build_service(broker5)
sym5 = "005930"
_setup_sell_pending(svc5, sym5, "700005", sell_base_qty=50)
broker5.script(_filled("700005", sym5, side=OrderSide.SELL, qty=50))
svc5._reconcile_tracked_order_status(sym5, broker_qty=0)  # 잔고 0 = 전량 매도 확인
state5 = svc5._position_state_machine.get(sym5)
check("5-1) SELL_PENDING + FILLED + 잔고0 -> FLAT 확정",
      state5.lifecycle == L.FLAT)
check("5-2) FLAT 확정 시 known_quantity=0",
      state5.known_quantity == 0)

broker6 = _ScriptedOrderStatusBroker()
svc6 = _build_service(broker6)
sym6 = "005930"
_setup_sell_pending(svc6, sym6, "700006", sell_base_qty=50)
broker6.script(_filled("700006", sym6, side=OrderSide.SELL, qty=50))
svc6._reconcile_tracked_order_status(sym6, broker_qty=20)  # 잔고 남음(불일치)
check("6-1) SELL_PENDING + FILLED + 잔고 남음 -> SELL_PENDING 유지",
      svc6._position_state_machine.get(sym6).lifecycle == L.SELL_PENDING)


# ══════════════════════════════════════════════════════════════
# 3. orphan 조회 결과별 행동
# ══════════════════════════════════════════════════════════════
broker7 = _ScriptedOrderStatusBroker()
svc7 = _build_service(broker7)
sym7 = "005930"
_setup_sell_orphan(svc7, sym7, "700007", known_qty=30)
broker7.script(_open("700007", sym7))
svc7._reconcile_tracked_order_status(sym7, broker_qty=30)
check("7-1) SELL orphan + OPEN -> orphan 유지",
      svc7._position_state_machine.get(sym7).orphan_order_id == "700007")

broker8 = _ScriptedOrderStatusBroker()
svc8 = _build_service(broker8)
sym8 = "005930"
_setup_sell_orphan(svc8, sym8, "700008", known_qty=30)
broker8.script(_filled("700008", sym8, side=OrderSide.SELL, qty=30))
svc8._reconcile_tracked_order_status(sym8, broker_qty=0)  # 목표(0) 도달
check("8-1) SELL orphan + FILLED + 잔고0(목표) -> orphan 해제",
      svc8._position_state_machine.get(sym8).orphan_order_id is None)

broker9 = _ScriptedOrderStatusBroker()
svc9 = _build_service(broker9)
sym9 = "005930"
_setup_buy_orphan(svc9, sym9, "700009", known_qty=4, target_qty=10)
broker9.script(_filled("700009", sym9, qty=10))
svc9._reconcile_tracked_order_status(sym9, broker_qty=10)  # 목표(10) 도달
check("9-1) BUY orphan + FILLED + 목표잔고 도달 -> orphan 해제",
      svc9._position_state_machine.get(sym9).orphan_order_id is None)

broker9b = _ScriptedOrderStatusBroker()
svc9b = _build_service(broker9b)
sym9b = "005930"
_setup_buy_orphan(svc9b, sym9b, "700009b", known_qty=4, target_qty=10)
broker9b.script(_filled("700009b", sym9b, qty=10))
svc9b._reconcile_tracked_order_status(sym9b, broker_qty=7)  # 목표 미도달(불일치)
check("9b-1) BUY orphan + FILLED + 목표 미도달 -> orphan 유지",
      svc9b._position_state_machine.get(sym9b).orphan_order_id == "700009b")

broker10 = _ScriptedOrderStatusBroker()
svc10 = _build_service(broker10)
sym10 = "005930"
_setup_sell_orphan(svc10, sym10, "700010", known_qty=30)
broker10.script(_unknown("700010", sym10))
svc10._reconcile_tracked_order_status(sym10, broker_qty=30)
check("10-1) orphan + UNKNOWN -> orphan 유지",
      svc10._position_state_machine.get(sym10).orphan_order_id == "700010")


# ══════════════════════════════════════════════════════════════
# 4. 조회 자체가 예외를 던지는 경우 — 전부 유지, 로그만
# ══════════════════════════════════════════════════════════════
from infra.broker.kiwoom_broker import KiwoomPaginationIncompleteError

broker11 = _ScriptedOrderStatusBroker()
svc11 = _build_service(broker11)
sym11 = "005930"
_setup_sell_pending(svc11, sym11, "700011", sell_base_qty=50)
broker11.script(KiwoomPaginationIncompleteError("cont-yn 프로토콜 위반(테스트)"))
try:
    svc11._reconcile_tracked_order_status(sym11, broker_qty=0)
    _raised11 = False
except Exception:
    _raised11 = True
check("11-1) 조회 예외가 호출부로 전파되지 않음(fail-close, 삼킴)",
      _raised11 is False)
check("11-2) 조회 예외 시 SELL_PENDING 그대로 유지(잔고=0이어도 FLAT으로 확정 안 됨)",
      svc11._position_state_machine.get(sym11).lifecycle == L.SELL_PENDING)

broker12 = _ScriptedOrderStatusBroker()
svc12 = _build_service(broker12)
sym12 = "005930"
_setup_buy_orphan(svc12, sym12, "700012", known_qty=4, target_qty=10)
broker12.script(RuntimeError("네트워크 오류(테스트)"))
svc12._reconcile_tracked_order_status(sym12, broker_qty=10)  # 목표 도달이어도
check("12-1) orphan + 조회 예외(일반 RuntimeError) -> orphan 유지",
      svc12._position_state_machine.get(sym12).orphan_order_id == "700012")


# ══════════════════════════════════════════════════════════════
# 5. 추적 불가능한 order_id — 애초에 조회하지 않음(429 절약)
# ══════════════════════════════════════════════════════════════
broker13 = _ScriptedOrderStatusBroker()
svc13 = _build_service(broker13)
sym13 = "005930"
# on_buy_requested 직후, confirm_pending_order_id를 호출하지 않은 상태
# -> pending_order_id가 여전히 "pending" placeholder
svc13._position_state_machine.on_buy_requested(sym13, 10, "pending")
state13 = svc13._position_state_machine.get(sym13)
state13.pending_since = _OLD_PENDING
svc13._reconcile_tracked_order_status(sym13, broker_qty=0)
check("13-1) pending_order_id='pending' -> get_order_status 호출 0회",
      len(broker13.get_order_status_calls) == 0)

broker14 = _ScriptedOrderStatusBroker()
svc14 = _build_service(broker14)
sym14 = "005930"
_setup_sell_orphan(svc14, sym14, "UNKNOWN_ORDER_ID", known_qty=30)
svc14._reconcile_tracked_order_status(sym14, broker_qty=30)
check("14-1) orphan_order_id='UNKNOWN_ORDER_ID' -> get_order_status 호출 0회",
      len(broker14.get_order_status_calls) == 0)


# ══════════════════════════════════════════════════════════════
# 6. ERROR(ambiguous placement) 자동복구 제외
# ══════════════════════════════════════════════════════════════
broker15 = _ScriptedOrderStatusBroker()
svc15 = _build_service(broker15)
sym15 = "005930"
svc15._position_state_machine.on_sell_requested(sym15, 50, "pending")
svc15._position_state_machine.on_placement_ambiguous(sym15, "SELL", "timeout(테스트)")
state15 = svc15._position_state_machine.get(sym15)
check("15-0) 사전조건: ERROR(ambiguous placement) 상태 진입 확인",
      state15.lifecycle == L.ERROR)
svc15._reconcile_tracked_order_status(sym15, broker_qty=50)
check("15-1) ERROR(ambiguous placement) -> get_order_status 호출 0회(추적 대상 아님)",
      len(broker15.get_order_status_calls) == 0)
check("15-2) ERROR 상태 그대로 유지",
      svc15._position_state_machine.get(sym15).lifecycle == L.ERROR)


# ══════════════════════════════════════════════════════════════
# 7. 조회 빈도 제한 — 나이 게이트 + 최소 간격
# ══════════════════════════════════════════════════════════════
broker16 = _ScriptedOrderStatusBroker()
svc16 = _build_service(broker16)
sym16 = "005930"
# 방금 시작된 PENDING(나이 0초에 가까움) -> 아직 조회 안 함
_setup_buy_pending(svc16, sym16, "700016", base_qty=0, requested_qty=10,
                    pending_since=datetime.now())
broker16.script(_filled("700016", sym16, qty=10))
svc16._reconcile_tracked_order_status(sym16, broker_qty=10)
check("16-1) 방금 시작된 BUY_PENDING(나이<임계값) -> 아직 조회 안 함",
      len(broker16.get_order_status_calls) == 0)
check("16-2) 나이 게이트로 스킵된 경우 BUY_PENDING 유지(잔고는 이미 일치해도)",
      svc16._position_state_machine.get(sym16).lifecycle == L.BUY_PENDING)

broker17 = _ScriptedOrderStatusBroker()
svc17 = _build_service(broker17)
sym17 = "005930"
_setup_sell_orphan(svc17, sym17, "700017", known_qty=30)
broker17.script(_open("700017", sym17))
svc17._reconcile_tracked_order_status(sym17, broker_qty=30)
check("17-1) orphan은 나이 게이트 없이 즉시 조회 대상",
      len(broker17.get_order_status_calls) == 1)

broker18 = _ScriptedOrderStatusBroker()
svc18 = _build_service(broker18)
sym18 = "005930"
_setup_sell_orphan(svc18, sym18, "700018", known_qty=30)
broker18.script(_open("700018", sym18))
svc18._reconcile_tracked_order_status(sym18, broker_qty=30)  # 1회차 — 조회함
svc18._reconcile_tracked_order_status(sym18, broker_qty=30)  # 2회차 — 최소 간격 미달
check("18-1) 최소 간격 이내 재호출 -> get_order_status 추가 호출 없음(여전히 1회)",
      len(broker18.get_order_status_calls) == 1)
svc18._last_order_status_query_at[sym18] = datetime.now() - timedelta(seconds=999)
svc18._reconcile_tracked_order_status(sym18, broker_qty=30)  # 간격 경과 후 — 재조회
check("18-2) 최소 간격 경과 후에는 재조회함(2회)",
      len(broker18.get_order_status_calls) == 2)


# ══════════════════════════════════════════════════════════════
# 8. 일반 OPEN/FLAT(추적 대상 아님) — 조회 자체를 하지 않음
# ══════════════════════════════════════════════════════════════
broker19 = _ScriptedOrderStatusBroker()
svc19 = _build_service(broker19)
sym19 = "005930"
svc19._position_state_machine.sync_from_broker(sym19, 30)  # OPEN, orphan 없음
svc19._reconcile_tracked_order_status(sym19, broker_qty=30)
check("19-1) 일반 OPEN(orphan 없음) -> get_order_status 호출 0회",
      len(broker19.get_order_status_calls) == 0)

broker20 = _ScriptedOrderStatusBroker()
svc20 = _build_service(broker20)
sym20 = "005930"
svc20._position_state_machine.sync_from_broker(sym20, 0)  # FLAT
svc20._reconcile_tracked_order_status(sym20, broker_qty=0)
check("20-1) 일반 FLAT -> get_order_status 호출 0회",
      len(broker20.get_order_status_calls) == 0)


# ══════════════════════════════════════════════════════════════
# 9. 통합 — _sync_position_state_machine_shadow() 전체 폴링 경로에서
#    order-status 확정이 기존 중앙화된 side-effect 실행 지점을 통해
#    정확히 1회만 실행되고, 중복 브로커 호출이 없는지 확인
# ══════════════════════════════════════════════════════════════
broker21 = _ScriptedOrderStatusBroker()
svc21 = _build_service(broker21)
sym21 = "005930"

svc21.broker._positions[sym21] = Position(symbol=sym21, quantity=100, average_price=10000)
svc21.broker._prices[sym21] = 10100
svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
check("21-0) 초기 동기화 후 OPEN",
      svc21._position_state_machine.get(sym21).lifecycle == L.OPEN)

svc21._try_sell(sym21, 100, current_price=10100,
                 exit_reason="트레일링 스탑 — 테스트", avg_buy_price=10000)
check("21-1) SELL accepted 후 SELL_PENDING + pending side-effect 컨텍스트 존재",
      svc21._position_state_machine.get(sym21).lifecycle == L.SELL_PENDING
      and sym21 in svc21._pending_sell_side_effects)

# 나이 게이트를 통과시키기 위해 pending_since를 과거로 되돌림(실제로는
# 시간이 흘러야 하지만 테스트에서는 직접 조정)
svc21._position_state_machine.get(sym21).pending_since = _OLD_PENDING
# 잔고 API는 아직 100(미반영)이지만, order status는 이미 FILLED(전량 매도)
svc21.broker._positions[sym21] = Position(symbol=sym21, quantity=100, average_price=10000)
broker21.script(_filled("MOCK_SELL", sym21, side=OrderSide.SELL, qty=100))
# 하지만 order-status 확정에는 "잔고와 동시 일치"가 필요하므로, 브로커
# 잔고가 정말로 0이 된 상태에서 폴링이 도는 시나리오로 구성
svc21.broker._positions.pop(sym21, None)

svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
check("21-2) order-status(FILLED)+잔고(0) 일치로 SELL_PENDING -> FLAT 확정",
      svc21._position_state_machine.get(sym21).lifecycle == L.FLAT)
check("21-3) 중앙화된 지점에서 deferred sell side-effect가 정확히 1회 실행(정리됨)",
      sym21 not in svc21._pending_sell_side_effects)
check("21-4) 이번 폴링에서 get_order_status는 정확히 1회만 호출됨(중복 없음)",
      len(broker21.get_order_status_calls) == 1)
check("21-5) 재진입 쿨다운(last_sold_at)이 기록됨 — side-effect가 실제로 실행됐다는 증거",
      sym21 in svc21.state.last_sold_at_by_symbol)


# ══════════════════════════════════════════════════════════════
# 10. 미지원 주문 상태(forward fail-close) — 2026-08-19 커밋 전 보강
#    BrokerOrderStatus에 아직 없는 값(예: 향후 PARTIALLY_FILLED)이
#    와도 FILLED처럼 처리하지 않고 무조건 유지해야 합니다.
# ══════════════════════════════════════════════════════════════
broker22 = _ScriptedOrderStatusBroker()
svc22 = _build_service(broker22)
sym22 = "005930"
_setup_buy_pending(svc22, sym22, "700022", base_qty=0, requested_qty=10)
broker22.script(_unsupported("700022", sym22))
svc22._reconcile_tracked_order_status(sym22, broker_qty=10)  # 잔고는 목표와 일치해도
state22 = svc22._position_state_machine.get(sym22)
check("22-1) BUY_PENDING + 미지원 status(잔고는 목표 일치) -> BUY_PENDING 유지",
      state22.lifecycle == L.BUY_PENDING)
check("22-2) 미지원 status에도 pending_order_id 보존",
      state22.pending_order_id == "700022")

broker23 = _ScriptedOrderStatusBroker()
svc23 = _build_service(broker23)
sym23 = "005930"
_setup_sell_pending(svc23, sym23, "700023", sell_base_qty=50)
broker23.script(_unsupported("700023", sym23))
svc23._reconcile_tracked_order_status(sym23, broker_qty=0)  # 잔고 0(목표)이어도
check("23-1) SELL_PENDING + 미지원 status(잔고는 목표 일치) -> SELL_PENDING 유지(FLAT 아님)",
      svc23._position_state_machine.get(sym23).lifecycle == L.SELL_PENDING)

broker24 = _ScriptedOrderStatusBroker()
svc24 = _build_service(broker24)
sym24 = "005930"
_setup_sell_orphan(svc24, sym24, "700024", known_qty=30)
broker24.script(_unsupported("700024", sym24))
svc24._reconcile_tracked_order_status(sym24, broker_qty=0)  # 목표(0) 일치해도
check("24-1) orphan + 미지원 status(잔고는 목표 일치) -> orphan 해제 안 됨(유지)",
      svc24._position_state_machine.get(sym24).orphan_order_id == "700024")
check("24-2) 미지원 status도 조회는 정확히 1회만(재시도 남발 없음)",
      len(broker24.get_order_status_calls) == 1)


# ══════════════════════════════════════════════════════════════
# 11. 통합 — BUY_PENDING 전체 폴링 경로에서 first-fill BUY side-effect가
#    정확히 1회만 실행되는지 확인 (섹션 9의 SELL 쪽과 대칭)
# ══════════════════════════════════════════════════════════════
broker25 = _ScriptedOrderStatusBroker()
svc25 = _build_service(broker25)
sym25 = "005930"

# _sync_position_state_machine_shadow()는 최초 호출 시(_position_state_
# machine_initialized=False) 브로커 잔고로 초기화만 하고 바로
# return합니다(위 구현 참고, 섹션 9의 21-0과 동일한 이유) — 아래
# BUY_PENDING 시나리오를 실제로 통과시키려면 먼저 한 번 "워밍업" 호출로
# 초기화를 끝내둬야 합니다.
svc25._sync_position_state_machine_shadow(svc25.broker.get_account_balance())

# _try_buy()의 진입 제한 게이트(당일 진입횟수/시간대/쿨다운 등)를
# 우회하기 위해, place_order()가 accepted됐을 때 실제로 남기는 상태만
# 그대로 재현합니다 — PSM 쪽은 _setup_buy_pending()(기존 헬퍼, on_buy_
# requested 기반)로, side-effect 컨텍스트는 _try_buy() 안에서 실제로
# 채우는 것과 동일한 키 구성으로 채웁니다.
_setup_buy_pending(svc25, sym25, "MOCK_BUY_25", base_qty=0, requested_qty=100)
svc25._pending_buy_side_effects[sym25] = {
    "order_id": "MOCK_BUY_25",
    "current_price": 10100,
    "quantity": 100,
    "regime": None,
    "signal_reason": "테스트",
}
check("25-0) 사전조건: BUY_PENDING + pending BUY side-effect 컨텍스트 존재",
      svc25._position_state_machine.get(sym25).lifecycle == L.BUY_PENDING
      and sym25 in svc25._pending_buy_side_effects)

# 잔고 API는 이미 100주(목표 일치), order status도 FILLED
svc25.broker._positions[sym25] = Position(symbol=sym25, quantity=100, average_price=10100)
broker25.script(_filled("MOCK_BUY_25", sym25, side=OrderSide.BUY, qty=100))

svc25._sync_position_state_machine_shadow(svc25.broker.get_account_balance())
check("25-1) order-status(FILLED)+잔고(100, 목표 일치)로 BUY_PENDING -> OPEN 확정",
      svc25._position_state_machine.get(sym25).lifecycle == L.OPEN)
check("25-2) 중앙화된 지점에서 first-fill BUY side-effect가 정확히 1회 실행(정리됨)",
      sym25 not in svc25._pending_buy_side_effects)
check("25-3) entry_time_by_symbol이 기록됨 — side-effect가 실제로 실행됐다는 증거",
      sym25 in svc25.state.entry_time_by_symbol)
check("25-4) symbol_entry_count_today가 정확히 1 증가(중복 실행 아님)",
      svc25.state.symbol_entry_count_today.get(sym25) == 1)
check("25-5) 이번 폴링에서 get_order_status는 정확히 1회만 호출됨(중복 없음)",
      len(broker25.get_order_status_calls) == 1)

# 다음 폴링에서 재실행되지 않는지(pop 기반 멱등성) 재확인
svc25._sync_position_state_machine_shadow(svc25.broker.get_account_balance())
check("25-6) 다음 폴링에서도 entry_count가 그대로(재실행 없음)",
      svc25.state.symbol_entry_count_today.get(sym25) == 1)


# ══════════════════════════════════════════════════════════════
# 12. 1P0.8-D.1.1 — order-status 조회 global query budget
#    (2026-08-19 커밋 전 리뷰 반영: 종목별 30초 throttle과 별개로,
#    폴링 1회당 get_order_status()는 최대 1건, 가장 오래 대기 중인
#    후보를 우선 조회)
# ══════════════════════════════════════════════════════════════
broker26 = _ScriptedOrderStatusBroker()
svc26 = _build_service(broker26)
symA, symB, symC = "111111", "222222", "333333"
_setup_buy_pending(svc26, symA, "800A", base_qty=0, requested_qty=10,
                    pending_since=datetime.now() - timedelta(seconds=500))
_setup_buy_pending(svc26, symB, "800B", base_qty=0, requested_qty=10,
                    pending_since=datetime.now() - timedelta(seconds=100))
_setup_sell_orphan(svc26, symC, "800C", known_qty=5)
svc26._position_state_machine.get(symC).orphan_since = datetime.now() - timedelta(seconds=300)

target26 = svc26._select_order_status_query_target([symA, symB, symC])
check("26-1) 세 후보(A:500초, B:100초, C:300초 대기) 중 가장 오래된 A가 선택됨",
      target26 == symA)

broker26.script(_open("800A", symA))
svc26._reconcile_tracked_order_status(symA, broker_qty=0)
check("26-2) 선택된 A만 실제로 조회됨(get_order_status 1회)",
      len(broker26.get_order_status_calls) == 1)
check("26-3) 선택되지 않은 B/C는 이번엔 조회 이력이 없음(_last_order_status_query_at에 없음)",
      symB not in svc26._last_order_status_query_at
      and symC not in svc26._last_order_status_query_at)

# A가 방금 조회돼 종목별 30초 throttle에 걸리면, 다음 선택은 두 번째로
# 오래된 C(300초 대기, B의 100초보다 오래 기다림)여야 함
target26b = svc26._select_order_status_query_target([symA, symB, symC])
check("26-4) A가 방금 조회돼 제외되면 다음은 C(B보다 더 오래 대기)가 선택됨",
      target26b == symC)

broker27 = _ScriptedOrderStatusBroker()
svc27 = _build_service(broker27)
target27 = svc27._select_order_status_query_target(["005930", "000660"])
check("27-1) 후보가 전혀 없으면 None 반환",
      target27 is None)

# 전체 폴링 경로 통합 확인: 세 종목이 동시에 조회 후보가 돼도
# _sync_position_state_machine_shadow() 한 번에 get_order_status는
# 정확히 1회만 호출돼야 함(가장 오래 대기 중인 종목만)
broker28 = _ScriptedOrderStatusBroker()
svc28 = _build_service(broker28)
symD, symE, symF = "444444", "555555", "666666"
svc28._sync_position_state_machine_shadow(svc28.broker.get_account_balance())  # 워밍업(초기화)

_setup_buy_pending(svc28, symD, "800D", base_qty=0, requested_qty=10,
                    pending_since=datetime.now() - timedelta(seconds=500))
_setup_buy_pending(svc28, symE, "800E", base_qty=0, requested_qty=10,
                    pending_since=datetime.now() - timedelta(seconds=100))
_setup_sell_orphan(svc28, symF, "800F", known_qty=5)
svc28._position_state_machine.get(symF).orphan_since = datetime.now() - timedelta(seconds=300)
# symF의 잔고를 orphan 목표(0)가 아니라 현재값(5)으로 유지 — 그래야
# 기존 잔고 기반 observe_for_orphan()이 이번 폴링에서 우연히 orphan을
# 풀지 않고, D.1.1이 조회를 늦춘 효과만 순수하게 관찰할 수 있음.
svc28.broker._positions[symF] = Position(symbol=symF, quantity=5, average_price=10000)
broker28.script(_open("800D", symD))  # 어떤 종목이 조회되든 OPEN(상태 변경 없음)으로 응답

svc28._sync_position_state_machine_shadow(svc28.broker.get_account_balance())
check("28-1) 세 종목이 동시에 후보여도 이번 폴링에서 get_order_status는 정확히 1회만 호출",
      len(broker28.get_order_status_calls) == 1)
check("28-2) 호출 대상은 가장 오래 대기 중인 D(500초)",
      broker28.get_order_status_calls[0] == ("800D", symD))
check("28-3) 조회되지 않은 E/F는 lifecycle이 그대로 유지됨(D.1.1이 상태 자체를 막지 않음, 조회만 늦춤)",
      svc28._position_state_machine.get(symE).lifecycle == L.BUY_PENDING
      and svc28._position_state_machine.get(symF).orphan_order_id == "800F")


print(f"\n총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
sys.exit(1 if failed else 0)
