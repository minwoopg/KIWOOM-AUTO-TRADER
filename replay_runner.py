#!/usr/bin/env python3
"""간이 리플레이 엔진 (신호 품질 검증용)

실행:
    python replay_runner.py                        # 오늘 전체
    python replay_runner.py 2026-05-27             # 특정 날짜
    python replay_runner.py 2026-05-27 010170      # 특정 종목

결과: 콘솔 출력 + reports/replay_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR     = Path("reports")
AFTER_MINUTES   = [5, 10, 20]

# ── 비용 설정 ─────────────────────────────────────────────────────
ROUND_TRIP_COST_PCT = 0.25   # 왕복 수수료 + 세금 (%)
SLIPPAGE_PCT        = 0.10   # 슬리피지 (%)
TOTAL_COST_PCT      = ROUND_TRIP_COST_PCT + SLIPPAGE_PCT  # 총 비용

# ── 고위험 종목 경고 기준 ─────────────────────────────────────────
RISK_MAE_THRESHOLD     = -3.0   # MAE 평균이 이 값 이하면 경고
RISK_5M_THRESHOLD      = -1.0   # 5분 평균 수익률이 이 값 이하면 경고
RISK_20M_THRESHOLD     = -1.0   # 20분 평균 수익률이 이 값 이하면 경고


@dataclass
class MinuteBarRow:
    cntr_tm: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    acc_volume: int = 0


def load_bars(symbol: str, target_date: date) -> list[MinuteBarRow]:
    path = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d") / f"{symbol}.csv"
    if not path.exists():
        return []
    bars, acc = [], 0
    with path.open(encoding="utf-8-sig") as f:
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
    try:
        import os
        sys.path.insert(0, os.getcwd())
        from config.settings import load_settings
        from domain.market_regime.minute_analyzer import MinuteAnalyzer
        settings = load_settings()
        cfg = settings.market_regime
        return MinuteAnalyzer(
            min_trading_value       = 0,
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
    except Exception as e:
        print(f"[WARN] MinuteAnalyzer 임포트 실패: {e}")
        return None


def get_time_bucket(cntr_tm: str) -> str:
    """봉 시각을 시간대 버킷으로 분류합니다."""
    try:
        hhmm = int(cntr_tm[8:12])
        if hhmm < 1000:   return "09:00~10:00"
        if hhmm < 1330:   return "10:00~13:30"
        if hhmm < 1450:   return "13:30~14:50"
        return "14:50~"
    except (ValueError, IndexError):
        return "기타"


def run_replay(symbol: str, bars: list[MinuteBarRow], analyzer) -> list[dict]:
    if len(bars) < 5:
        return []
    results = []
    prev_close = bars[0].close_price

    for i in range(5, len(bars)):
        window  = bars[:i]
        current = bars[i]
        is_v = is_pr = False
        patterns = "-"

        if analyzer is not None:
            try:
                analysis = analyzer.analyze(window, prev_close)
                if analysis is None:
                    continue
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
                if analysis.is_v_rebound:            pats.append("V")
                if analysis.is_pulldown_recovery:    pats.append("PR")
                if analysis.is_valid_change_rate:    pats.append("A")
                if analysis.is_valid_rebound:        pats.append("B")
                if analysis.is_valid_pulldown:       pats.append("C")
                patterns = "/".join(pats) if pats else "-"
            except Exception:
                continue
        else:
            chg = (current.close_price - prev_close) / prev_close * 100
            if not (2.0 <= chg <= 18.0):
                continue
            patterns = "A(simple)"

        entry_price = current.close_price
        entry_time  = current.cntr_tm

        after_pcts: dict[int, float | None] = {}
        for m in AFTER_MINUTES:
            idx = i + m
            if idx < len(bars):
                ap = bars[idx].close_price
                after_pcts[m] = (ap - entry_price) / entry_price * 100
            else:
                after_pcts[m] = None

        future = bars[i + 1: i + 21]
        if future:
            mfe = (max(b.high_price  for b in future) - entry_price) / entry_price * 100
            mae = (min(b.low_price   for b in future) - entry_price) / entry_price * 100
        else:
            mfe = mae = 0.0

        results.append({
            "entry_time":  entry_time,
            "entry_price": entry_price,
            "patterns":    patterns,
            "is_v":        is_v,
            "is_pr":       is_pr,
            "time_bucket": get_time_bucket(entry_time),
            "after_5m":    after_pcts.get(5),
            "after_10m":   after_pcts.get(10),
            "after_20m":   after_pcts.get(20),
            # 비용 반영 순수익
            "net_5m":   (after_pcts.get(5)  - TOTAL_COST_PCT) if after_pcts.get(5)  is not None else None,
            "net_10m":  (after_pcts.get(10) - TOTAL_COST_PCT) if after_pcts.get(10) is not None else None,
            "net_20m":  (after_pcts.get(20) - TOTAL_COST_PCT) if after_pcts.get(20) is not None else None,
            "mfe":         round(mfe, 2),
            "mae":         round(mae, 2),
        })
    return results


def is_high_risk(results: list[dict]) -> tuple[bool, list[str]]:
    """고위험 종목 여부와 사유를 반환합니다."""
    reasons = []
    vals_5m  = [r["net_5m"]  for r in results if r["net_5m"]  is not None]
    vals_20m = [r["net_20m"] for r in results if r["net_20m"] is not None]
    maes     = [r["mae"]     for r in results]

    if vals_5m and sum(vals_5m) / len(vals_5m) <= RISK_5M_THRESHOLD:
        reasons.append(f"5분 순수익 평균 {sum(vals_5m)/len(vals_5m):+.2f}%")
    if vals_20m and sum(vals_20m) / len(vals_20m) <= RISK_20M_THRESHOLD:
        reasons.append(f"20분 순수익 평균 {sum(vals_20m)/len(vals_20m):+.2f}%")
    if maes and sum(maes) / len(maes) <= RISK_MAE_THRESHOLD:
        reasons.append(f"MAE 평균 {sum(maes)/len(maes):+.2f}%")
    return bool(reasons), reasons


def format_report(symbol: str, results: list[dict], target_date: date) -> str:
    lines = []
    W = 60

    def sep(c="─"): lines.append(c * W)
    sep("═")
    lines.append(f"  📊 리플레이  {symbol}  {target_date}")
    sep("═")

    if not results:
        lines.append("  BUY 신호 없음")
        sep()
        return "\n".join(lines)

    lines.append(f"  BUY 신호: {len(results)}건  │  비용 가정: 왕복 {ROUND_TRIP_COST_PCT}% + 슬리피지 {SLIPPAGE_PCT}%")
    lines.append("  ※ 패턴 리플레이: 패턴 감지 봉 기준 수익률 (실제 전략 BUY와 다를 수 있음 — 점수제/쿨다운 미적용)")
    lines.append("")

    # ── 1. 수익률 (비용 전/후) ───────────────────────────────────
    for m in AFTER_MINUTES:
        raw_key = f"after_{m}m"
        net_key = f"net_{m}m"
        raw_vals = [r[raw_key] for r in results if r[raw_key] is not None]
        net_vals = [r[net_key] for r in results if r[net_key] is not None]
        if not raw_vals:
            continue
        raw_win = sum(1 for v in raw_vals if v > 0)
        net_win = sum(1 for v in net_vals if v > 0)
        raw_avg = sum(raw_vals) / len(raw_vals)
        net_avg = sum(net_vals) / len(net_vals) if net_vals else 0
        lines.append(
            f"  {m:>2}분 후  승률 {raw_win}/{len(raw_vals)} ({raw_win/len(raw_vals)*100:.0f}%)"
            f"  평균 {raw_avg:+.2f}%  →  비용 후 {net_avg:+.2f}%  "
            f"(순수익 승률 {net_win}/{len(net_vals)} {net_win/len(net_vals)*100:.0f}%)"
        )

    avg_mfe = sum(r["mfe"] for r in results) / len(results)
    avg_mae = sum(r["mae"] for r in results) / len(results)
    lines.append(f"  MFE 평균 {avg_mfe:+.2f}%  MAE 평균 {avg_mae:+.2f}%")

    # ── 고위험 경고 ────────────────────────────────────────────────
    high_risk, risk_reasons = is_high_risk(results)
    if high_risk:
        lines.append("")
        lines.append(f"  ⚠️  [WATCHLIST_EXCLUDE_CANDIDATE]  {symbol}")
        for r in risk_reasons:
            lines.append(f"      → {r}")

    # ── 2. 패턴 조합별 성과 ────────────────────────────────────────
    lines.append("")
    lines.append("  [ 패턴별 5분 순수익 성과 ]")
    pat_groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["net_5m"] is not None:
            pat_groups[r["patterns"]].append(r["net_5m"])
    for pat, vals in sorted(pat_groups.items(), key=lambda x: -len(x[1])):
        w   = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        flag = " ⚠️" if avg < -1.0 else (" ✅" if avg > 0 else "")
        lines.append(
            f"    {pat:<14} {len(vals):>3}건  승률 {w/len(vals)*100:>3.0f}%"
            f"  순수익 {avg:+.2f}%{flag}"
        )

    # ── 3. 시간대별 성과 ───────────────────────────────────────────
    lines.append("")
    lines.append("  [ 시간대별 5분 순수익 성과 ]")
    time_groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["net_5m"] is not None:
            time_groups[r["time_bucket"]].append(r["net_5m"])
    for bucket in ["09:00~10:00", "10:00~13:30", "13:30~14:50", "14:50~"]:
        vals = time_groups.get(bucket, [])
        if not vals:
            continue
        w   = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(
            f"    {bucket:<14} {len(vals):>3}건  승률 {w/len(vals)*100:>3.0f}%"
            f"  순수익 {avg:+.2f}%"
        )

    # ── 4. V자 vs 일반 비교 ─────────────────────────────────────────
    v_res   = [r for r in results if r["is_v"]  and r["net_5m"] is not None]
    pr_res  = [r for r in results if r["is_pr"] and r["net_5m"] is not None]
    gen_res = [r for r in results if not r["is_v"] and not r["is_pr"] and r["net_5m"] is not None]
    if v_res or pr_res:
        lines.append("")
        lines.append("  [ V자/PR vs 일반 비교 (5분 순수익) ]")
        lines.append("    ※ V자 건수 = 패턴 감지 횟수 (signal_log V자와 다름 — 전략 미적용 기준)")
        for label, grp in [("V자", v_res), ("PR", pr_res), ("일반", gen_res)]:
            if not grp: continue
            w   = sum(1 for r in grp if r["net_5m"] > 0)
            avg = sum(r["net_5m"] for r in grp) / len(grp)
            lines.append(f"    {label:<6} {len(grp):>3}건  승률 {w/len(grp)*100:>3.0f}%  순수익 {avg:+.2f}%")

    # ── 5. 신호 목록 (최근 10건) ────────────────────────────────────
    lines.append("")
    lines.append("  [ 신호 목록 (최근 10건) ]")
    for r in results[-10:]:
        a5  = f"{r['after_5m']:+.1f}%" if r["after_5m"] is not None else "  ?"
        n5  = f"{r['net_5m']:+.1f}%"   if r["net_5m"]  is not None else "  ?"
        v   = "V" if r["is_v"] else ("PR" if r["is_pr"] else "-")
        lines.append(
            f"    {r['entry_time'][8:12]}  {r['entry_price']:,}원"
            f"  [{v}]  5m:{a5}(순{n5})"
            f"  MFE:{r['mfe']:+.1f}%  MAE:{r['mae']:+.1f}%"
        )

    sep()
    return "\n".join(lines)


def build_summary(all_results: dict[str, list[dict]], actual_buys: set = None) -> str:
    """전 종목 요약 리포트를 생성합니다."""
    lines = ["═" * 60, "  📋 전 종목 요약", "═" * 60, ""]

    risk_symbols = []
    for symbol, results in sorted(all_results.items()):
        if not results:
            continue
        net_5m = [r["net_5m"] for r in results if r["net_5m"] is not None]
        maes   = [r["mae"] for r in results]
        if not net_5m:
            continue
        win  = sum(1 for v in net_5m if v > 0)
        avg  = sum(net_5m) / len(net_5m)
        mae  = sum(maes) / len(maes)
        high_risk, _ = is_high_risk(results)
        flag = "  ⚠️ 고위험" if high_risk else ""
        # 2026-07-16: 기존엔 신호 건수를 len(results)(net_5m=None인 장마감
        # 근처 봉까지 포함)로 표시하면서, 정작 옆의 평균은 net_5m만으로
        # 계산해서 분자·분모가 안 맞았음(004310 사례: 여기 13건 vs
        # "놓친 기회" 섹션 8건 — 같은 종목이 다른 숫자로 두 번 나와
        # 리포트 신뢰도 문제 유발). "놓친 기회"와 동일하게 len(net_5m)로 통일.
        lines.append(
            f"  {symbol}  신호 {len(net_5m):>3}건  "
            f"5분순수익 {avg:+.2f}%  MAE {mae:+.2f}%{flag}"
        )
        if high_risk:
            risk_symbols.append(symbol)

    if risk_symbols:
        lines.append("")
        lines.append("  ⚠️  고위험 종목 (조건검색 제외 검토):")
        for sym in risk_symbols:
            lines.append(f"      → {sym}")

    # ── 놓친 기회 (missed opportunity) ────────────────────────
    # replay 5분 순수익이 좋은데(평균 +0.5%↑) 실제로 매수 안 한 종목
    if actual_buys is not None:
        MIN_NET_5M = 0.5
        MIN_SIGNALS = 3
        missed = []
        for symbol, results in all_results.items():
            if symbol in actual_buys:
                continue
            net_5m = [r["net_5m"] for r in results if r["net_5m"] is not None]
            if len(net_5m) < MIN_SIGNALS:
                continue
            avg = sum(net_5m) / len(net_5m)
            if avg >= MIN_NET_5M:
                win = sum(1 for v in net_5m if v > 0)
                missed.append((symbol, len(net_5m), avg, win))
        if missed:
            missed.sort(key=lambda x: -x[2])
            lines.append("")
            lines.append("  💡 놓친 기회 (replay 양호 + 실제 미매수):")
            lines.append(f"      (기준: 5분순수익 평균 +{MIN_NET_5M}%+, 신호 {MIN_SIGNALS}건+)")
            for sym, cnt, avg, win in missed[:10]:
                lines.append(
                    f"      → {sym}  신호 {cnt}건  5분순수익 {avg:+.2f}%  승률 {win}/{cnt}"
                )
        else:
            lines.append("")
            lines.append("  💡 놓친 기회: 없음 (replay 양호한 미매수 종목 없음)")

    lines.append("─" * 60)
    return "\n".join(lines)


def load_actual_buys(target_date: date) -> set:
    """trades.csv에서 해당 날짜에 실제 매수한 종목 집합을 반환합니다."""
    trades_path = Path("logs/trades.csv")
    if not trades_path.exists():
        return set()
    buys = set()
    try:
        with trades_path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ts = r.get("timestamp", "")
                if ts[:10] != target_date.isoformat():
                    continue
                if r.get("side") == "BUY" and r.get("accepted") == "True":
                    buys.add(r.get("symbol", ""))
    except Exception:
        pass
    return buys


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
    all_results: dict[str, list[dict]] = {}

    for csv_path in csv_files:
        symbol  = csv_path.stem
        bars    = load_bars(symbol, target_date)
        if not bars:
            continue
        results = run_replay(symbol, bars, analyzer)
        all_results[symbol] = results
        report  = format_report(symbol, results, target_date)
        print(report)
        all_reports.append(report)

    # 전 종목 요약
    if not filter_sym and len(all_results) > 1:
        actual_buys = load_actual_buys(target_date)
        summary = build_summary(all_results, actual_buys)
        print(summary)
        all_reports.append(summary)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"replay_{target_date.strftime('%Y%m%d')}.txt"
    fname.write_text("\n\n".join(all_reports), encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
