# -*- coding: utf-8 -*-
"""analyze_shadow.py 검증 (2026-08-06, 1H단계)

읽기 전용 리포트지만, 잘못된 집계는 잘못된 enforce 판단으로
이어지므로 핵심 계산을 고정한다. 특히:
  - 행 수가 아니라 (종목, 분봉) 유니크로 세는지
  - 기존 규칙으로 이미 차단된 후보를 분모에서 분리하는지
  - 데이터가 비어 있어도 죽지 않는지
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

import analyze_shadow as A

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


# ── 유틸 동작 ───────────────────────────────────────────────────
check("1-1) truthy가 CSV의 문자열 True/False를 정확히 해석",
      A.truthy("True") and A.truthy("true") and not A.truthy("False")
      and not A.truthy("") and not A.truthy(None))
check("1-2) filled가 공백만 있는 값을 미기재로 취급",
      A.filled("x") and not A.filled("") and not A.filled("   ") and not A.filled(None))
check("1-3) uniq_key가 (종목, 분봉) 조합을 반환",
      A.uniq_key({"symbol": "005930", "latest_bar_timestamp": "20260805093000"})
      == ("005930", "20260805093000"))
check("1-4) pct가 0 분모에서 죽지 않음", A.pct(0, 0).strip() == "n/a")


# ── 중복집계 함정 회피 ──────────────────────────────────────────
# 같은 종목·같은 분봉이 4번 폴링된 상황 + 다른 분봉 1건
rows = [{"symbol": "005930", "latest_bar_timestamp": "T1"} for _ in range(4)]
rows.append({"symbol": "005930", "latest_bar_timestamp": "T2"})
check("2-1) 5행이지만 유니크는 2건으로 집계됨",
      len({A.uniq_key(r) for r in rows}) == 2)


# ── 리포트 생성 (임시 데이터) ───────────────────────────────────
SIG_COLS = ["timestamp", "symbol", "latest_bar_timestamp", "macd", "rolling_vwap",
            "session_metrics_ready", "legacy_buy_candidate", "order_block_reason",
            "would_block_existing_chasing_gate", "would_block_macd_dead_min_score5",
            "would_block_macd_above_signal_required",
            "chasing_overheated_condition", "chasing_overheated_applies"]


def sig_row(**kw):
    base = {c: "" for c in SIG_COLS}
    base.update(kw)
    return base


tmp = tempfile.mkdtemp()
sig_path = Path(tmp) / "signal_log.csv"
with sig_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=SIG_COLS)
    w.writeheader()
    # 같은 (종목,분봉) 3행 — 기존 규칙 통과, 하드 게이트 차단
    for _ in range(3):
        w.writerow(sig_row(timestamp="2026-08-05T09:30:00", symbol="005930",
                           latest_bar_timestamp="T1", macd="1.0", legacy_buy_candidate="True",
                           would_block_macd_above_signal_required="True"))
    # 다른 분봉 1행 — 기존 규칙으로 이미 차단됨
    w.writerow(sig_row(timestamp="2026-08-05T09:31:00", symbol="005930",
                       latest_bar_timestamp="T2", macd="1.0", legacy_buy_candidate="True",
                       order_block_reason="DAILY_ENTRY_LIMIT",
                       would_block_macd_above_signal_required="True"))
    # BUY 후보 아님
    w.writerow(sig_row(timestamp="2026-08-05T09:32:00", symbol="000660",
                       latest_bar_timestamp="T3", macd="1.0", legacy_buy_candidate="False"))
    # 날짜 범위 밖 — 걸러져야 함
    w.writerow(sig_row(timestamp="2026-08-04T09:30:00", symbol="111111",
                       latest_bar_timestamp="T9", macd="1.0", legacy_buy_candidate="True"))

orig_sig, orig_sh, orig_rep = A.SIGNAL_LOG, A.SHADOW_LOG, A.REPORTS_DIR
A.SIGNAL_LOG = sig_path
A.SHADOW_LOG = Path(tmp) / "entry_quality_shadow.csv"   # 존재하지 않음
A.REPORTS_DIR = Path(tmp) / "reports"

report = A.build_report(date(2026, 8, 5), date(2026, 8, 5))

check("3-1) 날짜 범위 밖 행이 제외됨(8/4 데이터 미포함)",
      "111111" not in report)
check("3-2) 총 4행이 로드됨", "4행" in report)
check("3-3) BUY 후보를 유니크 2건으로 집계", "유니크 2건" in report)
check("3-4) 기존 규칙 통과분을 1건으로 분리", "1건  ← 새 게이트 평가는 이쪽 기준" in report)
check("3-5) 하드 게이트 차단이 전체 2건으로 집계", "2건" in report)
check("3-6) VWAP 미수집 경고가 표시됨", "VWAP shadow 미수집" in report)
check("3-7) shadow 파일 부재를 안내함", "entry_quality_shadow.csv 파일이 없습니다" in report)
check("3-8) 성과 계산 미포함이 명시됨", "성과 계산은 미포함" in report)

# 빈 데이터에서도 죽지 않아야 함
empty = Path(tmp) / "empty.csv"
empty.write_text("timestamp,symbol\n", encoding="utf-8")
A.SIGNAL_LOG = empty
report_empty = A.build_report(date(2026, 8, 5), date(2026, 8, 5))
check("4-1) 빈 signal_log에서도 예외 없이 리포트 생성",
      "해당 기간 데이터가 없습니다" in report_empty)

# 파일 자체가 없어도 죽지 않아야 함
A.SIGNAL_LOG = Path(tmp) / "nope.csv"
report_none = A.build_report(date(2026, 8, 5), date(2026, 8, 5))
check("4-2) 파일이 아예 없어도 예외 없이 리포트 생성", len(report_none) > 0)

A.SIGNAL_LOG, A.SHADOW_LOG, A.REPORTS_DIR = orig_sig, orig_sh, orig_rep


# ── 장 마감 파이프라인 연결 확인 ────────────────────────────────
ts_src = open("domain/service/trading_service.py", encoding="utf-8").read()
check("5-1) _run_end_of_day_tasks가 shadow 분석을 호출함",
      "self._run_shadow_analysis_today(now.date())" in ts_src)
check("5-2) _run_shadow_analysis_today가 정의돼 있음",
      "def _run_shadow_analysis_today" in ts_src)
check("5-3) analyze_shadow.py를 subprocess로 실행함",
      '"analyze_shadow.py"' in ts_src)
check("5-4) 분석 실패가 매매를 중단시키지 않도록 예외를 삼킴",
      "[ANALYSIS] shadow 관측 분석 오류" in ts_src)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
