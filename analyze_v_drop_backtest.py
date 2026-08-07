#!/usr/bin/env python3
"""V자 낙폭 기준(-2.5%) 완화 가상 성과 백테스트

signal_log.csv에서 "낙폭부족"만 유일한 실패 사유였던 시점(다른 조건은
이미 충족)을 찾아, 그 낙폭이 병목 구간(-2.0~-2.5%)에 있었던 경우
"기준을 -2.0%로 낮췄다면 그때 V자 매수가 됐을 것"으로 보고
5/10/20분 후 실제로 어떻게 됐을지 1분봉으로 계산합니다.

실거래 없이 과거 로그+분봉만으로 답을 내는 백테스트입니다.
"낙폭부족만 유일한 사유"만 골라서, 기준 완화가 실제로 진입을 만들어냈을
경우만 검증합니다 (다른 조건도 같이 실패했다면 완화해도 안 샀을 것이므로 제외).

실행:
    python analyze_v_drop_backtest.py                     # 오늘
    python analyze_v_drop_backtest.py 2026-06-29 2026-07-06  # 기간 (권장 — 여러 날 누적)

결과: 콘솔 출력 + reports/v_drop_backtest_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from replay_runner import load_bars, TOTAL_COST_PCT
# replay_runner import 시점에 sys.stdout이 이미 UTF-8로 재래핑됨 (중복 래핑 금지)

SIGNAL_LOG = Path("logs/signal_log.csv")
REPORTS_DIR = Path("reports")
AFTER_MINUTES = [5, 10, 20]

# 병목 구간 정의 (기준 -2.5% 바로 위, 완화 후보 -2.0%)
CURRENT_THRESHOLD = -2.5
CANDIDATE_THRESHOLD = -2.0

# 연속 이벤트를 하나로 묶는 최소 간격 (분)
EVENT_GAP_MINUTES = 3


def load_bottleneck_rows(start: date, end: date) -> list[dict]:
    """signal_log에서 '낙폭부족만 유일한 실패 사유'인 병목 구간 행을 추출합니다."""
    if not SIGNAL_LOG.exists():
        print(f"[ERROR] {SIGNAL_LOG} 파일이 없습니다.")
        return []

    rows = []
    with SIGNAL_LOG.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            reason = (r.get("v_fail_reason", "") or "").strip()
            if not reason:
                continue
            # "낙폭부족"만 있고 다른 사유(반등부족/거래량부족/VWAP/MA5/거래대금)는 없어야 함
            other_keywords = ["반등부족", "반등과다", "거래량부족", "VWAP미회복",
                               "MA5조건미충족", "거래대금부족"]
            if any(kw in reason for kw in other_keywords):
                continue
            if "낙폭부족" not in reason:
                continue

            m = re.search(r"낙폭부족\(([+\-][\d.]+)%,최소([+\-][\d.]+)%\)", reason)
            if not m:
                continue
            drop_pct = float(m.group(1))
            # 병목 구간(-2.0~-2.5%)만: 완화 후보 기준(-2.0%)은 넘지만 현재 기준(-2.5%)엔 못 미침
            if not (CURRENT_THRESHOLD < drop_pct <= CANDIDATE_THRESHOLD):
                continue

            try:
                ts = datetime.fromisoformat(r["timestamp"])
            except (ValueError, KeyError):
                continue
            if not (start <= ts.date() <= end):
                continue

            rows.append({
                "timestamp": ts,
                "symbol": r.get("symbol", ""),
                "drop_pct": drop_pct,
            })
    return rows


def group_into_events(rows: list[dict]) -> list[dict]:
    """같은 종목의 연속 발생(간격<=EVENT_GAP_MINUTES)을 하나의 이벤트로 묶습니다."""
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
    """이벤트 시작 시점에 V자 매수를 했다면 5/10/20분 후 순수익률을 계산합니다."""
    symbol = event["symbol"]
    entry_ts = event["start"]["timestamp"]
    bars = load_bars(symbol, entry_ts.date())
    if not bars:
        return None

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
        "entry_time": entry_ts.strftime("%m/%d %H:%M:%S"),
        "entry_price": entry_price,
        "drop_pct": event["start"]["drop_pct"],
        "event_count": event["count"],
            # 2026-08-07 (1J.1): net_*는 Base alias, 3시나리오 병행 산출
            "gross_5m": COST_MODEL.net(after_pcts.get(5), "gross"),
            "base_5m": COST_MODEL.net(after_pcts.get(5), "base"),
            "stress_5m": COST_MODEL.net(after_pcts.get(5), "stress"),
            "net_5m": COST_MODEL.net(after_pcts.get(5), "base"),
            "gross_10m": COST_MODEL.net(after_pcts.get(10), "gross"),
            "base_10m": COST_MODEL.net(after_pcts.get(10), "base"),
            "stress_10m": COST_MODEL.net(after_pcts.get(10), "stress"),
            "net_10m": COST_MODEL.net(after_pcts.get(10), "base"),
            "gross_20m": COST_MODEL.net(after_pcts.get(20), "gross"),
            "base_20m": COST_MODEL.net(after_pcts.get(20), "base"),
            "stress_20m": COST_MODEL.net(after_pcts.get(20), "stress"),
            "net_20m": COST_MODEL.net(after_pcts.get(20), "base"),
    }


def analyze(start: date, end: date) -> str:
    lines = []
    W = 68

    def sep(c="═"): lines.append(c * W)
    def title(t): sep(); lines.append(f"  {t}"); sep()
    def sub(t): lines.append(""); lines.append(f"── {t} ──")
    def row(label, val): lines.append(f"  {label:<34} {val}")

    title(f"📊 V자 낙폭 기준 완화 백테스트  {start} ~ {end}")
    lines.append("")
    lines.append(f"  현재 기준: {CURRENT_THRESHOLD}%  →  완화 후보: {CANDIDATE_THRESHOLD}%")
    lines.append(f"  (낙폭부족만 유일한 실패 사유였던 병목 구간만 검증 — 다른 조건은 이미 충족)")

    rows = load_bottleneck_rows(start, end)
    if not rows:
        lines.append("")
        lines.append("  해당 기간에 조건에 맞는 병목 구간 기록이 없습니다.")
        sep()
        return "\n".join(lines)

    events = group_into_events(rows)
    lines.append("")
    row("병목 구간 원시 로그", f"{len(rows):,}건")
    row("이벤트로 압축 (같은 종목 연속 묶음)", f"{len(events):,}건")

    sims = []
    for ev in events:
        s = simulate_event(ev)
        if s is not None:
            sims.append(s)

    if not sims:
        lines.append("")
        lines.append("  1분봉 데이터가 없어 시뮬레이션할 수 없습니다.")
        sep()
        return "\n".join(lines)

    sub("가상 매수 시 성과 (기준 완화 시 진입 가정, 비용 반영)")
    row("시뮬레이션 가능 이벤트", f"{len(sims):,}건")

    for horizon in ["net_5m", "net_10m", "net_20m"]:
        vals = [s[horizon] for s in sims if s[horizon] is not None]
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        label = horizon.replace("net_", "").replace("m", "분 후")
        row(f"  {label}", f"{len(vals)}건  승률 {win}/{len(vals)} ({win/len(vals)*100:.0f}%)  평균 {avg:+.2f}%")

    vals5 = [s["net_5m"] for s in sims if s["net_5m"] is not None]
    if vals5:
        avg5 = sum(vals5) / len(vals5)
        win5 = sum(1 for v in vals5 if v > 0)
        lines.append("")
        if len(vals5) < 20:
            lines.append(f"  ⚠️ 표본 {len(vals5)}건 — 20건 미만이면 참고용입니다. 기간을 넓혀 재실행하세요.")
            lines.append(f"     예: python analyze_v_drop_backtest.py 2026-06-25 2026-07-10")
        elif avg5 > 0.2 and win5 / len(vals5) > 0.5:
            lines.append(f"  → 5분 평균 {avg5:+.2f}%, 승률 {win5}/{len(vals5)} : 완화 시 긍정적 신호")
        elif avg5 < -0.2:
            lines.append(f"  → 5분 평균 {avg5:+.2f}% : 완화하면 오히려 손실 위험 (현행 유지 권장)")
        else:
            lines.append(f"  → 5분 평균 {avg5:+.2f}% : 뚜렷한 방향성 없음")

    sub("이벤트 상세 목록")
    for s in sorted(sims, key=lambda x: x["entry_time"]):
        n5  = f"{s['net_5m']:+.2f}%"  if s['net_5m']  is not None else "  ?"
        n10 = f"{s['net_10m']:+.2f}%" if s['net_10m'] is not None else "  ?"
        n20 = f"{s['net_20m']:+.2f}%" if s['net_20m'] is not None else "  ?"
        row(
            f"  {s['symbol']} {s['entry_time']} (낙폭{s['drop_pct']:+.1f}%)",
            f"5m {n5}  10m {n10}  20m {n20}",
        )

    sep()
    return "\n".join(lines)



# ── 2026-08-07 (1J.1): 비용 3시나리오 요약 ────────────────────
# 1J에서 비용 숫자는 단일 출처화했지만 세 시나리오를 실제로 함께
# 출력하는 건 replay_runner뿐이었음. "비용 가정에 따른 결론 뒤집힘을
# 항상 노출한다"는 1J 원칙에 맞춰 나머지 분석기도 통일합니다.
def append_cost_scenarios(lines: list, gross_returns, label: str = "") -> None:
    """원수익률 목록에 대해 Gross/Base/Stress 평균과 플러스비율을 덧붙입니다."""
    vals = [v for v in gross_returns if v is not None]
    if not vals:
        return
    if label:
        lines.append(f"  [ {label} ] 비용 시나리오별 (n={len(vals)})")
    else:
        lines.append(f"  비용 시나리오별 (n={len(vals)})")
    lines.extend(COST_MODEL.scenario_lines(vals))

def main():
    import sys
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
    fname = REPORTS_DIR / f"v_drop_backtest_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
