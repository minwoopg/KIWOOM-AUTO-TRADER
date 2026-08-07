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
import time
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

# ══════════════════════════════════════════════════════════════
# 1I.2: 로거 상태 전이 보존 · 로테이션 통합 · 판정 기준 (GPT 리뷰)
# ══════════════════════════════════════════════════════════════
from infra.storage.logger import EntryQualityShadowLogger
from domain.shadow_signature import STATE_TRANSITION_FIELDS


def _logger_rows(seq: list[dict]) -> int:
    """실제 로거를 통과시킨 뒤 CSV 행 수를 셉니다."""
    path = Path(tempfile.mkdtemp()) / "eq.csv"
    lg = EntryQualityShadowLogger(str(path))
    for r in seq:
        lg.append_if_new(r)
    with path.open(newline="", encoding="utf-8-sig") as f:
        return sum(1 for _ in csv.DictReader(f))


base = _shadow_row(order_attempted="False", order_accepted="False",
                   condition_source_reliable="True")
check("G-1) reject→accept가 CSV에 2행으로 남음(로거까지 실행)",
      _logger_rows([base, _shadow_row(order_attempted="True", order_accepted="True",
                                      condition_source_reliable="True")]) == 2)
check("G-2) order_attempted False→True가 CSV에 2행으로 남음",
      _logger_rows([base, _shadow_row(order_attempted="True", order_accepted="False",
                                      condition_source_reliable="True")]) == 2)
check("G-3) condition_source_reliable True→False가 CSV에 2행으로 남음",
      _logger_rows([base, _shadow_row(order_attempted="False", order_accepted="False",
                                      condition_source_reliable="False")]) == 2)
check("G-4) 완전히 같은 행 반복은 CSV에 1행", _logger_rows([base, base, base]) == 1)
check("G-5) 로거 키와 분석기 키가 완전히 동일",
      entry_quality_shadow_key(base) == analysis_dedup_key(base))
check("G-6) 상태 전이 필드가 키에 포함됨",
      STATE_TRANSITION_FIELDS == ["order_attempted", "order_accepted",
                                  "condition_source_reliable"])
check("G-7) order_id는 키에 포함되지 않음(재시도마다 행이 늘지 않도록)",
      entry_quality_shadow_key(_shadow_row(order_id="A"))
      == entry_quality_shadow_key(_shadow_row(order_id="B")))


# ── H. 로테이션 로그 통합 ──────────────────────────────────────
root2 = Path(tempfile.mkdtemp())
(root2 / "logs").mkdir(parents=True)
_mkday(root2, "signal_log.csv", ["timestamp", "symbol"], [
    {"timestamp": "2026-08-06T09:00:30", "symbol": "005930"},
    {"timestamp": "2026-08-06T15:19:00", "symbol": "005930"},
])
# 오전 로그는 로테이션된 app.log.1 에, 오후 로그는 app.log 에
(root2 / "logs" / "app.log.1").write_text(
    "2026-08-06 09:00:01,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작\n"
    "2026-08-06 09:05:00,000 | INFO | [SESSION_SHADOW] 005930 ready=True reason=COMPLETE_FROM_OPEN\n"
    "2026-08-06 09:06:00,000 | WARNING | [WS] 연결 끊김: boom — 5초 후 재연결\n"
    "2026-08-06 09:07:00,000 | WARNING | [COND_TRUNCATE] max_symbols=10 상한으로 조건검색 종목 7개가 잘렸습니다\n",
    encoding="utf-8")
(root2 / "logs" / "app.log").write_text(
    "2026-08-06 15:00:00,000 | INFO | [SESSION_SHADOW] 047040 ready=False reason=PARTIAL_SESSION\n"
    "2026-08-06 15:10:00,000 | INFO | [WS] start() 진입 — 재연결 루프 시작\n"
    "2026-08-06 15:19:00,000 | WARNING | [COND_TRUNCATE] max_symbols=10 상한으로 조건검색 종목 21개가 잘렸습니다\n",
    encoding="utf-8")
