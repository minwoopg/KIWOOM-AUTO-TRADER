#!/usr/bin/env python3
"""ATR / 볼린저밴드 지표 분석 스크립트

signal_log.csv의 atr_14, atr_14_pct, bb_percent_b, bb_bandwidth_pct,
bb_position 필드를 분석해서 가상 차단 효과를 검증합니다.

현재 ATR/볼린저는 log_only 단계입니다 (매수 차단에 사용하지 않음).
이 스크립트는 "만약 차단했다면 손익이 어땠을지"를 가상으로 분석합니다.

실행:
    python analyze_indicators.py                          # 오늘 날짜
    python analyze_indicators.py 2026-06-16               # 특정 날짜
    python analyze_indicators.py 2026-06-09 2026-06-16    # 날짜 범위

결과: 콘솔 출력 + reports/indicator_analysis_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import csv
from collections import Counter
from datetime import date, datetime
from pathlib import Path


# ── 설정 ────────────────────────────────────────────────────────
SIGNAL_LOG = Path("logs/signal_log.csv")
TRADES_LOG = Path("logs/trades.csv")
REPORTS_DIR = Path("reports")


# ── 유틸 ────────────────────────────────────────────────────────
def pct(n: int, total: int) -> str:
    return f"{n/total*100:.1f}%" if total > 0 else "0.0%"


def safe_float(v):
    try:
        if v in (None, "", "None"):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


# ── 데이터 로드 ──────────────────────────────────────────────────
def load_signal_rows(start: date, end: date) -> list[dict]:
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


def load_trade_rows(start: date, end: date) -> list[dict]:
    """trades.csv에서 매수/매도 쌍을 만들어 entry 시점 지표와 손익을 매칭합니다."""
    if not TRADES_LOG.exists():
        return []

    raw = []
    with TRADES_LOG.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"]).date()
            except (ValueError, KeyError):
                continue
            if start <= ts <= end:
                raw.append(r)

    # symbol별로 BUY → SELL 순서로 페어링 (단순 FIFO)
    by_symbol: dict[str, list[dict]] = {}
    for r in raw:
        by_symbol.setdefault(r["symbol"], []).append(r)

    pairs = []
    for symbol, rs in by_symbol.items():
        buys = [r for r in rs if r.get("side") == "BUY" and r.get("accepted") == "True"]
        sells = [r for r in rs if r.get("side") == "SELL" and r.get("accepted") == "True"]
        for b, s in zip(buys, sells):
            try:
                buy_price = float(b["price"])
                sell_price = float(s["price"])
                qty = float(s.get("quantity") or b.get("quantity") or 0)
                pnl_pct = (sell_price - buy_price) / buy_price * 100 if buy_price else 0
                pairs.append({
                    "symbol": symbol,
                    "buy_row": b,
                    "sell_row": s,
                    "pnl_pct": pnl_pct,
                    "win": pnl_pct > 0,
                })
            except (ValueError, TypeError):
                continue
    return pairs


# ── 분석 ────────────────────────────────────────────────────────
def analyze(rows: list[dict], trade_pairs: list[dict], start: date, end: date) -> str:
    lines = []
    W = 62

    def sep(c="═"): lines.append(c * W)
    def title(t): sep(); lines.append(f"  {t}"); sep()
    def sub(t): lines.append(""); lines.append(f"── {t} ──")
    def row(label, val, note=""):
        n = f"  ({note})" if note else ""
        lines.append(f"  {label:<34} {val}{n}")

    title(f"📈 ATR / 볼린저 지표 분석  {start} ~ {end}  (총 {len(rows):,}건)")

    if not rows:
        lines.append("  데이터가 없습니다.")
        return "\n".join(lines)

    # ── ATR 데이터 존재 여부 체크 ──────────────────────────────
    atr_rows = [r for r in rows if safe_float(r.get("atr_14_pct")) is not None]
    bb_rows  = [r for r in rows if safe_float(r.get("bb_percent_b")) is not None]

    if not atr_rows and not bb_rows:
        lines.append("")
        lines.append("  ATR/볼린저 필드가 비어 있습니다.")
        lines.append("  (log_only 단계 — 패치 적용 후 데이터가 누적되면 분석됩니다)")
        return "\n".join(lines)

    # ════════════════════════════════════════════════════════
    # 1. ATR 분석
    # ════════════════════════════════════════════════════════
    sub("1. ATR(14) 분포")
    if atr_rows:
        atr_pcts = [safe_float(r["atr_14_pct"]) for r in atr_rows]
        atr_pcts = [a for a in atr_pcts if a is not None]
        row("ATR 데이터 보유 건수", f"{len(atr_rows):,}건")
        row("평균 ATR(14)%", f"{sum(atr_pcts)/len(atr_pcts):.2f}%")
        row("최소 / 최대", f"{min(atr_pcts):.2f}% / {max(atr_pcts):.2f}%")

        # 종목별 평균 ATR%
        sub("1-1. 종목별 평균 ATR(14)%")
        by_sym: dict[str, list[float]] = {}
        for r in atr_rows:
            a = safe_float(r.get("atr_14_pct"))
            if a is not None:
                by_sym.setdefault(r["symbol"], []).append(a)
        sym_avg = sorted(
            ((s, sum(v)/len(v), len(v)) for s, v in by_sym.items()),
            key=lambda x: -x[1],
        )
        for sym, avg, cnt in sym_avg[:10]:
            row(f"  {sym}", f"{avg:.2f}%  ({cnt}건)")
    else:
        row("ATR 데이터", "없음")

    # ── 1-2. ATR 가상 손절 분석 (실제 거래 매칭) ──────────────
    sub("1-2. ATR 기반 가상 손절 분석")
    atr_trade_pairs = []
    for p in trade_pairs:
        buy_atr_pct = safe_float(p["buy_row"].get("atr_14_pct") if "atr_14_pct" in p["buy_row"] else None)
        if buy_atr_pct is not None:
            atr_trade_pairs.append((p, buy_atr_pct))

    if atr_trade_pairs:
        row("ATR 데이터 보유 매매쌍", f"{len(atr_trade_pairs)}건")
        for mult in (1.0, 1.2, 1.5):
            sim_results = []
            for p, atr_pct in atr_trade_pairs:
                virtual_stop_pct = -atr_pct * mult
                actual_pnl = p["pnl_pct"]
                # 실제 손실이 가상 ATR 손절선보다 더 깊었으면 가상 손절가에서 청산됐다고 가정
                if actual_pnl <= virtual_stop_pct:
                    sim_pnl = virtual_stop_pct
                else:
                    sim_pnl = actual_pnl
                sim_results.append(sim_pnl)
            avg_sim = sum(sim_results) / len(sim_results)
            avg_actual = sum(p["pnl_pct"] for p, _ in atr_trade_pairs) / len(atr_trade_pairs)
            row(
                f"  ATR×{mult} 가상 손절",
                f"평균 {avg_sim:+.2f}%  (실제 평균 {avg_actual:+.2f}%)",
            )
    else:
        row("ATR 보유 매매쌍", "없음 (데이터 축적 필요)")

    # ════════════════════════════════════════════════════════
    # 2. 볼린저밴드 분석
    # ════════════════════════════════════════════════════════
    sub("2. 볼린저밴드 위치별 분포")
    if bb_rows:
        buckets = [
            (-999, 0.0,  "< 0 (하단 이탈)"),
            (0.0,  0.2,  "0~0.2 (하단 근처)"),
            (0.2,  0.5,  "0.2~0.5 (중심선 아래)"),
            (0.5,  0.8,  "0.5~0.8 (중심선 위)"),
            (0.8,  1.0,  "0.8~1.0 (상단 근처)"),
            (1.0,  999,  "> 1.0 (상단 돌파)"),
        ]
        row("볼린저 데이터 보유 건수", f"{len(bb_rows):,}건")
        for lo, hi, label in buckets:
            grp = [
                r for r in bb_rows
                if (b := safe_float(r.get("bb_percent_b"))) is not None and lo <= b < hi
            ]
            if grp:
                buy_cnt = sum(1 for r in grp if r.get("final_decision") == "BUY")
                row(f"  {label}", f"{len(grp):,}건  →  BUY {buy_cnt}건")

        # 밴드폭 통계
        bw_vals = [safe_float(r.get("bb_bandwidth_pct")) for r in bb_rows]
        bw_vals = [b for b in bw_vals if b is not None]
        if bw_vals:
            sub("2-1. 볼린저 밴드폭 통계")
            row("평균 밴드폭", f"{sum(bw_vals)/len(bw_vals):.2f}%")
            row("최소 / 최대", f"{min(bw_vals):.2f}% / {max(bw_vals):.2f}%")
    else:
        row("볼린저 데이터", "없음")

    # ── 2-2. 볼린저 가상 추격매수 차단 분석 ───────────────────
    sub("2-2. 볼린저 가상 추격매수 차단 분석")
    bb_trade_pairs = []
    for p in trade_pairs:
        buy_bb = safe_float(p["buy_row"].get("bb_percent_b") if "bb_percent_b" in p["buy_row"] else None)
        if buy_bb is not None:
            bb_trade_pairs.append((p, buy_bb))

    if bb_trade_pairs:
        row("볼린저 데이터 보유 매매쌍", f"{len(bb_trade_pairs)}건")
        above_upper = [(p, b) for p, b in bb_trade_pairs if b >= 1.0]
        below_upper = [(p, b) for p, b in bb_trade_pairs if b < 1.0]

        if above_upper:
            avg_pnl = sum(p["pnl_pct"] for p, _ in above_upper) / len(above_upper)
            win = sum(1 for p, _ in above_upper if p["win"])
            row(
                "  bb_percent_b >= 1.0 매매",
                f"{len(above_upper)}건  승률 {win}/{len(above_upper)}  평균 {avg_pnl:+.2f}%",
            )
        if below_upper:
            avg_pnl = sum(p["pnl_pct"] for p, _ in below_upper) / len(below_upper)
            win = sum(1 for p, _ in below_upper if p["win"])
            row(
                "  bb_percent_b < 1.0 매매",
                f"{len(below_upper)}건  승률 {win}/{len(below_upper)}  평균 {avg_pnl:+.2f}%",
            )

        if above_upper and below_upper:
            lines.append("")
            avg_above = sum(p["pnl_pct"] for p, _ in above_upper) / len(above_upper)
            avg_below = sum(p["pnl_pct"] for p, _ in below_upper) / len(below_upper)
            if avg_above < avg_below:
                lines.append(
                    f"  → bb_percent_b>=1.0 매매가 평균 {avg_below-avg_above:.2f}%p 더 나쁨 "
                    f"(차단 효과 있을 가능성)"
                )
            else:
                lines.append(
                    f"  → bb_percent_b>=1.0 매매가 오히려 나음 "
                    f"(아직 차단 근거 부족, 데이터 더 필요)"
                )
    else:
        row("볼린저 보유 매매쌍", "없음 (데이터 축적 필요)")

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

    signal_rows = load_signal_rows(start, end)
    trade_pairs = load_trade_rows(start, end)
    report = analyze(signal_rows, trade_pairs, start, end)

    print(report)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"indicator_analysis_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
