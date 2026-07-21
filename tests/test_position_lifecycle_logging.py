# -*- coding: utf-8 -*-
"""
포지션 상태전이 CSV 로깅(position_lifecycle.csv) 검증 (2026-07-22)

PositionStateMachine에 PositionLifecycleLogger를 주입하면 모든 이벤트
(요청/결과/동기화/불변조건검사)가 CSV로 기록되는지, logger=None이면
기존과 동일하게 아무것도 기록되지 않는지(하위호환)를 확인한다.
"""
from __future__ import annotations

import csv
import sys
import tempfile

sys.path.insert(0, ".")

from domain.position.lifecycle import PositionLifecycle, PositionStateMachine
from infra.storage.logger import PositionLifecycleLogger


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

# ── 1) logger=None이면 기존과 동일하게 아무것도 기록 안 함 ────────
psm = PositionStateMachine(logger=None)
psm.on_buy_requested(symbol, 100, "order-1")
psm.on_buy_result(symbol, accepted=True, broker_quantity=100)
check("1) logger=None이면 예외 없이 정상 동작 (하위호환)",
      psm.get(symbol).lifecycle == PositionLifecycle.OPEN)

# ── 2) logger 주입 시 매수 흐름이 CSV에 기록되는지 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = f"{tmpdir}/position_lifecycle.csv"
    logger = PositionLifecycleLogger(log_path)
    psm = PositionStateMachine(logger=logger)

    psm.on_buy_requested(symbol, 100, "order-1")
    psm.on_buy_result(symbol, accepted=True, broker_quantity=100)

    with open(log_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    check("2) 매수요청+결과 2개 이벤트가 기록됨", len(rows) == 2)
    check("   첫 행 event=BUY_REQUESTED", rows[0]["event"] == "BUY_REQUESTED")
    check("   첫 행 from=FLAT, to=BUY_PENDING", rows[0]["from_lifecycle"] == "FLAT" and rows[0]["to_lifecycle"] == "BUY_PENDING")
    check("   둘째 행 event=BUY_RESULT", rows[1]["event"] == "BUY_RESULT")
    check("   둘째 행 from=BUY_PENDING, to=OPEN", rows[1]["from_lifecycle"] == "BUY_PENDING" and rows[1]["to_lifecycle"] == "OPEN")
    check("   둘째 행 detail=FILLED", rows[1]["detail"] == "FILLED")

# ── 3) 매도 부분체결이 CSV에 정확히 기록되는지 ────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = f"{tmpdir}/position_lifecycle.csv"
    logger = PositionLifecycleLogger(log_path)
    psm = PositionStateMachine(logger=logger)
    psm.sync_from_broker(symbol, 100)  # OPEN 상태로 세팅 (broker_qty>0이므로 SYNC 이벤트 1개 기록)
    psm.on_sell_requested(symbol, 100, "order-2")
    psm.on_sell_result(symbol, accepted=True, broker_quantity=40)  # 부분체결

    with open(log_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    sell_rows = [r for r in rows if r["event"] in ("SELL_REQUESTED", "SELL_RESULT")]
    check("3) 매도요청+결과 이벤트 기록됨", len(sell_rows) == 2)
    result_row = sell_rows[-1]
    check("   부분체결 detail=PARTIAL_FILL", result_row["detail"] == "PARTIAL_FILL")
    check("   부분체결 후 known_quantity=40으로 기록", result_row["known_quantity"] == "40")
    check("   부분체결 후 to_lifecycle=OPEN(잔여수량 유지)", result_row["to_lifecycle"] == "OPEN")

# ── 4) API 미반영(SELL_PENDING 유지)도 기록되는지 (중복 폴링 확인용) ──
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = f"{tmpdir}/position_lifecycle.csv"
    logger = PositionLifecycleLogger(log_path)
    psm = PositionStateMachine(logger=logger)
    psm.sync_from_broker(symbol, 100)
    psm.on_sell_requested(symbol, 100, "order-3")
    psm.on_sell_result(symbol, accepted=True, broker_quantity=100)  # 아직 미반영
    psm.on_sell_result(symbol, accepted=True, broker_quantity=100)  # 다음 폴링도 미반영

    with open(log_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pending_rows = [r for r in rows if r.get("detail") == "PENDING_STILL_UNCONFIRMED"]
    check("4) 미반영 상태가 폴링마다 반복 기록됨(2회)", len(pending_rows) == 2)

# ── 5) 불변조건 위반이 CSV에 기록되는지 ───────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = f"{tmpdir}/position_lifecycle.csv"
    logger = PositionLifecycleLogger(log_path)
    psm = PositionStateMachine(logger=logger)
    # lifecycle 기본값 FLAT인 채로 잔고만 있는 이상 상황
    violation = psm.check_invariant(symbol, broker_quantity=201)

    with open(log_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    check("5) 불변조건 위반이 CSV에 기록됨", len(rows) == 1)
    check("   event=INVARIANT_VIOLATION", rows[0]["event"] == "INVARIANT_VIOLATION")
    check("   detail에 위반 메시지 포함", "POSITION_STATE_MISMATCH" in rows[0]["detail"])

# ── 6) 정상 상태(위반 없음)는 기록 안 됨 ──────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = f"{tmpdir}/position_lifecycle.csv"
    logger = PositionLifecycleLogger(log_path)
    psm = PositionStateMachine(logger=logger)
    psm.sync_from_broker(symbol, 100)
    before_count = 1  # sync_from_broker의 SYNC 이벤트 1개
    violation = psm.check_invariant(symbol, broker_quantity=100)  # OPEN 상태에서 잔고 일치 -> 정상

    with open(log_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    check("6) 정상 상태 불변조건 검사는 INVARIANT_VIOLATION 기록 안 함(SYNC만 1건)",
          len(rows) == before_count and violation is None)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
