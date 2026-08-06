#!/usr/bin/env python3
"""shadow 관측 데이터 품질·표본·게이트 집계 리포트 (2026-08-06, 1H단계)

실행:
    python analyze_shadow.py                        # 오늘 날짜
    python analyze_shadow.py 2026-08-05             # 특정 날짜
    python analyze_shadow.py 2026-08-03 2026-08-07  # 날짜 범위

결과: 콘솔 출력 + reports/shadow_analysis_YYYYMMDD.txt 저장

무엇을 하는 스크립트인가
------------------------
1E(MACD)·1E.5~1E.7(VWAP) shadow 관측이 쌓는 데이터를 해석합니다.
지금까지 이 데이터를 읽는 코드는 전혀 없었고(analyze_signal_log.py
등 기존 리포트 6종은 shadow 필드를 참조하지 않음), 매번 즉석
스크립트로 확인해야 했습니다.

**이 스크립트는 읽기 전용입니다.** 매매 판단이나 설정을 일절
바꾸지 않고, 이미 기록된 CSV만 읽어 요약합니다.

이번 단계(1H)에서 다루는 범위 — 1~4번:
    1) 스키마·품질 점검 (결측·중복·재시작 중복·timestamp 정상성)
    2) 표본 규모 (행 수가 아니라 유니크 기준)
    3) 게이트별 차단 건수 (기존 규칙으로 이미 막힌 후보는 분리)
    4) VWAP 8조합 비교 (PR-only / C-or-PR / condition-source
       × rolling / session)

의도적으로 **빠진 것 — 성과 계산(5·10·20분 수익률, MFE·MAE)**:
entry_quality_shadow.csv에는 판단 시점 current_price만 있고 이후
가격이 없어, 분봉 리플레이 CSV와 조인해야 합니다. 감시 대상에서
빠진 종목은 분봉이 수집되지 않았을 수 있어 "산출 불가" 처리가
필요한데, 실제 데이터가 어떻게 쌓이는지 먼저 확인한 뒤 붙이는
편이 정확합니다. 표본 100건도 안 되는 상태에서 성과 수치를 뽑으면
오히려 오해를 부르므로 첫 며칠은 품질 확인에 집중합니다.

통계 함정 주의
--------------
signal_log.csv는 폴링마다 기록되므로 **같은 종목·같은 분봉이 수십
번 반복**됩니다. 8/5 실측으로 legacy BUY 후보 777행이 (종목, 분봉)
유니크로는 212건이었습니다. 과거 "리플레이 5228건"을 독립 거래로
오해했던 것과 같은 함정이므로, 이 리포트는 **행 수와 유니크 수를
항상 나란히** 보여줍니다. 판단은 유니크 기준으로 하십시오.
"""
from __future__ import annotations

import csv
import io
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain.shadow_signature import analysis_dedup_key  # noqa: E402


SIGNAL_LOG = Path("logs/signal_log.csv")
SHADOW_LOG = Path("logs/entry_quality_shadow.csv")
REPORTS_DIR = Path("reports")

# 1E단계 MACD shadow 필드 (signal_log.csv)
MACD_GATES = [
    ("would_block_existing_chasing_gate", "기존 chasing_overheated 게이트"),
    ("would_block_macd_dead_min_score5", "MACD 데드 + 최소5점 (기존 확장)"),
    ("would_block_macd_above_signal_required", "MACD>Signal 하드 게이트"),
]

# 1E.5~1E.7단계 VWAP shadow 8조합 (entry_quality_shadow.csv)
VWAP_GATES = [
    ("would_block_pr_only_rolling_vwap", "PR-only", "rolling(60분)"),
    ("would_block_c_or_pr_rolling_vwap", "C-or-PR", "rolling(60분)"),
    ("would_block_pullback_condition_rolling_vwap", "condition-source", "rolling(60분)"),
    ("would_block_pr_or_pullback_condition_rolling_vwap", "PR-or-condition", "rolling(60분)"),
    ("would_block_pr_only_session_vwap", "PR-only", "session(당일)"),
    ("would_block_c_or_pr_session_vwap", "C-or-PR", "session(당일)"),
    ("would_block_pullback_condition_session_vwap", "condition-source", "session(당일)"),
    ("would_block_pr_or_pullback_condition_session_vwap", "PR-or-condition", "session(당일)"),
]


