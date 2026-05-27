#!/usr/bin/env python3
"""1분봉 저장 데이터 품질 검증 스크립트

실행:
    python validate_minute_bars.py                # 오늘
    python validate_minute_bars.py 2026-05-27     # 특정 날짜

결과: 콘솔 출력 + reports/minute_bar_quality_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR     = Path("reports")

WARN  = "⚠️ "
ERROR = "❌ "
OK    = "✅ "
INFO  = "   "


def check_symbol(symbol: str, path: Path) -> tuple[int, int, list[str]]:
    """단일 종목 CSV를 검사합니다. (errors, warnings, messages) 반환."""
    errors = warnings = 0
    msgs: list[str] = []

    if not path.exists():
        return 0, 0, []

    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    if not rows:
        msgs.append(f"{WARN}{symbol}: 데이터 없음")
        return 0, 1, msgs

    msgs.append(f"{INFO}{symbol}: {len(rows)}봉")

    # 1. timestamp 중복
    tms = [r["cntr_tm"] for r in rows]
    dupes = len(tms) - len(set(tms))
    if dupes > 0:
        msgs.append(f"{ERROR}{symbol}: timestamp 중복 {dupes}건")
        errors += 1
    else:
        msgs.append(f"{OK}{symbol}: timestamp 중복 없음")

    # 2. timestamp 정렬
    if tms != sorted(tms):
        msgs.append(f"{ERROR}{symbol}: timestamp 오름차순 정렬 깨짐")
        errors += 1
    else:
        msgs.append(f"{OK}{symbol}: timestamp 정렬 정상")

    # 3. OHLCV 0값 / 누락
    zero_rows = []
    for i, r in enumerate(rows):
        try:
            o = float(r.get("open", 0) or 0)
            h = float(r.get("high", 0) or 0)
            l = float(r.get("low", 0) or 0)
            c = float(r.get("close", 0) or 0)
            v = float(r.get("volume", 0) or 0)
            if any(x <= 0 for x in [o, h, l, c]):
                zero_rows.append(r["cntr_tm"])
        except (ValueError, TypeError):
            zero_rows.append(r.get("cntr_tm", f"row{i}"))

    if zero_rows:
        msgs.append(f"{ERROR}{symbol}: OHLC 0값/누락 {len(zero_rows)}건 — {zero_rows[:3]}")
        errors += 1
    else:
        msgs.append(f"{OK}{symbol}: OHLC 값 정상")

    # 4. high >= low, high >= open/close, low <= open/close
    logic_errors = []
    for r in rows:
        try:
            o = float(r["open"]); h = float(r["high"])
            l = float(r["low"]);  c = float(r["close"])
            if not (h >= l and h >= o and h >= c and l <= o and l <= c):
                logic_errors.append(r["cntr_tm"])
        except (ValueError, TypeError, KeyError):
            pass

    if logic_errors:
        msgs.append(f"{ERROR}{symbol}: OHLC 논리 오류 {len(logic_errors)}건 — {logic_errors[:3]}")
        errors += 1
    else:
        msgs.append(f"{OK}{symbol}: OHLC 논리 정상")

    # 5. volume 음수
    neg_vol = [r["cntr_tm"] for r in rows
               if float(r.get("volume", 0) or 0) < 0]
    if neg_vol:
        msgs.append(f"{WARN}{symbol}: volume 음수 {len(neg_vol)}건")
        warnings += 1

    return errors, warnings, msgs


def validate(target_date: date) -> str:
    lines: list[str] = []
    W = 55

    def sep(c="═"): lines.append(c * W)
    sep()
    lines.append(f"  🔍 1분봉 품질 검증  {target_date}")
    sep()

    date_dir = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d")
    if not date_dir.exists():
        lines.append(f"{WARN}  저장 경로 없음: {date_dir}")
        lines.append(f"  save_minute_bars: true 설정 여부 확인")
        lines.append("─" * W)
        return "\n".join(lines)

    csv_files = sorted(date_dir.glob("*.csv"))
    if not csv_files:
        lines.append(f"{WARN}  CSV 파일 없음")
        lines.append("─" * W)
        return "\n".join(lines)

    lines.append(f"{INFO}  종목 수: {len(csv_files)}개")
    lines.append("")

    total_errors = total_warnings = 0
    for csv_path in csv_files:
        symbol = csv_path.stem
        e, w, msgs = check_symbol(symbol, csv_path)
        total_errors   += e
        total_warnings += w
        for m in msgs:
            lines.append(f"  {m}")

    lines.append("")
    sep("─")
    if total_errors == 0 and total_warnings == 0:
        lines.append(f"{OK}  전체 {len(csv_files)}종목 검사 통과")
    else:
        if total_errors > 0:
            lines.append(f"{ERROR}  오류 {total_errors}건 — 리플레이 전 수정 필요")
        if total_warnings > 0:
            lines.append(f"{WARN}  경고 {total_warnings}건 — 확인 권장")
    sep("─")

    return "\n".join(lines)


def main():
    args   = sys.argv[1:]
    today  = date.today()
    target = date.fromisoformat(args[0]) if args else today

    report = validate(target)
    print(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"minute_bar_quality_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
