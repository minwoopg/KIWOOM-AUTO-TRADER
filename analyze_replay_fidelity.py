#!/usr/bin/env python3
"""Live ↔ Replay Fidelity 측정 (2026-08-07, 1J.5단계)

실행:
    python analyze_replay_fidelity.py 2026-08-07
    python analyze_replay_fidelity.py 2026-08-07 --signal-log logs/signal_log.csv

결과: 콘솔 + reports/replay_fidelity_YYYYMMDD.txt

무엇을 하는가
------------
**이 단계는 replay를 고치는 단계가 아니라, live가 실제로 계산한 값을
현재 replay가 얼마나 정확히 재현하는지 "측정"하는 단계입니다.**
불일치가 나와도 replay 로직을 바꾸지 않고 원인 코드(reason_code)로
분류합니다.

두 축으로 나눕니다.

A. Aligned Value Fidelity  (가장 중요)
   live signal_log의 `(symbol, latest_bar_timestamp)` 시점을 기준으로
   같은 60봉을 넣어 계산값이 일치하는지 봅니다. 계산식 자체의 동일성
   을 보는 것이므로 높은 일치율을 요구할 수 있습니다.

B. Candidate Discovery Fidelity
   replay가 하루를 독립 스캔해 live candidate를 얼마나 재발견하는지.
   **replay는 조건검색 universe·거래대금 필터·cooldown·보유 상태·
   daily risk limit을 재현하지 않으므로**, precision/recall이 낮아도
   곧바로 replay 결함이라고 판단하면 안 됩니다. 한계를 병기합니다.

Primary Fidelity Set (1J.5 가드레일 3)
--------------------------------------
    full_window == True
    prev_close_source ∈ Tier A
    analyzer_mode == LIVE_MINUTE_ANALYZER
    analyzer_error_count == 0

Tier A(Primary): SAME_FILE_PRETARGET, SIGNAL_LOG_INFERRED
Tier B(Secondary): PREVIOUS_DATA_DAY_EOD
    — "직전 **데이터** 날짜"일 뿐 거래소 캘린더상 직전 거래일이라는
      보장이 없습니다. 데이터 폴더가 하루 빠졌다면 며칠 전 종가일 수
      있어 Primary에서 제외합니다.
Excluded: PREVIOUS_DATA_DAY_PARTIAL, UNAVAILABLE

full-window 용어 (가드레일 2)
----------------------------
`replay_runner`의 `full_window_coverage_pct`는 **evaluation-point**
기준입니다(analyzer 실행 전 모든 target 분봉에서 집계). "BUY 후보의
61.6%가 full history였다"는 뜻이 **아닙니다**. 여기서는 별도로
`aligned_live_rows_full_window_pct` / `legacy_buy_candidate_full_
window_pct` / `accepted_buy_full_window_pct`를 따로 냅니다.
"""
from __future__ import annotations

import csv
import io
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.replay_context import (  # noqa: E402
    build_day_context, is_full_window, parse_bar_dt,
    PREV_CLOSE_SAME_FILE, PREV_CLOSE_SIGNAL_INFERRED, PREV_CLOSE_PREV_DAY,
)
import replay_runner as RR  # noqa: E402

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR = Path("reports")
DEFAULT_SIGNAL_LOG = Path("logs/signal_log.csv")

TIER_A_SOURCES = {PREV_CLOSE_SAME_FILE, PREV_CLOSE_SIGNAL_INFERRED}
TIER_B_SOURCES = {PREV_CLOSE_PREV_DAY}

# 불일치 원인 코드 — 모든 불일치에 반드시 하나가 붙어야 합니다.
REASON_PARTIAL_HISTORY = "PARTIAL_HISTORY"
REASON_LIVE_ACC_VOLUME = "LIVE_ACC_VOLUME_UNAVAILABLE"
REASON_NO_MINUTE_DATA = "NO_MINUTE_DATA"
REASON_BAR_NOT_FOUND = "BAR_NOT_FOUND"
REASON_PREV_CLOSE_UNTRUSTED = "PREV_CLOSE_UNTRUSTED"
REASON_VALUE_MISMATCH = "VALUE_MISMATCH"
REASON_CONDITION_UNIVERSE = "CONDITION_UNIVERSE_NOT_REPLAYED"
# live는 결정 시점의 실시간 체결가로 판단하고 replay는 분봉 종가를
# 씁니다. 형성 중인 봉에서는 구조적으로 값이 다르며, 이는 replay
# 버그가 아니라 데이터 성격의 차이입니다.
REASON_LIVE_TICK_VS_BAR_CLOSE = "LIVE_TICK_VS_BAR_CLOSE"
REASON_PREV_CLOSE_UNAVAILABLE = "PREV_CLOSE_UNAVAILABLE"
REASON_UNKNOWN = "UNKNOWN"
# MinuteAnalyzer는 MACD를 계산하지 않습니다(별도 indicator 경로).
REASON_MACD_NOT_IN_ANALYZER = "MACD_NOT_COMPUTED_BY_ANALYZER"
# live MACD는 TradingService가 cached_daily_bars(일봉 API)로 계산하며
# 그 일봉은 파일로 저장되지 않습니다 — 과거 시점 재현 자료가 없음.
REASON_MACD_NO_DAILY_DATA = "MACD_DAILY_BARS_NOT_PERSISTED"

