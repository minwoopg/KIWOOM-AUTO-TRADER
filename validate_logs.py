#!/usr/bin/env python3
"""로그 품질 검증 스크립트

실행:
    python validate_logs.py                # 오늘 날짜
    python validate_logs.py 2026-05-27     # 특정 날짜

결과: 콘솔 출력. 문제가 발견되면 exit code 1로 종료합니다.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

SIGNAL_LOG = Path("logs/signal_log.csv")
TRADES_LOG  = Path("logs/trades.csv")

# ── 기대 필드 목록 ────────────────────────────────────────────────
SIGNAL_REQUIRED = [
    "timestamp", "symbol", "price", "regime", "signal", "skip_reason",
    "detected_patterns", "is_v_rebound", "v_drop_pct", "v_rise_pct",
    "v_low_age", "current_vs_vwap_pct", "volume_ratio", "bar_amount",
    "rebound_volume_spike", "v_bottom_spike", "upside_to_recent_high_pct",
]
TRADE_REQUIRED = [
    "timestamp", "symbol", "side", "quantity", "price", "accepted",
    "entry_strategy", "market_regime", "entry_score", "entry_reason",
    "is_v_rebound", "exit_reason", "hold_minutes",
]

WARN  = "⚠️ "
ERROR = "❌ "
OK    = "✅ "
INFO  = "   "


def load_rows(path: Path, target: date) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"]).date()
                if ts == target:
                    rows.append(r)
            except (ValueError, KeyError):
                continue
    return rows


def check_signal_log(target: date) -> tuple[int, int]:
    """signal_log.csv 품질 검사. (errors, warnings) 반환."""
    print(f"\n{'─'*55}")
    print("  📋 signal_log.csv 검사")
    print(f"{'─'*55}")

    errors = warnings = 0

    if not SIGNAL_LOG.exists():
        print(f"{ERROR}파일 없음: {SIGNAL_LOG}")
        return 1, 0

    rows = load_rows(SIGNAL_LOG, target)
    if not rows:
        print(f"{WARN}대상 날짜({target}) 데이터 없음")
        return 0, 1

    print(f"{INFO}총 {len(rows):,}건")

    # 1. 필드 누락 검사
    if rows:
        missing_cols = [c for c in SIGNAL_REQUIRED if c not in rows[0]]
        if missing_cols:
            print(f"{ERROR}필수 컬럼 누락: {missing_cols}")
            errors += 1
        else:
            print(f"{OK}필수 컬럼 모두 존재")

    # 2. BUY인데 skip_reason이 BUY가 아닌 경우
    bad_buy = [r for r in rows
               if r.get("signal") == "BUY"
               and r.get("skip_reason", "").upper() not in ("BUY", "")]
    if bad_buy:
        print(f"{WARN}BUY인데 skip_reason 이상: {len(bad_buy)}건")
        warnings += 1
    else:
        print(f"{OK}BUY skip_reason 정상")

    # 3. HOLD인데 skip_reason 비어있는 경우
    hold_rows = [r for r in rows if r.get("signal") != "BUY"]
    empty_reason = [r for r in hold_rows if not r.get("skip_reason", "").strip()]
    if empty_reason:
        print(f"{ERROR}SKIP/HOLD인데 skip_reason 비어있음: {len(empty_reason)}건")
        for r in empty_reason[:3]:
            print(f"    {r.get('timestamp','')}  {r.get('symbol','')}  {r.get('signal','')}")
        errors += 1
    else:
        print(f"{OK}skip_reason 누락 없음")

    # 4. V자 관련 필드가 전부 0인지 확인
    v_fields = ["v_drop_pct", "v_rise_pct", "v_low_age", "volume_ratio"]
    all_zero = all(
        all(r.get(f, "0") in ("0", "0.0", "") for r in rows)
        for f in v_fields
    )
    if all_zero:
        print(f"{WARN}V자 관련 필드가 전부 0 또는 빈값 — MinuteAnalyzer 정상 작동 여부 확인 필요")
        warnings += 1
    else:
        print(f"{OK}V자 필드 정상 기록됨")

    # 5. skip_reason 분포 요약
    buy_cnt  = sum(1 for r in rows if r.get("signal") == "BUY")
    skip_cnt = len(rows) - buy_cnt
    print(f"{INFO}BUY {buy_cnt}건 / SKIP {skip_cnt}건")
    reason_cnt = Counter(r.get("skip_reason","") for r in hold_rows)
    for reason, cnt in reason_cnt.most_common(5):
        print(f"    {(reason or '(없음)'):<40} {cnt}건")

    # 6. detected_patterns 분포
    pat_cnt = Counter(r.get("detected_patterns", "-") for r in rows)
    v_cnt   = sum(1 for r in rows if r.get("is_v_rebound","").lower() == "true")
    print(f"{INFO}V자 감지: {v_cnt}건 / 전체 {len(rows)}건")
    print(f"{INFO}패턴 분포: " + " | ".join(f"{p}:{c}" for p,c in pat_cnt.most_common(5)))

    return errors, warnings


def check_trades_log(target: date) -> tuple[int, int]:
    """trades.csv 품질 검사. (errors, warnings) 반환."""
    print(f"\n{'─'*55}")
    print("  📈 trades.csv 검사")
    print(f"{'─'*55}")

    errors = warnings = 0

    if not TRADES_LOG.exists():
        print(f"{ERROR}파일 없음: {TRADES_LOG}")
        return 1, 0

    rows = load_rows(TRADES_LOG, target)
    if not rows:
        print(f"{WARN}대상 날짜({target}) 데이터 없음")
        return 0, 1

    accepted = [r for r in rows if str(r.get("accepted","")).lower() == "true"]
    buys  = [r for r in accepted if r.get("side") == "BUY"]
    sells = [r for r in accepted if r.get("side") == "SELL"]
    print(f"{INFO}총 {len(rows):,}건  (매수 {len(buys)}건 / 매도 {len(sells)}건)")

    # 1. 필드 누락
    if rows:
        missing_cols = [c for c in TRADE_REQUIRED if c not in rows[0]]
        if missing_cols:
            print(f"{ERROR}필수 컬럼 누락: {missing_cols}")
            errors += 1
        else:
            print(f"{OK}필수 컬럼 모두 존재")

    # 2. BUY인데 entry_score/entry_reason 비어있는 경우
    bad_entry = [r for r in buys
                 if not r.get("entry_score","").strip()
                 or not r.get("entry_reason","").strip()]
    if bad_entry:
        print(f"{ERROR}BUY인데 entry_score/entry_reason 비어있음: {len(bad_entry)}건")
        for r in bad_entry[:3]:
            print(f"    {r.get('timestamp','')}  {r.get('symbol','')}  score={r.get('entry_score','?')}")
        errors += 1
    else:
        if buys:
            print(f"{OK}entry_score/entry_reason 정상 기록")

    # 3. SELL인데 exit_reason 비어있는 경우
    bad_exit = [r for r in sells if not r.get("exit_reason","").strip()]
    if bad_exit:
        print(f"{WARN}SELL인데 exit_reason 비어있음: {len(bad_exit)}건")
        warnings += 1
    else:
        if sells:
            print(f"{OK}exit_reason 정상 기록")

    # 4. SELL인데 hold_minutes 비어있는 경우
    bad_hold = [r for r in sells if not r.get("hold_minutes","").strip()]
    if bad_hold:
        print(f"{WARN}SELL인데 hold_minutes 비어있음: {len(bad_hold)}건")
        warnings += 1
    else:
        if sells:
            print(f"{OK}hold_minutes 정상 기록")

    # 5. price=0 체결 확인
    zero_price = [r for r in accepted if int(r.get("price","0") or 0) == 0]
    if zero_price:
        print(f"{ERROR}price=0 체결 기록 발견: {len(zero_price)}건 — 강제청산 또는 체결가 미기록")
        errors += 1
    else:
        print(f"{OK}price=0 체결 없음")

    # 6. exit_reason 분포
    if sells:
        exit_cnt = Counter(r.get("exit_reason","(없음)") for r in sells)
        print(f"{INFO}exit_reason: " + " | ".join(f"{e}:{c}" for e,c in exit_cnt.most_common()))

    return errors, warnings


def main():
    args  = sys.argv[1:]
    today = date.today()
    target = date.fromisoformat(args[0]) if args else today

    print(f"\n{'═'*55}")
    print(f"  🔍 로그 품질 검증  {target}")
    print(f"{'═'*55}")

    e1, w1 = check_signal_log(target)
    e2, w2 = check_trades_log(target)

    total_errors   = e1 + e2
    total_warnings = w1 + w2

    print(f"\n{'─'*55}")
    if total_errors == 0 and total_warnings == 0:
        print(f"{OK}모든 검사 통과")
    else:
        if total_errors > 0:
            print(f"{ERROR}오류 {total_errors}건  →  로그 구조 점검 필요")
        if total_warnings > 0:
            print(f"{WARN}경고 {total_warnings}건  →  확인 권장")
    print(f"{'─'*55}\n")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