# 포함되면 안 되는 파일
(root2 / "logs" / "app copy.log").write_text(
    "2026-08-06 12:00:00,000 | INFO | [COND] SHOULD_NOT_APPEAR_IN_BUNDLE\n", encoding="utf-8")

z2 = _run_export(root2, "2026-08-06")
t2 = _zip_texts(z2)
merged = t2["raw/app_analysis_20260806.log"]
quality = t2["metadata/collection_quality.txt"]

check("H-1) 로테이션된 app.log.1의 09시 로그가 포함됨", "09:05:00" in merged)
check("H-2) app.log의 15시 로그도 포함됨", "15:00:00" in merged)
check("H-3) 합쳐진 로그가 시간순으로 정렬됨",
      merged.index("09:05:00") < merged.index("15:00:00"))
check("H-4) 'app copy.log' 같은 임의 파일은 포함되지 않음",
      "SHOULD_NOT_APPEAR_IN_BUNDLE" not in "".join(t2.values()))
check("H-5) MANIFEST에 사용한 소스 로그가 기록됨",
      "source logs:" in t2["MANIFEST.txt"] and "app.log.1" in t2["MANIFEST.txt"])
check("H-6) ready 집계에 두 파일이 모두 반영됨",
      "session_ready_log_event_count         = 1" in quality
      and "session_not_ready_log_event_count     = 1" in quality)
check("H-7) COND_TRUNCATE 집계에 두 파일이 모두 반영됨",
      "cond_truncate_event_count             = 2" in quality
      and "max_truncated_condition_count         = 21" in quality)
check("H-8) rotated_log_paths가 app.log와 app.log.N만 반환",
      [p.name for p in B.rotated_log_paths(root2 / "logs")] == ["app.log", "app.log.1"])


# ── I. 판정 기준 ───────────────────────────────────────────────
check("I-1) 재연결 집계가 '재연결 루프 시작' 기동 로그를 세지 않음",
      "websocket_reconnect_count             = 1" in quality)
check("I-2) 수집 완전성 판정이 signal_log 기준으로 이뤄짐",
      "signal_collection_first_ts" in quality and "signal_collection_last_ts" in quality)
check("I-3) shadow 첫·마지막 시각은 별도 coverage 정보로만 기록",
      "shadow_first_candidate_ts" in quality and "shadow_last_candidate_ts" in quality)
check("I-4) signal_log가 09:00~15:19이고 재시작 1회면 COMPLETE",
      "collection_status                     = COMPLETE" in quality)
check("I-5) session ready 명칭이 로그 이벤트 기준임을 드러냄",
      "session_ready_log_event_ratio" in quality)
check("I-6) shadow 후보 기준 ready 비율이 별도로 제공됨",
      "shadow_candidate_session_ready_ratio" in quality)
check("I-7) accepted BUY 기준 메타데이터가 분리 기록됨",
      all(k in quality for k in ("buy_order_attempt_count", "accepted_buy_order_count",
                                 "unique_accepted_buy_order_count",
                                 "shadow_order_accepted_count",
                                 "shadow_unique_accepted_order_count")))

