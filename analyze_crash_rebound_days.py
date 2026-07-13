#!/usr/bin/env python3
"""시장 급락 후 반등 패턴 분석 (데이터 수집 전용, 실거래 로직 변경 없음)

여러 종목이 동시에 당일 큰 폭(기본 -5% 이상) 하락한 날을 "급락일"로 식별하고,
그 종목들이 저점 형성 후 실제로 얼마나, 어떻게 반등했는지를
1분봉 데이터로 계산합니다.

핵심 질문: "저점을 실시간으로 알아챈 뒤(지연 반영) 매수했다면 당일 종가까지
            수익이 났을까?" — V자 패턴의 저점나이(v_low_age) 제한 때문에
            놓치고 있는 "느리고 큰" 반등이 실제로 돈이 되는 패턴인지 확인.

실행:
    python analyze_crash_rebound_days.py 2026-06-16 2026-07-07
    python analyze_crash_rebound_days.py 2026-06-16 2026-07-07 --crash-threshold -5.0 --min-crash-symbols 2

결과: 콘솔 출력 + reports/crash_rebound_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR = Path("reports")

# 비용 가정 (기존 replay_runner와 동일 기준)
ROUND_TRIP_COST_PCT = 0.25
SLIPPAGE_PCT = 0.10
TOTAL_COST_PCT = ROUND_TRIP_COST_PCT + SLIPPAGE_PCT

# 매수 확인까지의 지연 시나리오 (분 단위) — 저점을 그 순간 알 수 없으므로
ENTRY_DELAYS = [3, 5, 10]


class Bar:
    __slots__ = ("cntr_tm", "open", "high", "low", "close", "volume", "dt")

    def __init__(self, row: dict):
        self.cntr_tm = row["cntr_tm"].strip()
        self.open = float(row["open"])
        self.high = float(row["high"])
        self.low = float(row["low"])
        self.close = float(row["close"])
        self.volume = float(row.get("volume", 0) or 0)
        tm = self.cntr_tm
        if len(tm) >= 14:
            self.dt = datetime.strptime(tm[:14], "%Y%m%d%H%M%S")
        else:
            self.dt = None


def load_bars(symbol: str, target_date: date) -> list[Bar]:
    path = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d") / f"{symbol}.csv"
    if not path.exists():
        return []
    bars = []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                bars.append(Bar(row))
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b.cntr_tm)
    return bars


def list_symbols_for_date(target_date: date) -> list[str]:
    day_dir = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d")
    if not day_dir.exists():
        return []
    return [p.stem for p in day_dir.glob("*.csv")]


def analyze_symbol_day(symbol: str, target_date: date, crash_threshold: float) -> dict | None:
    """종목 하루치 분봉에서 당일 낙폭/저점/반등 정보를 계산합니다."""
    bars = load_bars(symbol, target_date)
    if len(bars) < 10:
        return None

    day_open = bars[0].open
    if day_open <= 0:
        return None

    # 당일 저점 탐색
    low_idx, low_bar = min(enumerate(bars), key=lambda x: x[1].low)
    decline_pct = (low_bar.low - day_open) / day_open * 100

    if decline_pct > crash_threshold:
        return None  # 급락 기준 미달

    day_close = bars[-1].close
    eod_from_low_pct = (day_close - low_bar.low) / low_bar.low * 100

    entries = {}
    for delay in ENTRY_DELAYS:
        entry_idx = low_idx + delay
        if entry_idx >= len(bars):
            entries[delay] = None
            continue
        entry_price = bars[entry_idx].close
        raw_return = (day_close - entry_price) / entry_price * 100
        net_return = raw_return - TOTAL_COST_PCT
        entries[delay] = {
            "entry_price": entry_price,
            "entry_time": bars[entry_idx].cntr_tm[8:14] if len(bars[entry_idx].cntr_tm) >= 14 else "?",
            "raw_return": raw_return,
            "net_return": net_return,
        }

    return {
        "symbol": symbol,
        "day_open": day_open,
        "day_low": low_bar.low,
        "day_close": day_close,
        "decline_pct": decline_pct,
        "low_time": low_bar.cntr_tm[8:14] if len(low_bar.cntr_tm) >= 14 else "?",
        "eod_from_low_pct": eod_from_low_pct,
        "entries": entries,
    }


def analyze(start: date, end: date, crash_threshold: float, min_crash_symbols: int) -> str:
    lines = []
    W = 70

    def sep(c="═"): lines.append(c * W)
    def title(t): sep(); lines.append(f"  {t}"); sep()
    def sub(t): lines.append(""); lines.append(f"── {t} ──")
    def row(label, val): lines.append(f"  {label:<36} {val}")

    title(f"📊 시장 급락-반등 패턴 분석  {start} ~ {end}")
    lines.append("")
    lines.append(f"  급락 기준: 당일 시가 대비 {crash_threshold}% 이하 하락")
    lines.append(f"  급락일 판정: 위 기준 종목이 {min_crash_symbols}개 이상인 날")
    lines.append(f"  매수 지연 시나리오: 저점 후 {ENTRY_DELAYS}분 (실시간 탐지 지연 반영)")

    if not MINUTE_BARS_DIR.exists():
        lines.append("")
        lines.append(f"  [ERROR] {MINUTE_BARS_DIR} 폴더가 없습니다.")
        sep()
        return "\n".join(lines)

    all_crash_days = []
    d = start
    while d <= end:
        symbols = list_symbols_for_date(d)
        if symbols:
            day_results = []
            for sym in symbols:
                r = analyze_symbol_day(sym, d, crash_threshold)
                if r is not None:
                    day_results.append(r)
            if len(day_results) >= min_crash_symbols:
                all_crash_days.append((d, day_results))
        d += timedelta(days=1)

    if not all_crash_days:
        lines.append("")
        lines.append("  해당 기간에 조건을 만족하는 급락일이 없습니다.")
        sep()
        return "\n".join(lines)

    sub(f"식별된 급락일: {len(all_crash_days)}일")
    for d, results in all_crash_days:
        syms = ", ".join(f"{r['symbol']}({r['decline_pct']:+.1f}%)" for r in sorted(results, key=lambda x: x['decline_pct'])[:6])
        row(f"  {d}", f"{len(results)}종목 급락 — {syms}")

    # ── 지연별 집계 (전체 급락일 통합) ──────────────────────────
    sub("매수 지연 시나리오별 종가까지 수익률 (전체 급락 종목 통합)")
    all_symbol_days = [r for _, results in all_crash_days for r in results]
    row("급락 종목-일 표본 수", f"{len(all_symbol_days):,}건")

    for delay in ENTRY_DELAYS:
        vals = [r["entries"][delay]["net_return"] for r in all_symbol_days
                if r["entries"].get(delay) is not None]
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        row(f"  저점+{delay}분 진입 → 종가", f"{len(vals)}건  승률 {win}/{len(vals)} ({win/len(vals)*100:.0f}%)  평균 {avg:+.2f}%")

    # 참고: 저점 직후 즉시 진입 (비현실적 상한선)
    eod_vals = [r["eod_from_low_pct"] - TOTAL_COST_PCT for r in all_symbol_days]
    if eod_vals:
        win = sum(1 for v in eod_vals if v > 0)
        avg = sum(eod_vals) / len(eod_vals)
        lines.append("")
        row("  (참고) 저점 즉시 진입 → 종가 [비현실적 상한]", f"{len(eod_vals)}건  승률 {win}/{len(eod_vals)} ({win/len(eod_vals)*100:.0f}%)  평균 {avg:+.2f}%")

    if len(all_symbol_days) < 20:
        lines.append("")
        lines.append(f"  ⚠️ 표본 {len(all_symbol_days)}건 — 20건 미만이면 참고용입니다.")
        lines.append(f"     기간을 넓혀 재실행하세요 (데이터가 있는 날짜 범위 내에서).")

    sub("종목-일 상세 목록")
    for d, results in all_crash_days:
        lines.append(f"  [{d}]")
        for r in sorted(results, key=lambda x: x['decline_pct']):
            e3 = r["entries"].get(3)
            e5 = r["entries"].get(5)
            e10 = r["entries"].get(10)
            fmt = lambda e: f"{e['net_return']:+.2f}%" if e else "  ?"
            row(
                f"    {r['symbol']} 낙폭{r['decline_pct']:+.1f}% 저점{r['low_time']}",
                f"+3m {fmt(e3)}  +5m {fmt(e5)}  +10m {fmt(e10)}  (EOD기준 저점대비 {r['eod_from_low_pct']:+.1f}%)",
            )

    sep()
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--crash-threshold", type=float, default=-5.0)
    parser.add_argument("--min-crash-symbols", type=int, default=2)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    report = analyze(start, end, args.crash_threshold, args.min_crash_symbols)
    print(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"crash_rebound_{date.today().strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()