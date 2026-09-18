# -*- coding: utf-8 -*-
"""2026-09-18 (우선순위1 1차: 체결조회 증거 독립 저장) 회귀 테스트.

민우님 GPT 기반 review(2026-09-18, v2에 대한 5개 구현 조건)를 그대로
검증합니다:

1. 새 조회 메서드(`get_order_status_evidence()`)는 모든 브로커에서
   호환되고, 관측 실패가 기존 판정을 막지 않는다.
2. 원문 필드(ord_qty 등)와 실패 조회도 빠짐없이 기록된다.
3. 커버리지 식별에 환경·주문일·분모 출처가 정확히 반영된다(고유 주문
   기준, 다른 env/order_date의 같은 order_id를 합치지 않음).
4. append 재시도/종료 마커의 의미가 정확하다(부분 쓰기 격리, dedup,
   종료 시간 상한).
5. 계좌 라벨 누락이 프로그램 전체 기동을 막지 않는다(관측만 비활성).

이 파일은 손익 계산이나 리스크 게이트를 전혀 건드리지 않습니다 —
새로 추가된 관측 계층 자체의 안전성만 검증합니다.
"""
from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# test_run_once_integration.py / test_order_status_reconciliation.py는 이
# 프로젝트의 표준(비-pytest) 스타일대로 모듈 최상단에서 자체 검사를 실행하고
# `sys.exit(...)`로 끝납니다. 그 도우미 함수만 재사용하려고 import하면
# 이 파일이 시작하기도 전에 프로세스가 종료돼버리므로, import 하는 동안만
# sys.exit()를 무력화합니다(그 파일 자체의 검사 결과는 이미 run_regression_tests.py
# 가 별도로 실행/보고하므로 여기서 다시 확인할 필요가 없습니다).
_real_exit = sys.exit
sys.exit = lambda *_a, **_k: None
try:
    from test_run_once_integration import build_minimal_settings
    from test_order_status_reconciliation import (
        _build_service, _ScriptedOrderStatusBroker, _filled, _unknown, _OLD_PENDING,
    )
finally:
    sys.exit = _real_exit
from domain.models import BrokerOrder, BrokerOrderStatus, OrderSide, OrderStatusEvidence
from domain.position.lifecycle import PositionLifecycle as L
from infra.broker.base import Broker
from infra.broker.mock_broker import MockBroker
from infra.broker.kiwoom_order_status import (
    build_order_status_evidence, derive_broker_order_status, find_all_matching,
)
from infra.storage.order_status_observation_store import (
    COVERAGE_DISABLED, OrderStatusObservation, OrderStatusObservationRecorder,
    build_entry_evidence, compute_coverage, dedupe_by_write_id, resolve_env,
)
from infra.storage.run_baseline import resolve_env_from_baseline_row
from config.settings import BrokerConfig

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


# ══════════════════════════════════════════════════════════════
# 1. 브로커 호환성 + 관측 실패 격리
# ══════════════════════════════════════════════════════════════

_mock = MockBroker()
check("1-1) MockBroker가 오버라이드 없이 get_order_status_evidence()를 지원함",
      hasattr(_mock, "get_order_status_evidence"))

_ev = _mock.get_order_status_evidence("999999", "005930")
check("1-2) 기본 구현이 OrderStatusEvidence를 반환함",
      isinstance(_ev, OrderStatusEvidence))
check("1-3) 기본 구현의 broker_order는 get_order_status()와 동일한 값",
      _ev.broker_order == _mock.get_order_status("999999", "005930"))
check("1-4) 기본 구현은 매칭 원본 행을 만들지 않음(빈 리스트 — get_order_status()만으로는 알 수 없음)",
      _ev.matched_cntr_entries == [] and _ev.matched_oso_entries == [])


class _RaisingBroker(Broker):
    """get_order_status()가 항상 예외를 던지는 브로커 — 기본 evidence
    구현이 그 예외를 그대로 전파하는지 확인."""

    def authenticate(self) -> None: ...
    def get_market_price(self, symbol): ...
    def get_account_balance(self): ...
    def place_order(self, order): ...
    def get_daily_prices(self, symbol, days): ...
    def get_weekly_prices(self, symbol, weeks): ...
    def get_minute_bars(self, symbol, tick_scope=3, count=40): ...

    def get_order_status(self, order_id, symbol):
        raise RuntimeError("네트워크 오류")


