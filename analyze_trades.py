#!/usr/bin/env python3
"""trades.csv 분석 스크립트

실행:
    python analyze_trades.py                          # 오늘 날짜
    python analyze_trades.py 2026-05-27               # 특정 날짜
    python analyze_trades.py 2026-05-27 2026-05-28   # 날짜 범위

결과: 콘솔 출력 + reports/trade_analysis_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path


# ── 설정 ────────────────────────────────────────────────────────
TRADES_LOG  = Path("logs/trades.csv")
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
    if not TRADES_LOG.exists():
        print(f"[ERROR] {TRADES_LOG} 파일이 없습니다.")
        sys.exit(1)

    rows = []
    with TRADES_LOG.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"]).date()
            except (ValueError, KeyError):
                continue
            if start <= ts <= end:
                rows.append(r)
    return rows


# ── 매매 쌍 매칭 (매수-매도 페어링) ──────────────────────────────
def pair_trades(rows: list[dict]) -> list[dict]:
    """BUY-SELL 쌍을 매칭해 손익을 계산합니다."""
    accepted = [r for r in rows if str(r.get("accepted","")).lower() == "true"]
    buys:  dict[str, list[dict]] = defaultdict(list)
    sells: dict[str, list[dict]] = defaultdict(list)

    for r in accepted:
        sym = r.get("symbol","")
        if r.get("side") == "BUY":
            buys[sym].append(r)
        elif r.get("side") == "SELL":
            sells[sym].append(r)

    pairs = []
    for sym, sell_list in sells.items():
        buy_list = buys.get(sym, [])
        for sell in sell_list:
            if not buy_list:
                break
            buy = buy_list.pop(0)
            buy_price  = int(buy.get("price", 0) or 0)
            sell_price = int(sell.get("price", 0) or 0)
            qty        = int(sell.get("quantity", 0) or 0)
            if buy_price <= 0 or sell_price <= 0:
                continue
            pnl_pct = (sell_price - buy_price) / buy_price * 100
            pnl_amt = (sell_price - buy_price) * qty
            pairs.append({
                "symbol":        sym,
                "buy_price":     buy_price,
                "sell_price":    sell_price,
                "quantity":      qty,
                "pnl_pct":       pnl_pct,
                "pnl_amount":    pnl_amt,
                "win":           pnl_pct > 0,
                "entry_strategy": buy.get("entry_strategy",""),
                "market_regime": buy.get("market_regime",""),
                "entry_score":   buy.get("entry_score",""),
                "entry_reason":  buy.get("entry_reason",""),
                "is_v_rebound":  safe_bool(buy.get("is_v_rebound")),
                "is_pr":         safe_bool(buy.get("is_pulldown_recovery")),
                "v_drop_pct":    safe_float(buy.get("v_drop_pct")),
                "v_rise_pct":    safe_float(buy.get("v_rise_pct")),
                "upside":        safe_float(buy.get("upside_to_recent_high_pct")),
                "rebound_spike": safe_bool(buy.get("rebound_volume_spike")),
                "exit_reason":   sell.get("exit_reason",""),
                "hold_minutes":  safe_float(sell.get("hold_minutes")),
                "buy_time":      buy.get("timestamp",""),
                "sell_time":     sell.get("timestamp",""),
            })
    return pairs


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

    buy_rows  = [r for r in rows if r.get("side") == "BUY"
                 and str(r.get("accepted","")).lower() == "true"]
    sell_rows = [r for r in rows if r.get("side") == "SELL"
                 and str(r.get("accepted","")).lower() == "true"]
    pairs = pair_trades(rows)

    title(f"📈 trades.csv 분석  {start} ~ {end}")
    row("총 체결 건수", f"{len(rows):,}건")
    row("매수 체결", f"{len(buy_rows):,}건")
    row("매도 체결", f"{len(sell_rows):,}건")
    row("매매 쌍 (손익 계산 가능)", f"{len(pairs):,}건")

    if not pairs:
        lines.append("  매매 쌍이 없습니다.")
        return "\n".join(lines)

    # ── 1. 전체 손익 요약 ──────────────────────────────────
    sub("1. 전체 손익 요약")
    wins   = [p for p in pairs if p["win"]]
    losses = [p for p in pairs if not p["win"]]
    total_pnl = sum(p["pnl_amount"] for p in pairs)
    avg_pnl   = sum(p["pnl_pct"] for p in pairs) / len(pairs)
    row("승률", f"{len(wins)}/{len(pairs)}  ({pct(len(wins), len(pairs))})")
    row("누적 손익", f"{total_pnl:+,.0f}원")
    row("평균 수익률", f"{avg_pnl:+.2f}%")
    if wins:
        row("평균 수익 (승)", f"{sum(p['pnl_pct'] for p in wins)/len(wins):+.2f}%")
    if losses:
        row("평균 손실 (패)", f"{sum(p['pnl_pct'] for p in losses)/len(losses):+.2f}%")
    holds = [p["hold_minutes"] for p in pairs if p["hold_minutes"] is not None]
    if holds:
        row("평균 보유 시간", f"{sum(holds)/len(holds):.1f}분")

    # ── 2. exit_reason별 손익 ──────────────────────────────
    sub("2. exit_reason별 손익")
    er_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        er_groups[p["exit_reason"] or "(없음)"].append(p)
    for er, grp in sorted(er_groups.items(), key=lambda x: -len(x[1])):
        w = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(er[:36], f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 3. 전략별 손익 ─────────────────────────────────────
    sub("3. 전략별 손익")
    st_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        st_groups[p["entry_strategy"] or "(없음)"].append(p)
    for st, grp in sorted(st_groups.items(), key=lambda x: -len(x[1])):
        w   = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(st, f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 4. 점수별 손익 ─────────────────────────────────────
    sub("4. entry_score별 손익")
    score_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        score_groups[p["entry_score"] or "?"].append(p)
    for sc, grp in sorted(score_groups.items()):
        w   = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(f"{sc}점", f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 5. V자 진입 vs 일반 진입 ───────────────────────────
    sub("5. V자 진입 vs 일반 진입 비교")
    v_pairs    = [p for p in pairs if p["is_v_rebound"] is True]
    non_v      = [p for p in pairs if p["is_v_rebound"] is not True]
    pr_pairs   = [p for p in pairs if p["is_pr"] is True]
    for label, grp in [("V자 진입", v_pairs), ("PR 진입", pr_pairs), ("일반 진입", non_v)]:
        if not grp: continue
        w   = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 6. rebound_volume_spike별 손익 ──────────────────────
    sub("6. rebound_volume_spike별 손익")
    for spike_val, label in [(True,"True (반등봉 급등)"),(False,"False (일반)")]:
        grp = [p for p in pairs if p["rebound_spike"] == spike_val]
        if not grp: continue
        w   = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 7. upside_to_recent_high 구간별 손익 ────────────────
    sub("7. 상승 여력 구간별 손익")
    buckets = [("< 1%", lambda u: u<1.0),
               ("1~2%", lambda u: 1.0<=u<2.0),
               ("2~3%", lambda u: 2.0<=u<3.0),
               ("3~5%", lambda u: 3.0<=u<5.0),
               ("5%↑",  lambda u: u>=5.0)]
    for label, fn in buckets:
        grp = [p for p in pairs if p["upside"] is not None and fn(p["upside"])]
        if not grp: continue
        w   = sum(1 for p in grp if p["win"])
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {pct(w,len(grp))}  평균 {avg:+.2f}%")

    # ── 8. 종목별 손익 ─────────────────────────────────────
    sub("8. 종목별 손익")
    sym_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        sym_groups[p["symbol"]].append(p)
    for sym, grp in sorted(sym_groups.items(), key=lambda x: -sum(p["pnl_amount"] for p in x[1])):
        w     = sum(1 for p in grp if p["win"])
        total = sum(p["pnl_amount"] for p in grp)
        avg   = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(sym, f"{len(grp)}건  승률 {pct(w,len(grp))}  손익 {total:+,.0f}원  평균 {avg:+.2f}%")

    # ── 9. 거래 상세 목록 ──────────────────────────────────
    sub("9. 거래 상세 목록")
    for p in sorted(pairs, key=lambda x: x["buy_time"]):
        mark = "✅" if p["win"] else "❌"
        hold = f"{p['hold_minutes']:.0f}분" if p["hold_minutes"] else "?"
        v    = "V" if p["is_v_rebound"] else ("PR" if p["is_pr"] else "-")
        lines.append(
            f"  {mark} {p['symbol']}  {p['pnl_pct']:+.1f}%  "
            f"{p['pnl_amount']:+,.0f}원  {hold}  [{v}]  {p['exit_reason'][:30]}"
        )

    # ── 10. 매도 점수제 / 트레일링 분석 ─────────────────────
    sub("10. 추세꺾임 점수제 분석")
    sell_score_pairs = [p for p in pairs if "추세 꺾임" in (p.get("exit_reason") or "")]
    trail_pairs      = [p for p in pairs if "트레일링" in (p.get("exit_reason") or "")]

    if sell_score_pairs:
        row("추세꺾임 청산 건수", f"{len(sell_score_pairs)}건")
        sc_win  = [p for p in sell_score_pairs if p["win"]]
        sc_avg  = sum(p["pnl_pct"] for p in sell_score_pairs) / len(sell_score_pairs)
        row("  승률", f"{len(sc_win)}/{len(sell_score_pairs)} ({len(sc_win)/len(sell_score_pairs)*100:.0f}%)")
        row("  평균 수익률", f"{sc_avg:+.2f}%")
        # 점수 분포
        import re
        for p in sell_score_pairs:
            er = p.get("exit_reason", "")
            m  = re.search(r'(\d)/5점', er)
            score = m.group(1) if m else "?"
            mark  = "✅" if p["win"] else "❌"
            lines.append(
                f"    {mark} {p['symbol']}  {p['pnl_pct']:+.1f}%  "
                f"점수 {score}/5  {er[:40]}"
            )
    else:
        row("추세꺾임 청산", "0건")

    sub("10-1. 구간형 트레일링 분석")
    if trail_pairs:
        row("트레일링 청산 건수", f"{len(trail_pairs)}건")
        t_win = [p for p in trail_pairs if p["win"]]
        t_avg = sum(p["pnl_pct"] for p in trail_pairs) / len(trail_pairs)
        row("  승률", f"{len(t_win)}/{len(trail_pairs)} ({len(t_win)/len(trail_pairs)*100:.0f}%)")
        row("  평균 수익률", f"{t_avg:+.2f}%")
        # 트레일링 폭별 분포
        import re
        band_groups = {}
        for p in trail_pairs:
            er = p.get("exit_reason", "")
            m  = re.search(r'폭 -([\d.]+)%', er)
            band = m.group(1) if m else "?"
            band_groups.setdefault(band, []).append(p)
        for band, grp in sorted(band_groups.items()):
            w   = sum(1 for p in grp if p["win"])
            avg = sum(p["pnl_pct"] for p in grp) / len(grp)
            row(f"  -{band}% 트레일링",
                f"{len(grp)}건  승률 {w/len(grp)*100:.0f}%  평균 {avg:+.2f}%")
    else:
        row("트레일링 청산", "0건")

    sep()
    return "\n".join(lines)


# ── 메인 ────────────────────────────────────────────────────────
def main():
    args  = sys.argv[1:]
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
    fname = REPORTS_DIR / f"trade_analysis_{today.strftime('%Y%m%d')}.txt"
    fname.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
