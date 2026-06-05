#!/usr/bin/env python3
"""signal_log.csv 분석 스크립트

실행:
    python analyze_signal_log.py                          # 오늘 날짜
    python analyze_signal_log.py 2026-05-27               # 특정 날짜
    python analyze_signal_log.py 2026-05-27 2026-05-28   # 날짜 범위

결과: 콘솔 출력 + reports/signal_analysis_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path


# ── 설정 ────────────────────────────────────────────────────────
SIGNAL_LOG = Path("logs/signal_log.csv")
REPORTS_DIR = Path("reports")


# ── 유틸 ────────────────────────────────────────────────────────
def pct(n: int, total: int) -> str:
    return f"{n/total*100:.1f}%" if total > 0 else "0.0%"

def safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if f != 0.0 else None
    except (ValueError, TypeError):
        return None

def safe_bool(v) -> bool | None:
    if str(v).lower() == "true":  return True
    if str(v).lower() == "false": return False
    return None


# ── 데이터 로드 ──────────────────────────────────────────────────
def load(start: date, end: date) -> list[dict]:
    if not SIGNAL_LOG.exists():
        print(f"[ERROR] {SIGNAL_LOG} 파일이 없습니다.")
        sys.exit(1)

    rows = []
    with SIGNAL_LOG.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"]).date()
            except (ValueError, KeyError):
                continue
            if start <= ts <= end:
                rows.append(r)
    return rows


# ── 분석 ────────────────────────────────────────────────────────
def analyze(rows: list[dict], start: date, end: date) -> str:
    lines = []
    W = 60

    def sep(c="═"): lines.append(c * W)
    def title(t): sep(); lines.append(f"  {t}"); sep()
    def sub(t): lines.append(""); lines.append(f"── {t} ──")
    def row(label, val, note=""): 
        n = f"  ({note})" if note else ""
        lines.append(f"  {label:<36} {val}{n}")

    title(f"📊 signal_log 분석  {start} ~ {end}  (총 {len(rows):,}건)")

    if not rows:
        lines.append("  데이터가 없습니다.")
        return "\n".join(lines)

    total = len(rows)
    buy_rows  = [r for r in rows if r.get("signal") == "BUY"]
    hold_rows = [r for r in rows if r.get("signal") != "BUY"]

    # ── 1. 시그널 분포 ──────────────────────────────────────────
    sub("1. 시그널 분포")
    row("전체 판단", f"{total:,}건")
    row("BUY",         f"{len(buy_rows):,}건  ({pct(len(buy_rows), total)})")
    row("BLOCKED",     f"{len(blocked_rows):,}건  ({pct(len(blocked_rows), total)})")
    row("HOLD / SKIP", f"{len(hold_rows):,}건  ({pct(len(hold_rows), total)})")

    # ── 2. 장세 분포 ──────────────────────────────────────────
    sub("2. 장세 분포")
    regime_cnt = Counter(r.get("regime","") for r in rows)
    for regime, cnt in regime_cnt.most_common():
        row(regime or "(없음)", f"{cnt:,}건  ({pct(cnt, total)})")

    # ── 3. skip_reason 분포 ─────────────────────────────────
    sub("3. skip_reason 분포  (BUY 제외)")
    reason_cnt = Counter(r.get("skip_reason","") for r in hold_rows)
    for reason, cnt in reason_cnt.most_common():
        row(reason or "(없음)", f"{cnt:,}건  ({pct(cnt, len(hold_rows))})")

    # ── 3-1. NO_PATTERN 세분화 ────────────────────────────
    no_pat_rows = [
        r for r in hold_rows
        if r.get("skip_reason","").startswith("NO_PAT_")
        or r.get("skip_reason","") == "SKIP_NO_PATTERN"
    ]
    if no_pat_rows:
        sub("3-1. NO_PATTERN 세분화")
        # skip_reason에서 NO_PAT_ 계열 값만 추출
        def _coarse_reason(r):
            sr = r.get("skip_reason","")
            # 숫자 포함된 세부 사유는 prefix만 추출 (예: NO_PAT_A_RATE(+1.3%) → NO_PAT_A_RATE)
            import re
            m = re.match(r'(NO_PAT_[A-Z_]+)', sr)
            return m.group(1) if m else sr
        coarse_cnt = Counter(_coarse_reason(r) for r in no_pat_rows)
        for reason, cnt in coarse_cnt.most_common():
            row(f"  {reason}", f"{cnt:,}건  ({pct(cnt, len(no_pat_rows))})")
        # 값 분포 (등락률, 반등폭, 눌림폭)
        a_rate_vals = []
        b_reb_vals  = []
        c_pull_vals = []
        import re
        for r in no_pat_rows:
            sr = r.get("skip_reason","")
            m = re.search(r'NO_PAT_A_RATE\(([+-]?[\d.]+)%\)', sr)
            if m: a_rate_vals.append(float(m.group(1)))
            m = re.search(r'NO_PAT_B_REBOUND_SMALL\(([+-]?[\d.]+)%\)', sr)
            if m: b_reb_vals.append(float(m.group(1)))
            m = re.search(r'NO_PAT_C_PULLBACK\(([+-]?[\d.]+)%\)', sr)
            if m: c_pull_vals.append(float(m.group(1)))
        if a_rate_vals:
            row("  A 등락률 평균", f"{sum(a_rate_vals)/len(a_rate_vals):+.2f}%",
                f"(min {min(a_rate_vals):+.1f}% max {max(a_rate_vals):+.1f}%)")
        if b_reb_vals:
            row("  B 반등폭 평균", f"{sum(b_reb_vals)/len(b_reb_vals):+.2f}%",
                f"(min {min(b_reb_vals):+.1f}% max {max(b_reb_vals):+.1f}%)")
        if c_pull_vals:
            row("  C 눌림폭 평균", f"{sum(c_pull_vals)/len(c_pull_vals):+.2f}%",
                f"(min {min(c_pull_vals):+.1f}% max {max(c_pull_vals):+.1f}%)")

    # ── 4. detected_patterns 분포 ───────────────────────────
    sub("4. 감지 패턴 분포")
    pat_cnt = Counter(r.get("detected_patterns","-") for r in rows)
    for pat, cnt in pat_cnt.most_common():
        buy_cnt = sum(1 for r in rows 
                      if r.get("detected_patterns") == pat 
                      and r.get("signal") == "BUY")
        row(pat or "-", f"{cnt:,}건  →  BUY {buy_cnt}건  ({pct(buy_cnt,cnt)})")

    # ── 5. V자 반등 분석 ───────────────────────────────────
    sub("5. V자 반등 분석")
    v_detected = [r for r in rows if safe_bool(r.get("is_v_rebound")) is True]
    v_buy      = [r for r in v_detected if r.get("signal") == "BUY"]
    row("V자 감지", f"{len(v_detected):,}건  ({pct(len(v_detected), total)})")
    row("V자 → BUY", f"{len(v_buy):,}건  ({pct(len(v_buy), len(v_detected))})")

    if v_detected:
        drops   = [safe_float(r.get("v_drop_pct"))   for r in v_detected]
        rises   = [safe_float(r.get("v_rise_pct"))   for r in v_detected]
        ages    = [safe_float(r.get("v_low_age"))     for r in v_detected]
        drops  = [x for x in drops  if x is not None]
        rises  = [x for x in rises  if x is not None]
        ages   = [x for x in ages   if x is not None]
        if drops:  row("v_drop_pct  평균", f"{sum(drops)/len(drops):+.2f}%")
        if rises:  row("v_rise_pct  평균", f"{sum(rises)/len(rises):+.2f}%")
        if ages:   row("v_low_age   평균", f"{sum(ages)/len(ages):.1f}봉")

        rspike = Counter(str(safe_bool(r.get("rebound_volume_spike"))) for r in v_detected)
        bspike = Counter(str(safe_bool(r.get("v_bottom_spike")))      for r in v_detected)
        row("rebound_volume_spike=True",  f"{rspike.get('True',0):,}건  ({pct(rspike.get('True',0), len(v_detected))})")
        row("v_bottom_spike=True",        f"{bspike.get('True',0):,}건  ({pct(bspike.get('True',0), len(v_detected))})")

    # v_low_age 분포 (전체 기준)
    sub("  v_low_age 분포 (V자 감지 여부 무관)")
    age_buckets: dict[str, int] = defaultdict(int)
    for r in rows:
        age = safe_float(r.get("v_low_age"))
        if age is None: continue
        if age <= 3:    age_buckets["1~3봉"] += 1
        elif age <= 5:  age_buckets["4~5봉"] += 1
        elif age <= 8:  age_buckets["6~8봉"] += 1
        elif age <= 15: age_buckets["9~15봉"] += 1
        else:           age_buckets["16봉↑"] += 1
    for k in ["1~3봉","4~5봉","6~8봉","9~15봉","16봉↑"]:
        cnt = age_buckets[k]
        row(k, f"{cnt:,}건  ({pct(cnt, total)})")

    # ── 6. upside_to_recent_high 분포 ──────────────────────
    sub("6. 상승 여력 (upside_to_recent_high_pct) 분포")
    up_buckets: dict[str, int] = defaultdict(int)
    for r in rows:
        up = safe_float(r.get("upside_to_recent_high_pct"))
        if up is None: continue
        if up < 1.0:   up_buckets["< 1%"] += 1
        elif up < 2.0: up_buckets["1~2%"] += 1
        elif up < 3.0: up_buckets["2~3%"] += 1
        elif up < 5.0: up_buckets["3~5%"] += 1
        else:          up_buckets["5%↑"] += 1
    total_up = sum(up_buckets.values())
    for k in ["< 1%","1~2%","2~3%","3~5%","5%↑"]:
        cnt = up_buckets[k]
        buy_cnt = sum(1 for r in rows
                      if r.get("signal") == "BUY"
                      and (lambda u: u is not None and (
                          (k=="< 1%" and u < 1.0) or
                          (k=="1~2%" and 1.0 <= u < 2.0) or
                          (k=="2~3%" and 2.0 <= u < 3.0) or
                          (k=="3~5%" and 3.0 <= u < 5.0) or
                          (k=="5%↑"  and u >= 5.0)
                      ))(safe_float(r.get("upside_to_recent_high_pct"))))
        row(k, f"{cnt:,}건  →  BUY {buy_cnt}건")

    # ── 7. VWAP 위치 분포 ──────────────────────────────────
    sub("7. VWAP 대비 위치")
    vwap_pos = [safe_float(r.get("current_vs_vwap_pct")) for r in rows]
    vwap_pos = [x for x in vwap_pos if x is not None]
    if vwap_pos:
        above = sum(1 for x in vwap_pos if x > 0)
        below = sum(1 for x in vwap_pos if x <= 0)
        row("VWAP 위  (현재가 > VWAP)", f"{above:,}건  ({pct(above, len(vwap_pos))})")
        row("VWAP 아래", f"{below:,}건  ({pct(below, len(vwap_pos))})")
        row("평균 VWAP 거리", f"{sum(vwap_pos)/len(vwap_pos):+.2f}%")

    # ── 8. 종목별 판단 횟수 Top 10 ──────────────────────────
    sub("8. 종목별 판단 횟수 Top 10")
    sym_cnt = Counter(r.get("symbol","") for r in rows)
    for sym, cnt in sym_cnt.most_common(10):
        buy_cnt = sum(1 for r in rows if r.get("symbol")==sym and r.get("signal")=="BUY")
        row(sym, f"{cnt:,}건  →  BUY {buy_cnt}건")

    # ── 9. condition_name별 성과 ────────────────────────────
    cond_rows = [r for r in rows if r.get("condition_name","")]
    if cond_rows:
        sub("9. 조건검색식별 성과")
        cond_names = sorted(set(r["condition_name"] for r in cond_rows))
        for cname in cond_names:
            grp = [r for r in cond_rows if r.get("condition_name") == cname]
            buy  = [r for r in grp if r.get("final_decision",r.get("signal","")) == "BUY"]
            blk  = [r for r in grp if r.get("final_decision") == "BLOCKED"]
            # 주요 skip_reason
            skip = [r for r in grp if r.get("signal") not in ("BUY","")]
            skip_top = Counter(r.get("skip_reason","") for r in skip).most_common(3)
            buy_rate = f"{len(buy)/len(grp)*100:.1f}%" if grp else "-"
            row(f"  {cname}",
                f"판단 {len(grp):,}건  BUY {len(buy)}건({buy_rate})  BLOCKED {len(blk)}건")
            for sr, sc in skip_top:
                if sr:
                    lines.append(f"      skip: {sr} {sc}건")

    sep()
    return "\n".join(lines)


# ── 메인 ────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    today = date.today()

    if len(args) == 0:
        start = end = today
    elif len(args) == 1:
        start = end = date.fromisoformat(args[0])
    else:
        start = date.fromisoformat(args[0])
        end   = date.fromisoformat(args[1])

    rows   = load(start, end)
    report = analyze(rows, start, end)

    print(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"signal_analysis_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