_raised = False
try:
    _RaisingBroker().get_order_status_evidence("1", "005930")
except RuntimeError:
    _raised = True
check("1-5) get_order_status()의 API 오류는 get_order_status_evidence()에서도 동일하게 전파됨",
      _raised)


# KiwoomBroker의 판정 로직 자체(derive_broker_order_status)는 무변경 —
# build_order_status_evidence()가 반환하는 broker_order가 기존 함수의
# 결과와 완전히 동일한지 순수 함수 수준에서 확인(네트워크 불필요).
_cntr = [{"ord_no": "0000123", "ord_stt": "체결", "ord_qty": "10",
          "cntr_qty": "10", "oso_qty": "0", "cntr_pric": "10000",
          "io_tp_nm": "+매수", "trde_tp": "시장가"}]
_evidence = build_order_status_evidence("123", "005930", [], _cntr)
_direct = derive_broker_order_status("123", "005930", [], _cntr)
check("1-6) build_order_status_evidence()의 broker_order == derive_broker_order_status() 직접 호출 결과",
      _evidence.broker_order == _direct)
check("1-7) 정상 입력에서는 evidence_error가 없음",
      _evidence.evidence_error is None)

# 관측 가공 자체가 실패해도(예: 리스트 원소가 dict가 아님) broker_order는
# 안전한 UNKNOWN 폴백으로 채워지고 예외가 전파되지 않음 — 지적 1번의
# "관측 실패가 기존 판정을 막지 않아야 한다"는 요구를 가장 극단적인
# 입력(완전히 잘못된 원소)으로 확인.
_broken_evidence = build_order_status_evidence("123", "005930", [], [None, "not-a-dict"])
check("1-8) 완전히 잘못된 원소가 섞여도 예외를 던지지 않고 evidence_error로 감쌈",
      _broken_evidence.evidence_error is not None
      and _broken_evidence.broker_order.status == BrokerOrderStatus.UNKNOWN)


# ══════════════════════════════════════════════════════════════
# 2. 원문 필드 보존 + 실패 조회 기록
# ══════════════════════════════════════════════════════════════

_entry = build_entry_evidence({
    "ord_no": "0000123", "ord_stt": "체결", "ord_qty": "10", "cntr_qty": "10",
    "oso_qty": "0", "cntr_pric": "10000", "io_tp_nm": "+매수", "trde_tp": "시장가",
    "체결시각": "091530",  # 실측 전 미확정 필드 — raw에 통째로 보존되는지 확인
})
check("2-1) build_entry_evidence()에 ord_qty_raw가 포함됨",
      _entry.get("ord_qty_raw") == "10")
check("2-2) 구조화되지 않은 필드(체결시각 등)도 raw에 원문 그대로 보존됨",
      _entry.get("raw", {}).get("체결시각") == "091530")

# UNKNOWN으로 판정되는 cntr 매칭(부분체결 유사 signature)에서도
# cntr_pric 원문이 matched_cntr_entries에는 남아있어야 함(filled_price는
# None이어도 됨) — "UNKNOWN인데 가격 문자열이 있었다"는 지적 대응.
_partial_like = [{"ord_no": "0000456", "ord_stt": "체결", "ord_qty": "10",
                   "cntr_qty": "3", "oso_qty": "7", "cntr_pric": "9990"}]
_ev2 = build_order_status_evidence("456", "005930", [], _partial_like)
check("2-3) UNKNOWN 판정이어도 broker_order.filled_price는 None(기존 로직 무변경)",
      _ev2.broker_order.status == BrokerOrderStatus.UNKNOWN
      and _ev2.broker_order.filled_price is None)
check("2-4) 그러나 matched_cntr_entries에는 cntr_pric 원문이 그대로 남아있음",
      len(_ev2.matched_cntr_entries) == 1
      and _ev2.matched_cntr_entries[0].get("cntr_pric") == "9990")