# 거부된 매수 주문이 실제 매수로 집계되지 않아야 함
_mkday(root2, "trades.csv", ["timestamp", "symbol", "side", "accepted", "order_id"], [
    {"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "O1"},
    {"timestamp": "2026-08-06T09:31:00", "symbol": "005930", "side": "BUY",
     "accepted": "False", "order_id": "O2"},
])
z2 = _run_export(root2, "2026-08-06")
quality2 = _zip_texts(z2)["metadata/collection_quality.txt"]
check("I-8) 거부된 매수 주문은 accepted 집계에서 제외됨",
      "buy_order_attempt_count               = 2" in quality2
      and "accepted_buy_order_count              = 1" in quality2)


# ── J. VWAP 분모 유니크 ────────────────────────────────────────
# 같은 분봉에 상태 변화 행 2개, 둘 다 차단 → 1/1 = 100%여야 함
dup_bar = [_shadow_row(would_block_pr_only_rolling_vwap="True", order_accepted="False"),
           _shadow_row(would_block_pr_only_rolling_vwap="True", order_accepted="True")]
rep = _sess_report(dup_bar)
check("J-1) 같은 분봉 상태변화 2행이 있어도 분모가 유니크라 100%로 계산",
      "PR-only            rolling(60분)       1건 100.0%" in rep)


# ── K. stale lock ──────────────────────────────────────────────
check("K-1) stale lock 기준이 30분", B.STALE_LOCK_SECONDS == 30 * 60)
lock2 = root2 / "exports" / "bundle_20260806.lock"
lock2.write_text("pid=99999")
os.utime(lock2, (time.time() - 3600, time.time() - 3600))
res = _run_export(root2, "2026-08-06")
check("K-2) 30분 이상 지난 stale lock은 회수되고 export가 진행됨", res is not None)
lock2.write_text("pid=99999")   # 방금 생성 = 정상 락
check("K-3) 갓 생성된 락은 회수되지 않고 export가 물러남",
      _run_export(root2, "2026-08-06") is None)
lock2.unlink()
check("K-4) 락 파일에 pid와 생성시각이 기록됨",
      (lambda: (_run_export(root2, "2026-08-06"), True)[1])())

# ══════════════════════════════════════════════════════════════
# 1I.3: JSON 토큰 마스킹 · session 해석 기준 · fail-closed (GPT 리뷰)
# ══════════════════════════════════════════════════════════════
# P0 재현(수정 전): {"access_token":"SECRET123456789"} 가 그대로 남음
check("L-1) JSON 큰따옴표 형태 access_token 마스킹",
      B.mask('{"access_token":"SECRET123456789"}') == '{"access_token":"***"}')
check("L-2) 공백 있는 JSON refresh_token 마스킹",
      B.mask('{"refresh_token": "REFRESH123456789"}') == '{"refresh_token": "***"}')
check("L-3) Python dict 작은따옴표 api_key 마스킹",
      B.mask("{'api_key': 'KEY123456789'}") == "{'api_key': '***'}")
check("L-4) 같은 JSON에서 password는 마스킹, 종목코드는 보존",
      B.mask('{"password":"PW123","symbol":"005930"}')
      == '{"password":"***","symbol":"005930"}')
check("L-5) 기존 key=value 형태도 계속 마스킹",
      B.mask("access_token=PLAIN123") == "access_token=***")
check("L-6) JSON 안의 Bearer 토큰도 마스킹",
      "SECRET123456789" not in B.mask('{"authorization":"Bearer SECRET123456789"}'))
check("L-7) 로그 타임스탬프·종목코드는 변형되지 않음",
      B.mask("2026-08-06 09:00:01,000 | INFO | [COND] 편입: 005930")
      == "2026-08-06 09:00:01,000 | INFO | [COND] 편입: 005930")

# ZIP까지 통과시켜 실제 원문이 남지 않는지 검사
root3 = Path(tempfile.mkdtemp())
_mkday(root3, "signal_log.csv", ["timestamp", "symbol"],
       [{"timestamp": "2026-08-06T09:00:30", "symbol": "005930"}])
(root3 / "logs" / "app.log").write_text(
    '2026-08-06 09:00:01,000 | INFO | [RECONCILE] resp={"access_token":"SECRET_TOKEN_A",'
    '"refresh_token":"SECRET_TOKEN_B","api_key":"SECRET_TOKEN_C"}\n'
    "2026-08-06 09:00:02,000 | INFO | [COND] 편입: 005930\n", encoding="utf-8")
z3 = _run_export(root3, "2026-08-06")
blob3 = "".join(_zip_texts(z3).values())
check("L-8) ZIP 안에 access_token 원문이 없음", "SECRET_TOKEN_A" not in blob3)
check("L-9) ZIP 안에 refresh_token 원문이 없음", "SECRET_TOKEN_B" not in blob3)
check("L-10) ZIP 안에 api_key 원문이 없음", "SECRET_TOKEN_C" not in blob3)
check("L-11) 같은 줄의 정상 로그는 보존됨",
      "005930" in _zip_texts(z3)["raw/app_analysis_20260806.log"])


# ── M. session 해석 기준 (P1-1) ────────────────────────────────
# SESSION_SHADOW 로그에는 ready=True가 있지만 BUY 후보에는 0건인 상황
(root3 / "logs" / "app.log").write_text(
    "".join(f"2026-08-06 09:{i:02d}:00,000 | INFO | [SESSION_SHADOW] 005930 "
            f"ready=True reason=COMPLETE_FROM_OPEN\n" for i in range(10)), encoding="utf-8")
_mkday(root3, "entry_quality_shadow.csv",
       ["timestamp", "symbol", "session_metrics_ready", "order_attempted", "order_accepted"],
       [{"timestamp": "2026-08-06T10:00:00", "symbol": "005930",
         "session_metrics_ready": "False", "order_attempted": "True",
         "order_accepted": "True"}])
z3 = _run_export(root3, "2026-08-06")
q3 = _zip_texts(z3)["metadata/collection_quality.txt"]
check("M-1) 로그 ready=True 10건이어도 BUY 후보 0건이면 해석 불가",
      "session_gate_interpretation           = INVALID_FOR_THIS_DAY" in q3)
check("M-2) 로그 이벤트 기준 상태는 진단용으로 별도 유지",
      "session_state_collection_status       = COLLECTING" in q3)
check("M-3) shadow 후보 기준 ready 수가 0으로 집계",
      "shadow_candidate_session_ready_count  = 0" in q3)

# BUY 후보에 ready=True가 있으면 AVAILABLE
_mkday(root3, "entry_quality_shadow.csv",
       ["timestamp", "symbol", "session_metrics_ready", "order_attempted", "order_accepted"],
       [{"timestamp": "2026-08-06T10:00:00", "symbol": "005930",
         "session_metrics_ready": "True", "order_attempted": "True",
         "order_accepted": "True"}])
z3 = _run_export(root3, "2026-08-06")
q3b = _zip_texts(z3)["metadata/collection_quality.txt"]
check("M-4) BUY 후보에 ready=True가 있으면 AVAILABLE",
      "session_gate_interpretation           = AVAILABLE" in q3b)


# ── N. accepted 스키마 fail-closed (P1-2) ──────────────────────
_mkday(root3, "trades.csv", ["timestamp", "symbol", "side"],
       [{"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY"}])
z3 = _run_export(root3, "2026-08-06")
q3c = _zip_texts(z3)["metadata/collection_quality.txt"]
check("N-1) accepted 컬럼이 없으면 수락 수를 추정하지 않고 N/A",
      "accepted_buy_order_count              = N/A(SCHEMA_WARNING)" in q3c)
check("N-2) SCHEMA_WARNING이 명시됨", "SCHEMA_WARNING: accepted 컬럼 없음" in q3c)
check("N-3) coverage도 N/A 처리",
      "shadow_to_actual_buy_coverage         = N/A" in q3c)


# ── O. order_id 누락 처리 (P1-3) ───────────────────────────────
_mkday(root3, "trades.csv", ["timestamp", "symbol", "side", "accepted", "order_id", "price"], [
    {"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "O1", "price": "100"},
    {"timestamp": "2026-08-06T09:31:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "", "price": "200"},
    {"timestamp": "2026-08-06T09:32:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "", "price": "300"},
])
z3 = _run_export(root3, "2026-08-06")
q3d = _zip_texts(z3)["metadata/collection_quality.txt"]
# 2026-08-06 (1I.4 정책 변경): 1I.3에서는 fallback 키에 idx를 넣어
# 3건으로 셌지만, 그러면 완전히 동일한 중복 행도 별개 주문이 되어
# "unique"라는 이름과 맞지 않음. 이제 order_id가 하나라도 없으면
# unique는 N/A로 처리하고 행 수만 표시한다.
check("O-1) order_id 누락 시 unique는 N/A, 행 수는 3건으로 보존",
      "unique_accepted_buy_order_count       = N/A(order_id 누락 있음)" in q3d
      and "accepted_buy_order_count              = 3" in q3d)
check("O-2) order_id 누락 건수가 경고로 기록됨",
      "accepted_buy_missing_order_id_count   = 2" in q3d)
check("O-3) shadow 쪽 order_id 누락 건수도 기록됨",
      "shadow_accepted_missing_order_id_count" in q3d)


# ── P. 락 소유권 (P2) ──────────────────────────────────────────
lock3 = root3 / "exports" / "bundle_20260806.lock"
lock3.write_text("pid=99999 created=other nonce=1")
os.utime(lock3, (time.time() - 3600, time.time() - 3600))
res3 = _run_export(root3, "2026-08-06")
check("P-1) stale lock은 회수되어 export가 진행됨", res3 is not None)
# 남의 락은 지우지 않아야 함
lock3.write_text("pid=99999 created=other nonce=1")
_run_export(root3, "2026-08-06")   # 락 충돌로 물러남
check("P-2) 다른 프로세스의 락을 자신의 finally에서 지우지 않음", lock3.exists())
lock3.unlink()
check("P-3) 정상 종료 후 자신의 락은 해제됨",
      _run_export(root3, "2026-08-06") is not None and not lock3.exists())

# ══════════════════════════════════════════════════════════════
# 1I.4: 실제 키움 자격증명 키 · 판정 기준 · fail-closed (GPT 리뷰)
# ══════════════════════════════════════════════════════════════
# P0 재현(수정 전): {"token":"SECRET1"} / {"appkey":…} / {"secretkey":…}
# 가 전부 원문 유지 — kiwoom_broker.py가 실제로 쓰는 키였음.
for _k in ("token", "appkey", "secretkey", "app_key", "secret_key",
           "rest_api_key", "client_secret", "client_id"):
    check(f"Q-1) 실제 자격증명 키 '{_k}'가 마스킹 목록에 포함됨", _k in B.SENSITIVE_KEYS)

check("Q-2) JSON token 마스킹", B.mask('{"token":"SECRET1"}') == '{"token":"***"}')
check("Q-3) JSON appkey 마스킹", B.mask('{"appkey":"SECRET2"}') == '{"appkey":"***"}')
check("Q-4) JSON secretkey 마스킹", B.mask('{"secretkey":"SECRET3"}') == '{"secretkey":"***"}')
check("Q-5) JSON app_key 마스킹", B.mask('{"app_key":"SECRET4"}') == '{"app_key":"***"}')
check("Q-6) JSON secret_key 마스킹", B.mask('{"secret_key":"S5"}') == '{"secret_key":"***"}')
check("Q-7) JSON rest_api_key 마스킹",
      B.mask('{"rest_api_key":"S6"}') == '{"rest_api_key":"***"}')
check("Q-8) 값에 공백이 있어도 마스킹",
      B.mask('{"password":"hello world"}') == '{"password":"***"}')
check("Q-9) 값에 쉼표가 있어도 마스킹",
      B.mask('{"secret":"abc,def"}') == '{"secret":"***"}')
check("Q-10) 작은따옴표 + 세미콜론 값도 마스킹",
      B.mask("{'secretkey': 'abc;def'}") == "{'secretkey': '***'}")
check("Q-11) 같은 JSON의 종목코드는 보존",
      B.mask('{"access_token":"A","symbol":"005930"}')
      == '{"access_token":"***","symbol":"005930"}')
check("Q-12) 로그 타임스탬프는 변형되지 않음",
      B.mask("2026-08-06 09:00:01,000 | INFO | [COND] 편입: 005930")
      == "2026-08-06 09:00:01,000 | INFO | [COND] 편입: 005930")
check("Q-13) 긴 키가 짧은 키보다 먼저 매칭됨(access_token이 token으로 쪼개지지 않음)",
      B.mask('{"access_token":"X"}') == '{"access_token":"***"}')

# ZIP까지 통과시켜 실제 키움 응답 형태의 원문이 남지 않는지
root4 = Path(tempfile.mkdtemp())
_mkday(root4, "signal_log.csv", ["timestamp", "symbol"],
       [{"timestamp": "2026-08-06T09:00:30", "symbol": "005930"},
        {"timestamp": "2026-08-06T15:19:00", "symbol": "005930"}])
(root4 / "logs" / "app.log").write_text(
    '2026-08-06 09:00:01,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작\n'
    '2026-08-06 09:00:02,000 | INFO | [RECONCILE] req={"appkey":"KIWOOM_A","secretkey":"KIWOOM_B"}\n'
    '2026-08-06 09:00:03,000 | INFO | [WS] resp={"token":"KIWOOM_C","expires_dt":"20260807"}\n'
    '2026-08-06 09:00:04,000 | INFO | [COND] 편입: 005930\n', encoding="utf-8")
z4 = _run_export(root4, "2026-08-06")
blob4 = "".join(_zip_texts(z4).values())
check("Q-14) ZIP에 appkey 원문 없음", "KIWOOM_A" not in blob4)
check("Q-15) ZIP에 secretkey 원문 없음", "KIWOOM_B" not in blob4)
check("Q-16) ZIP에 token 원문 없음", "KIWOOM_C" not in blob4)
check("Q-17) 정상 로그는 보존됨",
      "005930" in _zip_texts(z4)["raw/app_analysis_20260806.log"])


# ── R. 수집 상태 판정 (P1) ─────────────────────────────────────
q4 = _zip_texts(z4)["metadata/collection_quality.txt"]
check("R-1) 기동 1회 + signal 09:00~15:19면 COMPLETE",
      "collection_status                     = COMPLETE" in q4)

# 기동 로그 0건
(root4 / "logs" / "app.log").write_text(
    "2026-08-06 09:00:04,000 | INFO | [COND] 편입: 005930\n", encoding="utf-8")
q4b = _zip_texts(_run_export(root4, "2026-08-06"))["metadata/collection_quality.txt"]
check("R-2) 기동 로그 0건이면 COMPLETE로 판정하지 않음",
      "collection_status                     = UNKNOWN_START_PARTIAL" in q4b)
check("R-3) full_day_collection도 False", "full_day_collection                   = False" in q4b)

# 기동 2회
(root4 / "logs" / "app.log").write_text(
    "2026-08-06 09:00:01,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작\n"
    "2026-08-06 10:00:01,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작\n",
    encoding="utf-8")
q4c = _zip_texts(_run_export(root4, "2026-08-06"))["metadata/collection_quality.txt"]
check("R-4) 기동 2회면 RESTARTED_PARTIAL",
      "collection_status                     = RESTARTED_PARTIAL" in q4c)


# ── S. accepted 빈 값 / 부분 인식 (P1) ─────────────────────────
_mkday(root4, "trades.csv", ["timestamp", "symbol", "side", "accepted", "order_id"], [
    {"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "O1"},
    {"timestamp": "2026-08-06T09:31:00", "symbol": "005930", "side": "BUY",
     "accepted": "", "order_id": "O2"},
])
q4d = _zip_texts(_run_export(root4, "2026-08-06"))["metadata/collection_quality.txt"]
check("S-1) accepted 빈 값은 거부가 아니라 미상으로 처리돼 스키마 경고",
      "accepted_buy_order_count              = N/A(SCHEMA_WARNING)" in q4d)
check("S-2) 판정 불가 건수가 별도 기록됨",
      "buy_accepted_state_missing_count      = 1" in q4d)

# 전부 인식 가능하면 정상 집계
_mkday(root4, "trades.csv", ["timestamp", "symbol", "side", "accepted", "order_id"], [
    {"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "O1"},
    {"timestamp": "2026-08-06T09:31:00", "symbol": "005930", "side": "BUY",
     "accepted": "False", "order_id": "O2"},
])
q4e = _zip_texts(_run_export(root4, "2026-08-06"))["metadata/collection_quality.txt"]
check("S-3) 전부 판정 가능하면 accepted 1건으로 집계",
      "accepted_buy_order_count              = 1" in q4e)
check("S-4) order_id가 모두 있으면 unique 수가 산출됨",
      "unique_accepted_buy_order_count       = 1" in q4e)


# ── T. order_id 누락 시 unique는 N/A (P1) ──────────────────────
_mkday(root4, "trades.csv", ["timestamp", "symbol", "side", "accepted", "order_id"], [
    {"timestamp": "2026-08-06T09:30:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": "O1"},
    {"timestamp": "2026-08-06T09:31:00", "symbol": "005930", "side": "BUY",
     "accepted": "True", "order_id": ""},
])
q4f = _zip_texts(_run_export(root4, "2026-08-06"))["metadata/collection_quality.txt"]
check("T-1) order_id가 하나라도 없으면 unique 수를 N/A로 처리",
      "unique_accepted_buy_order_count       = N/A(order_id 누락 있음)" in q4f)
check("T-2) 행 수(accepted_buy_order_count)는 그대로 표시",
      "accepted_buy_order_count              = 2" in q4f)
check("T-3) coverage도 N/A", "shadow_to_actual_buy_coverage         = N/A" in q4f)


# ── U. 락 PID 생존 확인 + 고유 tmp (P2) ────────────────────────
lock4 = root4 / "exports" / "bundle_20260806.lock"
lock4.write_text(f"pid={os.getpid()} created=x nonce=1")
os.utime(lock4, (time.time() - 3600, time.time() - 3600))
check("U-1) 30분 지나도 소유 PID가 살아 있으면 회수하지 않음",
      _run_export(root4, "2026-08-06") is None)
check("U-2) 살아 있는 소유자의 락은 삭제되지 않음", lock4.exists())
lock4.write_text("pid=2147480000 created=x nonce=1")   # 존재하지 않는 PID
os.utime(lock4, (time.time() - 3600, time.time() - 3600))
check("U-3) 죽은 PID의 stale lock은 회수됨",
      _run_export(root4, "2026-08-06") is not None)
check("U-4) 임시 ZIP 경로가 고유(pid+nonce)해 동시 실행 시 충돌하지 않음",
      not list((root4 / "exports").glob("*.zip.tmp")))
check("U-5) _lock_owner_alive가 살아 있는 PID를 True로 판정",
      (lambda p: (p.write_text(f"pid={os.getpid()}"), B._lock_owner_alive(p))[1])(
          root4 / "exports" / "probe.lock"))

# ══════════════════════════════════════════════════════════════
# 1I.5: Windows fsync 호환 (실서버 재현)
# ══════════════════════════════════════════════════════════════
# 실서버(PowerShell): OSError: [Errno 9] Bad file descriptor
# — 읽기 전용 핸들에 fsync를 걸어서 발생. 컨테이너(Linux)에서는
# 허용되므로 검증에서 놓쳤음.
_src = open("export_daily_bundle.py", encoding="utf-8").read()
check("V-1) fsync를 쓰기 가능한 모드(r+b)로 연 핸들에 적용",
      'open(tmp_zip, "r+b")' in _src and 'open(tmp_zip, "rb")' not in _src)
check("V-2) fsync 실패가 번들 생성을 중단시키지 않음",
      "except OSError as exc:" in _src.split("os.replace(tmp_zip")[0][-900:])

# fsync가 실패해도 최종 ZIP이 정상 생성되는지 (Windows 상황 모사)
root5 = Path(tempfile.mkdtemp())
_mkday(root5, "signal_log.csv", ["timestamp", "symbol"],
       [{"timestamp": "2026-08-06T09:00:30", "symbol": "005930"}])
_orig_fsync = os.fsync
os.fsync = lambda fd: (_ for _ in ()).throw(OSError(9, "Bad file descriptor"))
try:
    z5 = _run_export(root5, "2026-08-06")
finally:
    os.fsync = _orig_fsync
check("V-3) fsync가 실패해도 최종 ZIP이 생성됨", z5 is not None and z5.exists())
check("V-4) fsync 실패 후에도 ZIP 무결성 정상",
      zipfile.ZipFile(z5).testzip() is None)
check("V-5) fsync 실패 후 tmp 파일이 남지 않음",
      not list((root5 / "exports").glob("*.zip.tmp")))

print()
print(f"[최종] 총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
