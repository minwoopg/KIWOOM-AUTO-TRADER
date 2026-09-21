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
import json
import logging
import sys
import tempfile
import time
import unittest.mock as mock
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
    PartialOrderStatusFetchError, build_order_status_evidence, derive_broker_order_status,
    find_all_matching, normalize_order_id,
)
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.storage.order_status_observation_store import (
    COVERAGE_DISABLED, OrderStatusObservation, OrderStatusObservationRecorder,
    build_entry_evidence, compute_coverage, dedupe_by_write_id,
    find_quarantined_observation_files, resolve_env,
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

# 2026-09-18 재검토 반영(지적 5번, 재현된 버그): 이전 구현은 여기서
# derive_broker_order_status()가 malformed 입력(리스트 원소가 dict가
# 아님)에 대해 던지는 예외까지 잡아 UNKNOWN 폴백으로 바꿔 반환했는데,
# 이는 기존 get_order_status() 호출부의 예외 계약(판정 함수 자체의
# 예외는 그대로 전파돼야 함)을 조용히 바꾸는 것이었습니다. 이제
# build_order_status_evidence()는 derive_broker_order_status()의
# 예외를 절대 잡지 않고 그대로 전파해야 하므로, "직접 호출과 동일한
# 예외가 그대로 전파되는지"를 확인합니다 — evidence_error로 감싸
# 정상처럼 보이게 하지 않는지가 핵심입니다.
_direct_exc_type = None
try:
    derive_broker_order_status("123", "005930", [], [None, "not-a-dict"])
except Exception as exc:
    _direct_exc_type = type(exc)

_wrapped_exc_type = None
try:
    build_order_status_evidence("123", "005930", [], [None, "not-a-dict"])
except Exception as exc:
    _wrapped_exc_type = type(exc)

check("1-8) 완전히 잘못된 원소(malformed) 입력에서 derive_broker_order_status() 직접 호출이"
      " 예외를 던짐(재현 전제 확인)",
      _direct_exc_type is not None)
check("1-8b) build_order_status_evidence()도 동일한 예외 타입을 그대로 전파함"
      "(evidence_error로 감싸 UNKNOWN처럼 보이게 바꾸지 않음)",
      _wrapped_exc_type is not None and _wrapped_exc_type is _direct_exc_type)

# 판정(derive_broker_order_status)이 "성공"한 다음 단계, 즉 이 함수가
# 새로 추가한 find_all_matching() 기반 증거 목록 구성 단계만 실패하는
# 경우는 여전히 격리돼야 합니다(격리 범위를 "판정 성공 이후"로 정확히
# 좁히는지 확인) — find_all_matching()을 인위적으로 실패시켜, 같은
# 입력에 대한 직접 판정 결과와 broker_order가 동일하게 보존되는지 봄.
_valid_cntr_for_isolation = [{"ord_no": "0000123", "ord_stt": "체결", "ord_qty": "10",
                              "cntr_qty": "10", "oso_qty": "0", "cntr_pric": "10000",
                              "io_tp_nm": "+매수", "trde_tp": "시장가"}]
with mock.patch(
    "infra.broker.kiwoom_order_status.find_all_matching",
    side_effect=RuntimeError("증거 목록 구성 중 인위적 실패(테스트 전용)"),
):
    _isolated_ev = build_order_status_evidence("123", "005930", [], _valid_cntr_for_isolation)
_direct_ok = derive_broker_order_status("123", "005930", [], _valid_cntr_for_isolation)
check("1-9) 판정이 성공한 다음 증거 목록 구성만 실패하면 예외를 던지지 않고"
      " evidence_error로 감싸며, broker_order는 직접 판정 결과와 동일하게 보존됨",
      _isolated_ev.evidence_error is not None and _isolated_ev.broker_order == _direct_ok)


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
# 2B. 두 번째 API 실패 시 첫 번째 응답 증거 보존 (재검토 지적 4번)
# ══════════════════════════════════════════════════════════════

# 브로커 단위: oso 조회는 성공, cntr 조회만 실패 — 이미 받은 oso_entries가
# 예외 객체에 그대로 실려있는지, failure_stage가 정확히 구분되는지 확인.
_ps_config = BrokerConfig(
    provider="kiwoom", use_mock=False, base_url="http://test.invalid",
    app_key="k", secret_key="s", account_number="000-00", is_paper_trading=True,
)
_ps_broker = KiwoomBroker(_ps_config)
_ps_oso_success = [{"ord_no": "0000999", "ord_stt": "접수", "ord_qty": "10",
                     "cntr_qty": "0", "oso_qty": "10"}]
_ps_broker._fetch_open_orders_raw = lambda symbol: _ps_oso_success


def _ps_cntr_fail(symbol):
    raise RuntimeError("cntr 조회 429(테스트 전용)")


_ps_broker._fetch_fill_history_raw = _ps_cntr_fail

_ps_raised: PartialOrderStatusFetchError | None = None
try:
    _ps_broker.get_order_status_evidence("999", "005930")
except PartialOrderStatusFetchError as exc:
    _ps_raised = exc
except Exception:
    _ps_raised = None

check("2B-1) oso 성공·cntr 실패 시 예외가 여전히 전파됨(실패를 성공으로 바꾸지 않음)",
      _ps_raised is not None)
check("2B-2) failure_stage가 cntr_fetch로 정확히 구분됨(1번째 조회 실패와 구분)",
      _ps_raised is not None and _ps_raised.failure_stage == "cntr_fetch")
check("2B-3) 이미 성공한 oso_entries가 예외 객체에 그대로 보존됨",
      _ps_raised is not None and _ps_raised.oso_entries == _ps_oso_success)

# 반대로 oso 조회 자체가 실패하면 cntr을 아예 시도하지 않고(추가 조회
# 금지) failure_stage=oso_fetch로 구분되며 oso_entries는 비어있어야 함.
_ps_broker2 = KiwoomBroker(_ps_config)
_ps_oso_calls = []


def _ps_oso_fail(symbol):
    _ps_oso_calls.append(symbol)
    raise RuntimeError("oso 조회 429(테스트 전용)")


_ps_cntr_calls = []
_ps_broker2._fetch_open_orders_raw = _ps_oso_fail
_ps_broker2._fetch_fill_history_raw = lambda symbol: _ps_cntr_calls.append(symbol)

_ps_raised2: PartialOrderStatusFetchError | None = None
try:
    _ps_broker2.get_order_status_evidence("999", "005930")
except PartialOrderStatusFetchError as exc:
    _ps_raised2 = exc

check("2B-4) oso 조회 자체가 실패하면 failure_stage=oso_fetch로 구분되고,"
      " cntr은 아예 시도되지 않음(추가 조회 없음)",
      _ps_raised2 is not None and _ps_raised2.failure_stage == "oso_fetch"
      and _ps_raised2.oso_entries == [] and len(_ps_cntr_calls) == 0)

# 서비스 단위: get_order_status_evidence()가 PartialOrderStatusFetchError를
# 던지면 _reconcile_tracked_order_status()가 이를 duck-typing으로 감지해
# failure_stage/oso_entries를 관측 기록에 전달하는지, 그리고 PSM 상태
# 자체는(실패이므로) 바뀌지 않는지 확인.
class _PartialEvidenceBroker(MockBroker):
    """oso는 성공, cntr만 실패하는 것을 서비스 레벨까지 재현하는 더블."""

    def __init__(self, oso_entries: list[dict]) -> None:
        super().__init__()
        self._oso_entries = oso_entries

    def get_order_status_evidence(self, order_id: str, symbol: str) -> OrderStatusEvidence:
        raise PartialOrderStatusFetchError(
            RuntimeError("cntr 429(테스트 전용)"), "cntr_fetch",
            oso_entries=self._oso_entries,
        )


_partial_oso_for_service = [{"ord_no": "555555", "ord_stt": "접수", "ord_qty": "10",
                              "cntr_qty": "0", "oso_qty": "10"}]
_partial_broker = _PartialEvidenceBroker(_partial_oso_for_service)
_partial_tmpdir = tempfile.mkdtemp()
_partial_settings = build_minimal_settings(_partial_tmpdir)
_partial_settings = dataclasses.replace(
    _partial_settings,
    broker=dataclasses.replace(_partial_settings.broker, account_scope_id="acct-partial"),
)
_partial_service = _build_service_with_settings(_partial_broker, _partial_settings)

_partial_psm = _partial_service._position_state_machine
_partial_psm.get("005930").lifecycle = L.BUY_PENDING
_partial_psm.get("005930").pending_order_id = "555555"
_partial_psm.get("005930").pending_since = _OLD_PENDING
_partial_psm.get("005930").base_quantity_before_order = 0
_partial_psm.get("005930").expected_final_quantity = 10

_partial_service._reconcile_tracked_order_status("005930", broker_qty=0)
time.sleep(0.2)
check("2B-5) 부분 실패 후에도 PSM lifecycle은 그대로 유지됨(실패를 성공으로 바꾸지 않음)",
      _partial_psm.get("005930").lifecycle == L.BUY_PENDING)

_partial_log_path = Path(_partial_settings.storage.order_status_observation_log_file)
_partial_lines = (
    _partial_log_path.read_text(encoding="utf-8").splitlines() if _partial_log_path.exists() else []
)
check("2B-6) 부분 실패 상황도 최소 1건 기록됨", len(_partial_lines) >= 1)
_partial_parsed = [json.loads(l) for l in _partial_lines if l.strip()]
check("2B-7) outcome=partial로 기록되고 failure_stage=cntr_fetch가 남음",
      any(d.get("outcome") == "partial" and d.get("failure_stage") == "cntr_fetch"
          for d in _partial_parsed))
check("2B-8) 이미 확보한 oso_entries가 oso_matches를 통해 관측에 보존됨"
      "(order_id=555555에 매칭되는 행이 실제로 담김)",
      any(
          d.get("outcome") == "partial" and len(d.get("oso_matches") or []) == 1
          and (d.get("oso_matches") or [{}])[0].get("response_order_id") == "555555"
          for d in _partial_parsed
      ))
_partial_service._order_status_observation_recorder.shutdown()


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


# ── 3B. 200% 커버리지 재현(재검토 지적 3번, 재현된 버그) ──────────
# 계좌 A accepted 1건 + 계좌 B accepted 1건, 각각 관측 1건 →
# 예전 구현은 A의 관측률이 200%로 나왔음(분자가 계좌로 스코핑되지
# 않아 B의 관측까지 A의 분자에 더해짐). 이제 A/B를 각각 계산하면
# 각자 100%를 넘지 않아야 함.
_accepted_two_accounts = {
    ("acct-A", "kiwoom_real", "2026-09-18", "10"),
    ("acct-B", "kiwoom_real", "2026-09-18", "20"),
}
_obs_two_accounts = [_obs("10", account="acct-A"), _obs("20", account="acct-B")]
_cov_a = compute_coverage(
    account_scope_id="acct-A", accepted_orders=_accepted_two_accounts,
    observations=_obs_two_accounts, order_date_resolver=lambda o: "2026-09-18",
)
_cov_b = compute_coverage(
    account_scope_id="acct-B", accepted_orders=_accepted_two_accounts,
    observations=_obs_two_accounts, order_date_resolver=lambda o: "2026-09-18",
)
check("3B-1) 재현된 200% 버그 수정 확인 — 계좌 A의 관측률이 100%를 넘지 않음"
      "(다른 계좌 B의 관측이 A의 분자에 섞이지 않음)",
      _cov_a["order_observation_rate"] == 1.0)
check("3B-2) 계좌 A의 observed_unique_orders는 자기 계좌 몫인 1건뿐",
      _cov_a["observed_unique_orders"] == 1)
check("3B-3) 계좌 B도 마찬가지로 자기 몫만 집계됨(교차 오염 없음)",
      _cov_b["order_observation_rate"] == 1.0 and _cov_b["observed_unique_orders"] == 1)

# ── 3C. order_id 정규화(재검토 지적 3번) ─────────────────────────
# "000123"(관측에 기록된 원문)과 "123"(accepted_orders 쪽 키)이
# 같은 주문으로 정규화되지 않으면 고아 관측으로 잘못 분류됨.
_accepted_padded = {("acct-1", "kiwoom_real", "2026-09-18", "123")}
_obs_padded = _obs("000123", account="acct-1")
_cov_norm = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted_padded, observations=[_obs_padded],
    order_date_resolver=lambda o: "2026-09-18",
)
check("3C-1) '000123'과 '123'이 같은 주문으로 정규화되어 고아 관측으로 분류되지 않음",
      _cov_norm["orphan_observation_count"] == 0 and _cov_norm["observed_unique_orders"] == 1)