# 같은 order_id로 여러 cntr 행이 존재하는 경우 — find_all_matching()이
# 전부 반환하는지(기존 _find_matching은 첫 행만 반환하므로 판정 자체는
# 그대로 첫 행 기준).
_dup_cntr = [
    {"ord_no": "0000789", "ord_stt": "체결", "ord_qty": "5", "cntr_qty": "5", "oso_qty": "0", "cntr_pric": "10000"},
    {"ord_no": "0000789", "ord_stt": "체결", "ord_qty": "5", "cntr_qty": "5", "oso_qty": "0", "cntr_pric": "10010"},
]
_ev3 = build_order_status_evidence("789", "005930", [], _dup_cntr)
check("2-5) 같은 order_id로 여러 cntr 행이 있으면 matched_cntr_entries가 전부 보존됨",
      len(_ev3.matched_cntr_entries) == 2)
check("2-6) 판정(broker_order)은 여전히 첫 행 기준(기존 로직 무변경)",
      _ev3.broker_order.filled_price == 10000)


# 실패한 조회도 기록되는지 — TradingService 통합 테스트.
def _make_enabled_service():
    tmpdir = tempfile.mkdtemp()
    settings = build_minimal_settings(tmpdir)
    settings = dataclasses.replace(
        settings, broker=dataclasses.replace(settings.broker, account_scope_id="acct-test"),
    )
    broker = _ScriptedOrderStatusBroker()
    return _build_service_with_settings(broker, settings), broker


def _build_service_with_settings(broker, settings):
    # test_order_status_reconciliation._build_service()는 settings를
    # 자체 생성하므로, account_scope_id를 주입하려면 동일한 구성을
    # 이 파일에서 한 번 더 조립합니다(그 파일의 구성 로직을 그대로 따름).
    from domain.market_regime.classifier import MarketRegimeClassifier
    from domain.risk.risk_manager import RiskManager
    from domain.service.trading_service import TradingService
    from domain.strategy.strategy_router import StrategyRouter
    from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
    from infra.storage.state_store import JsonStateStore

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


service, broker = _make_enabled_service()
check("2-7) account_scope_id가 설정되면 기록기가 생성됨",
      service._order_status_observation_recorder is not None)

psm = service._position_state_machine
psm.get("005930").lifecycle = L.BUY_PENDING
psm.get("005930").pending_order_id = "555555"
psm.get("005930").pending_since = _OLD_PENDING
psm.get("005930").base_quantity_before_order = 0
psm.get("005930").expected_final_quantity = 10

broker.script(RuntimeError("429 Too Many Requests"))
service._reconcile_tracked_order_status("005930", broker_qty=0)
time.sleep(0.2)  # 백그라운드 기록 스레드가 append할 시간을 줌
recorded_lines = Path(service.settings.storage.order_status_observation_log_file).read_text(
    encoding="utf-8"
).splitlines() if Path(service.settings.storage.order_status_observation_log_file).exists() else []
check("2-8) 실패한 조회도 최소 1건 기록됨(query_id 발급 후 실패해도 기록)",
      len(recorded_lines) >= 1)
check("2-9) 기록된 행에 outcome=api_error가 남음",
      any('"outcome": "api_error"' in line for line in recorded_lines))
service._order_status_observation_recorder.shutdown()


# ══════════════════════════════════════════════════════════════
# 3. 커버리지 식별 — 환경/주문일/분모 출처
# ══════════════════════════════════════════════════════════════

check("3-1) resolve_env(local_mock)", resolve_env(use_mock=True, is_paper_trading=False) == "local_mock")
check("3-2) resolve_env(kiwoom_mock)", resolve_env(use_mock=False, is_paper_trading=True) == "kiwoom_mock")
check("3-3) resolve_env(kiwoom_real)", resolve_env(use_mock=False, is_paper_trading=False) == "kiwoom_real")
check("3-4) resolve_env_from_baseline_row가 CSV 문자열('True'/'False')도 동일하게 판정",
      resolve_env_from_baseline_row({"is_mock": "True", "is_paper_trading": "False"}) == "local_mock"
      and resolve_env_from_baseline_row({"is_mock": "False", "is_paper_trading": "True"}) == "kiwoom_mock"
      and resolve_env_from_baseline_row({"is_mock": "False", "is_paper_trading": "False"}) == "kiwoom_real")

