#!/usr/bin/env python3
"""간이 리플레이 엔진 (신호 품질 검증용)

저장된 1분봉으로 analyze() + generate_signal()을 재실행해
BUY 신호 이후 5분/10분/20분 수익률을 계산합니다.

실제 체결 시뮬레이션은 하지 않습니다.
목적: "신호가 뜬 뒤 실제로 가격이 올랐는가?"

실행:
    python replay_runner.py 2026-05-27            # 특정 날짜 전체
    python replay_runner.py 2026-05-27 010170     # 특정 날짜 + 특정 종목

결과: 콘솔 출력 + reports/replay_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR     = Path("reports")
AFTER_MINUTES   = [5, 10, 20]


@dataclass
class MinuteBarRow:
    """CSV 한 행을 MinuteBar 형태로 변환합니다."""
    cntr_tm: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    acc_volume: int = 0  # 누적 거래량 (리플레이에서는 close×vol로 대체)


def load_bars(symbol: str, target_date: date) -> list[MinuteBarRow]:
    path = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d") / f"{symbol}.csv"
    if not path.exists():
        return []
    bars = []
    acc = 0
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v = int(float(r.get("volume", 0) or 0))
                acc += v
                bars.append(MinuteBarRow(
                    cntr_tm     = r["cntr_tm"],
                    open_price  = int(float(r.get("open",  0) or 0)),
                    high_price  = int(float(r.get("high",  0) or 0)),
                    low_price   = int(float(r.get("low",   0) or 0)),
                    close_price = int(float(r.get("close", 0) or 0)),
                    volume      = v,
                    acc_volume  = acc,
                ))
            except (ValueError, KeyError):
                continue
    return bars


def try_import_analyzer():
    """MinuteAnalyzer와 설정을 임포트합니다."""
    try:
        import importlib.util, os
        # 프로젝트 루트에서 실행한다고 가정
        sys.path.insert(0, os.getcwd())

        from config.settings import load_settings
        from domain.market_regime.minute_analyzer import MinuteAnalyzer

        settings = load_settings()
        cfg = settings.market_regime
        analyzer = MinuteAnalyzer(
            min_trading_value       = 0,  # 리플레이에서는 거래대금 필터 완화
            pullback_min_pct        = cfg.pullback_min_pct,
            pullback_max_pct        = cfg.pullback_max_pct,
            change_rate_min         = cfg.change_rate_min,
            change_rate_max         = cfg.change_rate_max,
            rebound_min_pct         = cfg.rebound_min_pct,
            v_bottom_lookback       = cfg.v_bottom_lookback,
            v_low_min_age           = cfg.v_low_min_age,
            v_low_max_age           = cfg.v_low_max_age,
            v_drop_threshold_pct    = cfg.v_drop_threshold_pct,
            v_rebound_threshold_pct = cfg.v_rebound_threshold_pct,
            v_max_rebound_pct       = cfg.v_max_rebound_pct,
            v_volume_ratio          = cfg.v_volume_ratio,
            v_min_bar_amount        = 0,
            v_bottom_spike_ratio    = cfg.v_bottom_spike_ratio,
            v_ma5_slope_bars        = cfg.v_ma5_slope_bars,
        )
        return analyzer
    except Exception as e:
        print(f"[WARN] MinuteAnalyzer 임포트 실패: {e}")
        print("       분봉 분석 없이 단순 가격 변화만 계산합니다.")
        return None


def run_replay(symbol: str, bars: list[MinuteBarRow],
               analyzer) -> list[dict]:
    """봉을 시간순으로 누적하며 BUY 신호를 탐색합니다."""
    if len(bars) < 5:
        return []

    results = []
    # 첫 봉의 종가를 전일 종가 대리값으로 사용
    prev_close = bars[0].close_price

    for i in range(5, len(bars)):
        window = bars[:i]
        current = bars[i]

        # MinuteAnalyzer로 분봉 분석
        is_v = is_pr = False
        patterns = "-"

        if analyzer is not None:
            try:
                analysis = analyzer.analyze(window, prev_close)
                if analysis is None:
                    continue

                # 간단한 BUY 조건: 패턴 하나 이상 + VWAP 위
                has_pattern = any([
                    analysis.is_valid_change_rate,
                    analysis.is_valid_rebound,
                    analysis.is_valid_pulldown,
                    analysis.is_v_rebound,
                    analysis.is_pulldown_recovery,
                ])
                if not (has_pattern and analysis.price_above_vwap):
                    continue

                is_v  = analysis.is_v_rebound
                is_pr = analysis.is_pulldown_recovery
                pats  = []
                if analysis.is_valid_change_rate:    pats.append("A")
                if analysis.is_valid_rebound:        pats.append("B")
                if analysis.is_valid_pulldown:       pats.append("C")
                if analysis.is_v_rebound:            pats.append("V")
                if analysis.is_pulldown_recovery:    pats.append("PR")
                patterns = "/".join(pats) if pats else "-"
            except Exception:
                continue
        else:
            # 분석기 없으면 단순 등락률로 대체
            chg = (current.close_price - prev_close) / prev_close * 100
            if not (2.0 <= chg <= 18.0):
                continue
            patterns = "A(simple)"

        entry_price = current.close_price
        entry_time  = current.cntr_tm

        # BUY 신호 이후 N분 뒤 수익률 계산
        after_pcts: dict[int, float | None] = {}
        for m in AFTER_MINUTES:
            target_idx = i + m
            if target_idx < len(bars):
                after_price = bars[target_idx].close_price
                after_pcts[m] = (after_price - entry_price) / entry_price * 100
            else:
                after_pcts[m] = None

        # 이후 구간 최고가 / 최저가 (최대 20봉)
        future = bars[i + 1: i + 21]
        if future:
            highs  = [b.high_price  for b in future]
            lows   = [b.low_price   for b in future]
            mfe    = (max(highs) - entry_price) / entry_price * 100   # 최대 수익
            mae    = (min(lows)  - entry_price) / entry_price * 100   # 최대 역행
        else:
            mfe = mae = 0.0

        results.append({
            "entry_time":  entry_time,
            "entry_price": entry_price,
            "patterns":    patterns,
            "is_v":        is_v,
            "is_pr":       is_pr,
            "after_5m":    after_pcts.get(5),
            "after_10m":   after_pcts.get(10),
            "after_20m":   after_pcts.get(20),
            "mfe":         round(mfe, 2),
            "mae":         round(mae, 2),
        })

    return results


def format_report(symbol: str, results: list[dict],
                  target_date: date) -> str:
    lines = []
    W = 58

    def sep(c="─"): lines.append(c * W)
    sep("═")
    lines.append(f"  📊 리플레이  {symbol}  {target_date}")
    sep("═")

    if not results:
        lines.append("  BUY 신호 없음")
        sep()
        return "\n".join(lines)

    lines.append(f"  BUY 신호: {len(results)}건")
    lines.append("")

    # 수익률 통계
    for m in AFTER_MINUTES:
        key  = f"after_{m}m"
        vals = [r[key] for r in results if r[key] is not None]
        if vals:
            win  = sum(1 for v in vals if v > 0)
            avg  = sum(vals) / len(vals)
            lines.append(
                f"  {m:>2}분 후  승률 {win}/{len(vals)} ({win/len(vals)*100:.0f}%)"
                f"  평균 {avg:+.2f}%"
            )

    avg_mfe = sum(r["mfe"] for r in results) / len(results)
    avg_mae = sum(r["mae"] for r in results) / len(results)
    lines.append(f"  MFE 평균 {avg_mfe:+.2f}%  MAE 평균 {avg_mae:+.2f}%")

    # 패턴별 성과
    lines.append("")
    lines.append("  [ 패턴별 5분 후 성과 ]")
    pat_groups: dict[str, list] = defaultdict(list)
    for r in results:
        if r["after_5m"] is not None:
            pat_groups[r["patterns"]].append(r["after_5m"])
    for pat, vals in sorted(pat_groups.items(), key=lambda x: -len(x[1])):
        w   = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(
            f"    {pat:<12} {len(vals)}건  승률 {w/len(vals)*100:.0f}%"
            f"  평균 {avg:+.2f}%"
        )

    # V자 vs 일반 비교
    v_results   = [r for r in results if r["is_v"] and r["after_5m"] is not None]
    non_results = [r for r in results if not r["is_v"] and r["after_5m"] is not None]
    if v_results and non_results:
        lines.append("")
        lines.append("  [ V자 vs 일반 진입 5분 후 비교 ]")
        for label, grp in [("V자 진입", v_results), ("일반 진입", non_results)]:
            w   = sum(1 for r in grp if r["after_5m"] > 0)
            avg = sum(r["after_5m"] for r in grp) / len(grp)
            lines.append(f"    {label:<10} {len(grp)}건  승률 {w/len(grp)*100:.0f}%  평균 {avg:+.2f}%")

    # 거래 목록 (최근 10건)
    lines.append("")
    lines.append("  [ 신호 목록 (최근 10건) ]")
    for r in results[-10:]:
        a5  = f"{r['after_5m']:+.1f}%" if r["after_5m"] is not None else "  ?"
        a10 = f"{r['after_10m']:+.1f}%" if r["after_10m"] is not None else "  ?"
        v   = "V" if r["is_v"] else ("PR" if r["is_pr"] else "-")
        lines.append(
            f"    {r['entry_time'][8:12]}  {r['entry_price']:,}원"
            f"  [{v}]  5m:{a5}  10m:{a10}"
            f"  MFE:{r['mfe']:+.1f}%  MAE:{r['mae']:+.1f}%"
        )

    sep()
    return "\n".join(lines)


def main():
    args        = sys.argv[1:]
    today       = date.today()
    target_date = date.fromisoformat(args[0]) if args else today
    filter_sym  = args[1].upper() if len(args) >= 2 else None

    date_dir = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d")
    if not date_dir.exists():
        print(f"[ERROR] 저장된 분봉 없음: {date_dir}")
        sys.exit(1)

    csv_files = sorted(date_dir.glob("*.csv"))
    if filter_sym:
        csv_files = [f for f in csv_files if f.stem == filter_sym]

    if not csv_files:
        print("[ERROR] 해당 종목의 분봉 데이터 없음")
        sys.exit(1)

    analyzer = try_import_analyzer()

    all_reports = []
    for csv_path in csv_files:
        symbol = csv_path.stem
        bars   = load_bars(symbol, target_date)
        if not bars:
            continue
        results = run_replay(symbol, bars, analyzer)
        report  = format_report(symbol, results, target_date)
        print(report)
        all_reports.append(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"replay_{target_date.strftime('%Y%m%d')}.txt"
    fname.write_text("\n\n".join(all_reports), encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
