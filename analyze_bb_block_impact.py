#!/usr/bin/env python3
"""볼린저 상단돌파(%B>=1.0) 차단 가상 성과 분석

signal_log.csv에서 "볼린저 상단돌파 차단"으로 막힌 시점을 찾아,
그때 실제로 매수했다면 5/10/20분 후 어떻게 됐을지 1분봉으로 계산합니다.

같은 종목이 짧은 간격(10초 폴링)으로 반복 차단되는 경우, 연속된 것을
하나의 "이벤트"로 묶어 첫 차단 시점만 매수 시뮬레이션합니다.

실행:
    python analyze_bb_block_impact.py                     # 오늘
    python analyze_bb_block_impact.py 2026-07-03           # 특정 날짜
    python analyze_bb_block_impact.py 2026-07-01 2026-07-03  # 범위

결과: 콘솔 출력 + reports/bb_block_impact_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from replay_runner import load_bars, ROUND_TRIP_COST_PCT, SLIPPAGE_PCT, TOTAL_COST_PCT
# replay_runner import 시점에 sys.stdout이 이미 UTF-8로 재래핑됨 (중복 래핑 금지)

SIGNAL_LOG = Path("logs/signal_log.csv")
REPORTS_DIR = Path("reports")
AFTER_MINUTES = [5, 10, 20]

# 연속 차단을 하나의 이벤트로 묶는 최소 간격 (분) — 이보다 벌어지면 새 이벤트
EVENT_GAP_MINUTES = 3


def load_bb_block_rows(start: date, end: date) -> list[dict]:
    """signal_log에서 볼린저 상단돌파 차단 행만 추출합니다."""
    if not SIGNAL_LOG.exists():
        print(f"[ERROR] {SIGNAL_LOG} 파일이 없습니다.")
        sys.exit(1)

    rows = []
    with SIGNAL_LOG.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            reason = r.get("skip_reason", "") or ""
            if not reason.startswith("볼린저 상단돌파 차단"):
                continue
            try:
                ts = datetime.fromisoformat(r["timestamp"])
            except (ValueError, KeyError):
                continue
            if not (start <= ts.date() <= end):
                continue
            m = re.search(r"%B=([\d.]+)", reason)
            bb_val = float(m.group(1)) if m else None
            rows.append({
                "timestamp": ts,
                "symbol": r.get("symbol", ""),
                "price": r.get("price", ""),
                "bb_percent_b": bb_val,
            })
    return rows


def group_into_events(rows: list[dict]) -> list[dict]:
    """같은 종목의 연속 차단(간격<=EVENT_GAP_MINUTES)을 하나의 이벤트로 묶습니다."""
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    events = []
    for symbol, rs in by_symbol.items():
        rs.sort(key=lambda x: x["timestamp"])
        current_event = None
        for r in rs:
            if current_event is None:
                current_event = {"symbol": symbol, "start": r, "last_ts": r["timestamp"], "count": 1}
            elif r["timestamp"] - current_event["last_ts"] <= timedelta(minutes=EVENT_GAP_MINUTES):
                current_event["last_ts"] = r["timestamp"]
                current_event["count"] += 1
            else:
                events.append(current_event)
                current_event = {"symbol": symbol, "start": r, "last_ts": r["timestamp"], "count": 1}
        if current_event:
            events.append(current_event)
    return events


def simulate_event(event: dict) -> dict | None:
    """이벤트 시작 시점에 매수했다면 5/10/20분 후 순수익률을 계산합니다."""
    symbol = event["symbol"]
    entry_ts = event["start"]["timestamp"]
    bars = load_bars(symbol, entry_ts.date())
    if not bars:
        return None

    # entry_ts와 가장 가까운(같거나 직후) 분봉 인덱스 찾기
    # cntr_tm은 'YYYYMMDDHHMMSS' 14자리 형식 (replay_runner의 get_time_bucket과 동일 슬라이싱)
    entry_idx = None
    for i, b in enumerate(bars):
        tm = b.cntr_tm.strip()
        try:
            if len(tm) >= 14:
                bar_time = datetime.strptime(tm[8:14], "%H%M%S").time()
            elif len(tm) == 6:
                bar_time = datetime.strptime(tm, "%H%M%S").time()
            else:
                continue
        except ValueError:
            continue
        if bar_time >= entry_ts.time():
            entry_idx = i
            break
    if entry_idx is None:
        return None

    entry_price = bars[entry_idx].close_price
    if entry_price <= 0:
        return None

    after_pcts: dict[int, float | None] = {}
    for m in AFTER_MINUTES:
        idx = entry_idx + m
        if idx < len(bars):
            ap = bars[idx].close_price
            after_pcts[m] = (ap - entry_price) / entry_price * 100
        else:
            after_pcts[m] = None

    return {
        "symbol": symbol,
        "entry_time": entry_ts.strftime("%H:%M:%S"),
        "entry_price": entry_price,
        "bb_percent_b": event["start"]["bb_percent_b"],
        "block_count": event["count"],
        "net_5m":  (after_pcts.get(5)  - TOTAL_COST_PCT) if after_pcts.get(5)  is not None else None,
        "net_10m": (after_pcts.get(10) - TOTAL_COST_PCT) if after_pcts.get(10) is not None else None,
        "net_20m": (after_pcts.get(20) - TOTAL_COST_PCT) if after_pcts.get(20) is not None else None,
    }


def analyze(start: date, end: date) -> str:
    lines = []
    W = 66

    def sep(c="═"): lines.append(c * W)
    def title(t): sep(); lines.append(f"  {t}"); sep()
    def sub(t): lines.append(""); lines.append(f"── {t} ──")
    def row(label, val): lines.append(f"  {label:<32} {val}")

    title(f"📊 볼린저 상단돌파 차단 가상 성과 분석  {start} ~ {end}")

    rows = load_bb_block_rows(start, end)
    if not rows:
        lines.append("")
        lines.append("  해당 기간에 볼린저 상단돌파 차단 기록이 없습니다.")
        sep()
        return "\n".join(lines)

    events = group_into_events(rows)
    lines.append("")
    row("차단 원시 로그", f"{len(rows):,}건")
    row("이벤트로 압축 (같은 종목 연속차단 묶음)", f"{len(events):,}건")

    sims = []
    for ev in events:
        s = simulate_event(ev)
        if s is not None:
            sims.append(s)

    if not sims:
        lines.append("")
        lines.append("  1분봉 데이터가 없어 가상 시뮬레이션을 할 수 없습니다.")
        sep()
        return "\n".join(lines)

    sub("가상 매수 시 성과 (차단 시점 매수 가정, 비용 반영)")
    row("시뮬레이션 가능 이벤트", f"{len(sims):,}건")

    for horizon in ["net_5m", "net_10m", "net_20m"]:
        vals = [s[horizon] for s in sims if s[horizon] is not None]
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        label = horizon.replace("net_", "").replace("m", "분 후")
        row(f"  {label}", f"{len(vals)}건  승률 {win}/{len(vals)} ({win/len(vals)*100:.0f}%)  평균 {avg:+.2f}%")

    # 결론 힌트 (5분 기준)
    vals5 = [s["net_5m"] for s in sims if s["net_5m"] is not None]
    if vals5:
        avg5 = sum(vals5) / len(vals5)
        lines.append("")
        if avg5 < -0.3:
            lines.append(f"  → 5분 평균 {avg5:+.2f}% : 차단이 손실을 막아준 것으로 보임 (차단 유지 권장)")
        elif avg5 > 0.3:
            lines.append(f"  → 5분 평균 {avg5:+.2f}% : 차단이 기회비용을 만들고 있을 수 있음 (표본 확인 필요)")
        else:
            lines.append(f"  → 5분 평균 {avg5:+.2f}% : 뚜렷한 방향성 없음 (더 지켜볼 것)")
        lines.append("  ⚠️ 표본이 20건 미만이면 위 결론은 참고용입니다. 완화/강화 결정에 쓰지 마세요.")

    sub("이벤트 상세 목록")
    for s in sims:
        n5  = f"{s['net_5m']:+.2f}%"  if s['net_5m']  is not None else "  ?"
        n10 = f"{s['net_10m']:+.2f}%" if s['net_10m'] is not None else "  ?"
        n20 = f"{s['net_20m']:+.2f}%" if s['net_20m'] is not None else "  ?"
        row(
            f"  {s['symbol']} {s['entry_time']} (%B={s['bb_percent_b']:.2f}, 차단{s['block_count']}회)",
            f"5m {n5}  10m {n10}  20m {n20}",
        )

    sep()
    return "\n".join(lines)


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

    report = analyze(start, end)
    print(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"bb_block_impact_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
