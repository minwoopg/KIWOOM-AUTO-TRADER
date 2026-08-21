#!/usr/bin/env python3
"""trades.csv 분석 스크립트

실행:
    python analyze_trades.py                          # 오늘 날짜
    python analyze_trades.py 2026-05-27               # 특정 날짜
    python analyze_trades.py 2026-05-27 2026-05-28   # 날짜 범위

결과: 콘솔 출력 + reports/trade_analysis_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import sys
import io

import csv
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, ".")
from utils.trade_outcome import classify_outcome, format_win_rate, WIN, LOSS, BREAKEVEN  # noqa: E402


# ── 설정 ────────────────────────────────────────────────────────
TRADES_LOG  = Path("logs/trades.csv")
REPORTS_DIR = Path("reports")


# ── 유틸 ────────────────────────────────────────────────────────
def pct(n: int, total: int) -> str:
    return f"{n/total*100:.1f}%" if total > 0 else "0.0%"


def win_rate_pct(grp, ndigits: int = 1) -> str:
    """그룹의 승률(%) 문자열 — breakeven은 분모에서 제외합니다
    (1P0.8-OBS.2-C closure).

    headline("1. 전체 손익 요약")은 format_win_rate()로 이미
    통일했지만, exit_reason/전략/점수/V-PR/volume_spike/Low Upside/
    당일 등락률/종목별/추세꺾임/트레일링/조건검색식별 등 세부 구간
    breakdown들은 여전히 `sum(1 for p in grp if p["win"]) / len(grp)`
    형태로 BREAKEVEN을 분모(len(grp))에 포함시키고 있었습니다 —
    headline과 다른 승률이 나옵니다(예: WIN1/BREAKEVEN1/LOSS2면
    headline은 1/(1+2)=33.3%인데 세부 구간은 1/4=25.0%). 이 헬퍼로
    전부 통일합니다.

    승률의 정의는 wins/(wins+losses)이므로, 분모(wins+losses)가
    0인 경우(그룹 전체가 BREAKEVEN, 즉 승패가 전혀 없는 경우)는
    "0%"가 아니라 정의 불가입니다 — "해당없음"을 반환합니다
    (1P0.8-OBS.2 최종 리뷰, all-BREAKEVEN 0% 오표시 수정).
    headline의 format_win_rate()가 이미 이 경우 "해당없음"을
    반환하는 것과 동일한 semantics입니다.
    """
    w = sum(1 for p in grp if p["outcome"] == WIN)
    l = sum(1 for p in grp if p["outcome"] == LOSS)
    decided = w + l
    if decided == 0:
        return "해당없음"
    return f"{w/decided*100:.{ndigits}f}%"


def win_rate_frac(grp) -> str:
    """'승/(승+패) (승률%)' 형태 — breakeven 제외 분모.
    "N/M (P%)" 표시를 쓰는 구간(추세꺾임/트레일링/조건검색식별
    승률)에서 win_rate_pct()와 동일한 분모 정의를 재사용하기 위한
    변형(1P0.8-OBS.2-C closure).

    분모(wins+losses)가 0(전체 BREAKEVEN)이면 승률 부분은
    "해당없음"을 반환합니다 — win_rate_pct()와 동일한 semantics
    (1P0.8-OBS.2 최종 리뷰, all-BREAKEVEN 0% 오표시 수정).
    """
    w = sum(1 for p in grp if p["outcome"] == WIN)
    l = sum(1 for p in grp if p["outcome"] == LOSS)
    decided = w + l
    pct_str = f"{w/decided*100:.0f}%" if decided > 0 else "해당없음"
    return f"{w}/{decided} ({pct_str})"

def safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if f != 0.0 else None
    except (ValueError, TypeError):
        return None

def safe_float_zero_valid(v) -> float | None:
    """0.0을 정상값으로 취급하는 safe_float 변형.

    2026-08-21 (1P0.8-OBS.2-B, 8/21 실측 재현): safe_float()는
    v_drop_pct/v_rise_pct처럼 "0.0 = 해당 없음(V자 패턴 아님)"인
    필드를 위해 의도적으로 0.0을 None으로 접습니다. 하지만
    upside_to_recent_high_pct(상승 여력)처럼 0.0 자체가 유효한
    측정값인 필드에 safe_float를 그대로 쓰면 정확히 0.0인 매매가
    통계에서 통째로 빠집니다(8/21 017670: upside=0.00 → "< 1%"
    구간 3건으로 집계, 실제는 4건이어야 함). 0.0은 그대로 유효한
    값으로 반환합니다.

    2026-08-21 (1P0.8-OBS.2-C closure): `float("nan")`/
    `float("inf")`/`float("-inf")`/`float("1e309")`(overflow → inf)
    처럼 유한하지 않은 값은 예외를 던지지 않고 파싱에 그냥
    "성공"해버립니다 — `math.isfinite()`로 걸러내지 않으면 이런
    값이 상승 여력 구간(예: "< 1%")에 잘못 집계될 수 있습니다.
    `kiwoom_order_status.py`의 `_parse_optional_qty()`가 이미 같은
    이유로 non-finite 값을 fail-close하는 전례(1P0.8-B.2 closure
    2차)를 그대로 따릅니다. 결측은 None/빈 문자열/파싱 실패/
    non-finite(nan/inf/-inf) 네 가지로만 판정합니다.
    """
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(f):
        return None
    return f

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
    with TRADES_LOG.open(encoding="utf-8-sig") as f:
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
                # 2026-08-21 (1P0.8-OBS.2-C → closure): 최초엔 "win"
                # (pnl_pct>0)을 그대로 두고 헤드라인 승률만 outcome
                # 기준으로 바꿨으나, 세부 구간
                # breakdown(exit_reason/전략/점수/V-PR/volume_spike/
                # Low Upside/당일 등락률/종목별/추세꺾임/트레일링/
                # 조건검색식별)도 전부 "wins / len(grp)"(=BREAKEVEN이
                # 분모에 섞임) 형태였다는 지적을 받아 전부
                # win_rate_pct()/win_rate_frac()(둘 다 outcome 기준,
                # 분모는 wins+losses만)로 통일했습니다. "win" 필드
                # 자체는 이제 이 파일 안에서 더 이상 쓰이지 않지만,
                # 하위호환을 위해 그대로 남겨둡니다(외부에서 참조할
                # 가능성 대비 — 의미는 예전과 동일하게 pnl_pct>0).
                "outcome":       classify_outcome(pnl_pct),
                "win":           pnl_pct > 0,
                "entry_strategy": buy.get("entry_strategy",""),
                "market_regime": buy.get("market_regime",""),
                "entry_score":   buy.get("entry_score",""),
                "entry_reason":  buy.get("entry_reason",""),
                "is_v_rebound":  safe_bool(buy.get("is_v_rebound")),
                "is_pr":         safe_bool(buy.get("is_pulldown_recovery")),
                "v_drop_pct":    safe_float(buy.get("v_drop_pct")),
                "v_rise_pct":    safe_float(buy.get("v_rise_pct")),
                "upside":        safe_float_zero_valid(buy.get("upside_to_recent_high_pct")),
                "rebound_spike": safe_bool(buy.get("rebound_volume_spike")),
                "change_rate":   safe_float(buy.get("change_rate_pct")),
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
    wins       = [p for p in pairs if p["outcome"] == WIN]
    losses     = [p for p in pairs if p["outcome"] == LOSS]
    breakevens = [p for p in pairs if p["outcome"] == BREAKEVEN]
    total_pnl = sum(p["pnl_amount"] for p in pairs)
    avg_pnl   = sum(p["pnl_pct"] for p in pairs) / len(pairs)
    # 2026-08-21 (1P0.8-OBS.2-C, 8/21 실측 재현): daily_reporter.py와
    # 동일한 WIN/LOSS/BREAKEVEN 정의(utils/trade_outcome.py) + 동일한
    # 표시 형식을 씁니다 — 동률 매매(017670, pnl=0)가 있을 때 두
    # 리포트가 서로 다른 승/패 수("3승 2패" vs "2/5")를 내던 문제 재발
    # 방지. 승률 분모는 wins+losses만(breakeven 제외).
    row("승률", format_win_rate(len(wins), len(losses), len(breakevens)))
    row("누적 손익", f"{total_pnl:+,.0f}원")
    row("평균 수익률", f"{avg_pnl:+.2f}%")
    if wins:
        row("평균 수익 (승)", f"{sum(p['pnl_pct'] for p in wins)/len(wins):+.2f}%")
    if losses:
        row("평균 손실 (패)", f"{sum(p['pnl_pct'] for p in losses)/len(losses):+.2f}%")
    if breakevens:
        row("동률 (무)", f"{len(breakevens)}건")
    holds = [p["hold_minutes"] for p in pairs if p["hold_minutes"] is not None]
    if holds:
        row("평균 보유 시간", f"{sum(holds)/len(holds):.1f}분")

    # ── 2. exit_reason별 손익 ──────────────────────────────
    sub("2. exit_reason별 손익")
    er_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        er_groups[p["exit_reason"] or "(없음)"].append(p)
    for er, grp in sorted(er_groups.items(), key=lambda x: -len(x[1])):
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(er[:36], f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

    # ── 3. 전략별 손익 ─────────────────────────────────────
    sub("3. 전략별 손익")
    st_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        st_groups[p["entry_strategy"] or "(없음)"].append(p)
    for st, grp in sorted(st_groups.items(), key=lambda x: -len(x[1])):
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(st, f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

    # ── 4. 점수별 손익 ─────────────────────────────────────
    sub("4. entry_score별 손익")
    score_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        score_groups[p["entry_score"] or "?"].append(p)
    for sc, grp in sorted(score_groups.items()):
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(f"{sc}점", f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

    # ── 5. V자 진입 vs 일반 진입 ───────────────────────────
    sub("5. V자 진입 vs 일반 진입 비교")
    v_pairs    = [p for p in pairs if p["is_v_rebound"] is True]
    non_v      = [p for p in pairs if p["is_v_rebound"] is not True]
    pr_pairs   = [p for p in pairs if p["is_pr"] is True]
    for label, grp in [("V자 진입", v_pairs), ("PR 진입", pr_pairs), ("일반 진입", non_v)]:
        if not grp: continue
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

    # ── 6. rebound_volume_spike별 손익 ──────────────────────
    sub("6. rebound_volume_spike별 손익")
    for spike_val, label in [(True,"True (반등봉 급등)"),(False,"False (일반)")]:
        grp = [p for p in pairs if p["rebound_spike"] == spike_val]
        if not grp: continue
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

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
        avg = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(label, f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")

    # ── 7-1. 당일 등락률 구간별 손익 (급등 종목 진입 추적) ────
    cr_pairs = [p for p in pairs if p.get("change_rate") is not None]
    if cr_pairs:
        sub("7-1. 당일 등락률 구간별 손익")
        cr_buckets = [
            ("< +3%",     lambda c: c < 3.0),
            ("+3~+5%",    lambda c: 3.0 <= c < 5.0),
            ("+5~+10%",   lambda c: 5.0 <= c < 10.0),
            ("+10~+15%",  lambda c: 10.0 <= c < 15.0),
            ("+15%+ (고변동)", lambda c: c >= 15.0),
        ]
        for label, fn in cr_buckets:
            grp = [p for p in cr_pairs if fn(p["change_rate"])]
            if not grp: continue
            avg = sum(p["pnl_pct"] for p in grp) / len(grp)
            row(label, f"{len(grp)}건  승률 {win_rate_pct(grp)}  평균 {avg:+.2f}%")
        # 2026-08-21 (1P0.8-OBS.2-C closure): "not p['win']"은
        # BREAKEVEN(무승부)까지 손실로 잘못 분류합니다 —
        # outcome == LOSS로 명시.
        loss_pairs = sorted([p for p in cr_pairs if p["outcome"] == LOSS],
                            key=lambda p: -(p["change_rate"] or 0))
        if loss_pairs:
            lines.append("")
            lines.append("  [ 손실 거래의 당일 등락률 (높은 순) ]")
            for p in loss_pairs[:8]:
                row(f"  {p['symbol']}", f"당일 {p['change_rate']:+.1f}%  →  {p['pnl_pct']:+.2f}%")

    # ── 8. 종목별 손익 ─────────────────────────────────────
    sub("8. 종목별 손익")
    sym_groups: dict[str, list] = defaultdict(list)
    for p in pairs:
        sym_groups[p["symbol"]].append(p)
    for sym, grp in sorted(sym_groups.items(), key=lambda x: -sum(p["pnl_amount"] for p in x[1])):
        total = sum(p["pnl_amount"] for p in grp)
        avg   = sum(p["pnl_pct"] for p in grp) / len(grp)
        row(sym, f"{len(grp)}건  승률 {win_rate_pct(grp)}  손익 {total:+,.0f}원  평균 {avg:+.2f}%")

    # ── 9. 거래 상세 목록 ──────────────────────────────────
    sub("9. 거래 상세 목록")
    for p in sorted(pairs, key=lambda x: x["buy_time"]):
        # 2026-08-21 (1P0.8-OBS.2-C): outcome 기준 3분류(daily_reporter.py와
        # 동일 표시) — 동률(017670 등)을 ❌로 잘못 표시하지 않도록.
        mark = "✅" if p["outcome"] == WIN else ("➖" if p["outcome"] == BREAKEVEN else "❌")
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
        sc_avg  = sum(p["pnl_pct"] for p in sell_score_pairs) / len(sell_score_pairs)
        row("  승률", win_rate_frac(sell_score_pairs))
        row("  평균 수익률", f"{sc_avg:+.2f}%")
        # 점수 분포
        import re
        for p in sell_score_pairs:
            er = p.get("exit_reason", "")
            m  = re.search(r'(\d)/5점', er)
            score = m.group(1) if m else "?"
            mark  = "✅" if p["outcome"] == WIN else ("➖" if p["outcome"] == BREAKEVEN else "❌")
            lines.append(
                f"    {mark} {p['symbol']}  {p['pnl_pct']:+.1f}%  "
                f"점수 {score}/5  {er[:40]}"
            )
    else:
        row("추세꺾임 청산", "0건")

    sub("10-1. 구간형 트레일링 분석")
    if trail_pairs:
        row("트레일링 청산 건수", f"{len(trail_pairs)}건")
        t_avg = sum(p["pnl_pct"] for p in trail_pairs) / len(trail_pairs)
        row("  승률", win_rate_frac(trail_pairs))
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
            avg = sum(p["pnl_pct"] for p in grp) / len(grp)
            row(f"  -{band}% 트레일링",
                f"{len(grp)}건  승률 {win_rate_pct(grp, ndigits=0)}  평균 {avg:+.2f}%")
    else:
        row("트레일링 청산", "0건")

    # ── 11. condition_name별 손익 ──────────────────────────────
    cond_pairs = [p for p in pairs if p.get("condition_name","")]
    if cond_pairs:
        sub("11. 조건검색식별 손익")
        cond_names = sorted(set(p["condition_name"] for p in cond_pairs))
        for cname in cond_names:
            grp  = [p for p in cond_pairs if p.get("condition_name") == cname]
            avg  = sum(p["pnl_pct"] for p in grp) / len(grp) if grp else 0
            pnl  = sum(p["pnl_amount"] for p in grp)
            row(f"  {cname}",
                f"{len(grp)}건  승률 {win_rate_frac(grp)}  "
                f"평균 {avg:+.2f}%  손익 {pnl:+,.0f}원")
            # 패턴별 세부
            pat_cnt: dict = {}
            for p in grp:
                pat = p.get("entry_reason", "-")
                pat_cnt.setdefault(pat, []).append(p)
            for pat, pgrp in sorted(pat_cnt.items(),
                                    key=lambda x: -len(x[1]))[:3]:
                pa  = sum(p["pnl_pct"] for p in pgrp) / len(pgrp)
                lines.append(
                    f"      {pat}: {len(pgrp)}건 "
                    f"승률 {win_rate_pct(pgrp, ndigits=0)} 평균 {pa:+.2f}%"
                )

    sep()
    return "\n".join(lines)


# ── 메인 ────────────────────────────────────────────────────────
def _force_utf8_stdout() -> None:
    """Windows 콘솔 한글 깨짐 방지 — 직접 실행할 때만 적용.

    2026-08-07 (1J.2): 모듈 최상위에서 sys.stdout을 교체하면
    이 모듈을 import하는 쪽(테스트 등)의 stdout까지 닫혀
    ValueError: I/O operation on closed file이 발생합니다.
    """
    import io as _io, sys as _sys
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")


def main():
    _force_utf8_stdout()
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
