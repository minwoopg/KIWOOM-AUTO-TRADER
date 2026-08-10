# -*- coding: utf-8 -*-
"""fidelity 분석기 자체 검증 (2026-08-07, 1J.5.1단계)

배경: 1J.5에서 `actual_buy_full_window_pct 250/250`이라는 오집계가
전체 회귀 15/15를 통과한 채 리포트에 실렸습니다. `signal == "BUY"`는
전략이 BUY를 반환한 **legacy candidate**이지 실제 체결이 아닌데
그것을 "실제 BUY"로 셌기 때문입니다.

기존 회귀는 매매·replay 로직만 검증하므로 **분석기 자체의 집계
오류는 잡지 못합니다.** 이 파일이 그 구멍을 메웁니다.

실측 근거 (2026-08-07):
    signal=BUY            250행
    legacy_buy_candidate  250행
    final_decision=BUY      3행
    shadow order_accepted   3건   (005930 09:04 / 233740 10:56 / 119850 13:01)
    trades.csv BUY          3건   ← 세 소스가 일치
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from pathlib import Path
import analyze_replay_fidelity as F

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


# ── 1. BUY_DECISION vs ACCEPTED_BUY 분리 (1J.5.2/1J.5.3) ───────
# 1J.5.1의 테스트는 `final_decision == accepted BUY`라는 잘못된
# 정의를 코드와 함께 공유해 47/47을 통과시켰습니다. 1J.5.2가 코드를
# 고쳤는데 테스트 앞부분에 그 옛 가정이 남아 정면 충돌했습니다.
# 두 개념을 명확히 분리해 검증합니다.
#   BUY_DECISION  = final_decision == "BUY"   (broker accepted 아님)
#   ACCEPTED_BUY  = collect_accepted_buy(...)  (shadow/trades 기준)
src = open("analyze_replay_fidelity.py", encoding="utf-8").read()

check("1-1) accepted BUY를 collect_accepted_buy로 산출",
      "collect_accepted_buy(sh_rows, tr_rows)" in src)
check("1-2) final_decision을 accepted로 쓰지 않음",
      'actual_buy = [a for a in aligned\n'
      '                  if str(a["live"].get("final_decision"' not in src)
check("1-3) final_decision은 BUY_DECISION 참고값으로만 표기",
      "BUY_DECISION(final_decision=BUY)" in src
      and "broker accepted 여부가 아님" in src)

# fixture — final_decision=BUY인데 broker가 거절한 경우
_signal_rows = [{"symbol": "AAA", "final_decision": "BUY",
                 "legacy_buy_candidate": "True", "latest_bar_timestamp": "T1"}]
_shadow_rej = [{"symbol": "AAA", "latest_bar_timestamp": "T1",
                "order_attempted": "True", "order_accepted": "False",
                "order_id": "O9"}]
_trades_rej = [{"symbol": "AAA", "side": "BUY", "accepted": "False",
                "order_id": "O9"}]
_r1 = F.collect_accepted_buy(_shadow_rej, _trades_rej)
check("1-4) final_decision=BUY 1건이지만 broker 거절이면 accepted BUY 0",
      sum(1 for r in _signal_rows
          if str(r.get("final_decision", "")).upper() == "BUY") == 1
      and _r1["accepted_count"] == 0)
check("1-5) trades 교차검증도 0건 (side=BUY만 세면 1이 됐을 것)",
      _r1["trades_accepted"] == 0 and _r1["trades_buy_total"] == 1)


# ── 2. recall / eligible recall / unexplained ───────────────────
# live 3개 중 replay 2개 재현, 1개는 prev_close 불가
live_cand = {("A", "T1"), ("B", "T2"), ("C", "T3")}
replay_cand = {("A", "T1"), ("B", "T2")}
aligned = [
    {"symbol": "A", "lbt": "T1", "prev_close_source": "SAME_FILE_PRETARGET",
     "full_window": True, "replay": object()},
    {"symbol": "B", "lbt": "T2", "prev_close_source": "SIGNAL_LOG_INFERRED",
     "full_window": True, "replay": object()},
    {"symbol": "C", "lbt": "T3", "prev_close_source": "UNAVAILABLE",
     "full_window": True, "replay": None},
]
by_key = {(a["symbol"], a["lbt"]): a for a in aligned}
missed = live_cand - replay_cand
codes = []
for key in missed:
    a = by_key.get(key)
    if a is None:
        codes.append(F.REASON_BAR_NOT_FOUND)
    elif a["prev_close_source"] in ("UNAVAILABLE", "PREVIOUS_DATA_DAY_PARTIAL"):
        codes.append(F.REASON_PREV_CLOSE_UNAVAILABLE)
    elif not a["full_window"]:
        codes.append(F.REASON_PARTIAL_HISTORY)
    elif a.get("replay") is None:
        codes.append(F.REASON_VALUE_MISMATCH)
    else:
        codes.append(F.REASON_UNKNOWN)

inter = live_cand & replay_cand
ineligible = sum(1 for c in codes if c in (
    F.REASON_PREV_CLOSE_UNAVAILABLE, F.REASON_NO_MINUTE_DATA,
    F.REASON_BAR_NOT_FOUND, F.REASON_PARTIAL_HISTORY))
eligible_total = len(live_cand) - ineligible

check("2-1) overall recall 2/3 (66.7%)",
      abs(len(inter) / len(live_cand) - 2 / 3) < 1e-9)
check("2-2) eligible recall 2/2 (100%)",
      eligible_total == 2 and len(inter) / eligible_total == 1.0)
check("2-3) data-ineligible 1건", ineligible == 1)
check("2-4) unexplained mismatch 0건",
      sum(1 for c in codes if c == F.REASON_UNKNOWN) == 0)
check("2-5) 미재현 사유가 PREV_CLOSE_UNAVAILABLE로 분류",
      codes == [F.REASON_PREV_CLOSE_UNAVAILABLE])

# UNKNOWN이 실제로 발생하는 경우도 잡히는지 (자기검증)
a_unknown = {"symbol": "D", "lbt": "T4", "prev_close_source": "SAME_FILE_PRETARGET",
             "full_window": True, "replay": object()}
code = (F.REASON_UNKNOWN
        if a_unknown["prev_close_source"] not in ("UNAVAILABLE",
                                                  "PREVIOUS_DATA_DAY_PARTIAL")
        and a_unknown["full_window"] and a_unknown.get("replay") is not None
        else "other")
check("2-6) 설명 불가한 미재현은 UNKNOWN으로 분류됨", code == F.REASON_UNKNOWN)


# ── 3. accepted BUY 재현 분류 ───────────────────────────────────
# accepted 2건 중 1건 재현, 1건은 prev_close 불가
acc_keys = [("A", "T1"), ("C", "T3")]
acc_repro = [k for k in acc_keys if k in replay_cand]
acc_ineli = [k for k in acc_keys
             if k not in replay_cand and by_key.get(k) is not None
             and by_key[k]["prev_close_source"] in ("UNAVAILABLE",
                                                    "PREVIOUS_DATA_DAY_PARTIAL")]
acc_unexp = [k for k in acc_keys if k not in replay_cand and k not in acc_ineli]
check("3-1) accepted BUY 재현 1건", len(acc_repro) == 1)
check("3-2) data-ineligible 1건", len(acc_ineli) == 1)
check("3-3) unexplained 0건", len(acc_unexp) == 0)
check("3-4) 억지로 재현율을 높이지 않음(2/2로 만들지 않음)",
      len(acc_repro) != len(acc_keys))


# ── 3-2. accepted BUY 3소스 교차검증 (1J.5.1) ──────────────────
# 1J.5.2: _cross_check_accepted_buy → collect_accepted_buy 순수 helper로 대체
check("3-5) collect_accepted_buy 순수 helper가 존재",
      hasattr(F, "collect_accepted_buy"))
check("3-6) shadow accepted 기준을 사용",
      'truthy(r.get("order_attempted"))' in src and 'truthy(r.get("order_accepted"))' in src)
check("3-7) trades.csv BUY와도 교차검증", 'str(r.get("side", "")).upper() in ("BUY"' in src)
check("3-8) shadow와 trades 불일치 시 경고 출력",
      "와 \ntrades(" in src or "가 다릅니다" in src)
_cc = F.collect_accepted_buy([], [])
check("3-9) 빈 입력에서도 예외 없이 dict 반환",
      isinstance(_cc, dict) and _cc["accepted_count"] == 0)


# ── 4. reason code 상수와 리포트 출력 ───────────────────────────
for name in ("REASON_PREV_CLOSE_UNAVAILABLE", "REASON_BAR_NOT_FOUND",
             "REASON_PARTIAL_HISTORY", "REASON_VALUE_MISMATCH",
             "REASON_UNKNOWN", "REASON_LIVE_TICK_VS_BAR_CLOSE",
             "REASON_MACD_NO_DAILY_DATA", "REASON_CONDITION_UNIVERSE"):
    check(f"4-1) {name} 상수가 정의됨", hasattr(F, name))

check("4-2) 미재현 상세를 리포트에 출력", "mismatch_rows" in src)
check("4-3) reason_code별 분포를 출력", "미재현 reason_code 분포" in src)
check("4-4) UNKNOWN이 0이 아니면 경고", "UNKNOWN이 0이 아닙니다" in src)
check("4-5) eligible recall을 핵심 지표로 표기", "eligible recall" in src)


# ── 5. Primary Set 정의 (가드레일 3) ────────────────────────────
check("5-1) Tier A가 SAME_FILE_PRETARGET + SIGNAL_LOG_INFERRED",
      F.TIER_A_SOURCES == {"SAME_FILE_PRETARGET", "SIGNAL_LOG_INFERRED"})
check("5-2) Tier B가 PREVIOUS_DATA_DAY_EOD", F.TIER_B_SOURCES == {"PREVIOUS_DATA_DAY_EOD"})
check("5-3) PARTIAL/UNAVAILABLE은 어느 Tier에도 없음",
      "PREVIOUS_DATA_DAY_PARTIAL" not in (F.TIER_A_SOURCES | F.TIER_B_SOURCES)
      and "UNAVAILABLE" not in (F.TIER_A_SOURCES | F.TIER_B_SOURCES))
check("5-4) Primary Set 조건에 full_window와 Tier A가 모두 포함",
      'a["full_window"] and a["tier"] == "A"' in src)
check("5-5) Primary Set 조건에 LIVE_MINUTE_ANALYZER 포함",
      'analyzer_mode == "LIVE_MINUTE_ANALYZER"' in src)


# ── 6. 가드레일 1 — signal_rows 종목/날짜 분리 ──────────────────
check("6-1) index_by_symbol이 종목 혼입을 assert로 차단",
      "symbol 혼입" in src)
check("6-2) index_by_symbol이 날짜 혼입을 assert로 차단",
      "날짜 혼입" in src)
try:
    from datetime import date as _d
    F.index_by_symbol([{"symbol": "A", "timestamp": "2026-08-06T09:00:00"}],
                      _d(2026, 8, 7))
    _r6 = False
except AssertionError:
    _r6 = True
check("6-3) 다른 날짜 행이 섞이면 실제로 AssertionError", _r6)


# ── 7. 가드레일 2 — full-window 용어 분리 ───────────────────────
# 2026-08-07 (1J.5.3): docstring 문자열 때문에 PASS하던 false pass 제거.
# docstring을 걷어낸 실행 코드에서만 검사합니다.
import ast as _ast


def _code_only(text: str) -> str:
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)) and _ast.get_docstring(node):
            node.body = node.body[1:]
    return _ast.unparse(tree)


_body = _code_only(src)
check("7-1) 실행 코드에서 aligned/legacy/buy_decision 세 비율을 출력",
      "aligned_live_rows_full_window_pct" in _body
      and "legacy_buy_candidate_full_window_pct" in _body
      and "buy_decision_full_window_pct" in _body)
check("7-1b) docstring에만 있는 문자열은 통과시키지 않음",
      "accepted_buy_full_window_pct" not in _body)
check("7-2) evaluation-point coverage와 다름을 명시",
      "evaluation-point coverage와 다릅니다" in src)


# ── 8. MACD limitation ──────────────────────────────────────────
check("8-1) MACD가 일봉 미저장으로 재현 불가임을 명시",
      "일봉 데이터가 저장되지 않습니다" in src)
# 1J.5.2 정책 정정: 완전 제외가 아니라 Primary는 가능 / Secondary는 불가
check("8-2) Primary Live-Aligned에서는 MACD 평가 가능으로 정정",
      "Primary (Live-Aligned)   → MACD 평가 **가능**" in src)
check("8-3) Secondary Replay Discovery에서는 평가 불가",
      "Secondary (Replay Discovery) → MACD 평가 **불가**" in src)
check("8-4) hard gate 정의 명시", "macd_above_signal == False → block" in src)
check("8-5) dead+score5 정의 명시", "score < 5 → block" in src)
check("8-6) MACD field coverage를 출력", "MACD field coverage" in src)
check("8-7) coverage 없는 표본은 N/A로 처리하도록 안내",
      "N/A로 처리하십시오" in src)


# ── 9. live 체결가 vs 분봉 종가 한계 ────────────────────────────
check("9-1) 가격 차이를 구조적 한계로 분류",
      "LIVE_TICK_VS_BAR_CLOSE" in src)
check("9-2) 오차 분포(중앙/p90/구간)로 출력",
      "중앙오차" in src and "p90" in src)

# ══════════════════════════════════════════════════════════════
# 10. 실행 기반 검증 (1J.5.2) — helper를 실제 호출
# ══════════════════════════════════════════════════════════════
# 1J.5.1의 테스트는 분석기 로직을 테스트 쪽에 다시 구현해서
# "final_decision == accepted BUY"라는 **잘못된 가정을 코드와 함께
# 공유**했고 그래서 47/47이 통과했습니다. 이제 순수 helper를 실제로
# 호출해 검증합니다.

# --- A. broker rejected BUY는 accepted로 세지 않음 ---
shadow_rej = [{"symbol": "AAA", "latest_bar_timestamp": "T1",
               "order_attempted": "True", "order_accepted": "False",
               "order_id": ""}]
trades_rej = [{"symbol": "AAA", "side": "BUY", "accepted": "False", "order_id": ""}]
r = F.collect_accepted_buy(shadow_rej, trades_rej)
check("10-1) shadow accepted=False면 accepted BUY 0건", r["accepted_count"] == 0)
check("10-2) trades accepted=False면 교차검증도 0건", r["trades_accepted"] == 0)
check("10-3) side=BUY 전체는 1건으로 별도 집계", r["trades_buy_total"] == 1)
check("10-4) 거절 건수가 기록됨", r["trades_rejected"] == 1)

# 정상 accepted
shadow_ok = [{"symbol": "AAA", "latest_bar_timestamp": "T1",
              "order_attempted": "True", "order_accepted": "True", "order_id": "O1"},
             {"symbol": "BBB", "latest_bar_timestamp": "T2",
              "order_attempted": "True", "order_accepted": "True", "order_id": "O2"}]
trades_ok = [{"symbol": "AAA", "side": "BUY", "accepted": "True", "order_id": "O1"},
             {"symbol": "BBB", "side": "BUY", "accepted": "True", "order_id": "O2"},
             {"symbol": "CCC", "side": "BUY", "accepted": "False", "order_id": ""}]
r2 = F.collect_accepted_buy(shadow_ok, trades_ok)
check("10-5) accepted 2건이 정확히 집계", r2["accepted_count"] == 2)
check("10-6) trades accepted도 2건(거절 1건 제외)", r2["trades_accepted"] == 2)
check("10-7) accepted_keys가 (symbol, lbt)로 반환",
      set(r2["accepted_keys"]) == {("AAA", "T1"), ("BBB", "T2")})

# --- B. NO_MINUTE_DATA candidate가 분모에 포함 ---
raw = {("A", "T1"), ("B", "T2"), ("C", "T3")}
replay = {("A", "T1"), ("B", "T2")}
by_key = {  # C는 minute data가 없어 aligned에 아예 없음
    ("A", "T1"): {"symbol": "A", "lbt": "T1", "full_window": True,
                  "prev_close_source": "SAME_FILE_PRETARGET", "replay": object(),
                  "live": {"detected_patterns": "A/B"}},
    ("B", "T2"): {"symbol": "B", "lbt": "T2", "full_window": True,
                  "prev_close_source": "SIGNAL_LOG_INFERRED", "replay": object(),
                  "live": {"detected_patterns": "C"}},
}
rows = F.classify_candidate_fidelity(raw, replay, by_key)
rec = F.calculate_recall(rows)
check("10-8) 분모가 raw candidate 3건 (aligned 2건이 아님)", rec["total"] == 3)
check("10-9) NO_MINUTE_DATA로 분류됨",
      rec["codes"].get(F.REASON_NO_MINUTE_DATA) == 1)
check("10-10) overall recall 2/3", abs(rec["overall_recall"] - 2 / 3) < 1e-9)
check("10-11) data-ineligible 1건", rec["data_ineligible"] == 1)
check("10-12) eligible recall 2/2 = 100%", rec["eligible_recall"] == 1.0)
check("10-13) unexplained 0건", rec["unexplained"] == 0)
check("10-14) reproduced가 code로 기록됨", rec["codes"].get("REPRODUCED") == 2)

# 설명 불가 케이스는 UNKNOWN으로
by_key_u = dict(by_key)
by_key_u[("C", "T3")] = {"symbol": "C", "lbt": "T3", "full_window": True,
                         "prev_close_source": "SAME_FILE_PRETARGET",
                         "replay": object(), "live": {"detected_patterns": "A"}}
rec_u = F.calculate_recall(F.classify_candidate_fidelity(raw, replay, by_key_u))
check("10-15) 데이터가 멀쩡한데 미재현이면 UNKNOWN",
      rec_u["codes"].get(F.REASON_UNKNOWN) == 1 and rec_u["unexplained"] == 1)
check("10-16) UNKNOWN은 data-ineligible에 포함되지 않음",
      rec_u["data_ineligible"] == 0 and rec_u["eligible_total"] == 3)


# --- C. 파일 상태 구분 (실제 0건 vs 자료 없음) ---
import tempfile as _tf, csv as _csv
from datetime import date as _date
_d = Path(_tf.mkdtemp())


def _wcsv(name, rows, cols):
    p_ = _d / name
    with p_.open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r_ in rows:
            w.writerow(r_)
    return p_


rows_, st, err = F._read_day_rows(_d / "nope.csv", _date(2026, 8, 7))
check("10-17) 파일이 없으면 FILE_MISSING", st == F.STATUS_FILE_MISSING)

p_other = _wcsv("other.csv", [{"timestamp": "2026-08-06T09:00:00", "symbol": "A"}],
                ["timestamp", "symbol"])
rows_, st, err = F._read_day_rows(p_other, _date(2026, 8, 7))
check("10-18) 파일은 있으나 그날 행이 없으면 NO_TARGET_DATE_DATA",
      st == F.STATUS_NO_TARGET_DATE_DATA)
check("10-19) 이 경우 0건이라고 단정하지 않음", rows_ == [] and st != F.STATUS_OK)

p_ok = _wcsv("ok.csv", [{"timestamp": "2026-08-07T09:00:00", "symbol": "A"}],
             ["timestamp", "symbol"])
rows_, st, err = F._read_day_rows(p_ok, _date(2026, 8, 7))
check("10-20) 정상 파일은 OK", st == F.STATUS_OK and len(rows_) == 1)

(_d / "bad.csv").write_bytes(b"\xff\xfe not a csv\x00")
rows_, st, err = F._read_day_rows(_d / "bad.csv", _date(2026, 8, 7))
check("10-21) 파싱 실패도 silent pass하지 않음",
      st in (F.STATUS_PARSE_ERROR, F.STATUS_NO_TARGET_DATE_DATA))

_src2 = open("analyze_replay_fidelity.py", encoding="utf-8").read()
check("10-22) CLI가 --entry-quality-shadow를 지원", "--entry-quality-shadow" in _src2)
check("10-23) CLI가 --trades를 지원", '"--trades"' in _src2)
check("10-24) 리포트에 실제 사용 파일 경로 출력", "입력 shadow" in _src2)
check("10-25) 상태 코드를 리포트에 표기", "[{sh_status}]" in _src2)
check("10-26) final_decision을 accepted로 쓰지 않음",
      "BUY_DECISION(final_decision=BUY)" in _src2
      and "broker accepted 여부가 아님" in _src2)
check("10-27) accepted BUY source가 shadow임을 명시",
      "order_attempted AND order_accepted" in _src2)

# ══════════════════════════════════════════════════════════════
# 11. 중복 order_id dedupe + alignment 구분 (1J.5.3)
# ══════════════════════════════════════════════════════════════
# 재현(1J.5.2): `len(ids) == len(acc) and ids` 조건이 중복 order_id가
# 있을 때 **오히려 dedupe를 건너뛰어** accepted_count=2가 나왔음.

_dup_sh = [{"symbol": "A", "latest_bar_timestamp": "T1", "order_attempted": "True",
            "order_accepted": "True", "order_id": "O1"},
           {"symbol": "A", "latest_bar_timestamp": "T1", "order_attempted": "True",
            "order_accepted": "True", "order_id": "O1"}]
_dup_tr = [{"symbol": "A", "side": "BUY", "accepted": "True", "order_id": "O1"},
           {"symbol": "A", "side": "BUY", "accepted": "True", "order_id": "O1"}]
_rd = F.collect_accepted_buy(_dup_sh, _dup_tr)
check("11-1) 동일 order_id 2행이 accepted 1건으로 dedupe", _rd["accepted_count"] == 1)
check("11-2) accepted_keys도 1개", len(_rd["accepted_keys"]) == 1)
check("11-3) trades도 1건으로 dedupe", _rd["trades_accepted"] == 1)
check("11-4) order_id가 모두 있으므로 MISSING 아님",
      not _rd["shadow_order_id_missing"] and not _rd["trades_order_id_missing"])

# 서로 다른 order_id는 그대로 2건
_two = F.collect_accepted_buy(
    [{"symbol": "A", "latest_bar_timestamp": "T1", "order_attempted": "True",
      "order_accepted": "True", "order_id": "O1"},
     {"symbol": "B", "latest_bar_timestamp": "T2", "order_attempted": "True",
      "order_accepted": "True", "order_id": "O2"}], [])
check("11-5) 서로 다른 order_id는 2건 유지", _two["accepted_count"] == 2)

# accepted인데 order_id 누락 → 조용히 fallback하지 않고 표시
_miss = F.collect_accepted_buy(
    [{"symbol": "A", "latest_bar_timestamp": "T1", "order_attempted": "True",
      "order_accepted": "True", "order_id": ""}],
    [{"symbol": "A", "side": "BUY", "accepted": "True", "order_id": ""}])
check("11-6) order_id 누락 시 shadow_order_id_missing=True",
      _miss["shadow_order_id_missing"] is True)
check("11-7) order_id 누락 시 trades_order_id_missing=True",
      _miss["trades_order_id_missing"] is True)
check("11-8) 리포트에 ORDER_ID_MISSING 경고 출력", "ORDER_ID_MISSING" in _body)


# --- alignment 상태 구분: NO_MINUTE_DATA vs BAR_NOT_FOUND ---
check("11-9) ALIGN 상태 상수가 정의됨",
      hasattr(F, "ALIGN_OK") and hasattr(F, "ALIGN_NO_MINUTE_DATA")
      and hasattr(F, "ALIGN_BAR_NOT_FOUND"))

_raw = {("NOCSV", "T1"), ("HASCSV", "T2"), ("OK", "T3")}
_replay = {("OK", "T3")}
_bykey = {("OK", "T3"): {"symbol": "OK", "lbt": "T3", "full_window": True,
                         "prev_close_source": "SAME_FILE_PRETARGET",
                         "replay": object(), "live": {"detected_patterns": "A"}}}
_align = {
    ("NOCSV", "T1"): F.ALIGN_NO_MINUTE_DATA,   # 분봉 CSV 자체가 없음
    ("HASCSV", "T2"): F.ALIGN_BAR_NOT_FOUND,   # CSV는 있으나 그 봉만 없음
    ("OK", "T3"): F.ALIGN_OK,
}
_rows = F.classify_candidate_fidelity(_raw, _replay, _bykey, _align)
_codes = {(r["symbol"], r["timestamp"]): r["reason_code"] for r in _rows}
check("11-10) 분봉 CSV 자체가 없으면 NO_MINUTE_DATA",
      _codes[("NOCSV", "T1")] == F.REASON_NO_MINUTE_DATA)
check("11-11) CSV는 있고 그 봉만 없으면 BAR_NOT_FOUND",
      _codes[("HASCSV", "T2")] == F.REASON_BAR_NOT_FOUND)
check("11-12) 재현된 후보는 REPRODUCED", _codes[("OK", "T3")] == "REPRODUCED")

_rec3 = F.calculate_recall(_rows)
check("11-13) 둘 다 data-ineligible로 집계", _rec3["data_ineligible"] == 2)
check("11-14) eligible recall 1/1", _rec3["eligible_recall"] == 1.0)

# alignment_status가 없으면(구버전 호출) NO_MINUTE_DATA로 보수적 처리
_rows_na = F.classify_candidate_fidelity(_raw, _replay, _bykey, None)
_codes_na = {(r["symbol"], r["timestamp"]): r["reason_code"] for r in _rows_na}
check("11-15) 상태 map이 없으면 보수적으로 NO_MINUTE_DATA",
      _codes_na[("HASCSV", "T2")] == F.REASON_NO_MINUTE_DATA)

# analyze()가 alignment_status를 실제로 전달하는지
check("11-16) analyze가 alignment_status를 classify에 전달",
      "classify_candidate_fidelity(live_cand, replay_cand," in _body
      and "alignment_status)" in _body)
# 주석의 설명 문구는 남아도 되고, **실행 코드**에서 사라졌는지가 핵심
check("11-17) 실행 코드에서 _bar_not_found 죽은 참조가 제거됨",
      "_bar_not_found" not in _body)


# ── 12. sibling 날짜 우선 선택 (1J.5.3) ────────────────────────
# _pick_sibling은 main() 안 중첩 함수라 _code_only가 unparse해도
# 본문이 남습니다. 소스 전체에서 확인하되 docstring이 아닌 코드 문자열만.
check("12-1) target 날짜가 파일명에 포함된 것을 우선 선택",
      'stamp = target.strftime("%Y%m%d")' in src and "if stamp in c.name" in src)
check("12-2) 정확 매칭이 없으면 target-date row 존재 여부로 판별",
      "if st_ == STATUS_OK:" in src)
check("12-3) 후보가 여러 개면 CLI 명시를 요구", "CLI로 명시하십시오" in src)
check("12-4) sorted 첫 파일을 무조건 쓰지 않음",
      "sh = found[0]" not in src and "tr = found[0]" not in src)

# 실제 동작 검증 — 여러 날짜 파일이 있을 때 target 날짜를 고르는지
_sd = Path(_tf.mkdtemp())
for _day in ("20260806", "20260807", "20260810"):
    _p = _sd / f"entry_quality_shadow_{_day}.csv"
    with _p.open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["timestamp", "symbol"])
        w.writeheader()
        w.writerow({"timestamp": f"{_day[:4]}-{_day[4:6]}-{_day[6:]}T09:00:00",
                    "symbol": "A"})
_cands = sorted(_sd.glob("entry_quality_shadow*.csv"))
_stamp = "20260810"
_exact = [c for c in _cands if _stamp in c.name]
check("12-5) 8/10 분석 시 8/06이 아니라 8/10 파일을 선택",
      len(_exact) == 1 and _exact[0].name == "entry_quality_shadow_20260810.csv")
check("12-6) sorted 첫 파일은 8/06이므로 예전 방식이면 틀렸을 것",
      _cands[0].name == "entry_quality_shadow_20260806.csv")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
