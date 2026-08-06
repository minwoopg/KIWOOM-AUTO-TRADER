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

# ══════════════════════════════════════════════════════════════
# 1I.1: 번들 export 정확성·보안·품질 (GPT 코드리뷰)
# ══════════════════════════════════════════════════════════════
import zipfile
import export_daily_bundle as B
from domain.shadow_signature import analysis_dedup_key, entry_quality_shadow_key
from infra.storage.logger import _entry_quality_shadow_key

ts_src = open("domain/service/trading_service.py", encoding="utf-8").read()


def _mkday(root: Path, name: str, header: list[str], rows: list[dict]) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    with (root / "logs" / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _run_export(root: Path, day: str):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        B.LOGS_DIR, B.REPORTS_DIR, B.EXPORTS_DIR = Path("logs"), Path("reports"), Path("exports")
        r = B.build(date(*map(int, day.split("-"))), quiet=True)
        return r.resolve() if r is not None else None
    finally:
        os.chdir(cwd)


def _zip_texts(zpath: Path) -> dict[str, str]:
    with zipfile.ZipFile(zpath) as z:
        return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


# ── A. 날짜 slicing ────────────────────────────────────────────
root = Path(tempfile.mkdtemp())
_mkday(root, "entry_watch_shadow.csv", ["trigger_at", "symbol", "trigger_type"], [
    {"trigger_at": "2026-08-05T10:00:00", "symbol": "005930", "trigger_type": "DROP"},
    {"trigger_at": "2026-08-06T10:00:00", "symbol": "047040", "trigger_type": "DROP"},
])
_mkday(root, "signal_log.csv", ["timestamp", "symbol"], [
    {"timestamp": "2026-08-05T09:30:00", "symbol": "111111"},
    {"timestamp": "2026-08-06T09:30:00", "symbol": "222222"},
])
_mkday(root, "entry_quality_shadow.csv", ["timestamp", "symbol"], [
    {"timestamp": "2026-08-05T09:30:00", "symbol": "333333"},
    {"timestamp": "2026-08-06T09:30:00", "symbol": "444444"},
])
_mkday(root, "trades.csv", ["timestamp", "symbol", "side"], [
    {"timestamp": "2026-08-05T09:30:00", "symbol": "555555", "side": "BUY"},
    {"timestamp": "2026-08-06T09:30:00", "symbol": "666666", "side": "BUY"},
])
# 시간 컬럼이 전혀 없는 CSV
_mkday(root, "position_lifecycle.csv", ["symbol", "state"], [
    {"symbol": "999999", "state": "OPEN"},
])

z = _run_export(root, "2026-08-06")
texts = _zip_texts(z)
ew = texts["raw/entry_watch_shadow_20260806.csv"]
check("A-1) entry_watch가 trigger_at 기준으로 8/6 행만 포함",
      "047040" in ew and "005930" not in ew)
check("A-2) 시간 컬럼 없는 CSV는 전체 복사되지 않고 번들에서 제외",
      not any("position_lifecycle" in n for n in texts))
check("A-3) MANIFEST에 SCHEMA_ERROR가 기록됨",
      "position_lifecycle.csv" in texts["MANIFEST.txt"]
      and "SCHEMA_ERROR" in texts["MANIFEST.txt"])
check("A-4) signal_log에 전일 행이 없음",
      "222222" in texts["raw/signal_log_20260806.csv"]
      and "111111" not in texts["raw/signal_log_20260806.csv"])
check("A-5) entry_quality_shadow에 전일 행이 없음",
      "444444" in texts["raw/entry_quality_shadow_20260806.csv"]
      and "333333" not in texts["raw/entry_quality_shadow_20260806.csv"])
check("A-6) trades에 전일 행이 없음",
      "666666" in texts["raw/trades_20260806.csv"]
      and "555555" not in texts["raw/trades_20260806.csv"])
check("A-7) 스키마 오류가 나도 다른 파일 export는 계속 진행됨",
      "raw/signal_log_20260806.csv" in texts and "raw/trades_20260806.csv" in texts)


# ── B. 로그 보안 ───────────────────────────────────────────────
log_lines = [
    "2026-08-06 09:00:01 | WARNING | 토큰 갱신 authorization=Bearer SECRET_TOKEN_123",
    "2026-08-06 09:00:02 | ERROR | 주문실패 account_no=1234567890 잔고부족",
    "2026-08-06 09:00:03 | WARNING | 일반 경고 NOTHING_SPECIAL_HERE",
    "2026-08-06 09:00:04 | INFO | [COND_STATUS] seq1=1종목 | final=5종목",
    "2026-08-06 09:00:05 | INFO | [WS] 연결 성공 access_token: abcdefghijk",
    "2026-08-06 09:00:06 | INFO | [COND] 계좌 12345678-01 편입: 005930",
    "2026-08-06 09:00:07 | INFO | [SESSION_SHADOW] api_key=KEY_ABC secret=S3CR3T password=pw123",
]
(root / "logs" / "app.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
z = _run_export(root, "2026-08-06")
texts = _zip_texts(z)
blob = "".join(texts.values())
applog = texts["raw/app_analysis_20260806.log"]

check("B-1) allowlist 밖 WARNING의 토큰 원문이 번들에 없음", "SECRET_TOKEN_123" not in blob)
check("B-2) allowlist 밖 ERROR의 계좌번호가 번들에 없음", "1234567890" not in blob)
check("B-3) allowlist 밖 일반 WARNING 줄 자체가 포함되지 않음",
      "NOTHING_SPECIAL_HERE" not in blob)
check("B-4) 허용 태그 [COND_STATUS] 줄은 포함됨", "[COND_STATUS]" in applog)
check("B-5) access_token 값이 마스킹됨",
      "abcdefghijk" not in blob and "access_token" in applog)
check("B-6) 8-2 형식 계좌번호가 마스킹됨",
      "12345678-01" not in blob and "005930" in applog)
check("B-7) api_key / secret / password 값이 전부 마스킹됨",
      "KEY_ABC" not in blob and "S3CR3T" not in blob and "pw123" not in blob)
check("B-8) 종목코드(6자리)는 마스킹되지 않음", "005930" in applog)
check("B-9) mask()가 Bearer 토큰을 직접 가림",
      "SECRET_TOKEN_123" not in B.mask("authorization=Bearer SECRET_TOKEN_123"))
check("B-10) mask()가 10자리 계좌번호를 가림",
      "1234567890" not in B.mask("account_no=1234567890"))
check("B-11) allowlist에 인증·잔고 태그가 없음",
      not any(k in t.upper() for t in B.LOG_TAGS
              for k in ("TOKEN", "BALANCE", "AUTH", "ORDER_RESP")))
check("B-12) 1F 폐기된 [COND_SWING]이 allowlist에서 제거됨",
      "[COND_SWING]" not in B.LOG_TAGS)
check("B-13) [MIN_STALE]이 allowlist에 추가됨", "[MIN_STALE]" in B.LOG_TAGS)


# ── C. 중복 판정 ───────────────────────────────────────────────
def _shadow_row(**kw):
    base = {f: "False" for f in
            ["would_block_macd_dead_min_score5", "would_block_macd_above_signal_required",
             "would_block_pr_only_rolling_vwap", "would_block_c_or_pr_rolling_vwap",
             "would_block_pullback_condition_rolling_vwap",
             "would_block_pr_or_pullback_condition_rolling_vwap",
             "would_block_pr_only_session_vwap", "would_block_c_or_pr_session_vwap",
             "would_block_pullback_condition_session_vwap",
             "would_block_pr_or_pullback_condition_session_vwap",
             "order_attempted", "order_accepted", "condition_source_reliable"]}
    base.update({"symbol": "005930", "latest_bar_timestamp": "T1",
                 "detected_patterns": "A/B", "score": "5",
                 "final_decision": "BUY", "order_block_reason": ""})
    base.update(kw)
    return base


a = _shadow_row()
b = _shadow_row(would_block_pr_only_rolling_vwap="True")
check("C-1) 같은 분봉에서 would_block False→True는 별도 행(중복 아님)",
      analysis_dedup_key(a) != analysis_dedup_key(b))
c = _shadow_row(order_accepted="True")
check("C-2) 같은 분봉에서 order_accepted False→True는 별도 행",
      analysis_dedup_key(a) != analysis_dedup_key(c))
d = _shadow_row(final_decision="BLOCKED")
check("C-3) final_decision 변화도 별도 행",
      analysis_dedup_key(a) != analysis_dedup_key(d))
e = _shadow_row(condition_source_reliable="True")
check("C-4) condition_source_reliable 변화도 별도 행",
      analysis_dedup_key(a) != analysis_dedup_key(e))
check("C-5) 완전히 동일한 두 행은 중복 1건으로 판정",
      analysis_dedup_key(a) == analysis_dedup_key(_shadow_row()))
check("C-6) analyzer와 logger가 동일한 signature 함수를 공유",
      _entry_quality_shadow_key is entry_quality_shadow_key)
check("C-7) analysis 키가 logger 키를 포함(확장 관계)",
      analysis_dedup_key(a)[:len(entry_quality_shadow_key(a))]
      == entry_quality_shadow_key(a))
check("C-8) bool True와 문자열 'True'가 같은 키로 정규화됨",
      analysis_dedup_key(_shadow_row(order_accepted=True))
      == analysis_dedup_key(_shadow_row(order_accepted="True")))


# ── D. session quality ─────────────────────────────────────────
def _sess_report(rows):
    buf = []
    A.section_vwap(buf.append, rows)
    return "\n".join(buf)


ready_rows = [_shadow_row(session_metrics_ready="True",
                          session_readiness_reason="COMPLETE_FROM_OPEN")]
partial_rows = [_shadow_row(session_metrics_ready="False",
                            session_readiness_reason="PARTIAL_SESSION")]
r_ready = _sess_report(ready_rows)
r_partial = _sess_report(partial_rows)
r_mixed = _sess_report(ready_rows * 3 + partial_rows)

check("D-1) COMPLETE_FROM_OPEN이 ready=True로 집계됨",
      "COMPLETE_FROM_OPEN (ready=True) : 1건" in r_ready)
check("D-2) PARTIAL_SESSION이 ready=False로 집계됨",
      "PARTIAL_SESSION    (ready=False): 1건" in r_partial)
check("D-3) 섞여 있을 때 ready 비율이 정확",
      "COMPLETE_FROM_OPEN (ready=True) : 3건" in r_mixed
      and "PARTIAL_SESSION    (ready=False): 1건" in r_mixed)
check("D-4) ready=False만 있는 날은 session 게이트 해석 불가를 명시",
      "session 게이트 성과 해석 불가" in r_partial)
check("D-5) ready=True가 있으면 해석 불가 경고가 나오지 않음",
      "session 게이트 성과 해석 불가" not in r_ready)
check("D-6) '한 번도 true 없음' 같은 단정적 문구를 쓰지 않음",
      "한 번도" not in r_partial)
check("D-7) 다른 날에는 ready=True가 정상 발생함을 안내",
      "다른 날에는 ready=True가 정상적으로 발생" in r_partial)
check("D-8) ready 기준 완화(CAPPED_BY_API 등)를 하지 않음",
      "CAPPED_BY_API" not in open("domain/market_regime/session_metrics.py",
                                  encoding="utf-8").read())


# ── E. ZIP 안정성 ──────────────────────────────────────────────
check("E-1) 최종 ZIP이 무결성 검사를 통과",
      zipfile.ZipFile(z).testzip() is None)

# 락이 잡혀 있으면 기존 ZIP을 건드리지 않고 물러남
before = z.read_bytes()
lock = root / "exports" / "bundle_20260806.lock"
lock.write_text("999999")
result = _run_export(root, "2026-08-06")
check("E-2) 동시 실행 시 하나만 진행되고 나머지는 None 반환", result is None)
check("E-3) 락 충돌 시 기존 최종 ZIP이 그대로 유지됨", z.read_bytes() == before)
lock.unlink()

# 생성 중 예외 → 기존 ZIP 유지 + tmp 정리
orig_q = B.build_collection_quality
B.build_collection_quality = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    _run_export(root, "2026-08-06")
    raised = False
except RuntimeError:
    raised = True
finally:
    B.build_collection_quality = orig_q
check("E-4) 생성 중 예외가 전파됨", raised)
check("E-5) 예외 후에도 기존 최종 ZIP이 유지됨", z.exists() and z.read_bytes() == before)
check("E-6) 예외 후 .tmp 파일이 정리됨",
      not (root / "exports" / "bundle_20260806.zip.tmp").exists())
check("E-7) 예외 후 락 파일이 해제됨", not lock.exists())
check("E-8) 예외 후 임시 작업 디렉터리가 남지 않음",
      not any(p.is_dir() and p.name.startswith("bundle_2026")
              for p in (root / "exports").iterdir()))


# ── F. 파이프라인 연결 ─────────────────────────────────────────
check("F-1) 파이프라인이 번들 export를 호출함",
      "self._export_daily_bundle_today(now.date())" in ts_src)
check("F-2) 번들 export가 모든 리포트 생성 뒤에 실행됨",
      ts_src.index("_export_daily_bundle_today(now.date())")
      > ts_src.index("_run_shadow_analysis_today(now.date())"))

print()
print(f"[최종] 총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