_disabled = compute_coverage(
    account_scope_id="", accepted_orders=set(), observations=[], order_date_resolver=lambda o: None,
)
check("3-5) account_scope_id가 비어있으면 커버리지는 0%가 아니라 '계측 비활성'",
      _disabled["status"] == COVERAGE_DISABLED)


def _obs(order_id, env="kiwoom_real", status="FILLED", price=10000, account="acct-1") -> OrderStatusObservation:
    return OrderStatusObservation(
        query_id=f"q-{order_id}-{time.time_ns()}", started_at="2026-09-18T09:00:00",
        finished_at="2026-09-18T09:00:01", account_scope_id=account, env=env,
        symbol="005930", requested_order_id=order_id, side_context="BUY_PENDING",
        query_kind="BUY_PENDING", pending_age_sec_at_query=31.0, outcome="success",
        psm_broker_status=status, filled_price_parsed=price, write_id=f"w-{order_id}",
    )


_accepted = {("acct-1", "kiwoom_real", "2026-09-18", "1"), ("acct-1", "kiwoom_real", "2026-09-18", "2")}
_repeated_obs = [_obs("1"), _obs("1"), _obs("1")]  # 같은 주문 3번 조회
_cov = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted, observations=_repeated_obs,
    order_date_resolver=lambda o: "2026-09-18",
)
check("3-6) 같은 주문을 3번 조회해도 observed_unique_orders는 1",
      _cov["observed_unique_orders"] == 1)
check("3-7) 주문 관측률이 100%를 넘지 않음(2건 중 1건 관측)",
      _cov["order_observation_rate"] == 0.5)

# 다른 env의 같은 order_id는 절대 합쳐지지 않음.
_accepted_mixed_env = {("acct-1", "kiwoom_real", "2026-09-18", "1"),
                        ("acct-1", "local_mock", "2026-09-18", "1")}
_obs_wrong_env = _obs("1", env="kiwoom_mock")  # accepted에 없는 env
_cov2 = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted_mixed_env, observations=[_obs_wrong_env],
    order_date_resolver=lambda o: "2026-09-18",
)
check("3-8) accepted_orders에 없는 (env, order_id) 조합의 관측은 고아 관측으로 분리됨(합쳐지지 않음)",
      _cov2["orphan_observation_count"] == 1 and _cov2["observed_unique_orders"] == 0)

_cov3 = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted, observations=[_obs("1")],
    order_date_resolver=lambda o: None,  # 주문일 미확인
)
check("3-9) order_date를 확정할 수 없으면 '미확인'으로 분리되고 관측에 포함되지 않음",
      _cov3["unresolved_order_date_count"] == 1 and _cov3["observed_unique_orders"] == 0)


# ══════════════════════════════════════════════════════════════
# 4. append/재시도/종료 마커 의미
# ══════════════════════════════════════════════════════════════

_tmpdir4 = tempfile.mkdtemp()
_log_path = str(Path(_tmpdir4) / "obs.jsonl")
_recorder = OrderStatusObservationRecorder(_log_path, maxsize=2, shutdown_drain_timeout_sec=1.0)
_recorder.start()

_dummy = OrderStatusObservation(
    query_id="q1", started_at="2026-09-18T09:00:00", finished_at=None,
    account_scope_id="acct-1", env="kiwoom_real", symbol="005930",
    requested_order_id="1", side_context="BUY_PENDING", query_kind="BUY_PENDING",
    pending_age_sec_at_query=31.0, outcome="success", write_id="w1",
)
_recorder.shutdown()  # 스레드를 먼저 세워둔 채로 종료해 큐 포화를 인위적으로 재현
for i in range(5):
    _recorder.record(dataclasses.replace(_dummy, write_id=f"w{i}", query_id=f"q{i}"))
check("4-1) 기록기 종료 후에도 record()가 예외 없이 즉시 반환됨(큐 처리 중단 상태에서도 블로킹 없음)",
      True)  # 위 for 루프가 예외 없이 끝났다면 통과

_result = _recorder.shutdown()
check("4-2) shutdown()은 clean_shutdown/queue_drained/dropped_count를 각각 반환",
      set(["clean_shutdown", "queue_drained", "dropped_count"]).issubset(_result.keys()))


