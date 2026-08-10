#!/usr/bin/env python3
"""고가/저가 대비 유효범위(pullback) 제한 제거 시뮬레이션 (v2)

1차 시도(v1)에서 MinuteAnalyzer.analyze()의 패턴 플래그(is_valid_pulldown 등)만
바꿔서 비교했더니 시나리오 B가 0건이 나왔다 — 원인을 코드로 추적해보니
is_valid_pulldown(C조건 최초 감지)은 pullback_min/max_pct가 아니라
__init__ 내부에 하드코딩된 pulldown_min/max_pct(-8.0~-1.0%, 당일 등락률 기준)를
쓰고 있었고, settings.yaml의 pullback_min/max_pct(고가 대비, 현재 -7.0~-0.3%)는
breakout_strategy.py 안에서 "이미 패턴이 감지된 이후" 순수 C조건 진입에 대해서만
적용되는 별개의 2차 게이트였다.

그래서 v2는 analyzer를 baseline 설정으로 딱 한 번만 돌려서 원시 필드
(pullback_pct, is_valid_change_rate, is_valid_pulldown, is_v_rebound,
is_pulldown_recovery, price_above_vwap)를 뽑아낸 뒤, breakout_strategy.py의
실제 분기 로직(A조건 -2% 캡 / C조건 pullback_min~max 게이트)을 그대로
재현해서 "게이트를 몇 %로 바꾸면 결과가 달라지는가"를 직접 비교한다.

시나리오:
    A) baseline      — 현재 운영값 그대로 (A조건 -2% 캡, C조건 -7.0%~-0.3%)
    B) c_gate_open   — C조건 게이트만 완전 개방 (A조건 -2% 캡은 유지)
                       → 이게 실제로 settings.yaml의 pullback_min/max_pct를
                         지우거나 무제한으로 바꿨을 때 벌어지는 일이다.
    C) both_open     — A조건 -2% 캡까지 완전 개방 (코드 수정까지 포함한 완전 삭제)

주의: is_valid_pulldown 자체(C패턴 최초 감지)는 여전히 하드코딩된
pulldown_min/max_pct(-8~-1%, 당일 등락률 기준)에 걸려 있어서 이건 이 시뮬레이션의
대상이 아니다 (사용자가 얘기한 "고가/저가 대비 유효범위"와는 다른 별도 필터라
결과 해석에 섞이지 않도록 그대로 둔다 — 결과에서 별도로 언급).

실행:
    python simulate_pullback_removal.py                # 저장된 1분봉 전체 기간
    python simulate_pullback_removal.py 2026-07-01 2026-07-14
"""

from __future__ import annotations

import sys
import io

import csv
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, os.getcwd())

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR     = Path("reports")
AFTER_MINUTES   = [5, 10, 20]

# 2026-08-07 (1J): 비용은 domain/cost_model.py 단일 출처에서 읽습니다.
# 예전엔 여기에 0.25 + 0.10 = 0.35%를 직접 박아뒀는데,
# daily_report는 0.90%를 쓰고 있어 두 기준이 갈라져 있었습니다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain.cost_model import load_cost_model  # noqa: E402
from domain.replay_context import ReplayDayContext  # noqa: E402
# 2026-08-07 (1J.3.2): replay_runner가 import 시 config를 읽지 않게
# 바뀌어 MINUTE_BAR_COUNT가 None이므로, 필요한 시점에 직접 resolve.
from replay_runner import resolve_minute_bar_count  # noqa: E402
REPLAY_MINUTE_BAR_COUNT = resolve_minute_bar_count()
COST_MODEL = load_cost_model()
TOTAL_COST_PCT = COST_MODEL.base_roundtrip_pct   # 하위호환(Base 시나리오)
ROUND_TRIP_COST_PCT = TOTAL_COST_PCT
SLIPPAGE_PCT = 0.0

# 현재 운영값 (config/settings.yaml과 동일)
BASE_A_CAP          = -2.0    # A조건: 고가 대비 이 값 미만이면 컷 (하드코딩)
BASE_C_MIN          = -7.0    # C조건: pullback_min_pct
BASE_C_MAX          = -0.3    # C조건: pullback_max_pct


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