# ── 3D. order_accepted_at 기반 order_date 확정(재검토 지적 3번) ──
# export_daily_bundle.py의 _order_date_resolver()가 "오늘 trades.csv에
# 같은 order_id가 있으면 오늘 주문"이라는 추정을 더 이상 쓰지 않고
# obs.order_accepted_at의 날짜 접두사만 근거로 삼는지 확인 — 이 값이
# 없으면 미확인으로 남아야 하고(주문 재사용 오인 방지), 있으면 그
# 날짜로 정확히 분리돼야 한다(같은 order_id라도 접수일이 다르면
# 서로 다른 키로 취급됨).
def _order_date_resolver_under_test(obs: OrderStatusObservation) -> str | None:
    if not obs.order_accepted_at:
        return None
    candidate = str(obs.order_accepted_at)[:10]
    return candidate if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-" else None


_obs_no_accepted_at = _obs("99", account="acct-1")  # order_accepted_at 미설정(기본 None)
check("3D-1) order_accepted_at이 없으면 order_date는 미확인(trades.csv 멤버십으로 추정하지 않음)",
      _order_date_resolver_under_test(_obs_no_accepted_at) is None)

_obs_today = dataclasses.replace(_obs("55", account="acct-1"), order_accepted_at="2026-09-18T09:00:00")
_obs_yesterday = dataclasses.replace(_obs("55", account="acct-1"), order_accepted_at="2026-09-17T15:30:00")
check("3D-2) 같은 order_id(55)라도 order_accepted_at의 날짜가 다르면 서로 다른 order_date로 확정됨"
      "(키움 주문번호 재사용을 서로 다른 주문으로 정확히 구분)",
      _order_date_resolver_under_test(_obs_today) == "2026-09-18"
      and _order_date_resolver_under_test(_obs_yesterday) == "2026-09-17"
      and _order_date_resolver_under_test(_obs_today) != _order_date_resolver_under_test(_obs_yesterday))

_accepted_cross_day = {("acct-1", "kiwoom_real", "2026-09-18", "55")}
_cov_cross_day = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted_cross_day,
    observations=[_obs_yesterday], order_date_resolver=_order_date_resolver_under_test,
)
check("3D-3) 어제 접수된 주문(55)에 대한 오늘 조회는 오늘 accepted_orders와 섞이지 않고"
      " 고아 관측으로 분리됨(서로 다른 주문을 같은 주문으로 오인하지 않음)",
      _cov_cross_day["orphan_observation_count"] == 1 and _cov_cross_day["observed_unique_orders"] == 0)


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
# 4B. 부분 쓰기 실패 후 다음 정상 기록 보호(재검토 지적 1번, 재현된 버그)
# ══════════════════════════════════════════════════════════════
#
# 재현: w1 쓰기 도중 예외 발생(부분 바이트만 파일에 남음) → w2 정상
# 기록 시도 → 예전엔 w1의 불완전한 바이트 뒤에 w2가 바로 이어붙어
# 두 레코드 모두 파싱 불가가 됐음. 이제 w1 실패 시 파일을 실패 이전
# 크기로 truncate하므로, w2/w3는 항상 깨끗한 경계 뒤에 붙어야 한다.

_tmpdir4b = tempfile.mkdtemp()
_pw_path = Path(_tmpdir4b) / "obs.jsonl"
_pw_recorder = OrderStatusObservationRecorder(str(_pw_path), shutdown_drain_timeout_sec=1.0)

_pw_call_count = {"n": 0}
_real_path_open = Path.open