# 비교 항목 — (live 컬럼, 허용 오차 %, 라벨)
NUMERIC_FIELDS = [
    ("price", 0.0, "가격"),
    ("change_rate_pct", 0.05, "등락률"),
    ("current_vs_vwap_pct", 0.05, "VWAP 거리"),
    ("upside_to_recent_high_pct", 0.05, "상승여력"),
    # MACD는 MinuteAnalyzer가 아니라 별도 indicator 경로에서 계산되므로
    # replay(pattern replay) 비교 대상이 아닙니다 — limitation으로 표기.
]
BOOL_FIELDS = [
    ("is_v_rebound", "V"),
    ("is_pulldown_recovery", "PR"),
]


def truthy(v) -> bool:
    return str(v or "").strip().lower() == "true"


def fnum(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def load_signal_rows(path: Path, target: date) -> list[dict]:
    if not path.exists():
        return []
    day = target.isoformat()
    out = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            if (r.get("timestamp") or "").startswith(day):
                out.append(r)
    return out


def index_by_symbol(rows: list[dict], target: date) -> dict[str, list[dict]]:
    """가드레일 1 — (거래일, 종목) 단위로 정확히 잘라서 전달."""
    idx: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        idx[r.get("symbol", "")].append(r)
    day = target.isoformat()
    for sym, rs in idx.items():
        # 전달 전 방어 — 잘못된 종목/날짜가 섞이면 역산이 오염됩니다.
        assert all(x.get("symbol") == sym for x in rs), f"symbol 혼입: {sym}"
        assert all((x.get("timestamp") or "").startswith(day) for x in rs), \
            f"날짜 혼입: {sym}"
    return dict(idx)


# ══════════════════════════════════════════════════════════════
# 순수 helper (1J.5.2) — 테스트가 실제로 호출해 검증합니다.
# 1J.5.1의 테스트는 분석기 로직을 테스트 쪽에 다시 구현해서,
# "final_decision == accepted BUY"라는 **잘못된 가정을 코드와 함께
# 공유**했고 그래서 47/47이 통과했습니다.
# ══════════════════════════════════════════════════════════════
STATUS_OK = "OK"
STATUS_FILE_MISSING = "FILE_MISSING"
STATUS_NO_TARGET_DATE_DATA = "NO_TARGET_DATE_DATA"
STATUS_PARSE_ERROR = "PARSE_ERROR"


def _read_day_rows(path: Path | None, target: date) -> tuple[list[dict], str, str]:
    """(rows, status, error). 파일은 있는데 그날 행이 없으면
    NO_TARGET_DATE_DATA — "실제 0건"과 "자료 없음"은 다릅니다."""
    if path is None or not Path(path).exists():
        return [], STATUS_FILE_MISSING, ""
    try:
        with Path(path).open(newline="", encoding="utf-8-sig", errors="replace") as f:
            all_rows = list(csv.DictReader(f))
    except Exception as exc:
        return [], STATUS_PARSE_ERROR, str(exc)
    day = target.isoformat()
    rows = [r for r in all_rows
            if (r.get("timestamp") or "").startswith(day)]
    if not rows:
        return [], STATUS_NO_TARGET_DATE_DATA, ""
    return rows, STATUS_OK, ""


def collect_accepted_buy(shadow_rows: list[dict], trades_rows: list[dict]) -> dict:
    """accepted BUY의 source of truth (1J.5.2).

    `final_decision == "BUY"`는 **broker accepted 여부가 아닙니다**.
    `trading_service.py:2515` 주석이 명시합니다 —
    "final_decision=\"BUY\"라도 result.accepted가 False일 수 있음".

    1순위: entry_quality_shadow의 order_attempted AND order_accepted
           (order_id가 있으면 unique count)
    교차:  trades.csv의 side=BUY AND accepted=True
           (side=BUY만 세면 broker가 거절한 주문도 accepted로 셉니다)
    """
    def _dedupe(rows: list[dict]) -> tuple[list[dict], int, bool]:
        """order_id 기준 unique. (unique_rows, count, id_missing)

        2026-08-07 (1J.5.3, 재현 확인): 이전 조건
        `len(ids) == len(acc) and ids`는 **중복 order_id가 있을 때
        오히려 dedupe를 건너뛰었습니다**. 같은 O1이 2행이면
        accepted_count=2가 나왔음. 의도는 정반대입니다.

        order_id가 전부 있으면 order_id 기준 unique.
        하나라도 비어 있으면 조용히 row count로 fallback하지 않고
        ORDER_ID_MISSING을 표시합니다.
        """
        if not rows:
            return [], 0, False
        missing = any(not str(r.get("order_id") or "").strip() for r in rows)
        if missing:
            return rows, len(rows), True
        seen, uniq = set(), []
        for r in rows:
            oid = str(r.get("order_id") or "").strip()
            if oid in seen:
                continue
            seen.add(oid)
            uniq.append(r)
        return uniq, len(uniq), False

    acc = [r for r in shadow_rows
           if truthy(r.get("order_attempted")) and truthy(r.get("order_accepted"))]
    acc_uniq, shadow_n, shadow_id_missing = _dedupe(acc)

    tr_all = [r for r in trades_rows
              if str(r.get("side", "")).upper() in ("BUY", "매수")]
    tr_acc = [r for r in tr_all if truthy(r.get("accepted"))]
    _tr_uniq, trades_n, trades_id_missing = _dedupe(tr_acc)

    return {
        "accepted_count": shadow_n,
        "accepted_keys": [(r.get("symbol", ""),
                           (r.get("latest_bar_timestamp") or "").strip())
                          for r in acc_uniq],
        "shadow_order_id_missing": shadow_id_missing,
        "trades_buy_total": len(tr_all),
        "trades_accepted": trades_n,
        "trades_rejected": len(tr_all) - len(tr_acc),
        "trades_order_id_missing": trades_id_missing,
    }


ALIGN_OK = "ALIGNED"
ALIGN_NO_MINUTE_DATA = "NO_MINUTE_DATA"
ALIGN_BAR_NOT_FOUND = "BAR_NOT_FOUND"


def classify_candidate_fidelity(raw_live_cand: set, replay_cand: set,
                                by_key: dict,
                                alignment_status: dict | None = None) -> list[dict]:
    """미재현 후보마다 reason_code를 붙입니다 (1J.5.2).

    분모는 **raw signal_log의 candidate 전체**입니다. 1J.5.1은
    `aligned`에서 만들어서, minute data가 없어 정렬 자체가 안 된
    candidate가 분모에서 통째로 사라졌습니다 — 그러면 recall이
    실제보다 좋게 나옵니다.
    """
    rows = []
    for key in sorted(raw_live_cand):
        if key in replay_cand:
            code = "REPRODUCED"
        else:
            a = by_key.get(key)
            status = (alignment_status or {}).get(key)
            if a is None:
                # 2026-08-07 (1J.5.3): 이전엔 `a.get("_bar_not_found")`를
                # 봤는데 그 키를 **설정하는 코드가 어디에도 없어서**
                # BAR_NOT_FOUND가 사실상 죽어 있었습니다. 분봉 CSV는
                # 있는데 해당 timestamp 봉만 없는 경우까지 전부
                # NO_MINUTE_DATA로 오분류됐음. alignment 단계에서
                # 상태를 보존해 전달받습니다.
                code = (REASON_BAR_NOT_FOUND if status == ALIGN_BAR_NOT_FOUND
                        else REASON_NO_MINUTE_DATA)
            elif a["prev_close_source"] in ("UNAVAILABLE", "PREVIOUS_DATA_DAY_PARTIAL"):
                code = REASON_PREV_CLOSE_UNAVAILABLE
            elif not a["full_window"]:
                code = REASON_PARTIAL_HISTORY
            elif a.get("replay") is None:
                code = REASON_VALUE_MISMATCH
            else:
                code = REASON_UNKNOWN
        a = by_key.get(key)
        rows.append({
            "symbol": key[0], "timestamp": key[1],
            "live_patterns": (a["live"].get("detected_patterns") if a else "?"),
            "full_window": (a["full_window"] if a else None),
            "prev_close_source": (a["prev_close_source"] if a else "N/A"),
            "reason_code": code,
        })
    return rows


DATA_INELIGIBLE_CODES = (
    REASON_PREV_CLOSE_UNAVAILABLE, REASON_NO_MINUTE_DATA,
    REASON_BAR_NOT_FOUND, REASON_PARTIAL_HISTORY,
)


def calculate_recall(mismatch_rows: list[dict]) -> dict:
    """overall / eligible recall (1J.5.2)."""
    codes = Counter(m["reason_code"] for m in mismatch_rows)
    total = len(mismatch_rows)
    reproduced = codes.get("REPRODUCED", 0)
    ineligible = sum(codes.get(c, 0) for c in DATA_INELIGIBLE_CODES)
    unexplained = codes.get(REASON_UNKNOWN, 0) + codes.get(REASON_VALUE_MISMATCH, 0)
    eligible_total = total - ineligible
    return {
        "total": total, "reproduced": reproduced,
        "data_ineligible": ineligible, "unexplained": unexplained,
        "overall_recall": (reproduced / total) if total else None,
        "eligible_total": eligible_total,
        "eligible_recall": (reproduced / eligible_total) if eligible_total > 0 else None,
        "codes": dict(codes),
    }


def analyze(target: date, signal_log_path: Path,
            shadow_path: Path | None = None,
            trades_path: Path | None = None) -> str:
    L: list[str] = []

    def out(s: str = "") -> None:
        L.append(s)

    sig_rows = load_signal_rows(signal_log_path, target)
    by_symbol = index_by_symbol(sig_rows, target)
    day_dir = MINUTE_BARS_DIR / target.strftime("%Y%m%d")
    have_bars = {p.stem for p in day_dir.glob("*.csv")} if day_dir.exists() else set()

    analyzer = RR.try_import_analyzer()
    analyzer_mode = "LIVE_MINUTE_ANALYZER" if analyzer is not None else "SIMPLE_FALLBACK"
    mbc = RR.resolve_minute_bar_count()

    out("=" * 66)
    out(f"  🔬 Live ↔ Replay Fidelity  {target.isoformat()}")
    out("=" * 66)
    out(f"  analyzer_mode = {analyzer_mode}   minute_bar_count = {mbc}")
    out(f"  signal_log 행 {len(sig_rows):,} / 종목 {len(by_symbol)}")
    # 1J.5.2: 서로 다른 데이터셋을 교차검증하는 사고를 막기 위해
    # 실제 사용 경로를 항상 출력합니다.
    out(f"  입력 signal_log : {signal_log_path}")
    out(f"  입력 shadow     : {shadow_path or '(미지정)'}")
    out(f"  입력 trades     : {trades_path or '(미지정)'}")
    out(f"  분봉 보유 종목 {len(have_bars)}  "
        f"(교집합 {len(set(by_symbol) & have_bars)})")
    out("")
    out("  ※ 이 단계는 replay를 고치지 않고 '얼마나 재현되는가'만 측정합니다.")
    # 표본이 없으면 낮은 수치를 fidelity로 오해할 수 있으므로 먼저 경고
    thin = [s_ for s_ in have_bars if len(RR.load_bars(s_, target)) < mbc]
    if thin:
        out("")
        out(f"  ⚠ 분봉이 {mbc}봉 미만인 종목 {len(thin)}개 — "
            f"이 종목은 full-window 표본을 만들 수 없습니다.")
    out("")

    # ── 시점별 replay 재계산 ───────────────────────────────────
    aligned: list[dict] = []
    reasons = Counter()
    # 2026-08-07 (1J.5.3): (symbol, lbt) → ALIGNED / NO_MINUTE_DATA /
    # BAR_NOT_FOUND. classify에 넘겨 원인을 정확히 세분화합니다.
    alignment_status: dict[tuple, str] = {}
    stats = RR.ReplayQualityStats()

    for symbol, rows in sorted(by_symbol.items()):
        if symbol not in have_bars:
            for r in rows:
                reasons[REASON_NO_MINUTE_DATA] += 1
                alignment_status[(symbol, (r.get("latest_bar_timestamp") or "").strip())] \
                    = ALIGN_NO_MINUTE_DATA
            continue
        bars = RR.load_bars(symbol, target)
        if not bars:
            for r in rows:
                reasons[REASON_NO_MINUTE_DATA] += 1
                alignment_status[(symbol, (r.get("latest_bar_timestamp") or "").strip())] \
                    = ALIGN_NO_MINUTE_DATA
            continue
        built = build_day_context(symbol, bars, target, mbc,
                                  RR.load_bars, MINUTE_BARS_DIR, rows)
        ctx, pc = built.ctx, built.prev_close
        stats.history_status[built.history_status] = \
            stats.history_status.get(built.history_status, 0) + 1

        # 분봉 timestamp → 인덱스
        by_ts = {}
        for i in ctx.target_indices:
            dt = ctx.bar_dt(i)
            if dt is not None:
                by_ts[dt.strftime("%Y%m%d%H%M%S")] = i

        for r in rows:
            lbt = (r.get("latest_bar_timestamp") or "").strip()
            i = by_ts.get(lbt)
            if i is None:
                # 분봉 CSV는 있는데 이 timestamp 봉만 없는 경우
                reasons[REASON_BAR_NOT_FOUND] += 1
                alignment_status[(symbol, lbt)] = ALIGN_BAR_NOT_FOUND
                continue
            alignment_status[(symbol, lbt)] = ALIGN_OK
            fw = is_full_window(ctx, i)
            rec = {
                "symbol": symbol, "lbt": lbt, "live": r,
                "current_price": getattr(ctx.all_bars[i], "close_price", None),
                "full_window": fw,
                "prev_close_source": pc.source,
                "tier": ("A" if pc.source in TIER_A_SOURCES else
                         "B" if pc.source in TIER_B_SOURCES else "X"),
                "history_status": built.history_status,
                "replay": None,
            }
            if analyzer is not None and pc.available:
                try:
                    rec["replay"] = analyzer.analyze(ctx.analysis_window(i), pc.value)
                except Exception as exc:
                    rec["error"] = str(exc)
            aligned.append(rec)

    # ── A. Aligned Value Fidelity ──────────────────────────────
    out("─" * 66)
    out("  [ A. Aligned Value Fidelity ]")
    out("─" * 66)

    total = len(aligned) + sum(reasons.values())
    out(f"  live 평가 시점 {total:,}건 중 replay 정렬 성공 {len(aligned):,}건")
    for k, v in reasons.most_common():
        out(f"    제외 {k:32s} {v:6,}건")

    primary = [a for a in aligned
               if a["full_window"] and a["tier"] == "A"
               and analyzer_mode == "LIVE_MINUTE_ANALYZER"
               and a.get("replay") is not None and "error" not in a]
    secondary = [a for a in aligned if a not in primary]

    out("")
    out(f"  Primary Set   {len(primary):,}건  "
        f"(full_window + TierA source + LIVE analyzer + error 0)")
    out(f"  Secondary Set {len(secondary):,}건")

    # 가드레일 2 — full-window 비율을 대상별로 분리
    def fw_pct(items) -> str:
        if not items:
            return "n/a"
        n = sum(1 for a in items if a["full_window"])
        return f"{n}/{len(items)} ({n / len(items) * 100:.1f}%)"

    buy_rows = [a for a in aligned if truthy(a["live"].get("legacy_buy_candidate"))]
    # 2026-08-07 (1J.5.1): `signal == "BUY"`는 전략이 BUY를 반환한
    # **legacy candidate**이지 실제 체결이 아닙니다. 8/7 기준
    # signal=BUY 250행 / final_decision=BUY 3행 / shadow accepted 3건 /
    # trades BUY 3건 — 셋이 일치합니다. 250/250은 명백한 오집계였음.
    # 1J.5.2: final_decision은 broker accepted가 아니므로 참고용.
    buy_decision = [a for a in aligned
                    if str(a["live"].get("final_decision", "")).upper() == "BUY"]
    # 2026-08-07 (1J.5.1): 세 소스 교차 검증. final_decision만 믿지 않고
    # entry_quality_shadow(order_attempted AND order_accepted)와
    # trades.csv(side=BUY)를 함께 확인해, 어느 하나가 어긋나면 리포트에
    # 드러나게 합니다. 8/7 실측은 셋 다 3건으로 일치했습니다.
    sh_rows, sh_status, sh_err = _read_day_rows(shadow_path, target)
    tr_rows, tr_status, tr_err = _read_day_rows(trades_path, target)
    accepted = collect_accepted_buy(sh_rows, tr_rows)
    out("")
    out("  full-window 비율 (대상별 — evaluation-point coverage와 다릅니다)")
    out(f"    aligned_live_rows_full_window_pct      {fw_pct(aligned)}")
    out(f"    legacy_buy_candidate_full_window_pct   {fw_pct(buy_rows)}")
    out(f"    buy_decision_full_window_pct           {fw_pct(buy_decision)}"
        f"   (final_decision=BUY — accepted 아님)")

    out("")
    out("  prev_close source 분포")
    for src, n in Counter(a["prev_close_source"] for a in aligned).most_common():
        tier = ("Tier A" if src in TIER_A_SOURCES else
                "Tier B" if src in TIER_B_SOURCES else "제외")
        out(f"    {src:28s} {n:6,}건  [{tier}]")

    # 값 비교
    if primary:
        out("")
        out(f"  {'항목':18s} {'n':>6s} {'중앙오차':>9s} {'p90':>9s}"
            f" {'≤0.1':>7s} {'≤0.25':>7s} {'≤0.5':>7s}")
        for col, tol, label in NUMERIC_FIELDS:
            pairs = []
            for a in primary:
                lv = fnum(a["live"].get(col))
                rv = _replay_value(a["replay"], col, a)
                if lv is None or rv is None:
                    continue
                # 가격은 절대값, 나머지는 %p 단위이므로 그대로 비교
                d = abs(lv - rv) / abs(lv) * 100 if col == "price" and lv else abs(lv - rv)
                pairs.append(d)
            if not pairs:
                out(f"  {label:18s} {'n/a':>6s}")
                continue
            pairs.sort()
            n = len(pairs)
            med = pairs[n // 2]
            p90 = pairs[int(n * 0.9)]
            w = lambda t: f"{sum(1 for d in pairs if d <= t) / n * 100:6.1f}%"
            out(f"  {label:18s} {n:6d} {med:9.4f} {p90:9.4f}"
                f" {w(0.1)} {w(0.25)} {w(0.5)}")
        out("  ※ 단위: 가격은 %, 나머지는 %p. live는 결정 시점의 실시간 체결가를,")
        out("    replay는 분봉 종가를 씁니다 — 형성 중인 봉에서는 구조적으로 다릅니다")
        out(f"    ({REASON_LIVE_TICK_VS_BAR_CLOSE}).")
        out("")
        for col, label in BOOL_FIELDS:
            pairs = [(truthy(a["live"].get(col)), bool(getattr(a["replay"], col, False)))
                     for a in primary if a["replay"] is not None]
            if not pairs:
                continue
            ok = sum(1 for l, r in pairs if l == r)
            out(f"  {label:16s} {ok:5d}/{len(pairs):<5d} {ok / len(pairs) * 100:7.1f}%")

        # 패턴 문자열
        pat_ok = 0
        pat_n = 0
        for a in primary:
            live_p = (a["live"].get("detected_patterns") or "-").strip()
            rp = a["replay"]
            flags = []
            if getattr(rp, "is_valid_change_rate", False):
                flags.append("A")
            if getattr(rp, "is_valid_rebound", False):
                flags.append("B")
            if getattr(rp, "is_valid_pulldown", False):
                flags.append("C")
            if getattr(rp, "is_v_rebound", False):
                flags.append("V")
            if getattr(rp, "is_pulldown_recovery", False):
                flags.append("PR")
            replay_p = "/".join(flags) if flags else "-"
            pat_n += 1
            if set(live_p.split("/")) == set(replay_p.split("/")):
                pat_ok += 1
        if pat_n:
            out(f"  {'패턴 조합':16s} {pat_ok:5d}/{pat_n:<5d} {pat_ok / pat_n * 100:7.1f}%")
    else:
        out("")
        out("  ⚠ Primary Set이 비어 있어 값 비교를 수행할 수 없습니다.")

    # ── A-2. Daily Indicator (MACD) Fidelity ───────────────────
    out("")
    out("─" * 66)
    out("  [ A-2. Daily Indicator (MACD) Fidelity ]")
    out("─" * 66)
    cand_rows = [a for a in aligned if truthy(a["live"].get("legacy_buy_candidate"))]
    out("  MACD field coverage (1K Primary가 ground truth로 사용할 값)")
    for col in ("macd", "macd_signal", "macd_above_signal", "macd_hist_direction", "score"):
        n_all = sum(1 for a in aligned if str(a["live"].get(col) or "").strip())
        n_cand = sum(1 for a in cand_rows if str(a["live"].get(col) or "").strip())
        out(f"    {col:22s} 전체 {n_all:5d}/{len(aligned):<5d}"
            f"   candidate {n_cand:4d}/{len(cand_rows):<4d}")
    daily_dir = Path("data/daily_bars")
    if daily_dir.exists() and any(daily_dir.iterdir()):
        out("  일봉 데이터 확인됨 — MACD 재계산 비교 가능")
    else:
        out("  ⚠ 일봉 데이터가 저장되지 않습니다 (data/에는 minute_bars만 존재).")
        out("")
        out("  live의 MACD는 MinuteAnalyzer가 아니라 TradingService가")
        out("  `cached_daily_bars`(일봉 API 응답)로 계산합니다:")
        out("    trading_service.py:1050  closes = [bar.close_price for bar in bars]")
        out("    trading_service.py:1056  regime_classifier._calc_macd(closes, ...)")
        out("  이 일봉은 메모리 캐시일 뿐 파일로 저장되지 않으므로,")
        out("  과거 시점의 MACD를 재현할 원본 자료가 없습니다.")
        out("")
        out(f"  → reason_code: {REASON_MACD_NO_DAILY_DATA}")
        out("")
        out("  ※ 1J.5.2 정책 정정 — 1K에서 MACD를 완전히 제외하지 않습니다.")
        out("    replay로 MACD를 **재계산**할 수 없다는 한계는 그대로지만,")
        out("    1K Primary는 Live-Aligned Episode이므로 live가 실제로 쓴 값이")
        out("    signal_log에 이미 저장돼 있어 재계산이 필요 없습니다.")
        out("")
        out("    Primary (Live-Aligned)   → MACD 평가 **가능**")
        out("      A) hard gate      : macd_above_signal == False → block")
        out("      B) dead + score5  : macd_above_signal == False AND score < 5 → block")
        out("    Secondary (Replay Discovery) → MACD 평가 **불가** (일봉 없음)")
        out("")
        out("    coverage가 없는 표본은 N/A로 처리하십시오.")

    # ── B. Candidate Discovery Fidelity ────────────────────────
    out("")
    out("─" * 66)
    out("  [ B. Candidate Discovery Fidelity ]")
    out("─" * 66)
    # 1J.5.2: 분모는 **raw signal_log의 candidate 전체**.
    # aligned에서 만들면 minute data가 없어 정렬 못 한 candidate가
    # 분모에서 사라져 recall이 실제보다 좋게 나옵니다.
    live_cand = {(r.get("symbol", ""), (r.get("latest_bar_timestamp") or "").strip())
                 for r in sig_rows if truthy(r.get("legacy_buy_candidate"))}
    live_cand = {k for k in live_cand if k[1]}
    replay_cand = set()
    for symbol in sorted(set(by_symbol) & have_bars):
        bars = RR.load_bars(symbol, target)
        if not bars:
            continue
        res = RR.run_replay(symbol, bars, analyzer, target,
                            minute_bar_count=mbc, quality_stats=stats,
                            signal_rows=by_symbol.get(symbol))
        for r in res:
            # entry_time 포맷이 live의 latest_bar_timestamp와 다를 수
            # 있으므로 14자리로 정규화해 비교합니다.
            et = str(r.get("entry_time", ""))
            digits = "".join(ch for ch in et if ch.isdigit())
            if len(digits) == 14:
                replay_cand.add((symbol, digits))
            elif len(digits) >= 6:
                replay_cand.add((symbol, target.strftime("%Y%m%d") + digits[-6:]))

    inter = live_cand & replay_cand
    by_key = {(a["symbol"], a["lbt"]): a for a in aligned}
    by_key_all = by_key
    mismatch_rows = classify_candidate_fidelity(live_cand, replay_cand,
                                                by_key, alignment_status)
    rec = calculate_recall(mismatch_rows)
    codes = Counter(m["reason_code"] for m in mismatch_rows)

    out(f"  Live unique candidates      {rec['total']:5d}  (raw signal_log 기준)")
    out(f"  Replay reproduced           {rec['reproduced']:5d}")
    out(f"  Data-ineligible             {rec['data_ineligible']:5d}")
    out(f"  Unexplained mismatch        {rec['unexplained']:5d}")
    out("")
    if rec["overall_recall"] is not None:
        out(f"  overall recall              {rec['reproduced']}/{rec['total']} "
            f"({rec['overall_recall'] * 100:.1f}%)")
    if rec["eligible_recall"] is not None:
        out(f"  eligible recall             {rec['reproduced']}/{rec['eligible_total']} "
            f"({rec['eligible_recall'] * 100:.1f}%)   ← 핵심 지표")
    if replay_cand:
        out(f"  precision (replay→live)     {len(inter)}/{len(replay_cand)} "
            f"({len(inter) / len(replay_cand) * 100:.1f}%)")
    out("")
    out("  미재현 reason_code 분포")
    for code in (REASON_PREV_CLOSE_UNAVAILABLE, REASON_NO_MINUTE_DATA,
                 REASON_BAR_NOT_FOUND, REASON_PARTIAL_HISTORY,
                 REASON_VALUE_MISMATCH, REASON_UNKNOWN):
        out(f"    {code:32s} {codes.get(code, 0):5d}")
    if rec["unexplained"]:
        out("    ⚠ UNKNOWN이 0이 아닙니다 — replay 결함 가능성을 조사하십시오.")
    miss_only = [m for m in mismatch_rows if m["reason_code"] != "REPRODUCED"]
    if miss_only:
        out("")
        out("  미재현 상세 (상위 8건)")
        for m in miss_only[:8]:
            out(f"    {m['symbol']} {m['timestamp']}  full_window={m['full_window']}"
                f"  {m['prev_close_source']:26s} {m['reason_code']}")
        bysym = Counter(m["symbol"] for m in miss_only)
        out(f"  종목별: {dict(bysym.most_common(5))}")

    # 실제 체결된 BUY 3건의 재현 여부
    out("")
    out("  [ 실제 accepted BUY 재현 ]")
    # 1J.5.2: accepted BUY는 shadow(order_attempted AND order_accepted)
    # 기준. shadow 자료가 없으면 평가하지 않습니다(0건이라 단정 금지).
    acc_keys = [k for k in accepted["accepted_keys"] if k[1]]
    actual_buy = [by_key_all[k] for k in acc_keys if k in by_key_all]
    acc_repro = [k for k in acc_keys if k in replay_cand]
    acc_ineli = [k for k in acc_keys
                 if k not in replay_cand
                 and by_key.get(k) is not None
                 and by_key[k]["prev_close_source"] in ("UNAVAILABLE",
                                                        "PREVIOUS_DATA_DAY_PARTIAL")]
    acc_unexp = [k for k in acc_keys if k not in replay_cand and k not in acc_ineli]
    out(f"    Accepted BUY (shadow 기준)   {accepted['accepted_count']}")
    out(f"      source: entry_quality_shadow order_attempted AND order_accepted [{sh_status}]"
        + (f" — {sh_err}" if sh_err else ""))
    out(f"    교차검증 trades accepted=True {accepted['trades_accepted']}"
        f"  (side=BUY 전체 {accepted['trades_buy_total']}, "
        f"거절 {accepted['trades_rejected']}) [{tr_status}]"
        + (f" — {tr_err}" if tr_err else ""))
    out(f"    참고 BUY_DECISION(final_decision=BUY) {len(buy_decision)}"
        f"  ← broker accepted 여부가 아님")
    if sh_status == STATUS_OK and accepted["accepted_count"] != accepted["trades_accepted"]:
        out(f"      ⚠ shadow({accepted['accepted_count']})와 "
            f"trades({accepted['trades_accepted']})가 다릅니다.")
    if accepted.get("shadow_order_id_missing"):
        out("      ⚠ ORDER_ID_MISSING — accepted인데 order_id가 없는 행이 있어")
        out("        order_id 기준 dedupe를 적용하지 못했습니다(행 수로 집계).")
    if accepted.get("trades_order_id_missing"):
        out("      ⚠ ORDER_ID_MISSING (trades) — 위와 동일")
    if sh_status != STATUS_OK:
        out(f"      ⚠ shadow 자료 상태가 {sh_status}입니다 — "
            f"0건이라고 단정할 수 없습니다.")
    out(f"    Full-window                  {sum(1 for a in actual_buy if a['full_window'])}"
        f"/{len(acc_keys)}")
    out(f"    Replay candidate reproduced  {len(acc_repro)}/{len(acc_keys)}")
    out(f"    Data-ineligible(prev_close)  {len(acc_ineli)}/{len(acc_keys)}")
    out(f"    Unexplained mismatch         {len(acc_unexp)}/{len(acc_keys)}")
    for k in acc_keys:
        mark = ("재현" if k in replay_cand else
                "평가불가(prev_close)" if k in acc_ineli else "미설명")
        out(f"      {k[0]} {k[1]}  → {mark}")

    out("")
    out("  ※ replay가 재현하지 않는 것 (낮은 수치를 replay 결함으로 보면 안 됨)")
    for lim in ("조건검색 universe 미재현", "거래대금 필터 미재현(min_trading_value=0)",
                "cooldown 미재현", "보유 상태 미재현", "daily risk limit 미재현",
                "acc_volume이 파일 volume 누적이라 live와 다를 수 있음"):
        out(f"    - {lim}")

    out("")
    out("=" * 66)
    return "\n".join(L)


def _replay_value(rp, col: str, rec: dict):
    """live 컬럼명 → replay analysis 속성 매핑."""
    if rp is None:
        return None
    # 2026-08-07 (1J.5): MinuteAnalysis 실제 속성명에 맞춘 매핑.
    # price/VWAP거리/MACD는 analysis가 직접 들고 있지 않으므로
    # 파생 계산하거나 비교 대상에서 제외합니다.
    mapping = {
        "change_rate_pct": "change_rate_pct",
        "upside_to_recent_high_pct": "upside_to_recent_high_pct",
        "score": "score",
    }
    if col == "current_vs_vwap_pct":
        # live의 current_vs_vwap_pct = (현재가 - VWAP) / VWAP * 100
        vwap = getattr(rp, "vwap", None)
        cur = rec.get("current_price")
        if vwap and cur:
            return (cur - vwap) / vwap * 100
        return None
    if col == "price":
        return rec.get("current_price")
    if col in ("macd", "macd_signal"):
        # MinuteAnalyzer는 MACD를 계산하지 않습니다(별도 indicator 경로).
        # replay가 재현하지 않는 항목이므로 비교 대상에서 제외.
        return None
    attr = mapping.get(col, col)
    v = getattr(rp, attr, None)
    if v is None:
        for alt in (col, attr.replace("_pct", ""), attr + "_pct"):
            v = getattr(rp, alt, None)
            if v is not None:
                break
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _force_utf8_stdout() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_stdout()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sl = DEFAULT_SIGNAL_LOG
    sh = tr = None
    for i, a in enumerate(sys.argv):
        if a == "--signal-log" and i + 1 < len(sys.argv):
            sl = Path(sys.argv[i + 1])
        if a == "--entry-quality-shadow" and i + 1 < len(sys.argv):
            sh = Path(sys.argv[i + 1])
        if a == "--trades" and i + 1 < len(sys.argv):
            tr = Path(sys.argv[i + 1])
    if not args:
        print("사용법: python analyze_replay_fidelity.py YYYY-MM-DD "
              "[--signal-log PATH] [--entry-quality-shadow PATH] [--trades PATH]")
        return 1
    target = datetime.strptime(args[0], "%Y-%m-%d").date()

    # 1J.5.2: 미지정이면 signal-log와 같은 폴더의 sibling을 자동 탐색.
    # 서로 다른 데이터셋을 섞지 않기 위함이며, 실제 경로는 리포트에 출력.
    # 2026-08-07 (1J.5.3): sorted 첫 파일을 잡으면 같은 폴더에 여러
    # 날짜 CSV가 있을 때 다른 날짜를 고릅니다. target 날짜가 파일명에
    # 들어간 것을 최우선으로, 없으면 target-date row가 실제 있는 파일을
    # 찾고, 후보가 둘 이상이면 CLI 명시를 요구합니다.
    def _pick_sibling(pattern: str, explicit):
        if explicit is not None:
            return explicit
        if not sl.parent.exists():
            return None
        cands = sorted(sl.parent.glob(pattern))
        if not cands:
            return None
        stamp = target.strftime("%Y%m%d")
        exact = [c for c in cands if stamp in c.name]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            print(f"⚠ {pattern}: 날짜 {stamp} 후보가 여러 개입니다 {[c.name for c in exact]}"
                  f" — CLI로 명시하십시오.")
            return None
        with_data = []
        for c in cands:
            rows_, st_, _ = _read_day_rows(c, target)
            if st_ == STATUS_OK:
                with_data.append(c)
        if len(with_data) == 1:
            return with_data[0]
        if len(with_data) > 1:
            print(f"⚠ {pattern}: target-date 데이터를 가진 파일이 여러 개입니다 "
                  f"{[c.name for c in with_data]} — CLI로 명시하십시오.")
        return None

    sh = _pick_sibling("entry_quality_shadow*.csv", sh)
    tr = _pick_sibling("trades*.csv", tr)

    report = analyze(target, sl, sh, tr)
    print(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORTS_DIR / f"replay_fidelity_{target.strftime('%Y%m%d')}.txt"
    p.write_text(report, encoding="utf-8")
    print(f"\n  → 저장: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