class _HangingRecorder(OrderStatusObservationRecorder):
    def _write_one(self, observation) -> None:
        time.sleep(5.0)  # 디스크가 멈춘 상황을 흉내냄


_hang_path = str(Path(tempfile.mkdtemp()) / "hang.jsonl")
_hanging = _HangingRecorder(_hang_path, shutdown_drain_timeout_sec=0.3)
_hanging.start()
_hanging.record(dataclasses.replace(_dummy, write_id="hang1"))
time.sleep(0.05)  # 기록 스레드가 그 항목을 집어 sleep에 들어갈 시간을 줌
_t0 = time.time()
_hang_result = _hanging.shutdown()
_elapsed = time.time() - _t0
check("4-3) 기록 스레드가 멈춰 있어도 shutdown()이 제한시간(약 0.3초) 안에 반환됨",
      _elapsed < 2.0)
check("4-4) 이 경우 clean_shutdown=False로 정직하게 표시됨(디스크 완료를 보장한다고 주장하지 않음)",
      _hang_result["clean_shutdown"] is False)

# 마지막 줄이 불완전한 파일을 만든 뒤 새 recorder가 격리하는지 확인.
_tmpdir5 = tempfile.mkdtemp()
_corrupt_src = Path(_tmpdir5) / "obs.jsonl"
_corrupt_src.write_text(
    '{"query_id": "ok1", "outcome": "success"}\n'
    '{"query_id": "broken", "outcome": "succ',  # 마지막 줄이 잘림(개행 없음)
    encoding="utf-8",
)
_recorder2 = OrderStatusObservationRecorder(str(_corrupt_src))
_recorder2._quarantine_incomplete_tail()
_remaining = _corrupt_src.read_text(encoding="utf-8").splitlines()
_corrupt_sidecar = _corrupt_src.with_suffix(_corrupt_src.suffix + ".corrupt")
check("4-5) 불완전한 마지막 줄만 격리되고 정상 줄은 유지됨",
      len(_remaining) == 1 and '"ok1"' in _remaining[0])
check("4-6) 격리된 줄은 .corrupt 사이드카 파일에 보존됨(유실 아님)",
      _corrupt_sidecar.exists() and "broken" in _corrupt_sidecar.read_text(encoding="utf-8"))

# dedupe_by_write_id: 같은 id+같은 내용 → 1건, 같은 id+다른 내용 → 충돌.
_same = [_dummy, dataclasses.replace(_dummy, finished_at="2026-09-18T09:00:02")]  # finished_at만 다름
_deduped, _conflicts = dedupe_by_write_id(_same)
check("4-7) 같은 write_id+같은 내용(시각만 다름)은 한 번만 집계됨",
      len(_deduped) == 1 and _conflicts == [])

_different = [_dummy, dataclasses.replace(_dummy, outcome="api_error")]
_deduped2, _conflicts2 = dedupe_by_write_id(_different)
check("4-8) 같은 write_id+다른 내용은 조용히 하나를 고르지 않고 충돌로 표시됨",
      len(_deduped2) == 0 and _conflicts2 == ["w1"])


# ══════════════════════════════════════════════════════════════
# 5. 계좌 라벨 누락 시 관측만 비활성화(전체 기동 차단 아님)
# ══════════════════════════════════════════════════════════════

_bc_empty = BrokerConfig(provider="kiwoom", use_mock=True, base_url="", app_key="",
                          secret_key="", account_number="", is_paper_trading=True)
check("5-1) account_scope_id 기본값은 빈 문자열이고 observation_enabled=False",
      _bc_empty.account_scope_id == "" and _bc_empty.observation_enabled is False)

_bc_blank = dataclasses.replace(_bc_empty, account_scope_id="   ")
check("5-2) 공백만 있는 라벨도 미설정으로 취급됨",
      _bc_blank.observation_enabled is False)

_bc_set = dataclasses.replace(_bc_empty, account_scope_id="acct-a")
check("5-3) 라벨이 설정되면 observation_enabled=True",
      _bc_set.observation_enabled is True)