def build_analyzer():
    from config.settings import load_settings
    from domain.market_regime.minute_analyzer import MinuteAnalyzer
    settings = load_settings()
    cfg = settings.market_regime
    return MinuteAnalyzer(
        min_trading_value       = 0,
        pullback_min_pct        = BASE_C_MIN,
        pullback_max_pct        = BASE_C_MAX,
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
    # pulldown_min/max_pct(-8~-1%, C 최초감지용)는 건드리지 않음 — 별도 필터


def decide(a, a_cap: float, c_min: float, c_max: float):
    """breakout_strategy.py의 실제 분기 로직 재현. (would_attempt, pattern_label)"""
    pass_change   = a.is_valid_change_rate
    pass_rebound  = False  # 운영코드에서 강제 비활성 (pass_rebound = False)
    pass_pulldown = a.is_valid_pulldown
    pass_v        = a.is_v_rebound
    pass_pr       = a.is_pulldown_recovery

    if not any([pass_change, pass_rebound, pass_pulldown, pass_v, pass_pr]):
        return False, "-"          # 패턴 자체 미감지 — 이 시뮬레이션 대상 아님
    if not a.price_above_vwap:
        return False, "VWAP아래"    # VWAP 게이트는 항상 그대로 적용

    pats = []
    if pass_v:        pats.append("V")
    if pass_pr:       pats.append("PR")
    if pass_change:   pats.append("A")
    if pass_pulldown: pats.append("C")
    label = "/".join(pats)

    if pass_change and not pass_pulldown:
        if a.pullback_pct < a_cap:
            return False, label
        return True, label
    elif pass_pulldown and not pass_change:
        if not (c_min <= a.pullback_pct <= c_max):
            return False, label
        return True, label
    # V/PR 단독 또는 A+C 동시 충족 등 — 추가 pullback 체크 없음
    return True, label


def get_time_bucket(cntr_tm: str) -> str:
    try:
        hhmm = int(cntr_tm[8:12])
        if hhmm < 1000:   return "09:00~10:00"
        if hhmm < 1330:   return "10:00~13:30"
        if hhmm < 1450:   return "13:30~14:50"
        return "14:50~"
    except (ValueError, IndexError):
        return "기타"


def compute_returns(ctx, entry_dt, entry_price):
    """clock-time 기준 5/10/20분 후 수익률.

    2026-08-07 (1J.3): 이전엔 `idx = i + m`(봉 개수)였음. 실측상
    46.1% 파일에 1분 초과 gap이 있어 "5분 후"가 8·15·35분 후가 될
    수 있었으므로 timestamp 기준으로 교체.
    """
    after = {}
    for m in AFTER_MINUTES:
        hp = ctx.price_at_horizon(entry_dt, m)
        after[m] = ((hp.price - entry_price) / entry_price * 100) if hp.available else None
    return after


EXTRA_SCENARIOS = {
    # name: (a_cap, c_min, c_max)
    "D_c_minus15": (BASE_A_CAP, -15.0, BASE_C_MAX),
    "E_c_minus20": (BASE_A_CAP, -20.0, BASE_C_MAX),
}


def run_symbol_day(symbol: str, target_date: date, analyzer) -> dict:
    """시나리오별 신규 감지분(baseline엔 없던 것)을 모아서 반환."""
    bars = load_bars(symbol, target_date)
    out = {"B_c_gate_open": [], "C_both_open": [], "D_c_minus15": [], "E_c_minus20": []}
    if len(bars) < 5:
        return out
    # 2026-08-07 (1J.3): replay_runner와 동일한 공용 시간축 컨텍스트.
    ctx = ReplayDayContext(bars, target_date, minute_bar_count=REPLAY_MINUTE_BAR_COUNT)
    prev_close = ctx.previous_close
    if prev_close is None:
        return out          # 임의 추정 금지 — 등락률 조건이 왜곡됨

    for i in ctx.target_indices:
        if i < 5:
            continue
        window = ctx.analysis_window(i)      # 현재봉 포함 최근 60봉
        current = bars[i]
        entry_dt = ctx.bar_dt(i)
        if entry_dt is None:
            continue
        try:
            a = analyzer.analyze(window, prev_close)
        except Exception:
            continue
        if a is None:
            continue

        base_ok, _        = decide(a, BASE_A_CAP, BASE_C_MIN, BASE_C_MAX)
        b_ok,   b_label    = decide(a, BASE_A_CAP, -100.0, 100.0)     # C게이트만 개방
        c_ok,   c_label    = decide(a, -100.0,    -100.0, 100.0)      # A캡+C게이트 전부개방

        entry_price = current.close_price
        after = compute_returns(ctx, entry_dt, entry_price)
        # 2026-08-07 (1J.3.1): MFE/MAE만 아직 "다음 20개 봉" 기준이었음.
        # 5/10/20분 수익률은 1J.3에서 clock-time으로 고쳤는데 여기만
        # 남아, gap이 있으면 20분을 훌쩍 넘는 구간이 포함됐음.
        mfe, mae = ctx.mfe_mae(entry_dt, entry_price, minutes=20)
        row_base = dict(
            symbol=symbol, date=target_date.isoformat(), entry_time=current.cntr_tm,
            time_bucket=get_time_bucket(current.cntr_tm), entry_price=entry_price,
            pullback_pct=round(a.pullback_pct, 2),
            after_5m=after.get(5), after_10m=after.get(10), after_20m=after.get(20),
            # 2026-08-07 (1J.1): net_*는 Base alias로 유지하고
            # gross/base/stress를 함께 산출합니다.
            gross_5m=COST_MODEL.net(after.get(5), "gross"),
            base_5m=COST_MODEL.net(after.get(5), "base"),
            stress_5m=COST_MODEL.net(after.get(5), "stress"),
            net_5m=COST_MODEL.net(after.get(5), "base"),   # Base alias
            gross_10m=COST_MODEL.net(after.get(10), "gross"),
            base_10m=COST_MODEL.net(after.get(10), "base"),
            stress_10m=COST_MODEL.net(after.get(10), "stress"),
            net_10m=COST_MODEL.net(after.get(10), "base"),   # Base alias
            gross_20m=COST_MODEL.net(after.get(20), "gross"),
            base_20m=COST_MODEL.net(after.get(20), "base"),
            stress_20m=COST_MODEL.net(after.get(20), "stress"),
            net_20m=COST_MODEL.net(after.get(20), "base"),   # Base alias
            mfe=round(mfe, 2), mae=round(mae, 2),
        )

        if b_ok and not base_ok:
            out["B_c_gate_open"].append({**row_base, "patterns": b_label})
        if c_ok and not base_ok:
            out["C_both_open"].append({**row_base, "patterns": c_label})

        for name, (a_cap, c_min, c_max) in EXTRA_SCENARIOS.items():
            ok, label = decide(a, a_cap, c_min, c_max)
            if ok and not base_ok:
                out[name].append({**row_base, "patterns": label})

    return out


def summarize(rows: list[dict], label: str) -> str:
    lines = [f"── {label} ──"]
    if not rows:
        lines.append("  (해당 없음 — baseline 대비 신규 감지 0건)")
        return "\n".join(lines)
    lines.append(f"  신규 감지 봉: {len(rows)}건")
    for m in AFTER_MINUTES:
        vals = [r[f"net_{m}m"] for r in rows if r[f"net_{m}m"] is not None]
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(f"  {m:>2}분 순수익  {len(vals):>5}건  승률 {win/len(vals)*100:5.1f}%  평균 {avg:+.2f}%")
    maes = [r["mae"] for r in rows]
    mfes = [r["mfe"] for r in rows]
    lines.append(f"  MFE 평균 {sum(mfes)/len(mfes):+.2f}%   MAE 평균 {sum(maes)/len(maes):+.2f}%")

    pat_cnt = defaultdict(int)
    for r in rows:
        pat_cnt[r["patterns"]] += 1
    top_pats = sorted(pat_cnt.items(), key=lambda x: -x[1])[:8]
    lines.append("  패턴 구성: " + ", ".join(f"{p}({c}건)" for p, c in top_pats))

    lines.append("")
    lines.append("  [시간대별 5분 순수익]")
    tb = defaultdict(list)
    for r in rows:
        if r["net_5m"] is not None:
            tb[r["time_bucket"]].append(r["net_5m"])
    for bucket in ["09:00~10:00", "10:00~13:30", "13:30~14:50", "14:50~"]:
        vals = tb.get(bucket, [])
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(f"    {bucket:<14} {len(vals):>4}건  승률 {win/len(vals)*100:4.0f}%  순수익 {avg:+.2f}%")

    lines.append("")
    lines.append("  [pullback_pct(고가대비) 구간별 5분 순수익]")
    buckets = [(-100,-15),(-15,-10),(-10,-7),(-7,-3),(-3,-0.3),(-0.3,0),(0,100)]
    pb = defaultdict(list)
    for r in rows:
        if r["net_5m"] is None:
            continue
        for lo, hi in buckets:
            if lo <= r["pullback_pct"] < hi:
                pb[(lo,hi)].append(r["net_5m"])
                break
    for lo, hi in buckets:
        vals = pb.get((lo,hi), [])
        if not vals:
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(f"    {lo:>6.1f}%~{hi:<6.1f}%  {len(vals):>4}건  승률 {win/len(vals)*100:4.0f}%  순수익 {avg:+.2f}%")

    # 2026-08-07 (1J.2): horizon별 3시나리오 실제 출력
    for _m in (5, 10, 20):
        _raws = [r.get(f"gross_{_m}m") for r in rows]
        append_cost_scenarios(lines, _raws, f"{_m}분 후")
    top_gain = sorted([r for r in rows if r['net_5m'] is not None], key=lambda r: -r['net_5m'])[:3]
    top_loss = sorted([r for r in rows if r['net_5m'] is not None], key=lambda r: r['net_5m'])[:3]
    lines.append("")
    lines.append("  [예시 - 상위 수익 3건]")
    for r in top_gain:
        lines.append(f"    {r['date']} {r['symbol']} {r['entry_time'][8:12]} pullback{r['pullback_pct']:+.1f}% 5m{r['net_5m']:+.2f}%")
    lines.append("  [예시 - 상위 손실 3건]")
    for r in top_loss:
        lines.append(f"    {r['date']} {r['symbol']} {r['entry_time'][8:12]} pullback{r['pullback_pct']:+.1f}% 5m{r['net_5m']:+.2f}%")

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
    args = sys.argv[1:]
    # 2026-08-07 (1J.2, 기존 결함): data/minute_bars 아래에는 날짜
    # 디렉터리 외에 rejected/ 같은 보조 폴더가 있어 int() 변환에서
    # ValueError로 죽었음. 8자리 숫자 디렉터리만 대상으로 제한.
    all_dates = sorted(p.name for p in MINUTE_BARS_DIR.iterdir() if p.is_dir())
    all_dates = [d for d in all_dates if len(str(d)) == 8 and str(d).isdigit()]
    if len(args) >= 2:
        s, e = args[0].replace("-", ""), args[1].replace("-", "")
        all_dates = [d for d in all_dates if s <= d <= e]

    analyzer = build_analyzer()

    results_B, results_C = [], []
    results_D, results_E = [], []
    n_symbol_days = 0
    for d in all_dates:
        target_date = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        day_dir = MINUTE_BARS_DIR / d
        for sym in sorted(p.stem for p in day_dir.glob("*.csv")):
            n_symbol_days += 1
            out = run_symbol_day(sym, target_date, analyzer)
            results_B.extend(out["B_c_gate_open"])
            results_C.extend(out["C_both_open"])
            results_D.extend(out["D_c_minus15"])
            results_E.extend(out["E_c_minus20"])

    report = []
    report.append("═" * 64)
    report.append(f"  📊 고가/저가 대비 유효범위 제거 시뮬레이션 v2  ({all_dates[0]}~{all_dates[-1]})")
    report.append("═" * 64)
    report.append(f"  대상: {len(all_dates)}거래일 × 종목-일 {n_symbol_days}건")
    # 2026-08-07 (1J.2): 아래 비용 시나리오 줄로 대체됨(중복 제거)
    report.append(f"  비용 시나리오: {COST_MODEL.describe()}")
    report.append(f"  baseline: A조건 고가대비 {BASE_A_CAP}% 캡 / C조건 고가대비 {BASE_C_MIN}%~{BASE_C_MAX}%")
    report.append("  ※ C조건 최초 패턴감지(is_valid_pulldown)에 쓰이는 하드코딩된")
    report.append("     당일등락률 -8%~-1% 필터는 그대로 유지 (이 시뮬레이션 범위 밖)")
    report.append("")
    report.append(summarize(results_D, f"시나리오 D: C조건 하한만 -15.0%로 완화 (상한 {BASE_C_MAX}% 유지, A캡 유지)"))
    report.append("")
    report.append(summarize(results_E, f"시나리오 E: C조건 하한만 -20.0%로 완화 (상한 {BASE_C_MAX}% 유지, A캡 유지)"))
    report.append("")
    report.append(summarize(results_B, "시나리오 B: C조건 게이트 완전 개방 (참고용, 무제한)"))
    report.append("")
    report.append(summarize(results_C, "시나리오 C: A조건 캡 + C조건 게이트 전부 개방 (완전 삭제, 참고용)"))
    report.append("")
    report.append("═" * 64)

    text = "\n".join(report)
    print(text)
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"pullback_removal_sim_{date.today().strftime('%Y%m%d')}.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"\n  → 저장: {out_path}")


if __name__ == "__main__":
    main()