def _flaky_open(self, *args, **kwargs):
    _pw_call_count["n"] += 1
    if _pw_call_count["n"] == 1 and self == _pw_path:
        # 첫 번째 쓰기(w1)만 "일부 바이트를 실제로 파일에 쓴 뒤 예외"
        # 상황을 흉내냅니다 — 디스크 풀/권한 등으로 인한 실제 부분
        # 쓰기를 재현하기 위함(단순히 write() 전체를 막기만 하면
        # "부분 쓰기"가 아니라 "쓰기 자체가 안 됨"이 되어 이 버그를
        # 재현하지 못함).
        real_f = _real_path_open(self, *args, **kwargs)

        class _PartialWriteFile:
            def write(_self, data):
                partial = data[: max(1, len(data) // 2)]
                real_f.write(partial)
                raise OSError("simulated partial write failure(테스트 전용)")

            def flush(_self):
                real_f.flush()

            def fileno(_self):
                return real_f.fileno()

            def close(_self):
                real_f.close()

            def __enter__(_self):
                return _self

            def __exit__(_self, exc_type, exc, tb):
                _self.close()
                return False

        return _PartialWriteFile()
    return _real_path_open(self, *args, **kwargs)


_w1 = dataclasses.replace(_dummy, write_id="pw-w1", query_id="pw-w1")
_w2 = dataclasses.replace(_dummy, write_id="pw-w2", query_id="pw-w2")
_w3 = dataclasses.replace(_dummy, write_id="pw-w3", query_id="pw-w3")

with mock.patch.object(Path, "open", _flaky_open):
    _pw_recorder._write_one(_w1)  # 부분 쓰기 실패 유도 — 파일에 불완전한 바이트가 남을 뻔함
_pw_recorder._write_one(_w2)  # 정상 기록 — truncate가 안 됐다면 w1의 잔여 바이트에 이어붙어 손상됨
_pw_recorder._write_one(_w3)  # 정상 기록

_pw_lines = [l for l in _pw_path.read_text(encoding="utf-8").splitlines() if l.strip()]
_pw_parsed_ok = []
for _l in _pw_lines:
    try:
        _pw_parsed_ok.append(json.loads(_l))
    except json.JSONDecodeError:
        pass

check("4B-1) w1 쓰기 실패는 dropped_count에 정확히 1건 반영됨",
      _pw_recorder.dropped_count == 1)
check("4B-2) 파일에 남은 모든 줄이 유효한 JSON으로 파싱됨"
      "(w1의 부분 바이트가 truncate로 제거돼 w2/w3와 뒤섞이지 않음)",
      len(_pw_lines) == len(_pw_parsed_ok))
check("4B-3) w2가 온전히 파싱 가능한 레코드로 존재함(재현된 버그: 예전엔 이것도 복구 불가였음)",
      any(d.get("write_id") == "pw-w2" for d in _pw_parsed_ok))
check("4B-4) w3도 온전히 파싱 가능한 레코드로 존재함",
      any(d.get("write_id") == "pw-w3" for d in _pw_parsed_ok))
check("4B-5) w1 자체는(실패했으므로) 파일에 남아있지 않음 — 유실로 처리되고 재시도되지 않음",
      not any(d.get("write_id") == "pw-w1" for d in _pw_parsed_ok))

# fsync 실패는 dropped_count와 별도로 셈 — write() 자체는 성공했으므로
# "쓰기 실패(유실)"가 아니라 "저장 내구성 미확인"으로 구분돼야 함.
_tmpdir4c = tempfile.mkdtemp()
_fsync_path = Path(_tmpdir4c) / "obs.jsonl"
_fsync_recorder = OrderStatusObservationRecorder(str(_fsync_path))
with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure(테스트 전용)")):
    _fsync_recorder._write_one(dataclasses.replace(_dummy, write_id="fsync-w1", query_id="fsync-w1"))
check("4B-6) fsync() 실패는 dropped_count를 올리지 않음(write()는 성공 — 레코드 유실 아님)",
      _fsync_recorder.dropped_count == 0)
check("4B-7) fsync() 실패는 별도 카운터(fsync_unconfirmed_count)로 구분됨"
      "(예전처럼 조용히 무시하지 않음)",
      _fsync_recorder.fsync_unconfirmed_count == 1)
_fsync_lines = [l for l in _fsync_path.read_text(encoding="utf-8").splitlines() if l.strip()]
check("4B-8) fsync 실패에도 레코드 자체는 파일에 온전히(파싱 가능하게) 남아있음",
      len(_fsync_lines) == 1 and json.loads(_fsync_lines[0]).get("write_id") == "fsync-w1")


# ══════════════════════════════════════════════════════════════
# 4C. 종료 배선·대기시간 상한·유실 계측(재검토 지적 2번, 재현된 버그)
# ══════════════════════════════════════════════════════════════

# (a) 마커 기록 자체가 느려도 shutdown()의 제한시간을 넘기지 않아야
# 함 — 예전엔 shutdown()을 호출한 스레드가 join() 이후 직접 마커를
# 썼으므로 그 쓰기 자체가 timeout 적용을 받지 않았음(재현: timeout=
# 0.01초로 둬도 실제로는 ~0.20초 걸림). 이제 마커 기록은 작업자
# 스레드 안에서만 이뤄지므로, 마커 쓰기가 느려도(여기서는 완전히
# 멈춘 것처럼 흉내) 호출 스레드는 join(timeout=...) 하나로만
# 제한된다.
class _SlowMarkerRecorder(OrderStatusObservationRecorder):
    def _write_shutdown_marker(self) -> None:
        time.sleep(5.0)  # 디스크가 멈춘 상황을 흉내냄
        super()._write_shutdown_marker()


_slow_marker_path = str(Path(tempfile.mkdtemp()) / "slow_marker.jsonl")
_slow_marker_recorder = _SlowMarkerRecorder(_slow_marker_path, shutdown_drain_timeout_sec=0.05)
_slow_marker_recorder.start()
_t_marker0 = time.time()
_slow_marker_result = _slow_marker_recorder.shutdown()
_marker_elapsed = time.time() - _t_marker0
check("4C-1) 마커 기록 자체가 멈춰도 shutdown()이 제한시간(약 0.05초) 안에 반환됨"
      "(재현된 버그: 예전엔 마커 쓰기가 timeout 적용을 받지 않았음)",
      _marker_elapsed < 2.0)
check("4C-2) 이 경우 marker_written은 아직 알 수 없으므로 True로 거짓 확정하지 않음",
      _slow_marker_result.get("marker_written") is not True)

# (b) record()가 큐 포화 시 느린 로그 핸들러를 기다리지 않아야 함 —
# 예전엔 record()가 큐 full 예외 처리 안에서 app_logger.warning()을
# **동기** 호출했으므로 느린 핸들러가 매매 스레드 자체를 막았음.
class _SlowHandler(logging.Handler):
    def emit(self, record):
        time.sleep(0.3)


_slow_logger = logging.getLogger("test_order_status_obs_slow_handler")
_slow_logger.addHandler(_SlowHandler())
_slow_logger.setLevel(logging.DEBUG)

_qf_path = str(Path(tempfile.mkdtemp()) / "queue_full.jsonl")
_qf_recorder = OrderStatusObservationRecorder(_qf_path, app_logger=_slow_logger, maxsize=1)
# 작업자 스레드를 시작하지 않아 큐가 절대 빠지지 않게 하고, put_nowait만
# 재현 대상으로 삼음(스레드 스케줄링에 좌우되지 않게 하기 위함).
_qf_recorder._queue.put_nowait(dataclasses.replace(_dummy, write_id="qf-0"))
_t_qf0 = time.time()
_qf_recorder.record(dataclasses.replace(_dummy, write_id="qf-1"))  # 큐가 이미 가득 참 → Full 경로
_qf_elapsed = time.time() - _t_qf0
check("4C-3) 큐 포화 시 record()가 느린 로그 핸들러를 기다리지 않고 즉시 반환됨"
      "(재현된 버그: 예전엔 이 호출 자체가 ~0.2~0.3초 걸렸음)",
      _qf_elapsed < 0.1)
check("4C-4) 큐 포화로 인한 유실은 카운터에 정확히 반영됨(로그가 없어도 사실 자체는 남음)",
      _qf_recorder.dropped_count == 1)


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
    def _obs_dict(
        query_id, started_at, order_id="OID1", write_id="w1",
        finished_at="2026-09-10T09:05:01", order_accepted_at="2026-09-10T09:04:50",
    ):
        # 2026-09-18 재검토 반영(지적 3번): order_date는 더 이상 "오늘
        # trades.csv에 같은 order_id가 있으면 오늘 주문"으로 추정하지
        # 않고, journal에서 확인된 order_accepted_at을 근거로 삼습니다
        # — 그래서 이 합성 관측에도 실제 주문 접수 시각을 명시적으로
        # 채워둡니다(기본값은 OID1이 실제로 접수된 시각).
        return {
            "query_id": query_id, "started_at": started_at, "finished_at": finished_at,
            "account_scope_id": "acct-bundle-test", "env": "kiwoom_real", "symbol": "005930",
            "requested_order_id": order_id, "side_context": "BUY_PENDING",
            "query_kind": "BUY_PENDING", "pending_age_sec_at_query": 31.0,
            "outcome": "success", "failure_stage": None, "error_repr": None,
            "psm_broker_status": "FILLED", "filled_price_parsed": 10000,
            "cntr_matches": [], "oso_matches": [], "cntr_match_count": 0, "oso_match_count": 0,
            "base_quantity_before_order": 0, "target_quantity_after_order": 10,
            "journal_linked": False, "order_accepted_at": order_accepted_at,
            "write_id": write_id, "restart_id": "r1", "seq_no": 1,
            "schema_version": 1,
            # 2026-09-18 재검토 반영(지적 6번, 재현된 버그): 합성 원문에
            # 따옴표 없는(bare) 10~13자리 숫자 필드를 넣어, 문자열
            # 전체에 정규식 mask()를 적용하던 예전 구현이 이 값을
            # 따옴표 없는 ***로 치환해 JSON을 깨뜨렸는지 검증합니다.
            # 이 필드는 알려진 스키마 필드가 아니므로 커버리지 계산에는
            # 영향이 없고, 오직 raw 슬라이스 결과의 JSON 유효성만
            # 확인하는 용도입니다.
            "numeric_probe": 1234567890,
        }

    # 실제 호출부(_reconcile_tracked_order_status)는 write_id=query_id로
    # 발급하므로, "같은 write_id의 재시도"는 실제로도 같은 query_id를
    # 공유합니다(호출부가 같은 관측 객체를 다시 큐에 넣는 경우) —
    # finished_at만 달라지는 것이 정상적인 재시도 시나리오입니다.
    _shutdown_marker_line = _json.dumps({
        "__marker__": "shutdown", "clean_shutdown": True, "queue_drained": True,
        "dropped_count": 0, "restart_id": "r1", "last_seq_no": 3,
        "shutdown_at": "2026-09-10T15:30:00",
    })
    obs_lines = [
        _json.dumps(_obs_dict("q1", "2026-09-10T09:05:00")),
        _json.dumps(_obs_dict("q1", "2026-09-10T09:05:00", finished_at="2026-09-10T09:05:02")),
        _json.dumps(_obs_dict("q3", "2026-09-09T09:05:00", write_id="w3")),
        _shutdown_marker_line,
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

        # 2026-09-18 재검토 반영(지적 6번, 재현된 버그): raw로 나간 관측
        # 레코드 각 줄은 다시 json.loads()로 파싱 가능해야 하고, 숫자
        # 필드(numeric_probe)는 값이 그대로 보존돼야 한다(따옴표 없는
        # ***로 치환돼 JSON이 깨지던 예전 버그의 재현 확인).
        _parsed_raw_lines = []
        _all_raw_json_valid = True
        for _l in obs_raw_lines:
            try:
                _parsed_raw_lines.append(_json.loads(_l))
            except _json.JSONDecodeError:
                _all_raw_json_valid = False
        check("6-11) 번들에 포함된 관측 레코드 각 줄이 전부 유효한 JSON으로 재파싱됨"
              "(재현된 버그: 예전엔 숫자 필드가 따옴표 없는 ***로 치환돼 파싱 불가였음)",
              _all_raw_json_valid and len(_parsed_raw_lines) == len(obs_raw_lines))
        check("6-12) 숫자 필드(numeric_probe)의 값이 마스킹 없이 그대로 보존됨"
              "(민감 키가 아닌 일반 숫자 필드는 가려지지 않음)",
              any(d.get("numeric_probe") == 1234567890 for d in _parsed_raw_lines))
        check("6-13) account_scope_id는 SENSITIVE_KEYS의 'account'와 정확히 일치하지 않으므로"
              " 가려지지 않고 그대로 남아있음(부분 문자열 매칭으로 오탐되지 않음)",
              any(d.get("account_scope_id") == "acct-bundle-test" for d in _parsed_raw_lines))
        check("6-14) 종료 마커도 raw 출력에 포함됨(shutdown_markers가 build_order_status_summary에"
              " 전달돼 요약에 반영될 수 있도록)",
              any(d.get("__marker__") == "shutdown" for d in _parsed_raw_lines))
        check("6-15) 커버리지 요약에 종료 마커 발견 사실이 표시됨"
              "(재검토 지적 2번 — 저장 실패/정상 종료 상태를 번들에서 확인 가능)",
              "recorder_shutdown_marker_count(이 날짜)  = 1" in coverage_text
              and "restart_id=r1" in coverage_text)
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


# ══════════════════════════════════════════════════════════════
# 7. LOG_TAGS 확장 + 관측 파일 경로 설정 일관성(재검토 지적 2/6번)
# ══════════════════════════════════════════════════════════════

check("7-1) 관측 관련 신규 로그 태그가 LOG_TAGS allowlist에 포함됨"
      "(예전엔 요약에서 'app.log 슬라이스에서 확인하라'고 안내하면서도"
      " 실제로는 번들에서 제외됐음)",
      all(
          t in _bundle.LOG_TAGS
          for t in (
              "[ORDER_STATUS_OBS_QUEUE_FULL]", "[ORDER_STATUS_OBS_WRITE_FAILED]",
              "[ORDER_STATUS_OBS_RECOVERY]", "[ORDER_STATUS_OBS_SHUTDOWN_MARKER_FAILED]",
              "[ORDER_STATUS_OBS_FSYNC_FAILED]",
          )
      ))

# app.log에 이 태그가 있는 줄이 실제로 번들에 포함되는지 통합 확인.
_tags_tmpdir = tempfile.mkdtemp()
try:
    _os.chdir(_tags_tmpdir)
    Path("logs").mkdir()
    _tags_day = datetime(2026, 9, 12).date()
    _tags_day_str = _tags_day.strftime("%Y-%m-%d")
    (Path("logs") / "app.log").write_text(
        f"{_tags_day_str} 09:00:00 WARNING [ORDER_STATUS_OBS_QUEUE_FULL] 누적 큐 포화 유실 3건\n"
        f"{_tags_day_str} 09:00:01 INFO some unrelated line without any tag\n",
        encoding="utf-8",
    )
    _tags_bundle_path = _bundle.build(_tags_day, quiet=True)
    check("7-2) 번들 생성 자체는 성공함(관측 로그 없이 app.log만 있어도)",
          _tags_bundle_path is not None and _tags_bundle_path.exists())
    with _zipfile.ZipFile(_tags_bundle_path) as z:
        _app_log_name = next((n for n in z.namelist() if n.startswith("raw/app_analysis_")), None)
        check("7-3) app.log 슬라이스 파일이 번들에 포함됨", _app_log_name is not None)
        if _app_log_name:
            _app_log_text = z.read(_app_log_name).decode("utf-8")
            check("7-4) [ORDER_STATUS_OBS_QUEUE_FULL] 태그가 붙은 줄이 실제로 번들에 포함됨"
                  "(재현된 버그: 예전엔 LOG_TAGS에 없어서 제외됐음)",
                  "[ORDER_STATUS_OBS_QUEUE_FULL]" in _app_log_text)
            check("7-5) 태그가 없는 무관한 줄은 여전히 제외됨(allowlist 원칙 유지)",
                  "unrelated line" not in _app_log_text)
finally:
    _os.chdir(_orig_cwd)

# 관측 파일 경로가 config.settings.StorageConfig의 기본값과 일치하는지
# (지적 6번 — "설정한 관측 파일 경로도 exporter가 현재 고정 경로 대신
# 일관되게 사용하도록 맞추세요").
from config.settings import StorageConfig as _StorageConfigForTest
_expected_obs_log_default = next(
    f.default for f in dataclasses.fields(_StorageConfigForTest)
    if f.name == "order_status_observation_log_file"
)
check("7-6) export_daily_bundle.ORDER_STATUS_OBSERVATION_LOG이 StorageConfig의"
      " order_status_observation_log_file 기본값과 정확히 일치함(별도 하드코딩 아님)",
      str(_bundle.ORDER_STATUS_OBSERVATION_LOG) == _expected_obs_log_default)


# ══════════════════════════════════════════════════════════════
# 8. app/main.py 종료 배선 실제 연결(재검토 지적 2번, 이전 라운드
#    유예분)
# ══════════════════════════════════════════════════════════════

import inspect as _inspect
import app.main as _app_main

check("8-1) app.main에 관측 기록기 종료 헬퍼가 정의됨",
      hasattr(_app_main, "_shutdown_order_status_observation_recorder"))

_run_application_src = _inspect.getsource(_app_main._run_application)
check("8-2) _run_application()이 두 실행 모드 실행을 try/finally로 감싸"
      " 관측 기록기 종료를 호출함(정상 종료/예외/취소 모두 포함)",
      "try:" in _run_application_src
      and "_shutdown_order_status_observation_recorder(" in _run_application_src
      and "finally:" in _run_application_src)

_run_trading_modes_src = _inspect.getsource(_app_main._run_trading_modes)
check("8-3) _run_trading_modes()가 두 실행 모드(websocket.enabled 분기)를 그대로 보존함"
      "(로직 자체는 변경하지 않고 종료 배선만 추가)",
      "if settings.websocket.enabled:" in _run_trading_modes_src
      and "await trading_loop(trading_service, settings, app_logger)" in _run_trading_modes_src)


class _FakeRecorderForWiringTest:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True
        return {"clean_shutdown": True, "dropped_count": 0, "fsync_unconfirmed_count": 0}


class _FakeServiceForWiringTest:
    def __init__(self, recorder):
        self._order_status_observation_recorder = recorder


_fake_recorder = _FakeRecorderForWiringTest()
_fake_service = _FakeServiceForWiringTest(_fake_recorder)
_app_main._shutdown_order_status_observation_recorder(_fake_service, logging.getLogger("test_wiring"))
check("8-4) 헬퍼 호출 시 실제로 recorder.shutdown()이 호출됨",
      _fake_recorder.shutdown_called is True)

_fake_service_none = _FakeServiceForWiringTest(None)
_no_crash_none_recorder = True
try:
    _app_main._shutdown_order_status_observation_recorder(_fake_service_none, logging.getLogger("test_wiring"))
except Exception:
    _no_crash_none_recorder = False
check("8-5) 기록기가 None(관측 비활성)이어도 예외 없이 아무 것도 하지 않고 반환함",
      _no_crash_none_recorder)


class _RaisingRecorderForWiringTest:
    def shutdown(self):
        raise RuntimeError("종료 중 인위적 실패(테스트 전용)")


_fake_service_raising = _FakeServiceForWiringTest(_RaisingRecorderForWiringTest())
_no_crash_raising = True
try:
    _app_main._shutdown_order_status_observation_recorder(_fake_service_raising, logging.getLogger("test_wiring"))
except Exception:
    _no_crash_raising = False
check("8-6) recorder.shutdown() 자체가 예외를 던져도 프로세스 종료를 막지 않음"
      "(best-effort — 관측 기능이 종료 절차를 방해하지 않음)",
      _no_crash_raising)


# ══════════════════════════════════════════════════════════════
# 9. 2026-09-18 재재검토(GPT 2차) 5개 지적 사항 재현
#    ①truncate 실패·개행 누락 시 후속 기록 보호
#    ②부분 조회 실패의 성공 오집계
#    ③익일 조회의 원래 주문일 연결
#    ④식별자를 보존하는 마스킹과 실제 설정 경로 전달
#    ⑤실행 중 저장 품질 스냅샷과 실행별 종료 상태 구분
# ══════════════════════════════════════════════════════════════

# ── 9A. truncate 자체가 실패하는 경우(지적 1번, 재현된 버그) ─────
# 재현 표의 2번째 행: "w1 부분 쓰기 실패 → truncate 실패" → 예전엔
# w2도 손상된 파일 위에 이어붙어 함께 파싱 불가가 됐음. 이제
# truncate 실패 시 손상 파일을 격리하고 같은 경로에 새 파일로
# 전환해야 하므로, w2/w3는 정상 기록돼야 하고 dropped_count는
# 실제로 복구 불가능해진 w1 1건만 반영해야 한다.
_tmpdir9a = tempfile.mkdtemp()
_tp_path = Path(_tmpdir9a) / "obs.jsonl"
_tp_recorder = OrderStatusObservationRecorder(str(_tp_path), shutdown_drain_timeout_sec=1.0)

_tp_call_count = {"n": 0}


def _flaky_open_truncate_fail(self, *args, **kwargs):
    _tp_call_count["n"] += 1
    n = _tp_call_count["n"]
    if self == _tp_path and n == 1:
        # 1번째 open: w1의 "a" 모드 쓰기 — 일부 바이트만 쓰고 예외.
        real_f = _real_path_open(self, *args, **kwargs)

        class _PartialWriteFile:
            def write(_self, data):
                partial = data[: max(1, len(data) // 2)]
                real_f.write(partial)
                raise OSError("simulated partial write failure(테스트 전용)")

            def flush(_self):
                real_f.flush()

            def fileno(_self):
                return real_f.fileno()

            def close(_self):
                real_f.close()

            def __enter__(_self):
                return _self

            def __exit__(_self, exc_type, exc, tb):
                _self.close()
                return False

        return _PartialWriteFile()
    if self == _tp_path and n == 2:
        # 2번째 open: _truncate_partial_write()의 "r+b" 시도 —
        # truncate 자체가 실패하는 상황을 그대로 재현(디스크 문제 등).
        raise OSError("simulated truncate failure(테스트 전용)")
    return _real_path_open(self, *args, **kwargs)


with mock.patch.object(Path, "open", _flaky_open_truncate_fail):
    _tp_recorder._write_one(dataclasses.replace(_dummy, write_id="tp-w1", query_id="tp-w1"))
# truncate 복구까지 실패했으므로 이 파일에는 더 이상 쓰지 않고
# 격리+새 파일 전환이 일어나야 한다 — 아래 두 호출은 실제 open()을 씀.
_tp_recorder._write_one(dataclasses.replace(_dummy, write_id="tp-w2", query_id="tp-w2"))
_tp_recorder._write_one(dataclasses.replace(_dummy, write_id="tp-w3", query_id="tp-w3"))

_tp_lines = [l for l in _tp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
_tp_parsed = []
for _l in _tp_lines:
    try:
        _tp_parsed.append(json.loads(_l))
    except json.JSONDecodeError:
        pass

check("9-1) truncate 자체가 실패해도(더 이상 그 파일을 믿지 않고 새 파일로 전환) w2/w3는"
      " 정상 기록됨(재현된 버그: 예전엔 w2까지 함께 파싱 불가였음)",
      len(_tp_lines) == len(_tp_parsed) == 2
      and any(d.get("write_id") == "tp-w2" for d in _tp_parsed)
      and any(d.get("write_id") == "tp-w3" for d in _tp_parsed))
check("9-2) dropped_count는 실제로 복구 불가능해진 w1 1건만 반영함(재현된 버그: 예전엔"
      " truncate 실패 시 w2까지 함께 손상돼 실제 유실 건수가 dropped_count보다 컸음)",
      _tp_recorder.dropped_count == 1)
_quarantined_files = [p for p in Path(_tmpdir9a).iterdir() if "unrecoverable" in p.name]
check("9-3) 손상된 원본 파일은 삭제되지 않고 별도 이름으로 격리되어 수동 조사가 가능함",
      len(_quarantined_files) == 1)

# ── 9B. 기존 파일의 마지막 줄이 유효 JSON이지만 개행 없이 끝나는 경우
#    (지적 1번의 3번째 재현 행) ─────────────────────────────────
_tmpdir9b = tempfile.mkdtemp()
_nnl_path = Path(_tmpdir9b) / "obs.jsonl"
_nnl_prev_line = json.dumps({"query_id": "prev1", "started_at": "2026-09-18T08:00:00"})
with _nnl_path.open("w", encoding="utf-8") as f:
    f.write(_nnl_prev_line)  # 의도적으로 마지막 개행을 쓰지 않음
_nnl_recorder = OrderStatusObservationRecorder(str(_nnl_path), shutdown_drain_timeout_sec=1.0)
_nnl_recorder._quarantine_incomplete_tail()
_nnl_after = _nnl_path.read_bytes()
check("9-4) 마지막 줄이 유효한 JSON이지만 개행 없이 끝나는 기존 파일을 시작 시 개행으로"
      " 보정함(재현된 버그: 이전엔 '마지막 줄이 파싱 실패'인 경우만 감지했음)",
      _nnl_after.endswith(b"\n") and _nnl_prev_line.encode("utf-8") in _nnl_after)

_nnl_recorder._write_one(dataclasses.replace(_dummy, write_id="nnl-w2", query_id="nnl-w2"))
_nnl_lines = [l for l in _nnl_path.read_text(encoding="utf-8").splitlines() if l.strip()]
_nnl_parsed = []
for _l in _nnl_lines:
    try:
        _nnl_parsed.append(json.loads(_l))
    except json.JSONDecodeError:
        pass
check("9-5) 개행 보정 후 다음 기록이 이전 줄과 뒤섞이지 않고 별도 줄로 온전히 남음"
      "(보정하지 않았다면 두 레코드가 한 줄로 뭉개져 둘 다 파싱 불가였을 것)",
      len(_nnl_lines) == 2 and len(_nnl_parsed) == 2
      and any(d.get("query_id") == "prev1" for d in _nnl_parsed)
      and any(d.get("write_id") == "nnl-w2" for d in _nnl_parsed))

# ── 9C. compute_coverage()의 부분 조회 성공 오집계 수정(지적 2번,
#    재현된 버그) — 재현: 조회 시도 1건, cntr 조회 실패(oso는 성공) →
#    outcome="partial". 예전엔 api_error가 아니라는 이유만으로
#    query_success_count에 들어가 주문 관측률이 100%로 나왔음.
_obs_partial = dataclasses.replace(
    _obs("30", account="acct-1"), outcome="partial", psm_broker_status=None,
    filled_price_parsed=None, failure_stage="cntr_fetch",
)
_accepted_partial = {("acct-1", "kiwoom_real", "2026-09-18", "30")}
_cov_partial = compute_coverage(
    account_scope_id="acct-1", accepted_orders=_accepted_partial,
    observations=[_obs_partial], order_date_resolver=lambda o: "2026-09-18",
)
check("9-6) 부분 조회(outcome=partial)는 query_success_count에 들어가지 않음"
      "(재현된 버그: 예전엔 api_error가 아니라는 이유만으로 성공으로 집계됐음)",
      _cov_partial["query_success_count"] == 0)
check("9-7) 부분 조회는 별도의 query_partial_count로 집계됨(조회 성공과 명시적으로 구분)",
      _cov_partial["query_partial_count"] == 1 and _cov_partial["query_failed_count"] == 0)
check("9-8) 부분 조회만으로는 주문 관측률(observed_unique_orders)에 포함되지 않음"
      "(원문 일부 확보 ≠ 조회 완전 성공 — 새 판정을 만들지 않는다는 서비스 계약과 일치)",
      _cov_partial["observed_unique_orders"] == 0 and _cov_partial["order_observation_rate"] == 0.0)

# ── 9D. _classify_run_state() 4가지 상태 구분(지적 5번) ──────────
_now_iso = datetime.now().isoformat()
_stale_iso = (datetime.now() - timedelta(seconds=999)).isoformat()
check("9-9) 상태 스냅샷이 아예 없으면 '계측_비활성'(관측 기능이 시작된 적 없음)",
      _bundle._classify_run_state(None) == "계측_비활성")
check("9-10) clean_shutdown=True면 '정상_종료'",
      _bundle._classify_run_state({"clean_shutdown": True, "updated_at": _now_iso}) == "정상_종료")
check("9-11) clean_shutdown=False면 '종료_확인_불가'(종료 배선은 탔지만 마커 기록 자체가 실패)",
      _bundle._classify_run_state({"clean_shutdown": False, "updated_at": _now_iso}) == "종료_확인_불가")
check("9-12) clean_shutdown=None(아직 종료 안 함)이고 최근 갱신이면 '실행_중'"
      "(이전 안내 정정 대상 — 실행 중인 프로그램의 자동 번들에는 종료 마커가 없는 게 정상)",
      _bundle._classify_run_state({"clean_shutdown": None, "updated_at": _now_iso}) == "실행_중")
check("9-13) clean_shutdown=None인데 갱신이 오래됐으면(하트비트 끊김) '종료_확인_불가'"
      "(정상 종료 배선을 타지 못하고 강제 종료됐을 가능성)",
      _bundle._classify_run_state({"clean_shutdown": None, "updated_at": _stale_iso}) == "종료_확인_불가")


# ── 9E. 실제 서비스/exporter 경로 통합 재현 — 부분조회 오집계·익일
#    조회 연결·식별자 보존 마스킹·사용자 지정 경로·실행 중 상태를
#    한 번에 exporter의 build() 경로로 확인 ─────────────────────
_tmpdir9e = tempfile.mkdtemp()
try:
    _os.chdir(_tmpdir9e)
    Path("logs").mkdir()
    Path("custom_obs_dir").mkdir()
    _day917 = datetime(2026, 9, 17).date()
    _day917_compact = _day917.strftime("%Y%m%d")

    # trades.csv — 9/17 접수된 두 주문(OID2: 정상 관측 대상, OID3: 부분 조회 대상).
    with (Path("logs") / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "timestamp": "2026-09-17T09:10:00", "symbol": "005930", "side": "BUY",
            "quantity": 10, "price": 10000, "accepted": "True", "message": "ok",
            "order_id": "OID2",
        })
        w.writerow({
            "timestamp": "2026-09-17T09:20:00", "symbol": "005930", "side": "BUY",
            "quantity": 5, "price": 20000, "accepted": "True", "message": "ok",
            "order_id": "OID3",
        })

    with (Path("logs") / "run_baseline.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=RUN_BASELINE_FIELDS)
        w.writeheader()
        w.writerow({
            "run_id": "run-9e", "started_at": "2026-09-17T08:30:00+09:00",
            "git_sha": "abc123", "git_dirty": "False",
            "effective_config_hash": "hash1", "is_mock": "False",
            "is_paper_trading": "False", "python_version": "3.11.0",
            "account_scope_id": "acct-9e",
        })

    def _obs9e(query_id, started_at, order_id, order_accepted_at, write_id,
               outcome="success", requested_order_id_raw=None, cntr_pric_raw=None):
        d = {
            "query_id": query_id, "started_at": started_at,
            "finished_at": started_at, "account_scope_id": "acct-9e", "env": "kiwoom_real",
            "symbol": "005930", "requested_order_id": order_id, "side_context": "BUY_PENDING",
            "query_kind": "BUY_PENDING", "pending_age_sec_at_query": 31.0,
            "outcome": outcome, "failure_stage": ("cntr_fetch" if outcome == "partial" else None),
            "error_repr": None,
            "psm_broker_status": (None if outcome == "partial" else "FILLED"),
            "filled_price_parsed": (None if outcome == "partial" else 10000),
            "cntr_matches": ([{
                "response_order_id": requested_order_id_raw or order_id,
                "cntr_pric_raw": cntr_pric_raw or "10000",
            }] if requested_order_id_raw or cntr_pric_raw else []),
            "oso_matches": [], "cntr_match_count": 0, "oso_match_count": 0,
            "base_quantity_before_order": 0, "target_quantity_after_order": 10,
            "journal_linked": False, "order_accepted_at": order_accepted_at,
            "write_id": write_id, "restart_id": "r9e", "seq_no": 1, "schema_version": 1,
        }
        return d

    obs9e_lines = [
        # OID2: 9/17에 접수됐지만 9/18에(익일) 조회됨 — 원래 주문일(9/17)의
        # 관측률에 연결돼야 함(재현: 지적 3번). started_at이 9/18이라
        # 9/17 raw 슬라이스(조회일 기준)에는 포함되지 않으므로 마스킹
        # 검증용 식별자 필드는 아래 OID3(9/17 당일 조회)에 둔다.
        _json.dumps(_obs9e(
            "q-oid2", "2026-09-18T09:00:00", "OID2", "2026-09-17T09:10:00", "w-oid2",
        )),
        # OID3: 9/17 당일 조회했지만 cntr 조회 실패로 outcome=partial —
        # 조회 성공으로 오집계되면 안 됨(재현: 지적 2번). 동시에 식별자
        # 필드(response_order_id/cntr_pric_raw)가 마스킹으로 뭉개지지
        # 않는지도 이 관측(9/17 raw 슬라이스에 포함됨)으로 확인한다
        # (재현: 지적 4번).
        _json.dumps(_obs9e(
            "q-oid3", "2026-09-17T09:21:00", "OID3", "2026-09-17T09:20:00", "w-oid3",
            outcome="partial",
            requested_order_id_raw="1234567890", cntr_pric_raw="9876543210",
        )),
    ]
    _custom_obs_path = Path("custom_obs_dir") / "custom_obs.jsonl"
    with _custom_obs_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(obs9e_lines) + "\n")

    # 실행 중 상태 스냅샷을 직접 만들어 둠(재현: 지적 5번) — 아직
    # 종료하지 않았고(clean_shutdown=None) 최근에 갱신된 것으로 표시.
    _status_path_9e = _bundle.status_path_for(_custom_obs_path)
    _status_path_9e.write_text(
        _json.dumps({
            "restart_id": "r9e", "updated_at": datetime.now().isoformat(),
            "dropped_count": 0, "fsync_unconfirmed_count": 0,
            "queue_full_dropped_count": 0, "file_healthy": True, "clean_shutdown": None,
        }),
        encoding="utf-8",
    )

    # 다른 날짜(9/12)의 과거 종료 마커도 같은 원본 로그에 남겨 둔다 —
    # 9/17 번들에는 나타나면 안 됨(재현: 지적 5번, "과거 종료 마커를
    # 날짜와 무관하게 모두 포함").
    with _custom_obs_path.open("a", encoding="utf-8") as f:
        f.write(_json.dumps({
            "__marker__": "shutdown", "clean_shutdown": True, "queue_drained": True,
            "dropped_count": 0, "restart_id": "r-old", "last_seq_no": 1,
            "shutdown_at": "2026-09-12T15:30:00",
        }) + "\n")

    _bundle_9e_path = _bundle.build(
        _day917, quiet=True, obs_log_path=_custom_obs_path,
    )
    check("9-14) 사용자 지정 관측 로그 경로(obs_log_path)로도 번들 생성이 성공함",
          _bundle_9e_path is not None and _bundle_9e_path.exists())

    with _zipfile.ZipFile(_bundle_9e_path) as z:
        _cov9e_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        _raw9e_name = f"raw/order_status_observations_{_day917_compact}.jsonl"
        _raw9e_text = z.read(_raw9e_name).decode("utf-8") if _raw9e_name in z.namelist() else ""

    check("9-15) 익일(9/18) 조회한 9/17 접수 주문(OID2)이 9/17 번들의 관측률에 정확히"
          " 집계됨(재현된 버그: 예전엔 양쪽 날짜 어디에도 반영되지 않았음)",
          "observed_unique_orders         = 1" in _cov9e_text)
    check("9-16) 부분 조회(OID3)는 query_partial_count로 집계되고 query_success_count에는"
          " 포함되지 않음(같은 번들 안에서 지적 2번도 함께 확인)",
          "query_partial_count              = 1" in _cov9e_text)
    check("9-17) 커버리지 요약에 '현재 실행 상태 = 실행_중'이 표시됨(지적 5번 — 프로그램이"
          " 실행 중일 때 생성된 번들은 종료 마커가 없는 게 정상임을 구분해서 보여줌)",
          "observation_run_state(현재 실행 상태)   = 실행_중" in _cov9e_text)
    check("9-18) 실행 중 상태이므로 종료 마커가 없다는 사실이 '정상'으로 안내됨"
          "(이전 안내: '종료 마커가 나타나는지 확인' — 실행 중엔 없는 게 정상이라고 정정)",
          "정상입니다(아직 종료하지 않았으니 종료 마커가 없는 게 맞습니다)" in _cov9e_text)
    check("9-19) 다른 날짜(9/12)의 과거 종료 마커는 9/17 번들에 나타나지 않음"
          "(재현된 버그: 예전엔 날짜와 무관하게 모든 과거 마커를 포함했음)",
          "r-old" not in _cov9e_text and "r-old" not in _raw9e_text)
    check("9-20) requested_order_id 원문(1234567890, 10자리)이 마스킹으로 '***'가 되지 않고"
          " 그대로 보존됨(재현된 버그: 예전엔 자유문자열 mask()가 10~13자리 숫자 문자열을"
          " 전부 가려 서로 다른 주문번호가 같은 '***'가 됐음)",
          "1234567890" in _raw9e_text)
    check("9-21) cntr_pric_raw 원문(9876543210, 10자리)도 마찬가지로 보존됨",
          "9876543210" in _raw9e_text)
finally:
    _os.chdir(_orig_cwd)

# ── 9F. resolve_order_status_observation_log_path() 폴백 동작 확인
#    (설정 파일이 없는 환경에서도 조용히 죽지 않고 기본값으로 폴백) ──
_resolved_path, _loaded_from_settings = _bundle.resolve_order_status_observation_log_path(
    "이런_파일은_존재하지_않음.yaml",
)
check("9-22) 설정 파일을 읽을 수 없으면 폴백하고(두 번째 반환값 False), 예외를 던지지 않음",
      _loaded_from_settings is False and _resolved_path == _bundle._default_order_status_observation_log_path())


# ══════════════════════════════════════════════════════════════
# 10. 2026-09-21 3차 재검토(GPT 3차) 3개 지적 사항 재현
#    ①격리 파일의 정상 관측이 집계·번들에서 사라짐
#    ②상태 파일 손상/미래 시각이 '계측_비활성'/'실행_중'으로 오표시
#    ③주문일 커버리지에 쓰인 익일 관측의 번들 내 근거 부재
# ══════════════════════════════════════════════════════════════

# ── 10A. 격리된 파일의 정상 관측이 집계·번들에 다시 나타남(지적 1번,
#    재현된 버그) — 9A와 동일한 방식(부분 쓰기 실패 → truncate 복구도
#    실패)으로 실제 recorder를 통해 진짜 격리 파일을 만든 뒤, 격리
#    직전까지 쌓여있던 정상 레코드(OIDQ1)와 격리 후 새 파일에 쓰인
#    레코드(OIDQ2)가 exporter의 raw/커버리지 양쪽에 모두 나타나는지
#    확인한다.
_tmpdir10a = tempfile.mkdtemp()
_orig_cwd10a = _os.getcwd()
try:
    _os.chdir(_tmpdir10a)
    Path("logs").mkdir()
    _day919 = datetime(2026, 9, 19).date()
    _day919_compact = _day919.strftime("%Y%m%d")
    _q_obs_path = Path("logs") / "order_status_observations.jsonl"

    with (Path("logs") / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "timestamp": "2026-09-19T09:00:00", "symbol": "005930", "side": "BUY",
            "quantity": 10, "price": 10000, "accepted": "True", "message": "ok",
            "order_id": "OIDQ1",
        })
        w.writerow({
            "timestamp": "2026-09-19T09:10:00", "symbol": "005930", "side": "BUY",
            "quantity": 10, "price": 10000, "accepted": "True", "message": "ok",
            "order_id": "OIDQ2",
        })

    with (Path("logs") / "run_baseline.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=RUN_BASELINE_FIELDS)
        w.writeheader()
        w.writerow({
            "run_id": "run-10a", "started_at": "2026-09-19T08:30:00+09:00",
            "git_sha": "abc123", "git_dirty": "False",
            "effective_config_hash": "hash1", "is_mock": "False",
            "is_paper_trading": "False", "python_version": "3.11.0",
            "account_scope_id": "acct-10a",
        })

    _q_recorder = OrderStatusObservationRecorder(str(_q_obs_path), shutdown_drain_timeout_sec=1.0)

    _healthy_before_quarantine = OrderStatusObservation(
        query_id="q-before", started_at="2026-09-19T09:00:00", finished_at="2026-09-19T09:00:01",
        account_scope_id="acct-10a", env="kiwoom_real", symbol="005930",
        requested_order_id="OIDQ1", side_context="BUY_PENDING", query_kind="BUY_PENDING",
        pending_age_sec_at_query=31.0, outcome="success", psm_broker_status="FILLED",
        filled_price_parsed=10000, order_accepted_at="2026-09-19T09:00:00",
        write_id="w-before", restart_id="rq", seq_no=1,
    )
    _q_recorder._write_one(_healthy_before_quarantine)  # 격리 직전까지의 "정상" 레코드

    _q_call_count = {"n": 0}

    def _flaky_open_for_quarantine(self, *args, **kwargs):
        _q_call_count["n"] += 1
        n = _q_call_count["n"]
        if self == _q_obs_path and n == 1:
            real_f = _real_path_open(self, *args, **kwargs)

            class _PartialWriteFile10a:
                def write(_self, data):
                    real_f.write(data[: max(1, len(data) // 2)])
                    raise OSError("simulated partial write failure(테스트 전용, 10A)")

                def flush(_self):
                    real_f.flush()

                def fileno(_self):
                    return real_f.fileno()

                def close(_self):
                    real_f.close()

                def __enter__(_self):
                    return _self

                def __exit__(_self, exc_type, exc, tb):
                    _self.close()
                    return False

            return _PartialWriteFile10a()
        if self == _q_obs_path and n == 2:
            raise OSError("simulated truncate failure(테스트 전용, 10A)")
        return _real_path_open(self, *args, **kwargs)

    _poison10a = dataclasses.replace(
        _dummy, write_id="poison", query_id="poison", started_at="2026-09-19T09:05:00",
    )
    with mock.patch.object(Path, "open", _flaky_open_for_quarantine):
        _q_recorder._write_one(_poison10a)
    # truncate까지 실패했으므로 파일이 격리되고 같은 경로에 새 파일로 전환됨.
    _healthy_after_quarantine = dataclasses.replace(
        _healthy_before_quarantine, write_id="w-after", query_id="q-after",
        requested_order_id="OIDQ2",
    )
    _q_recorder._write_one(_healthy_after_quarantine)  # 격리 후 새 파일에 쓰인 정상 레코드

    _found_quarantine_files = find_quarantined_observation_files(_q_obs_path)
    check("10-1) find_quarantined_observation_files()가 실제로 격리된 파일을 정확히 찾음",
          len(_found_quarantine_files) == 1)

    _bundle_path_10a = _bundle.build(_day919, quiet=True)
    check("10-2) 격리 파일이 있어도 번들 생성 자체는 예외 없이 성공함",
          _bundle_path_10a is not None and _bundle_path_10a.exists())

    with _zipfile.ZipFile(_bundle_path_10a) as z:
        _manifest10a = z.read("MANIFEST.txt").decode("utf-8")
        _raw10a_name = f"raw/order_status_observations_{_day919_compact}.jsonl"
        _raw10a_text = z.read(_raw10a_name).decode("utf-8") if _raw10a_name in z.namelist() else ""
        _cov10a_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")

    check("10-3) 격리 직전까지의 정상 레코드(OIDQ1)가 raw 슬라이스에 다시 나타남"
          "(재현된 버그: 예전엔 격리된 파일의 정상 레코드가 집계·번들에서 그냥 사라졌음)",
          "OIDQ1" in _raw10a_text)
    check("10-4) 격리 후 새 파일에 쓰인 레코드(OIDQ2)도 raw 슬라이스에 정상적으로 나타남",
          "OIDQ2" in _raw10a_text)
    check("10-5) MANIFEST에 격리 파일 발견 사실과 복구 건수가 표시됨",
          "격리된 손상 파일 1건" in _manifest10a and "정상 레코드 1건을 이 raw에 포함" in _manifest10a)
    check("10-6) 커버리지 요약에 OIDQ1/OIDQ2 둘 다 반영돼 관측된 고유 주문이 2건임"
          "(격리 파일의 정상 레코드가 집계에서 빠졌다면 1건으로만 나왔을 것)",
          "observed_unique_orders         = 2" in _cov10a_text)
finally:
    _os.chdir(_orig_cwd10a)


# ── 10B. 상태 파일 손상/미래 시각이 '계측_비활성'/'실행_중'으로
#    오표시되지 않음(지적 2번, 재현된 버그) ──────────────────────
check("10-7) 상태 파일이 있지만 손상(JSON 파싱 불가)됐으면 '계측_비활성'이 아니라"
      " '상태확인_불가'로 판정됨(재현된 버그: 예전엔 손상도 '계측_비활성'과"
      " 구분되지 않았음)",
      _bundle._classify_run_state(None, status_file_existed=True)
      == _bundle.RUN_STATE_STATUS_UNREADABLE)
check("10-8) 상태 파일이 아예 없으면 여전히 '계측_비활성'(기존 동작 그대로 유지)",
      _bundle._classify_run_state(None, status_file_existed=False)
      == _bundle.RUN_STATE_DISABLED)

_future_iso10b = (datetime.now() + timedelta(seconds=999)).isoformat()
check("10-9) clean_shutdown=None인데 updated_at이 미래 시각이면 '실행_중'으로 잘못"
      " 통과하지 않고 '상태확인_불가'로 판정됨(재현된 버그: 예전엔"
      " datetime.now()-updated_at이 음수가 돼 stale_after_sec 이하로 판정되면서"
      " 항상 '실행_중'으로 통과했음)",
      _bundle._classify_run_state({"clean_shutdown": None, "updated_at": _future_iso10b})
      == _bundle.RUN_STATE_STATUS_UNREADABLE)
check("10-10) clean_shutdown=None인데 updated_at 자체가 파싱 불가한 문자열이면"
      " '상태확인_불가'로 판정됨(예전엔 '종료_확인_불가'로 뭉뚱그려졌음)",
      _bundle._classify_run_state({"clean_shutdown": None, "updated_at": "이런-시각-아님"})
      == _bundle.RUN_STATE_STATUS_UNREADABLE)

_tmpdir10b = tempfile.mkdtemp()
_status_path_10b = Path(_tmpdir10b) / "obs.status.json"
_missing_result, _missing_existed = _bundle._read_running_status(_status_path_10b)
check("10-11) 상태 파일이 아예 없으면 (None, False)를 반환함",
      _missing_result is None and _missing_existed is False)

_status_path_10b.write_text("이건 JSON이 아님{{{", encoding="utf-8")
_corrupt_result, _corrupt_existed = _bundle._read_running_status(_status_path_10b)
check("10-12) 상태 파일이 있지만 JSON 파싱에 실패하면 (None, True)를 반환함"
      "(재현된 버그: 예전엔 '파일 없음'과 '파일은 있지만 손상' 둘 다 그냥 None"
      " 하나로 뭉뚱그려졌음)",
      _corrupt_result is None and _corrupt_existed is True)

_tmpdir10b2 = tempfile.mkdtemp()
_orig_cwd10b2 = _os.getcwd()
try:
    _os.chdir(_tmpdir10b2)
    Path("logs").mkdir()
    _day920 = datetime(2026, 9, 20).date()
    _obs_path_10b2 = Path("logs") / "order_status_observations.jsonl"
    _obs_path_10b2.write_text("", encoding="utf-8")
    _status_path_10b2 = _bundle.status_path_for(_obs_path_10b2)
    _status_path_10b2.write_text("{{{손상된 JSON", encoding="utf-8")

    _bundle_path_10b2 = _bundle.build(_day920, quiet=True)
    check("10-13) 상태 스냅샷 파일이 손상돼 있어도 번들 생성 자체는 예외 없이 성공함",
          _bundle_path_10b2 is not None and _bundle_path_10b2.exists())
    with _zipfile.ZipFile(_bundle_path_10b2) as z:
        _cov10b2_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        _manifest10b2_text = z.read("MANIFEST.txt").decode("utf-8")
    check("10-14) 커버리지 요약에 '계측_비활성'이 아니라 '상태확인_불가'로 표시됨"
          "(재현된 버그: 예전엔 상태 파일 손상이 '관측 기능을 켠 적 없음'으로"
          " 오해될 수 있었음)",
          "observation_run_state(현재 실행 상태)   = 상태확인_불가" in _cov10b2_text
          and "observation_run_state(현재 실행 상태)   = 계측_비활성" not in _cov10b2_text)
    check("10-15) MANIFEST에도 상태 스냅샷이 '있음'/'없음'이 아니라 '손상'으로 표시됨",
          "손상" in _manifest10b2_text)
finally:
    _os.chdir(_orig_cwd10b2)


# ── 10C. 주문일 커버리지에 쓰인 익일 관측의 번들 내 근거(지적 3번,
#    재현된 버그) — 9/19 접수 주문(OIDX1)을 9/20에 조회한 경우, 9/19
#    번들에 그 근거가 되는 raw 파일이 실제로 포함되는지 확인한다.
_tmpdir10c = tempfile.mkdtemp()
_orig_cwd10c = _os.getcwd()
try:
    _os.chdir(_tmpdir10c)
    Path("logs").mkdir()
    _day_10c = datetime(2026, 9, 19).date()
    _day_10c_compact = _day_10c.strftime("%Y%m%d")

    with (Path("logs") / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "timestamp": "2026-09-19T09:10:00", "symbol": "005930", "side": "BUY",
            "quantity": 10, "price": 10000, "accepted": "True", "message": "ok",
            "order_id": "OIDX1",
        })

    with (Path("logs") / "run_baseline.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=RUN_BASELINE_FIELDS)
        w.writeheader()
        w.writerow({
            "run_id": "run-10c", "started_at": "2026-09-19T08:30:00+09:00",
            "git_sha": "abc123", "git_dirty": "False",
            "effective_config_hash": "hash1", "is_mock": "False",
            "is_paper_trading": "False", "python_version": "3.11.0",
            "account_scope_id": "acct-10c",
        })

    _cross_day_obs = {
        "query_id": "q-oidx1", "started_at": "2026-09-20T09:00:00",
        "finished_at": "2026-09-20T09:00:01", "account_scope_id": "acct-10c",
        "env": "kiwoom_real", "symbol": "005930", "requested_order_id": "OIDX1",
        "side_context": "BUY_PENDING", "query_kind": "BUY_PENDING",
        "pending_age_sec_at_query": 31.0, "outcome": "success",
        "failure_stage": None, "error_repr": None,
        "psm_broker_status": "FILLED", "filled_price_parsed": 10000,
        "cntr_matches": [], "oso_matches": [], "cntr_match_count": 0, "oso_match_count": 0,
        "base_quantity_before_order": 0, "target_quantity_after_order": 10,
        "journal_linked": False, "order_accepted_at": "2026-09-19T09:10:00",
        "write_id": "w-oidx1", "restart_id": "r10c", "seq_no": 1, "schema_version": 1,
    }
    with (Path("logs") / "order_status_observations.jsonl").open("w", encoding="utf-8") as f:
        f.write(_json.dumps(_cross_day_obs) + "\n")

    _bundle_path_10c = _bundle.build(_day_10c, quiet=True)
    check("10-16) 익일 조회 관측이 있어도 번들 생성이 예외 없이 성공함",
          _bundle_path_10c is not None and _bundle_path_10c.exists())

    with _zipfile.ZipFile(_bundle_path_10c) as z:
        _names_10c = z.namelist()
        _extra_name_10c = f"raw/order_status_observations_orderday_extra_{_day_10c_compact}.jsonl"
        check("10-17) 주문일 커버리지의 익일 근거 raw 파일이 실제로 번들에 포함됨"
              "(재현된 버그: 예전엔 이 근거가 번들 어디에도 없었음)",
              _extra_name_10c in _names_10c)
        _extra_text_10c = z.read(_extra_name_10c).decode("utf-8") if _extra_name_10c in _names_10c else ""
        check("10-18) 그 근거 파일에 실제로 OIDX1 관측이 담겨 있음",
              "OIDX1" in _extra_text_10c)
        _main_raw_10c = f"raw/order_status_observations_{_day_10c_compact}.jsonl"
        _main_raw_text_10c = z.read(_main_raw_10c).decode("utf-8") if _main_raw_10c in _names_10c else ""
        check("10-19) 반면 조회일(9/19) raw 슬라이스에는 이 관측이 없음(9/20에 조회됐으므로)"
              " — 그래서 근거 파일이 별도로 필요했던 것",
              "OIDX1" not in _main_raw_text_10c)
        _cov10c_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        check("10-20) 커버리지 요약에 '조회일 raw 슬라이스에 없는 관측 = 1건'과 근거 파일"
              " 경로가 표시됨",
              "이 중 조회일 raw 슬라이스에 없는(다른 날짜에 조회된) 관측 = 1건" in _cov10c_text
              and f"order_status_observations_orderday_extra_{_day_10c_compact}.jsonl" in _cov10c_text)
        check("10-21) 커버리지 요약에 집계 기준 시각(전체 로그 재스캔 시각)이 기록됨"
              "(재현된 버그: 예전엔 언제 훑은 결과인지 번들만으로 알 수 없었음)",
              "집계 기준 시각(전체 로그 재스캔 시각) = " in _cov10c_text
              and "= 알 수 없음" not in _cov10c_text)
finally:
    _os.chdir(_orig_cwd10c)


# ── 11. 2026-09-21 4차 재검토(GPT 4차) 3개 지적 사항 재현 — 상태 처리만
#    한정(① 설정 활성인데 상태 파일 없음 → '상태확인_불가'로 구분,
#    ② 잘못된 UTF-8 상태 파일, ③ 시간대 포함 updated_at) — 세 사례 모두
#    실제 build() 경로로 검증한다. ──────────────────────────────────
from datetime import timezone as _timezone

# ── 11A. resolve_observation_configured_active() 단위 테스트 ──────
_tmpdir11a = tempfile.mkdtemp()
_repo_root_11 = Path(_os.getcwd())
_real_settings_yaml_text_11 = (_repo_root_11 / "config" / "settings.yaml").read_text(encoding="utf-8")
_active_settings_yaml_text_11 = _real_settings_yaml_text_11.replace(
    "  is_paper_trading: true\n",
    "  is_paper_trading: true\n  account_scope_id: acct-11a\n",
    1,
)
check("11-0) 사전조건: account_scope_id 주입이 정확히 1곳(broker 섹션)에서만"
      " 일어남(테스트 자체의 전제 확인)",
      _active_settings_yaml_text_11.count("account_scope_id: acct-11a") == 1)

_settings_path_11a_active = Path(_tmpdir11a) / "settings_active.yaml"
_settings_path_11a_active.write_text(_active_settings_yaml_text_11, encoding="utf-8")
check("11-1) 설정에서 account_scope_id가 채워져 있으면 True를 반환함",
      _bundle.resolve_observation_configured_active(_settings_path_11a_active) is True)

_settings_path_11a_blank = Path(_tmpdir11a) / "settings_blank.yaml"
_settings_path_11a_blank.write_text(_real_settings_yaml_text_11, encoding="utf-8")
check("11-2) 설정에 account_scope_id가 없으면(기본값 \"\") False를 반환함"
      "(명시적 비활성 확인 — 실제 저장소의 config/settings.yaml 그대로)",
      _bundle.resolve_observation_configured_active(_settings_path_11a_blank) is False)

_settings_path_11a_missing = Path(_tmpdir11a) / "does_not_exist.yaml"
check("11-3) 설정 파일 자체가 없으면 None을 반환함(확인 불가 — 호출부가 기존처럼"
      " 보수적으로 '계측_비활성' 기본값으로 폴백하게 함)",
      _bundle.resolve_observation_configured_active(_settings_path_11a_missing) is None)


# ── 11B. 설정이 활성인데 상태 파일이 없으면 '계측_비활성'이 아니라
#    '상태확인_불가'로 표시됨(지적 1번, 재현된 버그) — 실제 build()
#    경로로 검증 ───────────────────────────────────────────────────
_tmpdir11b = tempfile.mkdtemp()
_orig_cwd11b = _os.getcwd()
try:
    _os.chdir(_tmpdir11b)
    Path("logs").mkdir()
    Path("config").mkdir()
    (Path("config") / "settings.yaml").write_text(_active_settings_yaml_text_11, encoding="utf-8")
    _day921b = datetime(2026, 9, 21).date()
    (Path("logs") / "order_status_observations.jsonl").write_text("", encoding="utf-8")
    # 상태 파일(obs.status.json)은 의도적으로 만들지 않음 — 재시작 직후
    # 아직 첫 스냅샷을 쓰기 전이거나 파일이 삭제된 상황을 재현.

    _bundle_path_11b = _bundle.build(_day921b, quiet=True)
    check("11-4) 설정이 활성(account_scope_id 설정됨)인데 상태 파일이 없어도"
          " 번들 생성은 예외 없이 성공함",
          _bundle_path_11b is not None and _bundle_path_11b.exists())
    with _zipfile.ZipFile(_bundle_path_11b) as z:
        _cov11b_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        _manifest11b_text = z.read("MANIFEST.txt").decode("utf-8")
    check("11-5) 커버리지 요약에 '계측_비활성'이 아니라 '상태확인_불가'로 표시됨"
          "(재현된 버그: 예전엔 설정이 실제로 활성인데도 상태 파일 부재만으로"
          " '관측 기능을 켠 적이 없다'고 오판했음)",
          "observation_run_state(현재 실행 상태)   = 상태확인_불가" in _cov11b_text
          and "observation_run_state(현재 실행 상태)   = 계측_비활성" not in _cov11b_text)
    check("11-6) 커버리지 요약에 '설정(account_scope_id)은 활성화돼 있는 것으로"
          " 확인됐지만' 경고 문구가 표시되어 '계측 비활성'과 혼동되지 않게 함",
          "설정(account_scope_id)은 활성화돼 있는 것으로 확인됐지만" in _cov11b_text)
    check("11-7) MANIFEST에도 observation_status_snapshot이 '없음(설정상 활성화"
          " 확인됨 — 확인 필요)'로 표시됨",
          "없음(설정상 활성화 확인됨 — 확인 필요)" in _manifest11b_text)
finally:
    _os.chdir(_orig_cwd11b)


# ── 11C. 상태 파일에 잘못된 UTF-8 바이트가 있어도 번들 생성이 실패하지
#    않음(지적 2번, 재현된 버그) ────────────────────────────────────
_tmpdir11c_unit = tempfile.mkdtemp()
_status_path_11c_unit = Path(_tmpdir11c_unit) / "obs.status.json"
_status_path_11c_unit.write_bytes(b'{"clean_shutdown": null, "updated_at": "\xff\xfe bad utf8"}')
_utf8_result, _utf8_existed = _bundle._read_running_status(_status_path_11c_unit)
check("11-8) 잘못된 UTF-8 바이트가 있는 상태 파일을 읽어도 예외 없이 (None, True)를"
      " 반환함(재현된 버그: 예전엔 UnicodeDecodeError가 그대로 전파돼 번들 생성"
      " 자체가 실패했음)",
      _utf8_result is None and _utf8_existed is True)

_tmpdir11c = tempfile.mkdtemp()
_orig_cwd11c = _os.getcwd()
try:
    _os.chdir(_tmpdir11c)
    Path("logs").mkdir()
    _day921c = datetime(2026, 9, 21).date()
    _obs_path_11c = Path("logs") / "order_status_observations.jsonl"
    _obs_path_11c.write_text("", encoding="utf-8")
    _status_path_11c = _bundle.status_path_for(_obs_path_11c)
    _status_path_11c.write_bytes(b'{"clean_shutdown": null, "updated_at": "\xff\xfe bad utf8"}')

    _bundle_path_11c = _bundle.build(_day921c, quiet=True)
    check("11-9) 상태 파일에 잘못된 UTF-8 바이트가 있어도 번들 생성 자체는 예외 없이"
          " 성공함(재현된 버그: 예전엔 UnicodeDecodeError로 번들 생성이 통째로"
          " 실패했음)",
          _bundle_path_11c is not None and _bundle_path_11c.exists())
    with _zipfile.ZipFile(_bundle_path_11c) as z:
        _cov11c_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
        _manifest11c_text = z.read("MANIFEST.txt").decode("utf-8")
    check("11-10) 커버리지 요약에 '상태확인_불가'로 표시됨(손상을 '계측_비활성'과"
          " 혼동하지 않음)",
          "observation_run_state(현재 실행 상태)   = 상태확인_불가" in _cov11c_text)
    check("11-11) MANIFEST에도 '손상'으로 표시됨", "손상" in _manifest11c_text)
finally:
    _os.chdir(_orig_cwd11c)


# ── 11D. updated_at에 시간대(+09:00)가 포함돼도 TypeError 없이 판정됨
#    (지적 3번, 재현된 버그) ────────────────────────────────────────
_aware_now_iso_11d = datetime.now(_timezone(timedelta(hours=9))).isoformat()
check("11-12) clean_shutdown=None이고 updated_at이 시간대(+09:00) 포함 최근 시각이면"
      " TypeError 없이 '실행_중'으로 판정됨(재현된 버그: 예전엔 naive"
      " datetime.now()와의 뺄셈에서 TypeError가 나 번들 생성 자체가 실패했음)",
      _bundle._classify_run_state(
          {"clean_shutdown": None, "updated_at": _aware_now_iso_11d}
      ) == _bundle.RUN_STATE_RUNNING)

_stale_aware_iso_11d = (
    datetime.now(_timezone(timedelta(hours=9))) - timedelta(seconds=999)
).isoformat()
check("11-13) 같은 시간대 포함 형식이라도 오래된 시각이면 '종료_확인_불가'로 판정됨"
      "(시간대 처리가 모든 경우를 무조건 '실행_중'으로 통과시키는 게 아님을 확인)",
      _bundle._classify_run_state(
          {"clean_shutdown": None, "updated_at": _stale_aware_iso_11d}
      ) == _bundle.RUN_STATE_UNCLEAR_SHUTDOWN)

_tmpdir11d = tempfile.mkdtemp()
_orig_cwd11d = _os.getcwd()
try:
    _os.chdir(_tmpdir11d)
    Path("logs").mkdir()
    _day921d = datetime(2026, 9, 21).date()
    _obs_path_11d = Path("logs") / "order_status_observations.jsonl"
    _obs_path_11d.write_text("", encoding="utf-8")
    _status_path_11d = _bundle.status_path_for(_obs_path_11d)
    _status_path_11d.write_text(
        _json.dumps({
            "restart_id": "r11d", "clean_shutdown": None,
            "updated_at": datetime.now(_timezone(timedelta(hours=9))).isoformat(),
            "dropped_count": 0, "fsync_unconfirmed_count": 0,
            "queue_full_dropped_count": 0, "file_healthy": True,
        }),
        encoding="utf-8",
    )

    _bundle_path_11d = _bundle.build(_day921d, quiet=True)
    check("11-14) updated_at에 시간대가 포함돼 있어도 번들 생성 자체는 예외 없이"
          " 성공함(재현된 버그: 예전엔 TypeError로 번들 생성이 통째로 실패했음)",
          _bundle_path_11d is not None and _bundle_path_11d.exists())
    with _zipfile.ZipFile(_bundle_path_11d) as z:
        _cov11d_text = z.read("metadata/order_status_coverage.txt").decode("utf-8")
    check("11-15) 커버리지 요약에 '실행_중'으로 정상 판정됨(최근 시각이므로) —"
          " TypeError를 피하려고 무조건 '상태확인_불가'로 뭉뚱그리지 않았음을 확인",
          "observation_run_state(현재 실행 상태)   = 실행_중" in _cov11d_text)
finally:
    _os.chdir(_orig_cwd11d)


print(f"\n총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