# 라벨이 없어도 TradingService 생성 자체는 정상 — build_minimal_settings()가
# account_scope_id를 지정하지 않으므로 기본값(빈 문자열)이 그대로 쓰임.
_tmpdir6 = tempfile.mkdtemp()
_settings_no_label = build_minimal_settings(_tmpdir6)
_service_no_label = _build_service(_ScriptedOrderStatusBroker())
check("5-4) account_scope_id 미설정으로도 TradingService 생성이 성공함(기동 차단 없음)",
      _service_no_label is not None)
check("5-5) 이 경우 기록기가 None(관측 비활성) — 매매 로직과 무관",
      _service_no_label._order_status_observation_recorder is None)

# 관측이 비활성이어도 _reconcile_tracked_order_status() 자체는 예외 없이 정상 동작.
_psm2 = _service_no_label._position_state_machine
_psm2.get("005930").lifecycle = L.BUY_PENDING
_psm2.get("005930").pending_order_id = "777777"
_psm2.get("005930").pending_since = _OLD_PENDING
_psm2.get("005930").base_quantity_before_order = 0
_psm2.get("005930").expected_final_quantity = 10
_no_crash = True
try:
    _service_no_label._reconcile_tracked_order_status("005930", broker_qty=10)
except Exception:
    _no_crash = False
check("5-6) 관측 비활성 상태에서도 _reconcile_tracked_order_status()가 예외 없이 정상 동작",
      _no_crash)


# ══════════════════════════════════════════════════════════════
# 6. 일일 번들 연결(export_daily_bundle.py) — v2 지적 5번의
#    "A와 저장 상태 요약의 번들 연결까지 1차에 포함" 확인
# ══════════════════════════════════════════════════════════════

import csv as _csv
import json as _json
import os as _os
import zipfile as _zipfile

import export_daily_bundle as _bundle

