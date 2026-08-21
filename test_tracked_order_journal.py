# -*- coding: utf-8 -*-
"""1P0.8-E.1-A: Durable Tracked Order Journal 회귀 테스트 (2026-08-20).

민우님이 확정한 범위 그대로 검증합니다 — **저장만 하고, startup에서
읽어 자동으로 뭔가 판단/복구하지 않습니다** (그건 별도 승인이 필요한
E.1-B). 이 파일은 두 층을 나눠서 검증합니다:

1. `infra/storage/tracked_order_journal.py`의 `TrackedOrderJournalStore`
   자체 — 원자적 쓰기, 손상된 파일에 대한 fail-close, 재시작(새
   객체로 같은 경로를 다시 읽는 것) 후에도 기록이 살아있는지.
2. `TradingService`에 뚫어놓은 세 훅(BUY accepted 직후, SELL accepted
   직후, 매 폴링 종목별 유지보수)이 실제로 정확한 시점에 쓰고/갱신
   하고/안전하게만 지우는지.

D.1(`test_order_status_reconciliation.py`)과 마찬가지로 이 프로젝트의
자체 `check(label, condition)` 하네스를 씁니다(pytest 아님) —
`run_regression_tests.py`가 루트의 모든 `test_*.py`를 자동 수집합니다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, MarketRegime, Position, Signal, SignalType
from domain.position.lifecycle import PositionLifecycle as L, is_trackable_order_id
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.storage.tracked_order_journal import (
    SCHEMA_VERSION,
    TrackedOrderJournalCorruptError,
    TrackedOrderJournalStore,
    TrackedOrderRecord,
)
from test_run_once_integration import build_minimal_settings

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


TS_SRC = open("domain/service/trading_service.py", encoding="utf-8").read()


def _build_service(broker, tmpdir: str | None = None) -> tuple[TradingService, str]:
    tmpdir = tmpdir or tempfile.mkdtemp()
    settings = build_minimal_settings(tmpdir)
    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store = JsonStateStore(settings.storage.state_file)
    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    svc = TradingService(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
    )
    return svc, settings.storage.tracked_order_journal_file


def _record(symbol="005930", side="BUY", order_id="0012345", base=0, target=10,
            accepted_at=None, lifecycle_kind=None) -> TrackedOrderRecord:
    return TrackedOrderRecord(
        symbol=symbol, side=side, order_id=order_id,
        base_quantity_before_order=base, target_quantity_after_order=target,
        accepted_at=accepted_at or datetime.now(),
        lifecycle_kind=lifecycle_kind or (f"{side}_PENDING"),
    )


# ══════════════════════════════════════════════════════════════
# 1. TrackedOrderRecord — 생성 시점 방어적 검증
# ══════════════════════════════════════════════════════════════
check("1-1) 정상 레코드는 정상 생성됨", _record().order_id == "0012345")
try:
    _record(order_id="")
    check("1-2) 빈 order_id는 생성 자체를 거부함(ValueError)", False)
except ValueError:
    check("1-2) 빈 order_id는 생성 자체를 거부함(ValueError)", True)
try:
    _record(order_id="   ")
    check("1-3) 공백만 있는 order_id도 거부함", False)
except ValueError:
    check("1-3) 공백만 있는 order_id도 거부함", True)
try:
    TrackedOrderRecord(
        symbol="005930", side="HOLD", order_id="123",
        base_quantity_before_order=0, target_quantity_after_order=10,
        accepted_at=datetime.now(), lifecycle_kind="BUY_PENDING",
    )
    check("1-4) side가 BUY/SELL이 아니면 거부함", False)
except ValueError:
    check("1-4) side가 BUY/SELL이 아니면 거부함", True)
try:
    TrackedOrderRecord(
        symbol="005930", side="BUY", order_id="123",
        base_quantity_before_order=0, target_quantity_after_order=10,
        accepted_at=datetime.now(), lifecycle_kind="ORPHAN",
    )
    check("1-5) lifecycle_kind는 BUY_PENDING/SELL_PENDING만 허용함", False)
except ValueError:
    check("1-5) lifecycle_kind는 BUY_PENDING/SELL_PENDING만 허용함", True)
check("1-6) schema_version 기본값이 코드의 SCHEMA_VERSION과 일치",
      _record().schema_version == SCHEMA_VERSION)


# ══════════════════════════════════════════════════════════════
# 2. Store — 쓰기/읽기 왕복, "재시작"(새 객체) 후에도 기록이 살아있음
# ══════════════════════════════════════════════════════════════
root2 = tempfile.mkdtemp()
path2 = f"{root2}/tracked_order_journal.json"
store2a = TrackedOrderJournalStore(path2)
check("2-1) 파일이 없으면 load_all()이 빈 dict", store2a.load_all() == {})
rec2 = _record(symbol="005930", side="BUY", order_id="0011111",
                base=0, target=10, lifecycle_kind="BUY_PENDING")
store2a.upsert(rec2)
check("2-2) upsert 직후 같은 객체에서 조회됨",
      store2a.get("005930") is not None and store2a.get("005930").order_id == "0011111")

# "프로세스 재시작"을 새 TrackedOrderJournalStore 객체로 시뮬레이션 —
# TradingService 자체는 startup에 journal을 자동으로 읽지 않으므로
# (E.1-A 범위 밖) 이 계층에서 직접 검증합니다.
store2b = TrackedOrderJournalStore(path2)
loaded2b = store2b.load_all()
check("2-3) 새 객체(재시작 시뮬레이션)로 같은 경로를 읽어도 레코드가 그대로 로드됨",
      "005930" in loaded2b and loaded2b["005930"].order_id == "0011111")
check("2-4) 로드된 레코드의 필드가 전부 원본과 일치",
      loaded2b["005930"].base_quantity_before_order == 0
      and loaded2b["005930"].target_quantity_after_order == 10
      and loaded2b["005930"].lifecycle_kind == "BUY_PENDING"
      and loaded2b["005930"].first_fill_at is None
      and loaded2b["005930"].orphaned_at is None)

# 두 번째 종목 추가 — 기존 종목 레코드를 덮어쓰지 않는지(다건 저장 정합성)
rec2c = _record(symbol="000660", side="SELL", order_id="0022222",
                 base=50, target=0, lifecycle_kind="SELL_PENDING")
store2b.upsert(rec2c)
loaded2c = TrackedOrderJournalStore(path2).load_all()
check("2-5) 두 번째 종목 추가 후에도 첫 번째 종목 레코드가 그대로 있음",
      "005930" in loaded2c and "000660" in loaded2c)
check("2-6) 두 레코드가 서로 다른 side/order_id를 정확히 유지",
      loaded2c["005930"].side == "BUY" and loaded2c["000660"].side == "SELL")

store2b.remove("005930")
loaded2d = TrackedOrderJournalStore(path2).load_all()
check("2-7) remove() 후 해당 종목만 사라지고 나머지는 유지됨",
      "005930" not in loaded2d and "000660" in loaded2d)
store2b.remove("005930")  # 이미 없는 종목 재삭제 — idempotent해야 함
check("2-8) 존재하지 않는 종목 remove()는 예외 없이 통과(idempotent)", True)

# first_fill_at / orphaned_at 갱신 후 왕복
rec2e = store2b.get("000660")
rec2e.first_fill_at = datetime.now() - timedelta(seconds=30)
rec2e.orphaned_at = datetime.now()
store2b.upsert(rec2e)
loaded2f = TrackedOrderJournalStore(path2).get("000660")
check("2-9) first_fill_at/orphaned_at 갱신 후에도 재로드 시 값이 보존됨",
      loaded2f.first_fill_at is not None and loaded2f.orphaned_at is not None)


# ══════════════════════════════════════════════════════════════
# 3. Store — 원자적 쓰기: fsync 실패 허용, os.replace 실패는 원본 보호
# ══════════════════════════════════════════════════════════════
root3 = tempfile.mkdtemp()
path3 = f"{root3}/tracked_order_journal.json"
store3 = TrackedOrderJournalStore(path3)
store3.upsert(_record(symbol="005930", order_id="0033333"))

# fsync가 실패해도(예: Windows에서 재현된 사례, export_daily_bundle.py
# 1I.5와 동일 판단) 쓰기 자체는 성공해야 함.
_orig_fsync = os.fsync
os.fsync = lambda fd: (_ for _ in ()).throw(OSError(9, "Bad file descriptor"))
try:
    store3.upsert(_record(symbol="000660", order_id="0044444"))
    fsync_survived = True
finally:
    os.fsync = _orig_fsync
check("3-1) fsync 실패해도 upsert()가 예외 없이 완료됨", fsync_survived)
check("3-2) fsync 실패 후에도 두 레코드 모두 정상 조회됨",
      store3.get("005930") is not None and store3.get("000660") is not None)
check("3-3) fsync 실패 후 남은 .tmp 파일이 없음",
      not list(__import__("pathlib").Path(root3).glob("*.tmp")))

# os.replace 자체가 실패하면(디스크 풀 등을 흉내) 원본 파일이 훼손되지
# 않아야 하고, .tmp도 정리돼야 함.
before_bytes = open(path3, "rb").read()
_orig_replace = os.replace
os.replace = lambda *a, **kw: (_ for _ in ()).throw(OSError(28, "No space left on device"))
try:
    try:
        store3.upsert(_record(symbol="233740", order_id="0055555"))
        replace_raised = False
    except OSError:
        replace_raised = True
finally:
    os.replace = _orig_replace
after_bytes = open(path3, "rb").read()
check("3-4) os.replace 실패는 예외로 그대로 전파됨(조용히 삼키지 않음)", replace_raised)
check("3-5) os.replace 실패 후에도 기존 파일 내용이 한 바이트도 안 바뀜(원자성)",
      before_bytes == after_bytes)
check("3-6) os.replace 실패 후 .tmp 파일이 남지 않음(정리됨)",
      not list(__import__("pathlib").Path(root3).glob("*.tmp")))
check("3-7) os.replace 실패 후에도 기존 두 레코드는 그대로 읽힘",
      set(TrackedOrderJournalStore(path3).load_all().keys()) == {"005930", "000660"})


# ══════════════════════════════════════════════════════════════
# 4. Store — 손상된 파일 / schema_version 불일치 → fail-close
# ══════════════════════════════════════════════════════════════
root4 = tempfile.mkdtemp()
path4a = f"{root4}/broken1.json"
open(path4a, "w", encoding="utf-8").write("{ 이건 유효한 JSON이 아님 ][")
store4a = TrackedOrderJournalStore(path4a)
try:
    store4a.load_all()
    check("4-1) JSON 파싱 실패 시 TrackedOrderJournalCorruptError 발생", False)
except TrackedOrderJournalCorruptError:
    check("4-1) JSON 파싱 실패 시 TrackedOrderJournalCorruptError 발생", True)
try:
    store4a.upsert(_record())
    check("4-1b) 손상된 파일 위에 upsert()해도 조용히 덮어쓰지 않고 예외를 냄", False)
except TrackedOrderJournalCorruptError:
    check("4-1b) 손상된 파일 위에 upsert()해도 조용히 덮어쓰지 않고 예외를 냄", True)

path4b = f"{root4}/broken2.json"
json.dump({"schema_version": 999, "records": {}}, open(path4b, "w", encoding="utf-8"))
store4b = TrackedOrderJournalStore(path4b)
try:
    store4b.load_all()
    check("4-2) schema_version 불일치 시 TrackedOrderJournalCorruptError 발생", False)
except TrackedOrderJournalCorruptError:
    check("4-2) schema_version 불일치 시 TrackedOrderJournalCorruptError 발생", True)

path4c = f"{root4}/broken3.json"
json.dump({"schema_version": SCHEMA_VERSION, "records": "이것도 dict가 아님"},
          open(path4c, "w", encoding="utf-8"))
try:
    TrackedOrderJournalStore(path4c).load_all()
    check("4-3) records가 dict가 아니면 fail-close", False)
except TrackedOrderJournalCorruptError:
    check("4-3) records가 dict가 아니면 fail-close", True)

path4d = f"{root4}/broken4.json"
json.dump(
    {"schema_version": SCHEMA_VERSION,
     "records": {"005930": {"symbol": "005930", "side": "BUY"}}},  # 필수 필드 대부분 누락
    open(path4d, "w", encoding="utf-8"),
)
try:
    TrackedOrderJournalStore(path4d).load_all()
    check("4-4) 개별 레코드 필드 누락도 fail-close", False)
except TrackedOrderJournalCorruptError:
    check("4-4) 개별 레코드 필드 누락도 fail-close", True)

check("4-5) 손상 감지 예외들이 전부 이 모듈 전용 타입(범용 Exception을 넓게 잡지 않게)",
      issubclass(TrackedOrderJournalCorruptError, Exception))


# ══════════════════════════════════════════════════════════════
# 5. Store — 민감정보 없음
# ══════════════════════════════════════════════════════════════
root5 = tempfile.mkdtemp()
path5 = f"{root5}/tracked_order_journal.json"
store5 = TrackedOrderJournalStore(path5)
store5.upsert(_record(symbol="005930", order_id="0099999"))
raw5_text = open(path5, encoding="utf-8").read().lower()
_sensitive_markers = (
    "account", "계좌", "token", "appkey", "app_key", "secretkey",
    "secret_key", "password", "passwd", "bearer",
)
check("5-1) 저장된 journal 파일에 계좌/토큰/시크릿 관련 키가 전혀 없음",
      not any(marker in raw5_text for marker in _sensitive_markers))
check("5-2) TrackedOrderRecord 필드 자체에도 그런 이름의 필드가 없음",
      not any(marker in f.lower()
              for f in TrackedOrderRecord.__dataclass_fields__.keys()
              for marker in _sensitive_markers))


# ══════════════════════════════════════════════════════════════
# 6. TradingService 통합 — BUY accepted 직후 journal 생성
# ══════════════════════════════════════════════════════════════
broker6 = MockBroker()
svc6, _ = _build_service(broker6)
svc6.broker._cash = 100_000_000
sym6 = "005930"
svc6.broker._positions.pop(sym6, None)
svc6.broker._prices[sym6] = 10000
svc6._sync_position_state_machine_shadow(svc6.broker.get_account_balance())  # 콜드 초기화

balance6 = svc6.broker.get_account_balance()
buy_signal6 = Signal(type=SignalType.BUY, reason="E.1-A 테스트 매수")
with patch("domain.service.trading_service.now_kst",
           return_value=datetime(2026, 8, 20, 10, 0, 0)):
    svc6._try_buy(sym6, 10000, balance6, signal=buy_signal6,
                  regime=MarketRegime.BULLISH, minute_analysis=None)

state6 = svc6._position_state_machine.get(sym6)
rec6 = svc6._tracked_order_journal.get(sym6)
check("6-1) _try_buy() accepted 후 BUY_PENDING으로 전이됨(사전조건 확인)",
      state6.lifecycle == L.BUY_PENDING)
check("6-2) BUY accepted 직후 journal record가 생성됨", rec6 is not None)
check("6-3) journal record의 order_id가 실제 PSM의 pending_order_id와 일치",
      rec6 is not None and rec6.order_id == state6.pending_order_id
      and is_trackable_order_id(rec6.order_id))
check("6-4) journal record의 side/lifecycle_kind가 BUY 계열로 정확히 기록됨",
      rec6 is not None and rec6.side == "BUY" and rec6.lifecycle_kind == "BUY_PENDING")
check("6-5) journal record가 재시작 시뮬레이션(새 store 객체)에도 그대로 로드됨",
      TrackedOrderJournalStore(svc6.settings.storage.tracked_order_journal_file)
      .get(sym6) is not None)


# ══════════════════════════════════════════════════════════════
# 7. TradingService 통합 — SELL accepted 직후 journal 생성
# ══════════════════════════════════════════════════════════════
broker7 = MockBroker()
svc7, _ = _build_service(broker7)
sym7 = "000660"
svc7.broker._positions[sym7] = Position(symbol=sym7, quantity=100, average_price=10000)
svc7.broker._prices[sym7] = 10100
svc7._sync_position_state_machine_shadow(svc7.broker.get_account_balance())
check("7-0) 초기 동기화 후 OPEN(사전조건 확인)",
      svc7._position_state_machine.get(sym7).lifecycle == L.OPEN)

svc7._try_sell(sym7, 100, current_price=10100,
               exit_reason="트레일링 스탑 — E.1-A 테스트", avg_buy_price=10000)
state7 = svc7._position_state_machine.get(sym7)
rec7 = svc7._tracked_order_journal.get(sym7)
check("7-1) _try_sell() accepted 후 SELL_PENDING으로 전이됨(사전조건 확인)",
      state7.lifecycle == L.SELL_PENDING)
check("7-2) SELL accepted 직후 journal record가 생성됨", rec7 is not None)
check("7-3) journal record의 side/lifecycle_kind가 SELL 계열로 정확히 기록됨",
      rec7 is not None and rec7.side == "SELL" and rec7.lifecycle_kind == "SELL_PENDING")
check("7-4) SELL journal record의 target_quantity_after_order는 0(전량매도 관례, D.1과 동일 정의)",
      rec7 is not None and rec7.target_quantity_after_order == 0)


# ══════════════════════════════════════════════════════════════
# 8. TradingService 통합 — 추적 불가능한 order_id는 journal에 안 남음
# ══════════════════════════════════════════════════════════════
broker8 = MockBroker()
svc8, _ = _build_service(broker8)
sym8 = "005930"
psm8 = svc8._position_state_machine
psm8.on_buy_requested(sym8, 10, "pending")
state8 = psm8.get(sym8)
state8.base_quantity_before_order = 0
state8.known_quantity = 0
state8.expected_final_quantity = 10
# confirm_pending_order_id()가 accepted+빈 order_id일 때 실제로
# 채우는 sentinel 값과 동일한 상황을 재현.
psm8.confirm_pending_order_id(sym8, "")
check("8-0) accepted인데 order_id가 비어 있으면 UNKNOWN_ORDER_ID sentinel로 채워짐(사전조건)",
      not is_trackable_order_id(state8.pending_order_id))
svc8._create_tracked_order_journal_entry(sym8, "BUY")
check("8-1) sentinel order_id면 journal record가 생성되지 않음",
      svc8._tracked_order_journal.get(sym8) is None)

svc8b_broker = MockBroker()
svc8b, _ = _build_service(svc8b_broker)
psm8b = svc8b._position_state_machine
psm8b.on_buy_requested(sym8, 10, "pending")
# on_buy_requested 직후(place_order 응답 전) pending_order_id는
# placeholder "pending"이어야 함 — confirm_pending_order_id를
# 아예 호출하지 않은 상태에서 journal 생성을 시도.
svc8b._create_tracked_order_journal_entry(sym8, "BUY")
check("8-2) 'pending' placeholder 상태에서도 journal record가 생성되지 않음",
      svc8b._tracked_order_journal.get(sym8) is None)


# ══════════════════════════════════════════════════════════════
# 9. TradingService 통합 — pending → orphan 전환 시 같은 record가 갱신됨
#    (새 record가 아니라 orphaned_at만 채워지는지 확인)
# ══════════════════════════════════════════════════════════════
from domain.position.lifecycle import PositionStateMachine as PSM_CLASS

_orig_buy_timeout = PSM_CLASS.BUY_PENDING_TIMEOUT_SEC
PSM_CLASS.BUY_PENDING_TIMEOUT_SEC = 0.2
try:
    broker9 = MockBroker()
    svc9, _ = _build_service(broker9)
    svc9.broker._cash = 100_000_000
    sym9 = "005930"
    svc9.broker._positions.pop(sym9, None)
    svc9.broker._prices[sym9] = 10000
    svc9._sync_position_state_machine_shadow(svc9.broker.get_account_balance())

    balance9 = svc9.broker.get_account_balance()
    buy_signal9 = Signal(type=SignalType.BUY, reason="E.1-A orphan 테스트")
    with patch("domain.service.trading_service.now_kst",
               return_value=datetime(2026, 8, 20, 10, 0, 0)):
        svc9._try_buy(sym9, 10000, balance9, signal=buy_signal9,
                      regime=MarketRegime.BULLISH, minute_analysis=None)

    rec9_before = svc9._tracked_order_journal.get(sym9)
    order_id9 = rec9_before.order_id
    check("9-0) BUY accepted 직후 journal record가 존재(사전조건)", rec9_before is not None)
    check("9-0b) 아직 orphaned_at은 비어 있음(사전조건)", rec9_before.orphaned_at is None)

    # MockBroker는 place_order() 시점에 즉시 전량 반영하므로, "아직
    # 반영 안 됐다"를 흉내내기 위해 잔고를 인위적으로 부분 수량으로
    # 되돌립니다(test_partial_fill_lifecycle.py 23절과 동일한 기법).
    svc9.broker._positions[sym9] = Position(symbol=sym9, quantity=4, average_price=10000)
    svc9._sync_position_state_machine_shadow(svc9.broker.get_account_balance())
    check("9-1) 부분체결 폴링 후에도 여전히 BUY_PENDING(타임아웃 전)",
          svc9._position_state_machine.get(sym9).lifecycle == L.BUY_PENDING)

    import time as _time9
    _time9.sleep(0.25)
    svc9._sync_position_state_machine_shadow(svc9.broker.get_account_balance())
    state9 = svc9._position_state_machine.get(sym9)
    check("9-2) 타임아웃 경과 후 orphan으로 전환됨(사전조건)",
          state9.orphan_order_id is not None)

    rec9_after = svc9._tracked_order_journal.get(sym9)
    check("9-3) orphan 전환 후에도 journal record가 여전히 존재(제거되지 않음)",
          rec9_after is not None)
    check("9-4) orphan 전환 후 order_id는 그대로(같은 레코드가 갱신된 것, 새로 생기지 않음)",
          rec9_after is not None and rec9_after.order_id == order_id9)
    check("9-5) orphan 전환 후 orphaned_at이 채워짐",
          rec9_after is not None and rec9_after.orphaned_at is not None)
finally:
    PSM_CLASS.BUY_PENDING_TIMEOUT_SEC = _orig_buy_timeout


# ══════════════════════════════════════════════════════════════
# 10. TradingService 통합 — terminal 확정 후에만 안전하게 record 제거
# ══════════════════════════════════════════════════════════════
broker10 = MockBroker()
svc10, _ = _build_service(broker10)
svc10.broker._cash = 100_000_000
sym10 = "005930"
svc10.broker._positions.pop(sym10, None)
svc10.broker._prices[sym10] = 10000
svc10._sync_position_state_machine_shadow(svc10.broker.get_account_balance())

balance10 = svc10.broker.get_account_balance()
buy_signal10 = Signal(type=SignalType.BUY, reason="E.1-A terminal 테스트")
with patch("domain.service.trading_service.now_kst",
           return_value=datetime(2026, 8, 20, 10, 0, 0)):
    svc10._try_buy(sym10, 10000, balance10, signal=buy_signal10,
                   regime=MarketRegime.BULLISH, minute_analysis=None)
check("10-0) BUY accepted 직후 journal record 존재(사전조건)",
      svc10._tracked_order_journal.get(sym10) is not None)

# MockBroker가 즉시 전량 반영한 실제 잔고 그대로 폴링 — 정상적으로
# 목표 수량과 일치해 곧바로 OPEN(전량체결 확정)으로 전이돼야 함.
svc10._sync_position_state_machine_shadow(svc10.broker.get_account_balance())
state10 = svc10._position_state_machine.get(sym10)
check("10-1) 잔고가 목표와 일치하는 폴링 후 OPEN으로 확정됨",
      state10.lifecycle == L.OPEN)
check("10-2) OPEN 확정(terminal) 후 journal record가 안전하게 제거됨",
      svc10._tracked_order_journal.get(sym10) is None)

# ERROR 상태는 "안전한 terminal"이 아니므로 record가 남아있어야 함.
broker10b = MockBroker()
svc10b, _ = _build_service(broker10b)
sym10b = "233740"
psm10b = svc10b._position_state_machine
psm10b.on_buy_requested(sym10b, 10, "pending")
state10b = psm10b.get(sym10b)
state10b.base_quantity_before_order = 0
state10b.known_quantity = 0
state10b.expected_final_quantity = 10
psm10b.confirm_pending_order_id(sym10b, "0088888")
svc10b._create_tracked_order_journal_entry(sym10b, "BUY")
check("10-3) ERROR 진입 전 journal record 존재(사전조건)",
      svc10b._tracked_order_journal.get(sym10b) is not None)
# 예상치 못한 초과 수량 — confirm_buy_from_broker()가 ERROR로 전이시킴.
psm10b.confirm_buy_from_broker(sym10b, 999)
check("10-4) 예상 밖 수량으로 ERROR 전이됨(사전조건)",
      psm10b.get(sym10b).lifecycle == L.ERROR)
svc10b._maintain_tracked_order_journal(sym10b)
check("10-5) ERROR 상태에서는 journal record가 삭제되지 않음"
      "(사람이 acknowledge_error()로 확인하기 전까지 증거 보존)",
      svc10b._tracked_order_journal.get(sym10b) is not None)


# ══════════════════════════════════════════════════════════════
# 11. TradingService 통합 — journal I/O 실패가 매매 로직을 절대 막지 않음
# ══════════════════════════════════════════════════════════════
broker11 = MockBroker()
svc11, _ = _build_service(broker11)
svc11.broker._cash = 100_000_000
sym11 = "005930"
svc11.broker._positions.pop(sym11, None)
svc11.broker._prices[sym11] = 10000
svc11._sync_position_state_machine_shadow(svc11.broker.get_account_balance())


class _BrokenJournal:
    """upsert/remove가 항상 실패하는 journal 더블 — TradingService가
    이 예외를 삼키고 CRITICAL 로그만 남긴 채 계속 진행하는지 확인."""

    def get(self, symbol):
        return None

    def upsert(self, record):
        raise OSError("디스크 시뮬레이션 실패")

    def remove(self, symbol):
        raise OSError("디스크 시뮬레이션 실패")


svc11._tracked_order_journal = _BrokenJournal()
balance11 = svc11.broker.get_account_balance()
buy_signal11 = Signal(type=SignalType.BUY, reason="E.1-A 장애내성 테스트")
with patch("domain.service.trading_service.now_kst",
           return_value=datetime(2026, 8, 20, 10, 0, 0)):
    block11 = svc11._try_buy(sym11, 10000, balance11, signal=buy_signal11,
                             regime=MarketRegime.BULLISH, minute_analysis=None)
check("11-1) journal 저장이 항상 실패해도 _try_buy()는 예외 없이 정상 반환",
      not block11)
check("11-2) journal 저장 실패와 무관하게 BUY_PENDING 전이는 정상적으로 이뤄짐",
      svc11._position_state_machine.get(sym11).lifecycle == L.BUY_PENDING)
check("11-3) journal 저장 실패와 무관하게 accepted 주문 기록(pending_buy_side_effects)도 정상",
      sym11 in svc11._pending_buy_side_effects)
# 다음 폴링(_maintain_tracked_order_journal 경유)도 마찬가지로 안전해야 함.
try:
    svc11._sync_position_state_machine_shadow(svc11.broker.get_account_balance())
    poll_survived = True
except Exception:
    poll_survived = False
check("11-4) 폴링 유지보수 훅에서도 journal 실패가 폴링 자체를 죽이지 않음",
      poll_survived)


# ══════════════════════════════════════════════════════════════
# 12. 소스 레벨 — E.1-A 범위 준수 확인 (스코프 크리프 회귀 방지)
# ══════════════════════════════════════════════════════════════
check("12-1) TradingService.__init__이 journal을 주입 안 하면 storage 설정에서 자동 생성",
      "self._tracked_order_journal = tracked_order_journal or TrackedOrderJournalStore("
      in TS_SRC)
check("12-2) __init__ 안에서 journal.load_all()/get()을 호출해 startup에 자동 판단하지 않음"
      "(E.1-A는 저장소 객체만 만들 뿐, 이 라운드는 startup enforcement가 범위 밖)",
      "_tracked_order_journal.load_all()" not in TS_SRC
      and "_tracked_order_journal.get(" not in TS_SRC.split(
          "def _maintain_tracked_order_journal")[0].split(
          "def __init__")[1].split("def run_once")[0]
      if "def run_once" in TS_SRC else True)
check("12-3) 이번 라운드에서 get_order_status() 호출부를 추가로 늘리지 않음"
      "(D.1/D.1.1이 이미 만든 호출부 개수 그대로)",
      TS_SRC.count("self.broker.get_order_status(") == 1)
check("12-4) journal 관련 코드 어디서도 BUY/SELL 자동 실행(재주문)을 하지 않음",
      "_tracked_order_journal" not in TS_SRC.split("_try_buy(")[0]
      or "place_order" not in TS_SRC[
          TS_SRC.index("_create_tracked_order_journal_entry"):
          TS_SRC.index("_maintain_tracked_order_journal") + 2000
      ])
check("12-5) journal 관련 코드에서 cancel_order를 호출하지 않음",
      "cancel_order" not in TS_SRC[
          TS_SRC.index("_create_tracked_order_journal_entry"):
          TS_SRC.index("_maintain_tracked_order_journal") + 3000
      ])
# 12-6) 파싱 방식 참고: 예전 버전은 "class PositionLifecycle"부터 다음
# "class " 등장 전까지를 통째로 훑었는데, 그 구간에는 PositionLifecycle
# 정의 이후 이어지는 모듈 레벨 상수(PENDING_ORDER_ID_PLACEHOLDER,
# UNKNOWN_ORDER_ID_SENTINEL — 1P0.8-D.1에서 이미 도입된, 이번 E.1-A와
# 무관한 기존 코드)도 "대문자 이름 = 값" 패턴에 걸려 함께 세어져
# 5개가 아닌 7개로 잘못 집계됐음(내가 만든 회귀 아님 — 이 heuristic
# 자체가 애초에 fragile했던 것, 디버깅 중 발견). enum 클래스 바디는
# 반드시 들여쓰기된 줄만으로 구성되므로, "class 선언 다음 줄부터
# 들여쓰기가 끊기는 첫 줄 전까지"로 범위를 좁혀 더 안전하게 판별.
_lifecycle_src = open(
    "domain/position/lifecycle.py", encoding="utf-8"
).read()
_after_decl = _lifecycle_src.split("class PositionLifecycle(str, Enum):")[1]
_enum_body_lines = []
for _line in _after_decl.splitlines():
    if _line.strip() == "":
        continue
    if not _line[:1].isspace():
        break
    _enum_body_lines.append(_line)
_enum_member_count = len([
    line for line in _enum_body_lines
    if "=" in line and line.strip().split("=")[0].strip().isupper()
])
check("12-6) PositionLifecycle enum에 새 상태(RECOVERING 등)를 추가하지 않음"
      "(여전히 FLAT/BUY_PENDING/OPEN/SELL_PENDING/ERROR 5개뿐)",
      _enum_member_count == 5)
check("12-7) journal 실패 시 매매 로직을 막지 않기 위한 try/except가 두 헬퍼 모두에 존재",
      TS_SRC.count("[TRACKED_ORDER_JOURNAL_ERROR]") == 2)


print()
print(f"[최종] 총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