# ── 유틸 ────────────────────────────────────────────────────────
def truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


def filled(v) -> bool:
    return str(v or "").strip() != ""


def pct(n: int, total: int) -> str:
    return f"{n / total * 100:5.1f}%" if total else "  n/a"


def load_rows(path: Path, start: date, end: date) -> tuple[list[dict], list[str]]:
    """날짜 범위에 해당하는 행만 스트리밍으로 읽습니다.

    signal_log.csv는 수천만 바이트까지 커지므로 전체를 메모리에
    올리지 않고 timestamp 접두사로 걸러냅니다.
    """
    if not path.exists():
        return [], []
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        for row in reader:
            ts = (row.get("timestamp") or "")[:10]
            if not ts:
                continue
            try:
                d = datetime.strptime(ts, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= d <= end:
                rows.append(row)
    return rows, header


def uniq_key(row: dict) -> tuple[str, str]:
    """중복집계 함정 회피용 키 — 같은 종목·같은 분봉은 1건으로 셉니다."""
    return (row.get("symbol", ""), row.get("latest_bar_timestamp", ""))


# ── 1) 스키마·품질 점검 ─────────────────────────────────────────
def section_data_quality_header(out, sig_rows, sh_rows) -> None:
    """리포트 최상단 데이터 품질 요약 (1I.1)."""
    if not sh_rows:
        out("[ 데이터 품질 ] shadow 기록 없음 — 아래 게이트 집계는 해석 불가")
        out("")
        return
    complete = sum(1 for r in sh_rows if truthy(r.get("session_metrics_ready")))
    reliable = sum(1 for r in sh_rows if truthy(r.get("condition_source_reliable")))
    attempts = sum(1 for r in sh_rows if truthy(r.get("order_attempted")))
    accepted = sum(1 for r in sh_rows if truthy(r.get("order_accepted")))
    sh_first = next((str(r.get("timestamp") or "") for r in sh_rows), "")
    # 2026-08-06 (1I.2, GPT 지적 P1-1): 이 분석기에는 재시작 로그가
    # 없고, entry_quality_shadow는 legacy BUY 후보가 있을 때만
    # 기록되므로 첫 기록 시각만으로 "PARTIAL/RESTARTED"를 단정할 수
    # 없음. 관측 시작 시각만 사실대로 적고 판정은 넘기지 않는다.
    out("[ 데이터 품질 ]")
    out(f"  shadow 후보 관측 시작: {sh_first[11:19] or 'N/A'}")
    out("  전체 수집 상태       : metadata/collection_quality.txt 참고")
    out(f"  shadow 주문 연결     : 시도 {attempts}건 / 수락 {accepted}건")
    out(f"  session ready        : {complete}/{len(sh_rows)}")
    out(f"  조건식 출처 신뢰     : {reliable}/{len(sh_rows)}")
    interp_ok = ["rolling VWAP", "MACD"]
    interp_hold = []
    if complete == 0:
        interp_hold.append("session VWAP")
    if reliable < len(sh_rows) * 0.2:
        interp_hold.append("condition-source 기반 VWAP")
    out(f"  해석 가능            : {', '.join(interp_ok)}")
    out(f"  해석 보류            : {', '.join(interp_hold) if interp_hold else '없음'}")
    out("  ※ 수집 완전성·재시작 횟수는 signal_log 기준으로 collection_quality.txt에 기록됩니다.")
    out("")


def section_quality(out, sig_rows, sig_header, sh_rows, sh_header) -> None:
    out("[ 1. 스키마 · 데이터 품질 ]")

    if not sig_rows:
        out("  ⚠ signal_log.csv에 해당 기간 데이터가 없습니다.")
    else:
        out(f"  signal_log.csv            : {len(sig_rows):,}행 / 컬럼 {len(sig_header)}개")
        # shadow 필드가 실제로 채워지고 있는지 — 켜졌는지 확인하는 가장 정확한 방법
        macd_filled = sum(1 for r in sig_rows if filled(r.get("macd")))
        vwap_filled = sum(1 for r in sig_rows if filled(r.get("rolling_vwap")))
        sess_filled = sum(1 for r in sig_rows if filled(r.get("session_metrics_ready")))
        out(f"    macd 채워진 행           : {macd_filled:,} ({pct(macd_filled, len(sig_rows))})")
        out(f"    rolling_vwap 채워진 행   : {vwap_filled:,} ({pct(vwap_filled, len(sig_rows))})")
        out(f"    session_metrics 채워진 행: {sess_filled:,} ({pct(sess_filled, len(sig_rows))})")
        if macd_filled == 0:
            out("    ⚠ MACD shadow 미수집 — 1E단계 코드가 반영되지 않았을 수 있습니다.")
        if vwap_filled == 0:
            out("    ⚠ VWAP shadow 미수집 — entry_quality_guard_mode가 \"off\"일 가능성이 높습니다.")
            out("      (settings.yaml의 experimental.entry_quality_guard_mode 확인)")

        # timestamp 정상성 — KST 기준 정규장 밖 기록이 섞였는지
        outside = 0
        for r in sig_rows:
            ts = r.get("timestamp") or ""
            if len(ts) >= 16:
                hhmm = ts[11:16]
                if hhmm < "08:30" or hhmm > "15:40":
                    outside += 1
        out(f"    정규장(08:30~15:40) 밖   : {outside:,}행" + (" ⚠" if outside else ""))

    out("")
    if not SHADOW_LOG.exists():
        out("  ⚠ logs/entry_quality_shadow.csv 파일이 없습니다.")
        return
    out(f"  entry_quality_shadow.csv  : {len(sh_rows):,}행 / 컬럼 {len(sh_header)}개")
    if not sh_rows:
        out("    ℹ 행이 없습니다. 다만 이 파일은 legacy_buy_candidate=True인 경우에만")
        out("      기록하므로, shadow가 정상 동작해도 BUY 후보가 없었으면 비어 있습니다.")
        out("      shadow 활성 여부는 위 signal_log의 rolling_vwap 채움률로 판단하십시오.")
        return

    # 2026-08-06 (1I.1, GPT 코드리뷰 지적 4번): 이전엔 (종목, 분봉)
    # 만으로 중복을 판정해서, 같은 분봉이라도 게이트 상태가 바뀐
    # **정상적인 별도 행**(would_block False→True, order_accepted
    # False→True, final_decision BLOCKED→BUY 등)까지 "재시작 중복
    # 가능" 경고로 잡았음. 이제 로거와 동일한 assessment signature
    # (domain/shadow_signature.py 공용 함수)로 판정하고, 표본 규모
    # 참고용 (종목,분봉) 유니크와 실제 중복을 분리해서 표시.
    bar_keys = [uniq_key(r) for r in sh_rows]
    sig_keys = [analysis_dedup_key(r) for r in sh_rows]
    real_dup = len(sig_keys) - len(set(sig_keys))
    out(f"    (종목, 분봉) 유니크       : {len(set(bar_keys)):,}건  (표본 규모 참고용)")
    out(f"    판단 signature 유니크     : {len(set(sig_keys)):,}건")
    out(f"    완전 동일 중복 행         : {real_dup:,}행"
        + (" ⚠ 실제 중복 또는 재시작 중복 가능" if real_dup else ""))
    if len(set(bar_keys)) < len(set(sig_keys)):
        out(f"    ℹ 같은 분봉에서 게이트 상태가 바뀐 행이 있습니다 — 정상입니다.")

    # 결측 — 분석에 반드시 필요한 필드
    critical = ["current_price", "final_decision", "macd_above_signal",
                "session_metrics_ready", "condition_source_reliable"]
    missing = {c: sum(1 for r in sh_rows if not filled(r.get(c))) for c in critical}
    holes = {k: v for k, v in missing.items() if v}
    if holes:
        for k, v in holes.items():
            out(f"    결측 {k:26s}: {v:,}행 ({pct(v, len(sh_rows))})")
    else:
        out("    필수 필드 결측            : 없음")


# ── 2) 표본 규모 ────────────────────────────────────────────────
def section_sample(out, sig_rows, sh_rows) -> None:
    out("")
    out("[ 2. 표본 규모 (행 수가 아니라 유니크 기준으로 판단) ]")

    buy = [r for r in sig_rows if truthy(r.get("legacy_buy_candidate"))]
    buy_uniq = {uniq_key(r) for r in buy}
    symbols = {r.get("symbol") for r in buy}
    out(f"  legacy BUY 후보           : {len(buy):,}행 → 유니크 {len(buy_uniq):,}건 / 종목 {len(symbols)}개")
    if buy:
        ratio = len(buy) / max(len(buy_uniq), 1)
        out(f"    같은 (종목,분봉) 평균 반복: {ratio:.1f}회"
            + ("  ← 행 수로 판단하면 과대집계" if ratio > 1.5 else ""))

    if sh_rows:
        sh_uniq = {uniq_key(r) for r in sh_rows}
        attempted = [r for r in sh_rows if truthy(r.get("order_attempted"))]
        accepted = [r for r in sh_rows if truthy(r.get("order_accepted"))]
        unblocked = [r for r in sh_rows if not filled(r.get("order_block_reason"))]
        out(f"  shadow 기록 후보          : {len(sh_rows):,}행 → 유니크 {len(sh_uniq):,}건")
        out(f"    기존 규칙 통과(미차단)   : {len(unblocked):,}행")
        out(f"    실제 주문 시도            : {len(attempted):,}건 / 체결 수락 {len(accepted):,}건")

        # 종목·시간대 집중도 — 표본이 한쪽에 쏠렸는지
        by_sym = Counter(r.get("symbol") for r in sh_rows)
        if by_sym:
            top = by_sym.most_common(5)
            share = top[0][1] / len(sh_rows)
            out(f"    상위 종목 집중도          : "
                + ", ".join(f"{s}({n})" for s, n in top))
            if share > 0.5:
                out(f"      ⚠ 1개 종목이 전체의 {share*100:.0f}% — 표본 편향 주의")
        by_hour = Counter((r.get("timestamp") or "")[11:13] for r in sh_rows)
        if by_hour:
            out("    시간대 분포               : "
                + " ".join(f"{h}시:{n}" for h, n in sorted(by_hour.items()) if h))

    out("")
    out("  판단 기준(GPT 권고): 유니크 BUY 후보 100건 이상 / 실제 진입 20건 이상 /")
    out("  서로 다른 종목 10개 이상 확보 전에는 enforce 판단을 내리지 마십시오.")


# ── 3) MACD 게이트 집계 ─────────────────────────────────────────
def section_macd(out, sig_rows) -> None:
    out("")
    out("[ 3. MACD 게이트 집계 (signal_log.csv 기준) ]")

    buy = [r for r in sig_rows if truthy(r.get("legacy_buy_candidate"))]
    if not buy:
        out("  legacy BUY 후보가 없어 집계할 수 없습니다.")
        return

    # 기존 규칙으로 이미 차단된 후보는 분리 — 이걸 섞으면 "새 게이트가
    # 막았을 건수"가 부풀려짐(어차피 못 사던 후보이므로).
    live = [r for r in buy if not filled(r.get("order_block_reason"))]
    buy_u = {uniq_key(r) for r in buy}
    live_u = {uniq_key(r) for r in live}
    out(f"  분모: legacy BUY 후보 유니크 {len(buy_u):,}건")
    out(f"        그중 기존 규칙 통과     {len(live_u):,}건  ← 새 게이트 평가는 이쪽 기준")
    out("")
    out(f"  {'게이트':38s} {'전체':>16s} {'기존규칙 통과분':>18s}")
    for field, label in MACD_GATES:
        hit_u = {uniq_key(r) for r in buy if truthy(r.get(field))}
        live_hit_u = {uniq_key(r) for r in live if truthy(r.get(field))}
        out(f"  {label:38s} {len(hit_u):6,}건 {pct(len(hit_u), len(buy_u))} "
            f"{len(live_hit_u):8,}건 {pct(len(live_hit_u), len(live_u))}")

    # chasing_overheated 조건 자체는 BUY/HOLD 무관하게 계산됨(1E.4)
    cond = {uniq_key(r) for r in sig_rows if truthy(r.get("chasing_overheated_condition"))}
    appl = {uniq_key(r) for r in sig_rows if truthy(r.get("chasing_overheated_applies"))}
    out("")
    out(f"  참고(BUY/HOLD 전체): chasing_overheated 조건 성립 {len(cond):,}건 / "
        f"적용 대상 {len(appl):,}건")


# ── 4) VWAP 8조합 ───────────────────────────────────────────────
def section_vwap(out, sh_rows) -> None:
    out("")
    out("[ 4. VWAP 게이트 8조합 (entry_quality_shadow.csv 기준) ]")

    if not sh_rows:
        out("  기록된 후보가 없어 집계할 수 없습니다.")
        return

    live = [r for r in sh_rows if not filled(r.get("order_block_reason"))]
    live_u = {uniq_key(r) for r in live}
    all_u = {uniq_key(r) for r in sh_rows}
    out(f"  분모: 기록 후보 유니크 {len(all_u):,}건 / 기존 규칙 통과 {len(live_u):,}건")

    # 조건식 출처 신뢰도 — condition-source 계열 게이트의 유효 표본
    reliable = [r for r in sh_rows if truthy(r.get("condition_source_reliable"))]
    rel_u = {uniq_key(r) for r in reliable}
    out(f"  condition_source_reliable=True: {len(rel_u):,}건 ({pct(len(rel_u), len(all_u))})")
    if len(rel_u) < len(all_u) * 0.2:
        out("    ℹ 낮은 것은 예상된 정상 동작입니다 — 주기적 CNSRREQ 재조회가 없어")
        out("      장중 신규 편입 종목은 하루 종일 출처 미확정으로 남습니다.")
        out("      condition-source 계열 2개 게이트는 표본 부족으로 해석하지 마십시오.")

    # ── session 준비 상태 ──────────────────────────────────────
    # 2026-08-06 (1I.1 정정): 이전 1I 보고에서 "7/28 이후 ready=True가
    # 한 번도 없었다 / 분봉 API가 331봉에서 제한된다"고 했으나
    # **오류였음** — 업로드된 app.log 한 개만 보고 판단했고,
    # 로테이션 파일까지 합치면 ready=True가 매 거래일 발생함
    # (7/29 960건, 8/3 369건, 8/4 1,313건, bar_count=380인 날도 있음).
    # 331은 API 상한이 아니라 해당 종목의 SessionState 누적 시작
    # 시각부터 장 마감까지의 분봉 수임.
    #
    # readiness 의미는 그대로 유지한다:
    #   09:00 봉부터 누적된 종목        → COMPLETE_FROM_OPEN (ready=True)
    #   장중 신규 편입/재시작 이후 누적  → PARTIAL_SESSION   (ready=False)
    # ready 기준을 완화하지 않는다. 장중 신규 종목의 당일 전체
    # session VWAP이 필요하면 "09:00~현재 분봉 backfill"이라는
    # 별도 기능으로 풀어야 하며, 이번 단계 범위가 아니다.
    complete = [r for r in sh_rows if truthy(r.get("session_metrics_ready"))]
    partial = [r for r in sh_rows if not truthy(r.get("session_metrics_ready"))]
    total_sess = len(sh_rows)
    out(f"  COMPLETE_FROM_OPEN (ready=True) : {len(complete):,}건 "
        f"({pct(len(complete), total_sess)})")
    out(f"  PARTIAL_SESSION    (ready=False): {len(partial):,}건 "
        f"({pct(len(partial), total_sess)})")
    reasons = Counter(r.get("session_readiness_reason") for r in sh_rows
                      if filled(r.get("session_readiness_reason")))
    if reasons:
        out("    사유 분포: " + ", ".join(f"{k}({v})" for k, v in reasons.most_common(5)))
    if not complete:
        out("    ⚠ 이 거래일은 ready=True 행이 0건 — **session 게이트 성과 해석 불가**.")
        out("      장중 재시작이나 신규 편입으로 09:00부터 누적된 종목이 없었다는 뜻이며,")
        out("      다른 날에는 ready=True가 정상적으로 발생합니다(코드 결함 아님).")
        out("      아래 session 계열 4개 행은 참고용일 뿐 해석하지 마십시오.")

    out("")
    out(f"  {'범위':18s} {'기준':14s} {'차단(전체)':>14s} {'차단(기존규칙 통과분)':>22s}")
    for field, scope, basis in VWAP_GATES:
        if field not in (sh_rows[0].keys() if sh_rows else []):
            continue
        # 2026-08-06 (1I.1): session 기준 게이트는 ready=True 행만
        # 모집단으로 삼는다 — PARTIAL_SESSION 행의 session VWAP은
        # 당일 전체가 아니라 누적 시작 이후 구간만 반영하므로
        # 섞어서 세면 게이트 성능을 잘못 읽게 됨.
        is_session = basis.startswith("session")
        pool = complete if is_session else sh_rows
        pool_live = [r for r in live if truthy(r.get("session_metrics_ready"))] if is_session else live
        # 2026-08-06 (1I.2, GPT 지적 P1-3): 분자는 유니크 분봉인데
        # 분모가 행 수여서, 같은 분봉에 상태 변화 행이 2개 있으면
        # 비율이 절반으로 낮게 나왔음. 분모도 유니크로 통일.
        denom_all = len({uniq_key(r) for r in pool})
        denom_live = len({uniq_key(r) for r in pool_live})
        hit_u = {uniq_key(r) for r in pool if truthy(r.get(field))}
        live_hit_u = {uniq_key(r) for r in pool_live if truthy(r.get(field))}
        note = "  (ready=True 행만)" if is_session else ""
        out(f"  {scope:18s} {basis:14s} {len(hit_u):5,}건 {pct(len(hit_u), denom_all)} "
            f"{len(live_hit_u):10,}건 {pct(len(live_hit_u), denom_live)}{note}")

    out("")
    out("  ※ 성과(5·10·20분 수익률, MFE·MAE) 비교는 이번 단계에 포함되지 않았습니다.")
    out("     분봉 리플레이 CSV 조인이 필요하며, 데이터가 실제로 어떻게 쌓이는지")
    out("     확인한 뒤 별도 단계에서 추가합니다.")


# ── 메인 ────────────────────────────────────────────────────────
def build_report(start: date, end: date) -> str:
    buf: list[str] = []

    def out(line: str = "") -> None:
        buf.append(line)

    sig_rows, sig_header = load_rows(SIGNAL_LOG, start, end)
    sh_rows, sh_header = load_rows(SHADOW_LOG, start, end)

    period = start.strftime("%Y-%m-%d")
    if start != end:
        period += f" ~ {end.strftime('%Y-%m-%d')}"

    out("=" * 62)
    out(f"  🔍 shadow 관측 데이터 리포트  {period}")
    out("=" * 62)
    out("  범위: 품질·표본·게이트 집계 (성과 계산은 미포함)")
    out("")

    # 2026-08-06 (1I.1): 분석자가 그날 데이터를 어디까지 믿어도
    # 되는지 맨 위에서 바로 알 수 있도록 품질 요약을 먼저 표시.
    section_data_quality_header(out, sig_rows, sh_rows)
    section_quality(out, sig_rows, sig_header, sh_rows, sh_header)
    section_sample(out, sig_rows, sh_rows)
    section_macd(out, sig_rows)
    section_vwap(out, sh_rows)

    out("")
    out("=" * 62)
    return "\n".join(buf)


def _force_utf8_stdout() -> None:
    """Windows 콘솔 한글 깨짐 방지.

    2026-08-06 (1I): 모듈 최상위에서 sys.stdout을 교체하면,
    이 모듈을 import하는 쪽(테스트 등)의 stdout까지 닫혀서
    ValueError: I/O operation on closed file이 발생함.
    직접 실행할 때만 적용하도록 함수로 분리.
    """
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_stdout()
    args = sys.argv[1:]
    try:
        if len(args) == 0:
            start = end = date.today()
        elif len(args) == 1:
            start = end = datetime.strptime(args[0], "%Y-%m-%d").date()
        else:
            start = datetime.strptime(args[0], "%Y-%m-%d").date()
            end = datetime.strptime(args[1], "%Y-%m-%d").date()
    except ValueError:
        print("날짜 형식이 잘못됐습니다. 예: python analyze_shadow.py 2026-08-05")
        return 1

    report = build_report(start, end)
    print(report)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"shadow_analysis_{end.strftime('%Y%m%d')}.txt"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