_bundle_tmpdir = tempfile.mkdtemp()
_orig_cwd = _os.getcwd()
try:
    _os.chdir(_bundle_tmpdir)
    _logs = Path("logs")
    _logs.mkdir()
    _target_day = datetime(2026, 9, 10).date()
    _day_compact = _target_day.strftime("%Y%m%d")

    # trades.csv — 오늘 접수된 BUY 1건(order_id=OID1) accepted=True.
    from infra.storage.logger import TRADE_FIELDS
    with (_logs / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "timestamp": "2026-09-10T09:05:00", "symbol": "005930", "side": "BUY",
            "quantity": 10, "price": 10000, "accepted": "True", "message": "ok",
            "order_id": "OID1",
        })

    # run_baseline.csv — 이 실행이 오늘 새벽에 시작됐고 account_scope_id를 가짐.
    from infra.storage.run_baseline import RUN_BASELINE_FIELDS
    with (_logs / "run_baseline.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=RUN_BASELINE_FIELDS)
        w.writeheader()
        w.writerow({
            "run_id": "run-1", "started_at": "2026-09-10T08:30:00+09:00",
            "git_sha": "abc123", "git_dirty": "False",
            "effective_config_hash": "hash1", "is_mock": "False",
            "is_paper_trading": "False", "python_version": "3.11.0",
            "account_scope_id": "acct-bundle-test",
        })

    # order_status_observations.jsonl:
    #   1) 오늘 관측, OID1(오늘 접수) 대상, FILLED+가격 확보 — write_id=w1
    #   2) 1)과 같은 write_id, 같은 내용(시각만 다름) → dedup 후 1건
    #   3) 다른 날짜(9/9) 관측 — 날짜 슬라이스에서 제외돼야 함
    #   4) 말미 불완전 줄(강제종료 시뮬레이션) — 제외되지만 빌드는 성공해야 함
    def _obs_dict(query_id, started_at, order_id="OID1", write_id="w1", finished_at="2026-09-10T09:05:01"):
        return {
            "query_id": query_id, "started_at": started_at, "finished_at": finished_at,
            "account_scope_id": "acct-bundle-test", "env": "kiwoom_real", "symbol": "005930",
            "requested_order_id": order_id, "side_context": "BUY_PENDING",
            "query_kind": "BUY_PENDING", "pending_age_sec_at_query": 31.0,
            "outcome": "success", "failure_stage": None, "error_repr": None,
            "psm_broker_status": "FILLED", "filled_price_parsed": 10000,
            "cntr_matches": [], "oso_matches": [], "cntr_match_count": 0, "oso_match_count": 0,
            "base_quantity_before_order": 0, "target_quantity_after_order": 10,
            "journal_linked": False, "write_id": write_id, "restart_id": "r1", "seq_no": 1,
            "schema_version": 1,
        }

    # 실제 호출부(_reconcile_tracked_order_status)는 write_id=query_id로
    # 발급하므로, "같은 write_id의 재시도"는 실제로도 같은 query_id를
    # 공유합니다(호출부가 같은 관측 객체를 다시 큐에 넣는 경우) —
    # finished_at만 달라지는 것이 정상적인 재시도 시나리오입니다.
    obs_lines = [
        _json.dumps(_obs_dict("q1", "2026-09-10T09:05:00")),
        _json.dumps(_obs_dict("q1", "2026-09-10T09:05:00", finished_at="2026-09-10T09:05:02")),
        _json.dumps(_obs_dict("q3", "2026-09-09T09:05:00", write_id="w3")),
    ]
    with (_logs / "order_status_observations.jsonl").open("w", encoding="utf-8") as f:
        f.write("\n".join(obs_lines) + "\n")
        f.write('{"query_id": "q4", "started_at": "2026-09-10T10:00:00", "outcome": "succ')  # 잘린 줄, 개행 없음

    bundle_path = _bundle.build(_target_day, quiet=True)
    check("6-1) account_scope_id/trades/observations가 갖춰지면 번들 생성이 성공함",
          bundle_path is not None and bundle_path.exists())

    with _zipfile.ZipFile(bundle_path) as z:
        names = z.namelist()
        check("6-2) 관측 raw 파일이 번들에 포함됨",
              f"raw/order_status_observations_{_day_compact}.jsonl" in names)
        check("6-3) 커버리지 요약 파일이 metadata/에 포함됨",
              f"metadata/order_status_coverage.txt" in names)

        obs_raw_text = z.read(f"raw/order_status_observations_{_day_compact}.jsonl").decode("utf-8")
        obs_raw_lines = [l for l in obs_raw_text.splitlines() if l.strip()]
        check("6-4) 말미 불완전 줄은 raw 출력에서 제외됨(q4가 없음)",
              all('"q4"' not in l for l in obs_raw_lines))
        check("6-5) 다른 날짜(9/9) 관측(q3)도 raw 출력에서 제외됨(날짜 슬라이스)",
              all('"q3"' not in l for l in obs_raw_lines))
        check("6-6) 오늘 관측(q1)은 raw 출력에 포함됨",
              any('"q1"' in l for l in obs_raw_lines))

        coverage_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        check("6-7) 커버리지 요약에 같은 write_id 중복(q1/q2)이 1건으로 집계됨을 알 수 있음",
              "observation_unique_write_id_count      = 1" in coverage_text)
        check("6-8) 커버리지 요약에 OID1이 관측된 고유 주문으로 집계됨",
              "observed_unique_orders         = 1" in coverage_text)
        check("6-9) 커버리지 요약에 acct-bundle-test 스코프가 나타남",
              "[ account_scope_id = acct-bundle-test ]" in coverage_text)
finally:
    _os.chdir(_orig_cwd)


# 관측 로그 자체가 없는 날(계측 비활성 또는 아직 관측 없음)도 번들
# 생성이 실패하지 않아야 함(5번 지적의 "기동/운영을 막지 않는다"는
# 원칙을 exporter 쪽에서도 동일하게 지킴).
_bundle_tmpdir2 = tempfile.mkdtemp()
try:
    _os.chdir(_bundle_tmpdir2)
    Path("logs").mkdir()
    _target_day2 = datetime(2026, 9, 11).date()
    bundle_path2 = _bundle.build(_target_day2, quiet=True)
    check("6-10) 관측 로그가 아예 없어도(MISSING) 번들 생성이 예외 없이 성공함",
          bundle_path2 is not None and bundle_path2.exists())
finally:
    _os.chdir(_orig_cwd)


print(f"\n총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
