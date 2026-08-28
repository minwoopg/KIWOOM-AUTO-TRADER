#!/usr/bin/env python3
"""Profitability Sprint v1 — 실제 거래 기반 수익성 개선 후보 발굴 (analysis-only).

1P0.8 이후 첫 "수익성" 분석 전용 도구입니다. 지금까지의 1P0.8 라운드는
전부 안전성/관측성(D.1/D.1.1/E.1-A/OBS.2)이었고, 이 도구는 그 위에서
"실제 손실을 줄이고 기대수익을 높일 수 있는 전략 변경 후보를 좁히는"
것이 유일한 목적입니다.

절대 원칙 (이 파일 자체가 지켜야 하는 제약):
    - Broker 호출 금지
    - 네트워크 호출 금지
    - 주문 API 호출 금지
    - TradingService 동작 변경 없음(이 파일은 TradingService를 import하지
      않고, production 코드를 어떤 방식으로도 수정/실행하지 않습니다)
    - runtime state 변경 없음
    100% offline 분석기입니다 — 로컬 bundle CSV만 읽고, 로컬 diagnostics/
    폴더에만 씁니다.

사용법:
    python tools/profitability_sprint.py \\
        --bundle /path/to/bundle_20260820_v2 \\
        --bundle /path/to/bundle_20260821 \\
        --out-dir diagnostics/profitability

각 --bundle 인자는 daily bundle 디렉터리(내부에 raw/trades_YYYYMMDD.csv 등이
있는 구조, export_daily_bundle.py 산출물과 동일)를 가리킵니다. 여러 날짜를
한 번에 넘기면 합쳐서 다중일 분석을 수행합니다.

출력(모두 --out-dir 아래, 기본 diagnostics/profitability/):
    trade_feature_table.csv
    low_upside_study.csv
    exit_extension_study.csv
    exit_extension_study_by_regime.csv
    entry_quality_study.csv
    candidate_scorecard.csv
    candidate_cost_aware_summary.csv
    candidate_cost_aware_per_day.csv
    pnl_terminology.md  (2026-08-27, Sprint v1.3.1 — PnL 용어집, 데이터 무관 고정 문서)
    profitability_summary_<YYYYMMDD~YYYYMMDD>.md

데이터 무결성 원칙 (민우님 지시 그대로):
    - 원본 bundle이 없는 날짜의 수치를 문서만 보고 임의로 복원하지 않습니다.
    - 모든 산출 값에는 근거 등급(RAW/DERIVED_FROM_RAW/DOCUMENT_ONLY/
      UNAVAILABLE)을 붙일 수 있도록 quality_notes에 기록합니다.
    - 채울 수 없는 필드는 빈 문자열(NA)로 두고 추정하지 않습니다.
    - live 실측 데이터 우선, replay/가상 데이터는 최후 수단이며 이 도구는
      replay를 전혀 실행하지 않습니다(entry_watch_shadow에 이미 기록된
      실측 checkpoint만 사용) — LIVE_SUPPORTED만 만들고 REPLAY_ONLY는
      생성하지 않습니다(replay 재실행 자체를 하지 않으므로).
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ── 비용 모델 ────────────────────────────────────────────────────
# 이 프로젝트의 비용 시나리오는 domain/cost_model.py가 단일 출처입니다
# (1J/1J.1 — "분석기마다 비용을 직접 하드코딩하고 값이 서로 달랐다"는
# 사고를 막기 위한 정책, test_cost_model.py가 저장소 전체를 스캔해
# 허용 위치 밖의 0.35/0.90 리터럴 하드코딩을 검출합니다). 이 도구도
# 예외 없이 domain.cost_model.load_cost_model()로만 비용을 얻습니다.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from domain.cost_model import load_cost_model  # noqa: E402

COST_MODEL = load_cost_model()
BASE_SCENARIO = "base"
STRESS_SCENARIO = "stress"

# 2026-08-26 (Candidate A forward shadow 착수, 민우님 지시): Candidate A
# (upside<0.50% AND rebound_volume_spike==False)를 8/20~8/25 historical
# 데이터로 탐색을 마치고 이 날짜부터 production shadow(observation-only,
# BUY 차단 없음)로 조건을 고정했습니다. "8/20~8/25는 이 후보를 만드는 데
# 쓰인 historical/backtest-like evidence, 이날 이후 쌓이는 표본은 forward
# evidence — 절대 한 덩어리로 섞어서 표본 수를 부풀리면 안 된다"는 민우님
# 지시에 따라, trade_date가 이 날짜 이전이면 HISTORICAL, 이날 이후(포함)면
# FORWARD로 분류합니다. YYYYMMDD 문자열 비교로 충분합니다(trade_date가
# 항상 이 포맷).
# 2026-08-26 date-boundary reclosure(민우님 지적): 최초 구현 시점에는
# "내일(8/27)부터 적용"을 가정해 8/27로 썼으나, 실제 적용 시점이 8/26
# 장 시작 전으로 확정되면서 8/26 실측이 최초 forward 표본이 됩니다.
# 8/27로 두면 8/26 하루치 forward 데이터가 historical로 잘못 섞이므로
# 8/26으로 수정합니다(계산식/로직 변경 없음, 상수값만 하루 당김).
CANDIDATE_A_FORWARD_START_DATE = "20260826"

# 2026-08-27 (Sprint v1.3.1, methodology closure, 민우님 지시): 위
# CANDIDATE_A_FORWARD_START_DATE 하나를 Candidate G/M1까지 공유해서
# forward 경계로 쓰면 안 됩니다 — Candidate A는 Sprint v1.2 GPT 리뷰로
# 8/26 이전에 이미 조건이 고정됐지만, Candidate G(갭눌림D 자동분류)와
# Candidate M1(5분 checkpoint)은 이 조건/분류 코드 자체가 8/26 Sprint
# v1.3 구현·디버깅 도중 만들어지고 고쳐졌습니다. 특히 M1은 8/25(052690)
# ~8/26(003490/006360) 실거래를 직접 보고 만든 가설이고, 8/26 데이터로
# qualifies_rule_m1()의 pnl 게이트 버그까지 잡았습니다 — 이 날짜들을
# "forward 검증 표본"으로 쓰면 가설을 만드는 데 쓴 데이터로 그 가설을
# 검증하는 순환논리가 됩니다. 그래서 세 구간으로 분리합니다:
#   HISTORICAL         : 이 후보를 구성하는 데 전혀 관여하지 않은 순수 이전 데이터
#   HYPOTHESIS_FORMING  : 이 후보의 조건/코드를 만들거나 디버깅하는 데 실제로
#                         들여다본 데이터 — enforce 근거로 쓰지 않음
#   TRUE_FORWARD        : 후보 조건이 완전히 고정된 뒤 새로 쌓인 진짜
#                         out-of-sample 데이터 — enforce 판단의 유일한 근거 구간
# Candidate A/B/F2 baseline은 이 표에 없습니다 — 위 CANDIDATE_A_FORWARD_
# START_DATE 그대로(HYPOTHESIS_FORMING 구간이 사실상 비어 있어 기존
# HISTORICAL/FORWARD 2-way 분리와 동일한 결과를 냄, 굳이 3-way로
# 바꾸지 않음 — 민우님 지시 범위(A/G/M1) 밖).
CANDIDATE_REGIME_BOUNDARIES = {
    "CandidateG": {"hypothesis_forming_start": "20260826", "true_forward_start": "20260827"},
    "M1": {"hypothesis_forming_start": "20260826", "true_forward_start": "20260827"},
}

# 2026-08-27 (Sprint v1.3.1, methodology closure, 민우님 지시): 리포트에
# 섞여 나오는 "손익" 수치가 서로 다른 정의라 혼동하기 쉽다는 지적 —
# daily_reporter.py(production, 이 도구 밖)가 쓰는 주문가 기준 실현
# 손익과, 이 도구가 쓰는 avg_buy 기준 proxy gross/Base/Stress 모델
# 순손익은 전부 "손익"이라는 같은 이름을 쓰지만 계산 기준이 다릅니다.
# 이 도구는 production 코드(daily_reporter.py 포함)를 수정하지 않으므로
# (민우님 지시: TradingService/전략/Broker/lifecycle/BUY-SELL 조건은
# 물론이고, 이 리포트 파일도 손대지 않음) 대신 이 도구 자체의 출력
# 옆에 참조용 용어집을 남깁니다 — write_pnl_terminology_md()가
# out_dir에 pnl_terminology.md로 씁니다.
PNL_TERMINOLOGY = [
    {
        "term": "daily_report_order_price_pnl",
        "korean_label": "일일 리포트 손익(주문가 기준 예상)",
        "computed_by": "infra/storage/daily_reporter.py (production, 이 도구 밖 — 이 도구는 계산하지 않음)",
        "definition": (
            "그날 신규 매매의 손익을 BUY/SELL 각각의 '주문 발행 시점 참조가'로 계산한 값. "
            "실제 체결가 기준이 아니고, 비용(수수료/슬리피지) 모델도 반영하지 않은 값입니다."
        ),
    },
    {
        "term": "avg_buy_proxy_gross_pnl",
        "korean_label": "avg_buy 기준 proxy gross 손익",
        "computed_by": "이 도구의 gross_pnl_pct/gross_pnl (build_trade_features 내부 계산)",
        "definition": (
            "매수 원가는 실제 체결 평균단가(avg_buy_price, 브로커 잔고조회 기준 realized 값)를 "
            "쓰지만, 매도가는 SELL 판단 시점의 참조가(proxy quote)입니다 — 실제 SELL 체결가가 "
            "아닙니다(모든 feature row의 data_quality_flag에 이 caveat이 항상 명시됨). "
            "비용(수수료/슬리피지) 미반영."
        ),
    },
    {
        "term": "base_modeled_net_pnl",
        "korean_label": "Base 비용모델 반영 순손익",
        "computed_by": "이 도구의 base_net_pnl_pct/base_net_pnl_krw (COST_MODEL.net(.., 'base'))",
        "definition": "avg_buy_proxy_gross_pnl에서 domain.cost_model의 Base 시나리오(0.35%) 비용을 반영한 값.",
    },
    {
        "term": "stress_modeled_net_pnl",
        "korean_label": "Stress 비용모델 반영 순손익(보수적 상한)",
        "computed_by": "이 도구의 stress_net_pnl_pct/stress_net_pnl_krw (COST_MODEL.net(.., 'stress'))",
        "definition": "avg_buy_proxy_gross_pnl에서 domain.cost_model의 Stress 시나리오(0.90%, 보수적 상한) 비용을 반영한 값.",
    },
]


def write_pnl_terminology_md(out_dir: Path) -> Path:
    """PNL_TERMINOLOGY를 사람이 읽을 수 있는 용어집 md로 씁니다.

    2026-08-27 (Sprint v1.3.1, methodology closure): 데이터에 의존하지
    않는 고정 문서이므로 run()마다 그대로 덮어씁니다 — 새 bundle을
    더한다고 내용이 달라지지 않습니다.
    """
    lines = [
        "# PnL 용어집 (Profitability Sprint v1.3.1)",
        "",
        "리포트에 등장하는 \"손익\"이 전부 같은 정의가 아닙니다. 아래 4개를 섞어서 비교하지 마세요.",
        "",
    ]
    for item in PNL_TERMINOLOGY:
        lines.append(f"## {item['korean_label']} (`{item['term']}`)")
        lines.append("")
        lines.append(f"- 계산 위치: {item['computed_by']}")
        lines.append(f"- 정의: {item['definition']}")
        lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "pnl_terminology.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

# ── 승/무/패 정의 — utils/trade_outcome.py와 동일 정의를 이 파일 안에
#    독립적으로 재정의합니다(analysis-only 도구를 production 모듈에
#    결합시키지 않기 위해 — 정의 자체는 완전히 동일: wins/(wins+losses),
#    breakeven은 분모 제외). ───────────────────────────────────────
WIN = "WIN"
LOSS = "LOSS"
BREAKEVEN = "BREAKEVEN"


def classify_outcome(pnl: float) -> str:
    if pnl > 0:
        return WIN
    if pnl < 0:
        return LOSS
    return BREAKEVEN


def win_rate_str(wins: int, losses: int, ndigits: int = 1) -> str:
    """분모 wins+losses. 0이면 정의 불가 — '해당없음'(OBS.2 최종 리뷰와
    동일한 semantics)."""
    decided = wins + losses
    if decided == 0:
        return "해당없음"
    return f"{wins/decided*100:.{ndigits}f}%"


def safe_float(v):
    """빈 문자열/None/파싱 실패/non-finite(nan/inf/-inf)는 전부 None(NA).
    0.0은 유효값으로 유지합니다(OBS.2-B/OBS.2-C 원칙과 동일)."""
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


def safe_int(v):
    f = safe_float(v)
    if f is None:
        return None
    return int(f)


def safe_bool(v):
    s = str(v).strip().lower()
    if s == "true":
        return True
    if s == "false":
        return False
    return None


def parse_ts(v: str):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# ── 데이터 적재 ──────────────────────────────────────────────────
RAW_FILES = {
    "trades": "trades_{date}.csv",
    "entry_watch_shadow": "entry_watch_shadow_{date}.csv",
    "entry_quality_shadow": "entry_quality_shadow_{date}.csv",
    "position_lifecycle": "position_lifecycle_{date}.csv",
    "signal_log": "signal_log_{date}.csv",
    # 2026-08-26 (Sprint v1.3, Candidate M1): MIN_PROFIT_5M 청산 판단
    # "그 순간"의 price_vs_vwap_pct/macd_above_signal/peak_pnl_pct를
    # 쓰려면 이 CSV가 필요합니다 — 없으면 UNAVAILABLE로 표시되고
    # (load_bundle_day의 기존 동작 그대로) M1 관련 checkpoint 필드는
    # 전부 결측으로 남습니다(추정하지 않음).
    "min_profit_extension_shadow": "min_profit_extension_shadow_{date}.csv",
}


@dataclass
class BundleDay:
    date: str  # YYYYMMDD
    source_dir: Path
    tables: dict = field(default_factory=dict)          # name -> list[dict]
    availability: dict = field(default_factory=dict)     # name -> "RAW" | "UNAVAILABLE"
    quality_notes: list = field(default_factory=list)


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_bundle_day(bundle_dir: Path) -> BundleDay:
    """bundle_dir/raw/*_<date>.csv 를 읽습니다. date는 raw/trades_*.csv
    파일명에서 추출합니다(필수 파일). 없는 파일은 UNAVAILABLE로 표시하고
    조용히 넘어가지 않습니다(quality_notes에 기록)."""
    raw_dir = bundle_dir / "raw"
    trades_candidates = sorted(raw_dir.glob("trades_*.csv")) if raw_dir.is_dir() else []
    if not trades_candidates:
        raise FileNotFoundError(
            f"{bundle_dir}: raw/trades_*.csv 를 찾을 수 없습니다 — "
            "이 디렉터리는 daily bundle이 아니거나 raw 파일이 없습니다. "
            "원본 bundle 없이 문서만으로 이 날짜를 분석에 포함하지 않습니다."
        )
    trades_path = trades_candidates[0]
    date = trades_path.stem.split("_")[-1]

    day = BundleDay(date=date, source_dir=bundle_dir)
    for name, pattern in RAW_FILES.items():
        p = raw_dir / pattern.format(date=date)
        if p.is_file():
            try:
                day.tables[name] = _read_csv(p)
                day.availability[name] = "RAW"
            except Exception as e:  # noqa: BLE001 — 진단 도구이므로 광범위 예외를 기록만 하고 계속
                day.tables[name] = []
                day.availability[name] = "UNAVAILABLE"
                day.quality_notes.append(f"{name}: 읽기 실패({e}) — UNAVAILABLE 처리")
        else:
            day.tables[name] = []
            day.availability[name] = "UNAVAILABLE"
            day.quality_notes.append(f"{name}: 파일 없음({p.name}) — UNAVAILABLE 처리, 추정하지 않음")
    return day


# ── entry_quality_shadow / signal_log 조인 ──────────────────────
def _index_entry_quality_by_order_id(rows: list[dict]) -> dict:
    idx = {}
    for r in rows:
        oid = (r.get("order_id") or "").strip()
        if oid and safe_bool(r.get("order_accepted")):
            idx[oid] = r
    return idx


def _index_signal_log_buy_by_symbol(rows: list[dict]) -> dict:
    """symbol -> [(timestamp, row), ...] (final_decision == BUY 인 행만).
    order_id가 없는 파일이므로 심볼+초 단위 근접 시각으로 매칭합니다."""
    idx: dict = defaultdict(list)
    for r in rows:
        if (r.get("final_decision") or "").strip() != "BUY":
            continue
        ts = parse_ts(r.get("timestamp", ""))
        if ts is None:
            continue
        idx[r.get("symbol", "")].append((ts, r))
    return idx


def _find_nearest_signal_log(idx: dict, symbol: str, buy_ts, tolerance_sec: float = 5.0):
    candidates = idx.get(symbol, [])
    best = None
    best_dt = None
    for ts, row in candidates:
        dt = abs((ts - buy_ts).total_seconds())
        if dt <= tolerance_sec and (best_dt is None or dt < best_dt):
            best, best_dt = row, dt
    return best


def _find_entry_watch_counterfactual(shadow_rows: list[dict], symbol: str, sell_price: float,
                                      sell_ts, tolerance_sec: float = 60.0):
    """entry_watch_shadow에서 이 SELL과 실제로 대응되는 checkpoint 3건을
    찾습니다. symbol + trigger_price(정확히 SELL 체결가와 일치) +
    trigger_at가 SELL 시각과 tolerance_sec 이내일 때만 매칭합니다.

    이 이중 검증(가격+시각)이 중요한 이유: 8/21 실측 bundle에서 064260
    심볼의 entry_watch_shadow 행이 trigger_price=5330(실제 SELL가
    5290과 불일치) + trigger_at가 실제 SELL 시각보다 171초 늦은 것으로
    확인됐습니다 — OBS.2-A에서 고친 "entry_watch_shadow가 실제 accepted
    SELL이 아니라 판단 시점에 앵커링되던" 오염 패턴이 이 pre-fix 번들에
    실제로 남아있는 사례입니다. 가격+시각이 둘 다 일치하지 않으면 이
    counterfactual을 신뢰하지 않고 UNAVAILABLE로 처리합니다(추정 금지).
    """
    matches = [r for r in shadow_rows if r.get("symbol") == symbol]
    price_matches = []
    for r in matches:
        tp = safe_float(r.get("trigger_price"))
        if tp is None or abs(tp - sell_price) > 1e-6:
            continue
        tat = parse_ts(r.get("trigger_at", ""))
        if tat is None:
            continue
        if abs((tat - sell_ts).total_seconds()) > tolerance_sec:
            continue
        price_matches.append(r)
    if not price_matches:
        return None, matches  # (검증된 매칭 없음, 참고용 후보 목록)
    by_checkpoint = {}
    for r in price_matches:
        cp = safe_int(r.get("checkpoint_min"))
        if cp is not None:
            by_checkpoint[cp] = r
    return by_checkpoint, matches


# 2026-08-26 (Sprint v1.3, Candidate M1) — min_profit_extension_shadow.csv
# 조인. entry_time 컬럼으로 매칭하지 않는 이유: entry_time은 BUY_CONFIRMED
# (체결 확인) 시각이라 trades.csv BUY 행의 주문 시각과 초 단위로 어긋날 수
# 있습니다(8/26 실측: 034020 entry_time=09:26:08.96 vs BUY 행 타임스탬프
# 09:25:58.94, 약 10초 차이). 대신 이 CSV의 timestamp(=_log_min_profit_
# extension_shadow() 호출 시각)는 대응하는 SELL이 trades.csv에 기록되는
# 순간과 사실상 동시입니다(8/26 실측 3건 전부 27~47ms 이내) — 이 함수가
# `_check_entry_watch()`의 SELL 판정 바로 그 순간 호출되기 때문입니다.
# 그래서 symbol + SELL timestamp 근접 매칭을 씁니다(기존 signal_log
# 근접 매칭과 동일한 tolerance_sec=5.0 관례).
def _index_min_profit_shadow_by_symbol(rows: list[dict]) -> dict:
    idx: dict = defaultdict(list)
    for r in rows:
        ts = parse_ts(r.get("timestamp", ""))
        if ts is None:
            continue
        idx[r.get("symbol", "")].append((ts, r))
    return idx


def _find_nearest_min_profit_shadow(idx: dict, symbol: str, sell_ts, tolerance_sec: float = 5.0):
    candidates = idx.get(symbol, [])
    best = None
    best_dt = None
    for ts, row in candidates:
        dt = abs((ts - sell_ts).total_seconds())
        if dt <= tolerance_sec and (best_dt is None or dt < best_dt):
            best, best_dt = row, dt
    return best


# 2026-08-26 (Sprint v1.3, Candidate G): entry_reason 텍스트에서 갭눌림D
# (breakout_strategy.py의 cond_gap_pullback) 충족 여부를 파싱합니다. 새
# 판정 기준을 만들지 않고 이미 존재하는 문구만 구분합니다
# (classify_entry_watch_trigger()와 동일한 관례) — gap_label은
# breakout_strategy.py:190에서 "갭눌림D✓(+N.N%)" 또는 "갭눌림D✗"로 고정
# 문구이고, 이 전략의 모든 BUY/HOLD reason에 항상 포함됩니다(요약 tags의
# 마지막 항목). trades.csv의 entry_reason은 signal.reason을 120자로 자른
# 값이라(trading_service.py) 이론상 잘려나갈 수 있지만, 실측(8/26)으로는
# 96~103자로 전부 여유 있게 포함됩니다. 마커가 전혀 없으면(다른 전략
# 경로이거나 절단됐거나) False로 추정하지 않고 None(결측)을 반환합니다.
def parse_gap_pullback_d(entry_reason: str) -> bool | None:
    r = entry_reason or ""
    if "갭눌림D✓" in r:
        return True
    if "갭눌림D✗" in r:
        return False
    return None


# ── feature row 빌드 ─────────────────────────────────────────────
FEATURE_COLUMNS = [
    "trade_date", "symbol", "buy_time", "sell_time", "buy_price", "sell_price",
    "quantity", "gross_pnl", "gross_pnl_pct", "base_net_pnl_pct", "stress_net_pnl_pct",
    # 2026-08-24 (민우님 리뷰 3번 지적 반영, 금액 기준 scorecard용) — 비용은
    # 여전히 domain.cost_model 단일 출처(COST_MODEL.cost_amount)로만 계산합니다.
    "entry_notional_krw", "modeled_base_cost_krw", "modeled_stress_cost_krw",
    "base_net_pnl_krw", "stress_net_pnl_krw",
    "outcome",
    # 2026-08-27 (Candidate A forward shadow 착수, 민우님 지시): "outcome"
    # (위)은 기존 F0~F3 계산 전체가 의존하는 gross 기준 정의라 의미를
    # 바꾸지 않고 그대로 둡니다. base_outcome/stress_outcome은 비용
    # 반영 후 WIN/LOSS/BREAKEVEN이 gross와 달라질 수 있음을 보려는
    # 순수 추가 컬럼입니다 — "+0.3%짜리 거래는 gross로는 승자여도
    # 왕복비용을 내면 실제로는 좋은 거래가 아닐 수 있다"(민우님
    # 지적)를 그대로 노출. candidate_cost_aware_metrics()가 이
    # base_outcome을 기준으로 skip_precision_base/loss_recall_base/
    # winner_damage_base를 계산합니다.
    "base_outcome", "stress_outcome",
    "entry_score", "pattern", "condition_name", "upside_to_recent_high_pct",
    "current_vs_vwap_pct", "rsi", "macd", "macd_signal", "macd_above_signal",
    "macd_hist_direction", "ma5_above_ma20", "volume_ratio", "rebound_volume_spike",
    "rebound_volume_ratio", "atr_14_pct", "bb_percent_b", "bb_position",
    "session_metrics_ready", "condition_source_reliable",
    "rolling_vwap_distance_pct", "session_vwap_distance_pct",
    "would_block_macd_dead_min_score5", "would_block_macd_above_signal_required",
    "would_block_pr_or_pullback_condition_rolling_vwap",
    "would_block_pr_or_pullback_condition_session_vwap",
    "is_v_rebound", "is_pulldown_recovery",
    "exit_reason", "holding_minutes", "is_entry_watch_exit",
    # 2026-08-24 (민우님 리뷰 2번 지적 반영): entry_watch 청산을 유형별로
    # 명시적으로 분리 — 기존 is_entry_watch_exit 하나로는 급락청산/VWAP
    # 이탈청산/최소수익미달청산이 전부 섞였습니다. exit_extension_study()가
    # 이 필드로 MIN_PROFIT_5M만 골라 조건부 연장 후보를 계산합니다.
    "entry_watch_trigger_type",
    "first_sell_accepted_at",
    "fwd5m_price_return_pct", "fwd5m_base_net_pct", "fwd5m_stress_net_pct",
    "fwd10m_price_return_pct", "fwd10m_base_net_pct", "fwd10m_stress_net_pct",
    "fwd20m_price_return_pct", "fwd20m_base_net_pct", "fwd20m_stress_net_pct",
    "fwd_data_source",
    "data_quality_flag",
    # 2026-08-26 (Sprint v1.3, 민우님 지시): Candidate G(갭눌림D)와
    # Candidate M1(5분 checkpoint price>VWAP AND MACD>signal) 자동 분류를
    # 위한 신규 컬럼. entry_reason은 원본 텍스트 그대로 보존(감사용),
    # gap_pullback_d는 parse_gap_pullback_d()로 파싱한 결과(True/False/
    # 결측=None). checkpoint_* 3개는 min_profit_extension_shadow.csv에서
    # SELL 판정 시점 값을 그대로 조인한 것 — 기존 current_vs_vwap_pct/
    # macd_above_signal(진입 시점 값, R1/R2가 참조하던 값)과는 별개입니다.
    "entry_reason", "gap_pullback_d",
    "checkpoint_price_vs_vwap_pct", "checkpoint_macd_above_signal", "checkpoint_peak_pnl_pct",
]

# entry_watch_trigger_type 값 — exit_reason 텍스트 접두 매칭으로 결정(그 외
# 문자열 파싱 없음). "entry_watch "로 시작하지 않으면 NOT_ENTRY_WATCH.
TRIGGER_MIN_PROFIT_5M = "MIN_PROFIT_5M"
TRIGGER_EARLY_VWAP_EXIT = "EARLY_VWAP_EXIT"
TRIGGER_CRASH_CUT = "CRASH_CUT"
TRIGGER_OTHER_ENTRY_WATCH = "OTHER_ENTRY_WATCH"
TRIGGER_NOT_ENTRY_WATCH = "NOT_ENTRY_WATCH"


def classify_entry_watch_trigger(exit_reason: str) -> str:
    """exit_reason 텍스트로 entry_watch 청산 유형을 분류합니다.

    2026-08-24 (민우님 코드/CSV 직접 대조 리뷰 2번 지적 반영). Sprint v1의
    Study B는 "entry_watch " 접두사만으로 급락청산/VWAP이탈청산/최소수익
    미달청산을 한 버킷에 섞었습니다 — "5분 최소수익 타이머를 연장할
    것인가"라는 질문에 VWAP 조기청산(1~4분, 다른 전략 질문)이 섞여
    들어가 근거가 약해지는 문제가 있었습니다. domain/service/trading_
    service.py의 `_check_entry_watch()`가 만드는 정확한 문구
    ("entry_watch 급락청산 —", "entry_watch VWAP이탈청산 —", "entry_watch
    최소수익미달청산 —")를 그대로 재사용해 분류합니다 — 새 판정 기준을
    만들지 않고 이미 존재하는 문구만 구분합니다.
    """
    r = exit_reason or ""
    if not r.startswith("entry_watch "):
        return TRIGGER_NOT_ENTRY_WATCH
    if "최소수익미달청산" in r:
        return TRIGGER_MIN_PROFIT_5M
    if "VWAP이탈청산" in r:
        return TRIGGER_EARLY_VWAP_EXIT
    if "급락청산" in r:
        return TRIGGER_CRASH_CUT
    return TRIGGER_OTHER_ENTRY_WATCH


def build_trade_features(days: list[BundleDay]) -> tuple[list[dict], list[str]]:
    rows_out = []
    warnings_out = []

    for day in days:
        trades = day.tables.get("trades", [])
        accepted = [r for r in trades if safe_bool(r.get("accepted"))]
        buys: dict = defaultdict(list)
        sells: dict = defaultdict(list)
        for r in accepted:
            side = (r.get("side") or "").strip()
            sym = r.get("symbol", "")
            if side == "BUY":
                buys[sym].append(r)
            elif side == "SELL":
                sells[sym].append(r)

        eq_idx = _index_entry_quality_by_order_id(day.tables.get("entry_quality_shadow", []))
        sl_idx = _index_signal_log_buy_by_symbol(day.tables.get("signal_log", []))
        ew_rows = day.tables.get("entry_watch_shadow", [])
        mp_idx = _index_min_profit_shadow_by_symbol(day.tables.get("min_profit_extension_shadow", []))

        if day.availability.get("entry_quality_shadow") != "RAW":
            warnings_out.append(f"{day.date}: entry_quality_shadow 없음 — 이 날짜 거래의 entry 지표(MACD/VWAP거리/게이트) 전부 NA")
        if day.availability.get("signal_log") != "RAW":
            warnings_out.append(f"{day.date}: signal_log 없음 — 이 날짜 거래의 MA5/ATR/BB 등 보조 지표 NA")
        if day.availability.get("entry_watch_shadow") != "RAW":
            warnings_out.append(f"{day.date}: entry_watch_shadow 없음 — 이 날짜 entry_watch 청산의 +5/10/20분 counterfactual 전부 NA")
        if day.availability.get("min_profit_extension_shadow") != "RAW":
            warnings_out.append(f"{day.date}: min_profit_extension_shadow 없음 — 이 날짜 최소수익미달청산의 Candidate M1 checkpoint(5분 시점 price_vs_vwap/MACD/peak_pnl) 전부 NA")

        for sym, sell_list in sells.items():
            buy_list = buys.get(sym, [])
            for sell in sell_list:
                if not buy_list:
                    warnings_out.append(f"{day.date} {sym}: SELL에 대응하는 BUY가 없음 — 이 SELL 스킵(추정 금지)")
                    continue
                buy = buy_list.pop(0)

                buy_price_field = safe_float(buy.get("price"))
                sell_price = safe_float(sell.get("price"))
                avg_buy_price = safe_float(sell.get("avg_buy_price"))
                qty = safe_int(sell.get("quantity"))
                buy_qty = safe_int(buy.get("quantity"))

                dq_flags = []
                # 실제 체결 평균단가(avg_buy_price, SELL 로그에 기록)를 매수 원가로
                # 사용합니다 — entry_watch_shadow의 actual_pnl_pct 계산 기준과
                # 일치시키기 위함입니다. BUY 행의 "price"는 주문 시점 참조가일 뿐
                # 실제 체결 평균과 다를 수 있습니다(8/21 005935: 요청가 197,500원 vs
                # 실제 평균 체결가 196,933원, 차이 0.29%p). analyze_trades.py
                # 헤드라인 계산(BUY price 필드 사용)과는 이 지점에서 다를 수 있음을
                # 명시합니다.
                buy_price = avg_buy_price if avg_buy_price and avg_buy_price > 0 else buy_price_field
                if avg_buy_price and buy_price_field and abs(avg_buy_price - buy_price_field) / buy_price_field > 0.001:
                    dq_flags.append(f"avg_buy_price({avg_buy_price})가 BUY 주문가({buy_price_field})와 {abs(avg_buy_price-buy_price_field)/buy_price_field*100:.2f}%p 차이")

                # ── PnL price-source 감사 결과 (2026-08-24, 민우님 지시 3번) ──
                # sell_price(위 sell.get("price"))는 SELL 실제 체결 평균가가
                # 아니라 SELL 주문을 내기 직전 판단 시점의 참조가(quote)입니다.
                # 근거(file:line, domain/service/trading_service.py):
                #   - BUY도 SELL도 trades.csv "price" 필드는 동일하게
                #     _write_trade_log(..., price=current_price)로 기록되고
                #     (BUY: L3179/3232, SELL: L3448/3514), 이 current_price는
                #     주문 발행 "직전" 폴링에서 읽은 시세입니다.
                #   - SELL 주문 자체도 OrderRequest(symbol=.., side=SELL,
                #     quantity=..)로 가격 없이(order_type 기본값 "market")
                #     발행됩니다(L3420) — 즉 실제 체결가는 브로커가 결정하고,
                #     그 값은 어디에도 기록되지 않습니다.
                #   - OrderResult(domain/models.py L86-104)에는 애초에 체결가
                #     필드 자체가 없습니다 — accepted/message/order_id뿐.
                #   - 실제 체결가(cntr_pric)는 ka10076(체결조회) 응답에
                #     존재하고 infra/broker/kiwoom_order_status.py L212의
                #     filled_price로 이미 파싱까지 되어 있지만, 이 모듈은
                #     아직 KiwoomBroker/Broker 인터페이스에 연결되지
                #     않았습니다(1P0.8-C 예정, 그 파일 자체 docstring 참고)
                #     — 즉 지금 살아있는 트레이딩 루프 어디에도 실제 SELL
                #     체결가를 잡아서 기록하는 코드가 없습니다.
                #   - 반대로 avg_buy_price(SELL 로그 필드)는 브로커 잔고
                #     조회 API(position.average_price, L1634)에서 온 진짜
                #     체결 평균단가입니다 — 이 필드는 realized 값이 맞습니다.
                # 결론: 이 도구의 gross/base/stress PnL은 "실현손익"이 아니라
                # "매도 판단 시점 참조가 기준 proxy 손익"입니다(매수 쪽은
                # avg_buy_price를 쓰므로 realized). 유동성이 충분한 종목·
                # 시장가 체결이면 참조가와 실제 체결가 차이(슬리피지)가
                # 작겠지만, 이 데이터로는 그 크기를 검증할 방법이 없습니다.
                # 코드는 이번 라운드에서 고치지 않습니다(민우님 지시: 감사만,
                # 수정은 별도 승인 필요) — 대신 모든 산출 행에 아래처럼
                # data_quality_flag로 명시합니다.
                dq_flags.append(
                    "sell_price는 SELL 실제 체결가가 아니라 판단 시점 참조가(proxy) — "
                    "이 거래의 PnL은 realized가 아니라 proxy로 취급할 것"
                )

                if not buy_price or not sell_price or buy_price <= 0 or sell_price <= 0 or not qty:
                    warnings_out.append(f"{day.date} {sym}: 가격/수량 결측으로 이 쌍 스킵")
                    continue
                if buy_qty is not None and qty is not None and buy_qty != qty:
                    dq_flags.append(f"BUY수량({buy_qty})!=SELL수량({qty}) — 부분체결 가능성, quantity는 SELL 기준")

                gross_pnl_pct = (sell_price - buy_price) / buy_price * 100.0
                gross_pnl = (sell_price - buy_price) * qty
                outcome = classify_outcome(gross_pnl_pct)
                base_net_pnl_pct = COST_MODEL.net(gross_pnl_pct, BASE_SCENARIO)
                stress_net_pnl_pct = COST_MODEL.net(gross_pnl_pct, STRESS_SCENARIO)

                # 금액 기준(KRW) — 2026-08-24, 민우님 리뷰 3번 지적 반영.
                # cost_amount()는 buy notional(=buy_price*qty) 기준이라
                # base_net_pnl_krw == gross_pnl - modeled_base_cost_krw가
                # base_net_pnl_pct*entry_notional_krw/100과 정확히 일치합니다
                # (COST_MODEL 단일 출처, 이 파일에서 비용을 직접 계산하지 않음).
                entry_notional_krw = buy_price * qty
                modeled_base_cost_krw = COST_MODEL.cost_amount(entry_notional_krw, BASE_SCENARIO)
                modeled_stress_cost_krw = COST_MODEL.cost_amount(entry_notional_krw, STRESS_SCENARIO)
                base_net_pnl_krw = gross_pnl - modeled_base_cost_krw
                stress_net_pnl_krw = gross_pnl - modeled_stress_cost_krw

                buy_ts = parse_ts(buy.get("timestamp", ""))
                sell_ts = parse_ts(sell.get("timestamp", ""))
                hold_minutes = safe_float(sell.get("hold_minutes"))
                exit_reason = sell.get("exit_reason", "") or ""
                is_entry_watch = exit_reason.startswith("entry_watch ")
                entry_watch_trigger_type = classify_entry_watch_trigger(exit_reason)

                eq_row = eq_idx.get((buy.get("order_id") or "").strip())
                sl_row = _find_nearest_signal_log(sl_idx, sym, buy_ts) if buy_ts else None

                row = {c: "" for c in FEATURE_COLUMNS}
                row.update({
                    "trade_date": day.date,
                    "symbol": sym,
                    "buy_time": buy.get("timestamp", ""),
                    "sell_time": sell.get("timestamp", ""),
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "quantity": qty,
                    "gross_pnl": round(gross_pnl, 2),
                    "gross_pnl_pct": round(gross_pnl_pct, 4),
                    "base_net_pnl_pct": round(base_net_pnl_pct, 4),
                    "stress_net_pnl_pct": round(stress_net_pnl_pct, 4),
                    "entry_notional_krw": round(entry_notional_krw, 2),
                    "modeled_base_cost_krw": round(modeled_base_cost_krw, 2),
                    "modeled_stress_cost_krw": round(modeled_stress_cost_krw, 2),
                    "base_net_pnl_krw": round(base_net_pnl_krw, 2),
                    "stress_net_pnl_krw": round(stress_net_pnl_krw, 2),
                    "outcome": outcome,
                    "base_outcome": classify_outcome(base_net_pnl_pct),
                    "stress_outcome": classify_outcome(stress_net_pnl_pct),
                    "entry_score": safe_int(buy.get("entry_score")),
                    "upside_to_recent_high_pct": safe_float(buy.get("upside_to_recent_high_pct")),
                    "current_vs_vwap_pct": safe_float(buy.get("current_vs_vwap_pct")),
                    "volume_ratio": safe_float(buy.get("volume_ratio")),
                    "rebound_volume_spike": safe_bool(buy.get("rebound_volume_spike")),
                    "is_v_rebound": safe_bool(buy.get("is_v_rebound")),
                    "is_pulldown_recovery": safe_bool(buy.get("is_pulldown_recovery")),
                    "exit_reason": exit_reason,
                    "holding_minutes": hold_minutes,
                    "is_entry_watch_exit": is_entry_watch,
                    "entry_watch_trigger_type": entry_watch_trigger_type,
                    "first_sell_accepted_at": sell.get("timestamp", ""),
                    "entry_reason": buy.get("entry_reason", "") or "",
                    "gap_pullback_d": parse_gap_pullback_d(buy.get("entry_reason", "")),
                    "rsi": None,  # 현재 어떤 raw 파일에도 RSI가 기록되지 않음 — UNAVAILABLE
                    "fwd_data_source": "",
                })

                if eq_row is not None:
                    row.update({
                        "pattern": eq_row.get("detected_patterns", ""),
                        "condition_name": eq_row.get("condition_name", ""),
                        "macd": safe_float(eq_row.get("macd")),
                        "macd_signal": safe_float(eq_row.get("macd_signal")),
                        "macd_above_signal": safe_bool(eq_row.get("macd_above_signal")),
                        "session_metrics_ready": safe_bool(eq_row.get("session_metrics_ready")),
                        "condition_source_reliable": safe_bool(eq_row.get("condition_source_reliable")),
                        "rolling_vwap_distance_pct": safe_float(eq_row.get("rolling_vwap_distance_pct")),
                        "session_vwap_distance_pct": safe_float(eq_row.get("session_vwap_distance_pct")),
                        "would_block_macd_dead_min_score5": safe_bool(eq_row.get("would_block_macd_dead_min_score5")),
                        "would_block_macd_above_signal_required": safe_bool(eq_row.get("would_block_macd_above_signal_required")),
                        "would_block_pr_or_pullback_condition_rolling_vwap": safe_bool(eq_row.get("would_block_pr_or_pullback_condition_rolling_vwap")),
                        "would_block_pr_or_pullback_condition_session_vwap": safe_bool(eq_row.get("would_block_pr_or_pullback_condition_session_vwap")),
                    })
                else:
                    dq_flags.append("entry_quality_shadow order_id 매칭 실패 — entry gate/MACD/VWAP거리 NA")

                if sl_row is not None:
                    row.update({
                        "ma5_above_ma20": safe_bool(sl_row.get("ma5_above_ma20")),
                        "rebound_volume_ratio": safe_float(sl_row.get("rebound_volume_ratio")),
                        "atr_14_pct": safe_float(sl_row.get("atr_14_pct")),
                        "bb_percent_b": safe_float(sl_row.get("bb_percent_b")),
                        "bb_position": sl_row.get("bb_position", ""),
                        "macd_hist_direction": sl_row.get("macd_hist_direction", ""),
                    })
                else:
                    dq_flags.append("signal_log 근접 매칭 실패(±5초) — MA5/ATR/BB 보조지표 NA")

                # entry_watch 청산이면 counterfactual 시도(실측 shadow checkpoint만 사용,
                # replay 재실행 없음 — LIVE_SUPPORTED 데이터만).
                if is_entry_watch and sell_ts is not None:
                    by_cp, candidates = _find_entry_watch_counterfactual(ew_rows, sym, sell_price, sell_ts)
                    if by_cp:
                        row["fwd_data_source"] = "LIVE_SUPPORTED(entry_watch_shadow 실측 checkpoint)"
                        for m in (5, 10, 20):
                            r = by_cp.get(m)
                            if r is None:
                                continue
                            pr = safe_float(r.get("counterfactual_pnl_pct"))
                            if pr is None:
                                continue
                            row[f"fwd{m}m_price_return_pct"] = pr
                            row[f"fwd{m}m_base_net_pct"] = round(COST_MODEL.net(pr, BASE_SCENARIO), 4)
                            row[f"fwd{m}m_stress_net_pct"] = round(COST_MODEL.net(pr, STRESS_SCENARIO), 4)
                    elif candidates:
                        row["fwd_data_source"] = "UNAVAILABLE(entry_watch_shadow 후보 있으나 가격/시각 불일치 — 사용 안 함)"
                        dq_flags.append(
                            "entry_watch_shadow 후보가 있으나 trigger_price/trigger_at가 실제 SELL과 불일치 "
                            "(OBS.2-A 이전 entry_watch_shadow 오염 패턴과 일치 — counterfactual 사용 안 함, 추정 금지)"
                        )
                    else:
                        row["fwd_data_source"] = "UNAVAILABLE(entry_watch_shadow에 해당 심볼 없음)"
                elif not is_entry_watch:
                    row["fwd_data_source"] = "UNAVAILABLE(entry_watch 청산이 아니므로 shadow가 추적하지 않음)"

                # 2026-08-26 (Sprint v1.3, Candidate M1): MIN_PROFIT_5M
                # 청산일 때만 min_profit_extension_shadow.csv에서 "그 판단
                # 순간"의 checkpoint 값을 조인합니다 — 다른 청산 유형(급락/
                # VWAP이탈/entry_watch 아님)은 애초에 이 CSV에 행이 없으므로
                # (production 로거가 최소수익미달 분기에서만 기록) 시도하지
                # 않습니다.
                if entry_watch_trigger_type == TRIGGER_MIN_PROFIT_5M and sell_ts is not None:
                    mp_row = _find_nearest_min_profit_shadow(mp_idx, sym, sell_ts)
                    if mp_row is not None:
                        row.update({
                            "checkpoint_price_vs_vwap_pct": safe_float(mp_row.get("price_vs_vwap_pct")),
                            "checkpoint_macd_above_signal": safe_bool(mp_row.get("macd_above_signal")),
                            "checkpoint_peak_pnl_pct": safe_float(mp_row.get("peak_pnl_pct")),
                        })
                    else:
                        dq_flags.append(
                            "min_profit_extension_shadow 근접 매칭 실패(±5초) — "
                            "Candidate M1 checkpoint(price_vs_vwap/MACD/peak_pnl) NA"
                        )

                row["data_quality_flag"] = "; ".join(dq_flags)
                rows_out.append(row)

    return rows_out, warnings_out


# ── Study A: Low Upside ──────────────────────────────────────────
UPSIDE_BUCKETS = [
    ("<0.25%", lambda u: u < 0.25),
    ("0.25~<0.50%", lambda u: 0.25 <= u < 0.50),
    ("0.50~<0.75%", lambda u: 0.50 <= u < 0.75),
    ("0.75~<1.00%", lambda u: 0.75 <= u < 1.00),
    ("1.00~<1.50%", lambda u: 1.00 <= u < 1.50),
    ("1.50~<2.00%", lambda u: 1.50 <= u < 2.00),
    (">=2.00%", lambda u: u >= 2.00),
]


def _bucket_stats(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [r for r in rows if r["outcome"] == WIN]
    losses = [r for r in rows if r["outcome"] == LOSS]
    breakevens = [r for r in rows if r["outcome"] == BREAKEVEN]
    gross = [r["gross_pnl_pct"] for r in rows]
    return {
        "trades": n,
        "wins": len(wins),
        "breakevens": len(breakevens),
        "losses": len(losses),
        "win_rate": win_rate_str(len(wins), len(losses)),
        "avg_gross_return_pct": round(statistics.mean(gross), 4) if gross else "",
        "median_gross_return_pct": round(statistics.median(gross), 4) if gross else "",
        "gross_pnl_sum": round(sum(r["gross_pnl"] for r in rows), 2),
        "base_net_pnl_pct_sum": round(sum(r["base_net_pnl_pct"] for r in rows), 4),
        "stress_net_pnl_pct_sum": round(sum(r["stress_net_pnl_pct"] for r in rows), 4),
        "worst_trade_pct": round(min(gross), 4) if gross else "",
        "best_trade_pct": round(max(gross), 4) if gross else "",
    }


def low_upside_bucket_study(rows: list[dict]) -> list[dict]:
    out = []
    have_upside = [r for r in rows if r["upside_to_recent_high_pct"] is not None]
    for label, pred in UPSIDE_BUCKETS:
        grp = [r for r in have_upside if pred(r["upside_to_recent_high_pct"])]
        stats = _bucket_stats(grp)
        stats["bucket"] = label
        stats["symbols"] = ";".join(sorted({r["symbol"] for r in grp}))
        out.append(stats)
    return out


def low_upside_filter_candidates(rows: list[dict]) -> list[dict]:
    """upside_to_recent_high_pct 기반 skip 후보 F0~F3을 시뮬레이션합니다.

    2026-08-24 (민우님 리뷰 1번 지적 반영): F1(<1.00%)과 F2(<0.50%)가
    실거래 9건에서 완전히 같은 6건을 제거하는 이유는 이 표본에 0.50~
    1.00% 구간 거래가 우연히 하나도 없었기 때문입니다 — 두 후보가
    "같은 효과"라는 뜻이 아닙니다. 새 데이터가 들어오면 F1은 그 구간도
    추가로 차단하지만 F2는 살립니다. 동일한 과거 개선 효과라면 더 좁은
    필터(F2)가 논리적으로 우월하므로, **주 후보는 F2입니다** — F1/F3은
    보조 비교용으로만 계산합니다. 이 함수 자체의 계산 로직은 셋 다
    동일하게 다루고, 우선순위 표시는 build_scorecard()/verdict에서만
    반영합니다(과거 데이터로 셋을 차별 계산할 근거가 없기 때문).
    """
    have_upside = [r for r in rows if r["upside_to_recent_high_pct"] is not None]
    candidates = [
        _simulate_skip_candidate(have_upside, "F0_current_strategy(baseline, no skip)", lambda r: False),
        # 2026-08-24 (민우님 확정): F2가 주 후보 — 순서를 F2 우선으로 정렬.
        _simulate_skip_candidate(have_upside, "F2_skip_upside<0.50%(주 후보)", lambda r: r["upside_to_recent_high_pct"] < 0.50),
        _simulate_skip_candidate(have_upside, "F1_skip_upside<1.00%(보조 비교용)", lambda r: r["upside_to_recent_high_pct"] < 1.00),
        _simulate_skip_candidate(have_upside, "F3_skip_upside<0.25%(보조 비교용)", lambda r: r["upside_to_recent_high_pct"] < 0.25),
    ]
    return candidates


def _simulate_skip_candidate(have_upside: list[dict], name: str, skip_pred) -> dict:
    """단일 skip 후보(BUY 스킵 조건 하나)의 제거 효과를 계산합니다.

    2026-08-26 (Sprint v1.2 준비, 순수 리팩터): 기존 low_upside_filter_
    candidates() 내부의 sim() 클로저를 그대로 module-level 함수로
    분리했습니다 — 계산 로직/반환 필드는 단 한 글자도 바뀌지 않았고,
    `have_upside`(호출부가 미리 upside_to_recent_high_pct가 있는
    행만 필터링해서 넘김)를 파라미터로 받도록만 바뀌었습니다. F2
    단독 후보(low_upside_filter_candidates)와 Sprint v1.2의 2-조건
    조합 후보(two_condition_low_upside_candidates)가 동일한 계산
    로직을 공유하도록 하기 위함 — 후보마다 다른 metric 정의를 쓰면
    비교 자체가 무의미해지므로, 이 함수 하나가 유일한 계산 출처입니다.
    """
    removed = [r for r in have_upside if skip_pred(r)]
    kept = [r for r in have_upside if not skip_pred(r)]
    removed_w = [r for r in removed if r["outcome"] == WIN]
    removed_b = [r for r in removed if r["outcome"] == BREAKEVEN]
    removed_l = [r for r in removed if r["outcome"] == LOSS]
    orig_base = sum(r["base_net_pnl_pct"] for r in have_upside)
    orig_stress = sum(r["stress_net_pnl_pct"] for r in have_upside)
    kept_base = sum(r["base_net_pnl_pct"] for r in kept)
    kept_stress = sum(r["stress_net_pnl_pct"] for r in kept)
    orig_gross = sum(r["gross_pnl_pct"] for r in have_upside)
    kept_gross = sum(r["gross_pnl_pct"] for r in kept)
    # 금액 기준(KRW) — 2026-08-24, 민우님 리뷰 3번 지적 반영.
    orig_base_krw = sum(r["base_net_pnl_krw"] for r in have_upside)
    orig_stress_krw = sum(r["stress_net_pnl_krw"] for r in have_upside)
    kept_base_krw = sum(r["base_net_pnl_krw"] for r in kept)
    kept_stress_krw = sum(r["stress_net_pnl_krw"] for r in kept)
    total_wins = [r for r in have_upside if r["outcome"] == WIN]
    winner_preservation = (
        "해당없음" if not total_wins else
        f"{(len(total_wins)-len(removed_w))/len(total_wins)*100:.0f}%"
    )
    total_losses = [r for r in have_upside if r["outcome"] == LOSS]
    loser_removal = (
        "해당없음" if not total_losses else
        f"{len(removed_l)/len(total_losses)*100:.0f}%"
    )
    single_large_winner_flag = ""
    if removed_w:
        biggest = max(removed_w, key=lambda r: r["gross_pnl_pct"])
        # 제거된 하나의 승자가 전체 gross 변화의 절반 이상을 차지하면 경고
        gross_delta = kept_gross - orig_gross
        if gross_delta != 0 and abs(biggest["gross_pnl_pct"]) >= abs(gross_delta) * 0.5:
            single_large_winner_flag = (
                f"⚠ 대형 승자 1건({biggest['symbol']} {biggest['trade_date']}, "
                f"gross {biggest['gross_pnl_pct']:+.2f}%) 제거가 전체 변화폭의 상당 부분을 차지함"
            )
    return {
        "candidate": name,
        "original_trades": len(have_upside),
        "removed_trades": len(removed),
        "removed_winners": len(removed_w),
        "removed_breakevens": len(removed_b),
        "removed_losers": len(removed_l),
        "winner_preservation_rate": winner_preservation,
        "loser_removal_rate": loser_removal,
        "gross_pnl_pct_delta": round(kept_gross - orig_gross, 4),
        "base_net_pnl_pct_delta": round(kept_base - orig_base, 4),
        "stress_net_pnl_pct_delta": round(kept_stress - orig_stress, 4),
        "base_net_delta_krw": round(kept_base_krw - orig_base_krw, 2),
        "stress_net_delta_krw": round(kept_stress_krw - orig_stress_krw, 2),
        "avg_trade_base_net_before_pct": round(orig_base / len(have_upside), 4) if have_upside else "",
        "avg_trade_base_net_after_pct": round(kept_base / len(kept), 4) if kept else "해당없음(전량 제거)",
        "max_single_loss_before_pct": round(min((r["gross_pnl_pct"] for r in have_upside), default=0), 4),
        "max_single_loss_after_pct": round(min((r["gross_pnl_pct"] for r in kept), default=0), 4) if kept else "해당없음",
        "single_large_winner_or_loser_flag": single_large_winner_flag,
        "removed_symbols": ";".join(sorted({r["symbol"] for r in removed})),
        "removed_symbol_dates": ";".join(sorted({f"{r['symbol']}@{r['trade_date']}" for r in removed})),
    }


# ── Study A-2 (Sprint v1.2): F2 + 보조조건 2-condition 후보 비교 ─────
def two_condition_low_upside_candidates(rows: list[dict]) -> list[dict]:
    """2026-08-26 (Sprint v1.2, 민우님 GPT 검토 경유 지시): F2 baseline
    대비 정확히 2개의 2-조건 조합 후보만 비교합니다 — 최대 2개 조건
    까지만 허용(과거 데이터에 맞춘 추가 조합 생성 금지, 민우님 명시
    지시). 계산 로직은 _simulate_skip_candidate() 하나로 F0~F3과
    완전히 동일하게 공유합니다.

      - F2_baseline: upside_to_recent_high_pct < 0.50%
      - CandidateA : F2 AND rebound_volume_spike == False
      - CandidateB : F2 AND is_pulldown_recovery(PR 조건) == False

    rebound_volume_spike/is_pulldown_recovery가 결측(None, safe_bool
    파싱 실패 또는 entry_quality_shadow/signal_log 매칭 실패)인 거래는
    `is False`가 아니므로 Candidate A/B 스킵 대상에서 자동 제외됩니다
    — 결측을 False로 임의 대입하지 않습니다(추정 금지 원칙).
    """
    have_upside = [r for r in rows if r["upside_to_recent_high_pct"] is not None]

    missing_rvs = sum(1 for r in have_upside if _f2_pred(r) and r.get("rebound_volume_spike") is None)
    missing_pr = sum(1 for r in have_upside if _f2_pred(r) and r.get("is_pulldown_recovery") is None)

    out = [
        _simulate_skip_candidate(have_upside, "F2_baseline(upside<0.50%)", _f2_pred),
        _simulate_skip_candidate(have_upside, "CandidateA(F2 AND rebound_volume_spike==False)", _candidate_a_pred),
        _simulate_skip_candidate(have_upside, "CandidateB(F2 AND PR==False)", _candidate_b_pred),
    ]
    out[1]["missing_boolean_feature_count"] = missing_rvs
    out[2]["missing_boolean_feature_count"] = missing_pr
    out[0]["missing_boolean_feature_count"] = 0
    return out


# 2026-08-27 (Candidate A forward shadow): F2/CandidateA/CandidateB 판정
# 조건을 module-level 함수로 뽑아 candidate_cost_aware_metrics()와
# split_historical_forward() 이후의 run()에서도 재사용합니다 — 판정
# 로직 자체는 two_condition_low_upside_candidates()가 원래 쓰던 것과
# 완전히 동일합니다(순수 위치 이동, 행동 변화 없음).
def _f2_pred(r) -> bool:
    return r["upside_to_recent_high_pct"] < 0.50


def _candidate_a_pred(r) -> bool:
    return _f2_pred(r) and r.get("rebound_volume_spike") is False


def _candidate_b_pred(r) -> bool:
    return _f2_pred(r) and r.get("is_pulldown_recovery") is False


# 2026-08-26 (Sprint v1.3, Candidate G, 민우님 지시): F2/CandidateA/B와
# 달리 upside 문턱과 무관한 독립 조건입니다 — "갭 급등 후 눌림목"
# 패턴(갭눌림D, breakout_strategy.py의 cond_gap_pullback)이 나온 진입
# 자체를 후보로 봅니다. gap_pullback_d가 결측(None, entry_reason에
# 마커가 없거나 파싱 실패)이면 True로 추정하지 않고 자동으로 후보에서
# 제외됩니다(`is True`만 인정 — Candidate A/B의 `is False`와 동일한
# 추정 금지 관례).
def _candidate_g_pred(r) -> bool:
    return r.get("gap_pullback_d") is True


# ── Sprint v1.2 후속(2026-08-27): 비용 반영(base) 기준 precision/recall
# + 거래일별 delta breakdown, 그리고 historical/forward 표본 분리 ──────
def candidate_cost_aware_metrics(rows: list[dict], skip_pred, label: str) -> dict:
    """base_outcome(비용 반영) 기준 skip precision/recall/winner damage와
    거래일별 delta를 계산합니다.

    2026-08-27 (Candidate A forward shadow, 민우님 지시): 기존
    _simulate_skip_candidate()의 loser_removal_rate/winner_preservation_rate
    는 gross outcome 기준입니다 — "+0.3%짜리 거래는 gross로는 승자여도
    왕복비용을 내면 실제 전략에는 좋은 거래가 아닐 수 있다"는 지적에
    대응해 base_outcome(비용 반영 후 WIN/LOSS/BREAKEVEN) 기준으로
    별도 계산합니다. 세 지표는 서로 다른 질문에 답합니다:

      - skip_precision_base: 이 후보가 스킵한 거래 중 몇 %가 실제로
        base 기준 손실이었나 — "막겠다고 찍은 거래가 실제로 나쁜
        거래일 확률"(민우님 표현 그대로). Candidate A는 "모든 손실을
        잡는 필터"가 아니라 이 정밀도가 핵심인 필터에 가깝습니다.
      - loss_recall_base: 전체 base 손실 중 몇 %를 이 후보가 막았나
        (기존 loser_removal_rate의 base-cost 버전 — gross 기준과
        다를 수 있음, gross WIN이 비용 반영 후 base LOSS가 되는
        경우가 있기 때문).
      - winner_damage_base: base 기준 승자 중 몇 %를 잘못 막았나.

    거래일별 delta(per_day_base_delta_pct/krw)는 "그 날 하루의 거래만
    놓고 이 후보를 적용하면 base net이 얼마나 바뀌는가"를 봅니다 —
    candidate_leave_one_out()의 "그 날을 빼면"과는 다른 질문(그쪽은
    나머지 날짜 전체의 안정성, 이쪽은 그 날 하루의 기여도)입니다.
    """
    have_upside = [r for r in rows if r["upside_to_recent_high_pct"] is not None]
    removed = [r for r in have_upside if skip_pred(r)]

    removed_base_losses = [r for r in removed if r["base_outcome"] == LOSS]
    removed_base_wins = [r for r in removed if r["base_outcome"] == WIN]
    all_base_losses = [r for r in have_upside if r["base_outcome"] == LOSS]
    all_base_wins = [r for r in have_upside if r["base_outcome"] == WIN]

    skip_precision_base = (
        "해당없음(제거 대상 0건)" if not removed else
        f"{len(removed_base_losses)/len(removed)*100:.0f}%"
    )
    loss_recall_base = (
        "해당없음(base 손실 0건)" if not all_base_losses else
        f"{len(removed_base_losses)/len(all_base_losses)*100:.0f}%"
    )
    winner_damage_base = (
        "해당없음(base 승자 0건)" if not all_base_wins else
        f"{len(removed_base_wins)/len(all_base_wins)*100:.0f}%"
    )

    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in have_upside:
        by_day[r["trade_date"]].append(r)
    per_day_base_delta_pct: dict[str, float] = {}
    per_day_base_delta_krw: dict[str, float] = {}
    for d, grp in sorted(by_day.items()):
        kept_d = [r for r in grp if not skip_pred(r)]
        orig_d_pct = sum(r["base_net_pnl_pct"] for r in grp)
        kept_d_pct = sum(r["base_net_pnl_pct"] for r in kept_d)
        orig_d_krw = sum(r["base_net_pnl_krw"] for r in grp)
        kept_d_krw = sum(r["base_net_pnl_krw"] for r in kept_d)
        per_day_base_delta_pct[d] = round(kept_d_pct - orig_d_pct, 4)
        per_day_base_delta_krw[d] = round(kept_d_krw - orig_d_krw, 2)

    return {
        "candidate": label,
        "n_trades_in_scope": len(have_upside),
        "removed_trades": len(removed),
        "skip_precision_base": skip_precision_base,
        "loss_recall_base": loss_recall_base,
        "winner_damage_base": winner_damage_base,
        "per_day_base_delta_pct": per_day_base_delta_pct,
        "per_day_base_delta_krw": per_day_base_delta_krw,
    }


def split_historical_forward(rows: list[dict], forward_start_date: str) -> tuple[list[dict], list[dict]]:
    """trade_date(YYYYMMDD)를 기준으로 historical(그 이전)/forward(그날
    이후 포함)로 나눕니다.

    2026-08-27 (Candidate A forward shadow, 민우님 명시 지시): "8/20~
    8/25는 Candidate A를 만드는 데 쓰인 historical/backtest-like
    evidence이고, Candidate A shadow 적용 이후는 forward evidence —
    절대 한 덩어리로 섞어서 표본 수를 부풀리면 안 된다." forward가
    아직 비어 있어도(수집 이제 막 시작) 조용히 0건으로 보고할 뿐
    historical로 끌어와 채우지 않습니다.
    """
    historical = [r for r in rows if r["trade_date"] < forward_start_date]
    forward = [r for r in rows if r["trade_date"] >= forward_start_date]
    return historical, forward


def split_regimes_3way(rows: list[dict], hypothesis_forming_start: str,
                        true_forward_start: str) -> tuple[list[dict], list[dict], list[dict]]:
    """trade_date(YYYYMMDD) 기준 HISTORICAL/HYPOTHESIS_FORMING/TRUE_FORWARD
    3-way 분리 (2026-08-27, Sprint v1.3.1 methodology closure).

    split_historical_forward()의 2-way 분리를 대체하는 게 아니라(Candidate
    A/B/F2는 여전히 그쪽을 씁니다 — 위 CANDIDATE_REGIME_BOUNDARIES 주석
    참고), Candidate G/M1처럼 "이 후보를 만드는 데 실제로 들여다본 구간"이
    "이 후보가 완전히 고정된 뒤의 진짜 forward 구간"과 분리되어야 하는
    경우에만 씁니다. hypothesis_forming_start > true_forward_start이면
    설정 실수이므로 조용히 잘못 계산하지 않고 즉시 예외를 던집니다.
    """
    if hypothesis_forming_start > true_forward_start:
        raise ValueError(
            f"hypothesis_forming_start({hypothesis_forming_start})가 "
            f"true_forward_start({true_forward_start})보다 늦을 수 없습니다"
        )
    historical = [r for r in rows if r["trade_date"] < hypothesis_forming_start]
    hypothesis_forming = [
        r for r in rows if hypothesis_forming_start <= r["trade_date"] < true_forward_start
    ]
    true_forward = [r for r in rows if r["trade_date"] >= true_forward_start]
    return historical, hypothesis_forming, true_forward


def build_candidate_regime_cost_aware_report(
    rows: list[dict], skip_pred, candidate_name: str,
    hypothesis_forming_start: str, true_forward_start: str,
) -> list[dict]:
    """단일 후보(Candidate G 등)의 HISTORICAL/HYPOTHESIS_FORMING/TRUE_FORWARD/
    COMBINED(참고용) cost-aware 리포트를, 그 후보 고유의 regime 경계로
    계산합니다 (2026-08-27, Sprint v1.3.1 methodology closure).

    build_cost_aware_report()와 계산 로직 자체는 candidate_cost_aware_
    metrics() 하나를 그대로 재사용해 완전히 동일합니다 — 차이는 이
    함수가 F2/CandidateA/CandidateB/CandidateG를 한 번에 묶어 계산하지
    않고, 후보 하나만 그 후보 고유의 3-way 경계로 분리해서 계산한다는
    점뿐입니다(Candidate G가 Candidate A의 forward 경계를 공유하면 안
    된다는 이번 재closure의 핵심 지적을 반영).
    """
    historical, hypothesis_forming, true_forward = split_regimes_3way(
        rows, hypothesis_forming_start, true_forward_start)
    return [
        candidate_cost_aware_metrics(historical, skip_pred, f"{candidate_name}[HISTORICAL]"),
        candidate_cost_aware_metrics(hypothesis_forming, skip_pred, f"{candidate_name}[HYPOTHESIS_FORMING]"),
        candidate_cost_aware_metrics(true_forward, skip_pred, f"{candidate_name}[TRUE_FORWARD]"),
        candidate_cost_aware_metrics(rows, skip_pred, f"{candidate_name}[COMBINED(참고용)]"),
    ]


def build_cost_aware_report(rows: list[dict], regime_label: str) -> list[dict]:
    """F2/CandidateA/CandidateB/CandidateG 네 후보의 cost-aware 지표를 한
    regime(HISTORICAL/FORWARD/COMBINED) 범위에서 계산해 리스트로 반환합니다.

    2026-08-26 (Sprint v1.3, 민우님 지시): CandidateG("갭눌림D")를 추가.
    upside 문턱과 무관한 독립 조건이지만, candidate_cost_aware_metrics()의
    공통 유효 행 필터(upside_to_recent_high_pct 존재)는 "분석 가능한 행"을
    걸러내는 범용 기준이라 CandidateG에도 그대로 적용합니다 — F2 전용
    필터가 아니므로 조건 자체를 바꾸는 게 아닙니다.
    """
    return [
        candidate_cost_aware_metrics(rows, _f2_pred, f"F2_baseline[{regime_label}]"),
        candidate_cost_aware_metrics(rows, _candidate_a_pred, f"CandidateA[{regime_label}]"),
        candidate_cost_aware_metrics(rows, _candidate_b_pred, f"CandidateB[{regime_label}]"),
        candidate_cost_aware_metrics(rows, _candidate_g_pred, f"CandidateG[{regime_label}]"),
    ]


def flatten_cost_aware_reports(reports: list[dict]) -> tuple[list[dict], list[dict]]:
    """candidate_cost_aware_metrics()가 candidate별로 반환하는 결과를
    CSV로 쓸 수 있는 두 개의 flat 리스트로 변환합니다: candidate당 1행인
    summary(per_day 중첩 dict 제외)와 candidate x trade_date당 1행인
    per_day breakdown.

    2026-08-27 (Candidate A forward shadow, 민우님 지시 #6): historical/
    forward/combined 세 regime의 리스트를 이 함수에 그대로 이어붙여
    호출해도 candidate 라벨에 이미 regime이 포함돼 있어([HISTORICAL]/
    [FORWARD]/[COMBINED(참고용)]) 섞이지 않습니다.
    """
    summary_rows = []
    per_day_rows = []
    for rep in reports:
        summary_rows.append({
            "candidate": rep["candidate"],
            "n_trades_in_scope": rep["n_trades_in_scope"],
            "removed_trades": rep["removed_trades"],
            "skip_precision_base": rep["skip_precision_base"],
            "loss_recall_base": rep["loss_recall_base"],
            "winner_damage_base": rep["winner_damage_base"],
        })
        pct_map = rep["per_day_base_delta_pct"]
        krw_map = rep["per_day_base_delta_krw"]
        for d in sorted(pct_map.keys()):
            per_day_rows.append({
                "candidate": rep["candidate"],
                "trade_date": d,
                "per_day_base_delta_pct": pct_map[d],
                "per_day_base_delta_krw": krw_map[d],
            })
    return summary_rows, per_day_rows


# ── Study B: 5-Min Exit Timing ───────────────────────────────────
def _label_trade(r: dict) -> dict:
    NEUTRAL_BAND_PCT = 0.10
    actual_base = r["base_net_pnl_pct"]
    fwd5_base = r["fwd5m_base_net_pct"]
    delta = fwd5_base - actual_base
    if abs(delta) < NEUTRAL_BAND_PCT:
        label = "NEUTRAL"
    elif delta > 0:
        label = "EXTEND_HELPED"
    else:
        label = "EXTEND_HURT"
    return {
        "trade_date": r["trade_date"], "symbol": r["symbol"], "exit_reason": r["exit_reason"],
        "entry_watch_trigger_type": r["entry_watch_trigger_type"],
        "actual_gross_pnl_pct": r["gross_pnl_pct"], "actual_base_net_pct": actual_base,
        "actual_stress_net_pct": r["stress_net_pnl_pct"],
        "fwd5m_price_return_pct": r["fwd5m_price_return_pct"],
        "fwd5m_base_net_pct": r["fwd5m_base_net_pct"],
        "fwd10m_price_return_pct": r["fwd10m_price_return_pct"],
        "fwd10m_base_net_pct": r["fwd10m_base_net_pct"],
        "fwd20m_price_return_pct": r["fwd20m_price_return_pct"],
        "fwd20m_base_net_pct": r["fwd20m_base_net_pct"],
        "best_forward_price_return_pct": max(
            v for v in (r["fwd5m_price_return_pct"], r["fwd10m_price_return_pct"], r["fwd20m_price_return_pct"])
            if v not in ("", None)
        ),
        "worst_forward_price_return_pct": min(
            v for v in (r["fwd5m_price_return_pct"], r["fwd10m_price_return_pct"], r["fwd20m_price_return_pct"])
            if v not in ("", None)
        ),
        "extension_label_5m": label,
        "delta_vs_actual_base_5m_pct": round(delta, 4),
        "entry_score": r["entry_score"],
        "current_vs_vwap_pct": r["current_vs_vwap_pct"],
        "macd_above_signal": r["macd_above_signal"],
        # 2026-08-26 (Sprint v1.3, Candidate M1): "5분 판단 시점" checkpoint
        # 값 — 위 current_vs_vwap_pct/macd_above_signal(진입 시점 값, R1/R2가
        # 참조)과 별개로 그대로 통과시킵니다. extension_rule_candidates()의
        # M1 규칙이 이 필드로 진입 시점이 아닌 청산 판단 순간의 VWAP/MACD
        # 상태를 검증합니다. 미매칭이면 build_trade_features()에서 이미
        # ""(FEATURE_COLUMNS 초기값)로 남아있고, 여기서도 추정하지 않고
        # 그대로 전달합니다.
        "checkpoint_price_vs_vwap_pct": r["checkpoint_price_vs_vwap_pct"],
        "checkpoint_macd_above_signal": r["checkpoint_macd_above_signal"],
        "checkpoint_peak_pnl_pct": r["checkpoint_peak_pnl_pct"],
    }


def exit_extension_study(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """entry_watch 청산 중 유효한 forward counterfactual이 있는 거래에
    EXTEND_HELPED/EXTEND_HURT/NEUTRAL 라벨을 매깁니다.

    2026-08-24 (민우님 코드/CSV 직접 대조 리뷰 2번 지적 반영, 핵심 수정):
    Sprint v1은 "entry_watch " 접두사만으로 급락청산/VWAP이탈청산/
    최소수익미달청산을 한 그룹(ew)으로 섞었습니다. 그런데 우리가 답해야
    할 질문은 "5분 minimum-profit 타이머를 연장할 것인가"이고, 1~4분
    시점 VWAP 조기청산을 5분 더 들고 있었으면 어땠는지를 보는 것은
    "VWAP 위험청산을 무시하고 더 들고 있기"라는 **다른 전략 질문**
    입니다. 이제 entry_watch_trigger_type으로 명확히 분리합니다:

      - MIN_PROFIT_5M   → 조건부 연장 정책의 유일한 근거 표본
                          (extension_rule_candidates가 이 그룹만 사용)
      - EARLY_VWAP_EXIT → 별도 진단용으로만 반환. 조건부 연장 후보
                          계산에는 절대 쓰이지 않음(다른 질문이므로).
      - CRASH_CUT/그 외 → 이 스터디 대상 아님(제외).

    반환값이 (per_trade_min_profit, per_trade_early_vwap, excluded_note)
    3-tuple로 바뀌었습니다(기존 2-tuple에서 변경) — 호출부(run())도 함께
    수정했습니다.
    """
    min_profit_rows = [
        r for r in rows
        if r["entry_watch_trigger_type"] == TRIGGER_MIN_PROFIT_5M
        and r["fwd5m_price_return_pct"] not in ("", None)
    ]
    early_vwap_rows = [
        r for r in rows
        if r["entry_watch_trigger_type"] == TRIGGER_EARLY_VWAP_EXIT
        and r["fwd5m_price_return_pct"] not in ("", None)
    ]

    per_trade_min_profit = [_label_trade(r) for r in min_profit_rows]
    per_trade_early_vwap = [_label_trade(r) for r in early_vwap_rows]

    excluded_no_cf = [
        r for r in rows
        if r["entry_watch_trigger_type"] in (TRIGGER_MIN_PROFIT_5M, TRIGGER_EARLY_VWAP_EXIT)
        and r["fwd5m_price_return_pct"] in ("", None)
    ]
    excluded_note = [
        {"trade_date": r["trade_date"], "symbol": r["symbol"], "exit_reason": r["exit_reason"],
         "entry_watch_trigger_type": r["entry_watch_trigger_type"], "reason": r["fwd_data_source"]}
        for r in excluded_no_cf
    ]
    return per_trade_min_profit, per_trade_early_vwap, excluded_note


def extension_rule_candidates(per_trade: list[dict]) -> list[dict]:
    """조건부 5분 연장 후보 R1~R3을 계산합니다.

    호출부(run())는 이제 이 함수에 exit_extension_study()가 반환한
    per_trade_min_profit(MIN_PROFIT_5M만, EARLY_VWAP_EXIT 제외)만
    넘깁니다 — 2026-08-24 (민우님 리뷰 2번).

    2026-08-24 (민우님 리뷰 3번 지적, 중요): R1/R2가 참조하는
    current_vs_vwap_pct/macd_above_signal은 이 per_trade 딕셔너리에
    "판단 순간"이 아니라 **매수 진입 시점** 값으로 채워져 있습니다
    (trade_feature_table.py의 build_trade_features()가 entry_quality_
    shadow/signal_log를 매수 시점 기준으로만 조인하기 때문 — 5분 청산
    판단 순간의 재조회 값이 아님). 즉 R1은 실제로는 "5분 시점 PnL>0
    AND 진입 당시 VWAP 위"를, R2는 "5분 시점 PnL>0 AND 진입 당시 MACD
    상태"를 검증한 것이지, 원래 의도("5분 시점 PnL>0 AND 5분 시점
    VWAP/MACD 상태")를 검증한 게 아닙니다. 이건 코드를 잘못 짠 게
    아니라 필요한 "5분 시점 checkpoint feature"가 이 CSV들에 애초에
    없기 때문입니다 — Profitability Shadow v2의 MIN_PROFIT_EXTENSION_
    SHADOW가 정확히 이 공백(판단 순간의 price_vs_vwap_pct/macd/
    macd_signal)을 새로 기록하기 시작합니다.
    그래서 R1/R2는 valid_evidence=False로 표시하고, 이번 라운드
    OBSERVE(n=1) 결과를 전략 후보 근거로 사용하지 않습니다(민우님
    확정). R3(entry_score>=5 AND pnl>0)은 entry_score 자체가 원래
    진입 시점에만 존재하는 값이라 이 문제에 해당하지 않습니다 —
    valid_evidence=True로 유지합니다.

    2026-08-26 (Sprint v1.3, Candidate M1, 민우님 지시): R1/R2가 안고
    있던 바로 그 결함("판단 순간"이 아니라 진입 시점 값을 쓴다는 것)을
    고친 버전입니다. min_profit_extension_shadow.csv에서 조인한
    checkpoint_price_vs_vwap_pct/checkpoint_macd_above_signal은
    _check_entry_watch()가 실제로 최소수익미달청산을 판단하던 바로 그
    순간의 값입니다(build_trade_features()의 join 주석 참고) — 그래서
    M1은 valid_evidence=True입니다. checkpoint 필드가 결측(매칭 실패
    또는 이 청산이 애초에 min_profit_extension_shadow 대상이 아님)이면
    `(x or 0) > 0`/`is True` 판정이 자연히 False가 되어 조건 미충족으로
    빠집니다 — False로 추정해서 채워 넣는 게 아니라 결측이 그냥 탈락으로
    이어지는 것뿐입니다(A/B/G와 동일한 무추정 관례).

    2026-08-26 재closure(민우님 리뷰 지적, 의미 변경 1건): M1의 고정
    조건은 "MIN_PROFIT_5M AND price>VWAP AND MACD>signal"이지
    R1/R2/R3처럼 `actual_gross_pnl_pct > 0` 게이트가 없습니다. 최초
    구현은 R1/R2의 "5분 시점 pnl>0 AND ..." 패턴을 그대로 따라
    이 게이트를 붙였는데, 이는 M1을 만들게 한 핵심 회복 사례(8/25
    052690 -0.68%, 8/26 003490 -0.10%, 8/26 006360 -0.43% — 전부
    checkpoint 시점엔 VWAP 위·MACD 상방이었지만 실제 gross는 음수)를
    전부 제외해버리는 결함이었습니다. M1이 답하려는 질문 자체가
    "지금 당장 마이너스여도 checkpoint 기술적 상태가 좋으면 5분
    더 들고 있는 게 회복에 도움이 되는가"이므로, pnl 부호는 애초에
    이 규칙의 게이트가 아닙니다(대신 실제 도움이 됐는지는
    `extension_label_5m`(EXTEND_HELPED/HURT/NEUTRAL)이 이미 별도로
    판정합니다 — M1 예선 통과 여부와 결과 라벨은 서로 다른 질문).
    R1/R2/R3의 `actual_gross_pnl_pct > 0` 게이트는 그 규칙들의
    고정 정의 그대로이므로 이번 재closure에서 손대지 않습니다.

    2026-08-27 재closure 2(Sprint v1.3.1, methodology closure, 민우님
    지시): sample_tier 명칭을 PROMISING/SHADOW_READY에서 중립적인
    evidence label로 바꿨습니다 — 아래 _evidence_tier() 참고. 기존
    명칭은 표본 크기·방향 혼재 여부를 표현한 것이지 성과를 표현한 게
    아닌데, "PROMISING"/"SHADOW_READY"라는 단어 자체가 긍정적으로
    읽혀 오해를 삽니다(특히 n>=5인데 방향이 전부 EXTEND_HURT로
    일관되면 옛 로직은 "SHADOW_READY"를 찍었습니다 — 성과가 전부
    나쁜데 좋아 보이는 이름이 붙는 경우였습니다).
    """
    def qualifies_rule_a(t):  # 5분 시점 pnl>0 AND "진입 시점" price>VWAP(주의: 아래 docstring 참고)
        return t["actual_gross_pnl_pct"] > 0 and (t["current_vs_vwap_pct"] or 0) > 0

    def qualifies_rule_b(t):  # 5분 시점 pnl>0 AND "진입 시점" MACD>signal(주의: 아래 docstring 참고)
        return t["actual_gross_pnl_pct"] > 0 and t["macd_above_signal"] is True

    def qualifies_rule_c(t):  # entry_score >= 5 AND 5분 시점 pnl > 0 (entry_score는 원래 진입시점 값)
        return (t["entry_score"] or 0) >= 5 and t["actual_gross_pnl_pct"] > 0

    def qualifies_rule_m1(t):  # "5분 판단 시점(checkpoint)" price>VWAP AND MACD>signal — pnl 게이트 없음(위 재closure 참고)
        return (
            (t["checkpoint_price_vs_vwap_pct"] or 0) > 0
            and t["checkpoint_macd_above_signal"] is True
        )

    def _evidence_tier(qualified: list[dict]) -> str:
        """2026-08-27 (Sprint v1.3.1, methodology closure, 민우님 지시):
        성과를 암시하지 않는 중립적 evidence label. n<5는 표본 부족을
        그대로 이름에 담고(OBSERVE_N_LT_5), n>=5부터는 방향이 일관되면
        DIRECTIONAL_HELPED/DIRECTIONAL_HURT/DIRECTIONAL_NEUTRAL, 방향이
        섞여 있으면 MIXED_EVIDENCE — 어떤 조합에서도 "좋다"고 읽히는
        단어(PROMISING/READY/GOOD 등)가 나오지 않습니다. 특히 전부
        EXTEND_HURT인 경우 반드시 DIRECTIONAL_HURT가 되어야 하고, 절대
        긍정적으로 읽히는 이름이 붙지 않아야 합니다(민우님 명시 요구
        — test_profitability_sprint.py의 회귀 테스트가 이를 고정합니다).
        """
        n = len(qualified)
        if n < 5:
            return "OBSERVE_N_LT_5"
        directions = {t["extension_label_5m"] for t in qualified}
        if directions == {"EXTEND_HELPED"}:
            return "DIRECTIONAL_HELPED"
        if directions == {"EXTEND_HURT"}:
            return "DIRECTIONAL_HURT"
        if directions == {"NEUTRAL"}:
            return "DIRECTIONAL_NEUTRAL"
        return "MIXED_EVIDENCE"

    rules = [
        ("R1_pnl>0_AND_price>VWAP", qualifies_rule_a, False),
        ("R2_pnl>0_AND_MACD_above_signal", qualifies_rule_b, False),
        ("R3_entry_score>=5_AND_pnl>0", qualifies_rule_c, True),
        ("M1_5minCheckpoint_price>VWAP_AND_MACD_above_signal", qualifies_rule_m1, True),
    ]
    out = []
    for name, pred, valid_evidence in rules:
        qualified = [t for t in per_trade if pred(t)]
        helped = [t for t in qualified if t["extension_label_5m"] == "EXTEND_HELPED"]
        hurt = [t for t in qualified if t["extension_label_5m"] == "EXTEND_HURT"]
        neutral = [t for t in qualified if t["extension_label_5m"] == "NEUTRAL"]
        actual_base = sum(t["actual_base_net_pct"] for t in qualified)
        hypo_base = sum(t["fwd5m_base_net_pct"] for t in qualified)
        actual_stress = sum(t["actual_stress_net_pct"] for t in qualified)
        hypo_stress = sum(
            COST_MODEL.net(t["fwd5m_price_return_pct"], STRESS_SCENARIO) for t in qualified
        )
        worst_case = min((t["fwd5m_base_net_pct"] - t["actual_base_net_pct"] for t in qualified), default=None)
        n = len(qualified)
        tier = _evidence_tier(qualified)
        if not valid_evidence:
            # 2026-08-24 (민우님 리뷰 3번): feature 시점 불일치(진입 시점
            # 값을 5분 시점 값처럼 취급) — 표본 크기와 무관하게 전략
            # 후보 근거로 사용 불가. 계산값 자체는 참고로 남겨두되
            # sample_tier를 명시적으로 무효 처리합니다.
            tier = f"INVALID(feature 시점 불일치 — 진입 시점 값을 5분 시점 값처럼 사용함, 근거로 사용 안 함; 계산상 tier는 {tier})"
        out.append({
            "rule": name,
            "valid_evidence": valid_evidence,
            "extension_qualified_trades": n,
            "helped": len(helped),
            "hurt": len(hurt),
            "neutral": len(neutral),
            "actual_base_net_pct_sum": round(actual_base, 4),
            "hypothetical_base_net_pct_sum": round(hypo_base, 4),
            "base_delta_pct": round(hypo_base - actual_base, 4),
            "actual_stress_net_pct_sum": round(actual_stress, 4),
            "hypothetical_stress_net_pct_sum": round(hypo_stress, 4),
            "stress_delta_pct": round(hypo_stress - actual_stress, 4),
            "worst_case_degradation_pct": round(worst_case, 4) if worst_case is not None else "해당없음",
            "sample_tier": tier if n > 0 else "표본없음(적용 후보 아님, 관측 불가)",
        })
    return out


def extension_rule_candidates_by_regime(per_trade: list[dict], hypothesis_forming_start: str,
                                         true_forward_start: str) -> list[dict]:
    """R1~R3/M1 조건부 연장 후보를 HISTORICAL/HYPOTHESIS_FORMING/
    TRUE_FORWARD/COMBINED(참고용) regime으로 나눠 계산합니다.

    2026-08-26 (Sprint v1.3, 민우님 지시): build_cost_aware_report()의
    regime 분리와 동일한 원칙 — "Candidate M1은 forward 실거래 표본이
    아직 clean 4건뿐이라 historical/backtest-like 표본과 절대 한
    덩어리로 섞어서 표본 수를 부풀리면 안 된다"(민우님 지적, Candidate
    A 때와 동일한 원칙을 M1에도 그대로 적용).

    2026-08-27 재closure(Sprint v1.3.1, methodology closure, 민우님
    지시): 기존 2-way(HISTORICAL/FORWARD)는 forward_start_date 하나로
    Candidate A와 같은 경계(20260826)를 썼는데, 이는 M1을 만드는 데
    실제로 쓰인 8/26 데이터(003490/006360 — qualifies_rule_m1()의 pnl
    게이트 버그를 잡는 데도 쓰인 바로 그 날짜)를 "forward 증거"로
    잘못 포함시켰습니다. 3-way로 바꿔 hypothesis_forming_start(이
    후보를 만들거나 디버깅하는 데 들여다본 구간의 시작)와
    true_forward_start(후보가 완전히 고정된 뒤의 진짜 forward 시작)를
    분리했습니다 — 호출부(run())는 CANDIDATE_REGIME_BOUNDARIES["M1"]의
    두 값을 넘깁니다. split_historical_forward()는 raw feature
    row(trade_date 키 보유)를 대상으로 하지만, 이 함수의 입력 per_trade는
    _label_trade()가 만든 딕셔너리입니다 — trade_date 키를 그대로
    보존하고 있으므로 split_regimes_3way()를 그대로 재사용할 수
    있습니다(새 비교 기준을 만들지 않고 기존 관례 재사용).
    """
    historical, hypothesis_forming, true_forward = split_regimes_3way(
        per_trade, hypothesis_forming_start, true_forward_start)

    def _tag(rule_rows: list[dict], regime_label: str) -> list[dict]:
        for r in rule_rows:
            r["rule"] = f"{r['rule']}[{regime_label}]"
        return rule_rows

    return (
        _tag(extension_rule_candidates(historical), "HISTORICAL")
        + _tag(extension_rule_candidates(hypothesis_forming), "HYPOTHESIS_FORMING")
        + _tag(extension_rule_candidates(true_forward), "TRUE_FORWARD")
        + _tag(extension_rule_candidates(per_trade), "COMBINED(참고용)")
    )


# ── Study C: Entry Quality gates ─────────────────────────────────
GATE_COLUMNS = [
    "would_block_macd_dead_min_score5",
    "would_block_macd_above_signal_required",
    "would_block_pr_or_pullback_condition_rolling_vwap",
    "would_block_pr_or_pullback_condition_session_vwap",
]


def entry_quality_gate_study(rows: list[dict]) -> list[dict]:
    have_gate_data = [r for r in rows if r["data_quality_flag"].find("entry_quality_shadow order_id 매칭 실패") == -1]
    out = []
    for gate in GATE_COLUMNS:
        would_block = [r for r in have_gate_data if r.get(gate) is True]
        n = len(would_block)
        blocked_w = [r for r in would_block if r["outcome"] == WIN]
        blocked_b = [r for r in would_block if r["outcome"] == BREAKEVEN]
        blocked_l = [r for r in would_block if r["outcome"] == LOSS]
        all_losses = [r for r in have_gate_data if r["outcome"] == LOSS]
        all_wins = [r for r in have_gate_data if r["outcome"] == WIN]
        loser_removal_rate = (
            "해당없음" if not all_losses else f"{len(blocked_l)/len(all_losses)*100:.0f}%"
        )
        winner_damage_rate = (
            "해당없음" if not all_wins else f"{len(blocked_w)/len(all_wins)*100:.0f}%"
        )
        base_delta = -sum(r["base_net_pnl_pct"] for r in would_block)
        stress_delta = -sum(r["stress_net_pnl_pct"] for r in would_block)
        verdict = "관측 대상 실행 거래 0건(would_block=0에 가까움) — 효과 없음/threshold too weak/관측만" if n == 0 else (
            f"OBSERVE(n={n}, 실제로 이 게이트가 걸린 실행 거래 존재 — 방향성 판단은 이르지만 주시 필요)"
        )
        out.append({
            "gate": gate,
            "would_block_trade_count": n,
            "blocked_winners": len(blocked_w),
            "blocked_breakevens": len(blocked_b),
            "blocked_losers": len(blocked_l),
            "loser_removal_rate": loser_removal_rate,
            "winner_damage_rate": winner_damage_rate,
            "base_net_pnl_pct_delta_if_enforced": round(base_delta, 4),
            "stress_net_pnl_pct_delta_if_enforced": round(stress_delta, 4),
            "verdict": verdict,
        })
    return out


# ── Leave-one-out / sensitivity (후보별) ──────────────────────────
def candidate_leave_one_out(rows: list[dict], skip_pred, label: str) -> dict:
    """특정 필터 후보(skip_pred로 정의)의 base_net_pnl_pct_delta가
    개별 거래 하나, 혹은 거래일 하나에 얼마나 좌우되는지 계산합니다.
    '이 후보의 효과가 특정 1개 거래/거래일에 전부 의존하는가?'에 답하기
    위한 것으로, 전체 포트폴리오 leave-one-out(leave_one_out_report)과는
    다른 질문입니다."""
    pool = [r for r in rows if r["upside_to_recent_high_pct"] is not None]
    if not pool:
        return {"candidate": label, "note": "표본 없음"}

    def delta_for(subset):
        kept = [r for r in subset if not skip_pred(r)]
        orig = sum(r["base_net_pnl_pct"] for r in subset)
        kept_sum = sum(r["base_net_pnl_pct"] for r in kept)
        return round(kept_sum - orig, 4)

    full_delta = delta_for(pool)
    per_trade_removed = {}
    for r in pool:
        remaining = [x for x in pool if x is not r]
        per_trade_removed[f"exclude_{r['symbol']}_{r['trade_date']}"] = delta_for(remaining)

    by_day = defaultdict(list)
    for r in pool:
        by_day[r["trade_date"]].append(r)
    per_day_removed = {}
    for d in by_day:
        remaining = [r for r in pool if r["trade_date"] != d]
        per_day_removed[f"exclude_day_{d}"] = delta_for(remaining) if remaining else "전량 제거됨(하루짜리 표본)"

    signs = [v for v in per_day_removed.values() if isinstance(v, (int, float))]
    sign_flips = len({(v > 0) for v in signs + [full_delta]}) > 1 if signs else None

    # 2026-08-26 (Sprint v1.2, 민우님 명시 지시): "leave-one-best-trade-out
    # 결과, leave-one-worst-trade-out 결과"를 별도 필드로 명시 — 기존
    # per_trade_excluded_delta에 이미 모든 거래 각각의 제외 delta가
    # 들어있으므로(전체를 다 도는 것이 더 엄격한 상위 집합), 그 중
    # gross_pnl_pct 기준 최고/최저 거래 하나만 골라 명시적으로
    # 뽑아냅니다(leave_one_out_report의 best/worst 정의와 동일 기준).
    best = max(pool, key=lambda r: r["gross_pnl_pct"])
    worst = min(pool, key=lambda r: r["gross_pnl_pct"])
    best_key = f"exclude_{best['symbol']}_{best['trade_date']}"
    worst_key = f"exclude_{worst['symbol']}_{worst['trade_date']}"
    return {
        "candidate": label,
        "full_base_net_pnl_pct_delta": full_delta,
        "per_trade_excluded_delta": per_trade_removed,
        "per_day_excluded_delta": per_day_removed,
        "n_trading_days": len(by_day),
        "day_dependent_sign_flip": sign_flips,
        "leave_one_best_trade_out": {
            "excluded_trade": f"{best['symbol']}/{best['trade_date']} (gross {best['gross_pnl_pct']:+.2f}%)",
            "delta_after_exclusion": per_trade_removed[best_key],
        },
        "leave_one_worst_trade_out": {
            "excluded_trade": f"{worst['symbol']}/{worst['trade_date']} (gross {worst['gross_pnl_pct']:+.2f}%)",
            "delta_after_exclusion": per_trade_removed[worst_key],
        },
        "verdict": (
            "날짜 1개뿐 — 거래일 안정성 판단 불가" if len(by_day) < 2 else
            ("⚠ 특정 거래일을 빼면 delta 부호가 뒤집힘 — 날짜 의존적, 강한 후보 아님" if sign_flips else
             "거래일을 하나씩 빼도 delta 부호가 유지됨")
        ),
    }


def leave_one_out_report(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"candidate": label, "note": "표본 없음"}
    base_sum = sum(r["base_net_pnl_pct"] for r in rows)
    best = max(rows, key=lambda r: r["gross_pnl_pct"])
    worst = min(rows, key=lambda r: r["gross_pnl_pct"])
    without_best = [r for r in rows if r is not best]
    without_worst = [r for r in rows if r is not worst]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["trade_date"]].append(r)
    per_day = {}
    for d, grp in by_day.items():
        remaining = [r for r in rows if r["trade_date"] != d]
        per_day[f"exclude_{d}"] = round(sum(r["base_net_pnl_pct"] for r in remaining), 4) if remaining else "전량 제거됨"
    return {
        "candidate": label,
        "n": len(rows),
        "base_net_pnl_pct_sum_full": round(base_sum, 4),
        "base_net_pnl_pct_sum_excl_best_trade": round(sum(r["base_net_pnl_pct"] for r in without_best), 4),
        "best_trade": f"{best['symbol']}/{best['trade_date']} {best['gross_pnl_pct']:+.2f}%",
        "base_net_pnl_pct_sum_excl_worst_trade": round(sum(r["base_net_pnl_pct"] for r in without_worst), 4),
        "worst_trade": f"{worst['symbol']}/{worst['trade_date']} {worst['gross_pnl_pct']:+.2f}%",
        "per_day_exclusion": per_day,
        "n_trading_days": len(by_day),
        "stability_verdict": (
            "단일 거래일 의존(하루만 있음) — 날짜별 안정성 판단 불가" if len(by_day) < 2 else
            "여러 거래일 존재 — 날짜별 부호 일관성 참고 가능(본문 해설 참고)"
        ),
    }


# ── Scorecard ─────────────────────────────────────────────────────
def build_scorecard(low_upside_candidates, extension_rules, gate_study) -> list[dict]:
    """통합 scorecard — 2026-08-24 (민우님 리뷰 3번): %p 델타뿐 아니라
    금액(KRW) 델타도 함께 표시합니다(base_delta_krw/stress_delta_krw).
    Low Upside 후보는 candidate 문자열에 이미 "(주 후보)"/"(보조 비교용)"
    표기가 붙어 있어 정렬만으로 F2가 먼저 나옵니다(low_upside_filter_
    candidates가 F2를 F1보다 먼저 반환하도록 이미 순서를 바꿨습니다).
    """
    rows = []
    for c in low_upside_candidates:
        if c["candidate"].startswith("F0"):
            continue
        n = c["removed_trades"]
        tier = "OBSERVE" if n < 5 else ("PROMISING" if n < 8 else "SHADOW_READY")
        rows.append({
            "study": "Low Upside",
            "candidate": c["candidate"],
            "n_affected": n,
            "removed_w_b_l": f"{c['removed_winners']}/{c['removed_breakevens']}/{c['removed_losers']}",
            "base_delta_pct": c["base_net_pnl_pct_delta"],
            "stress_delta_pct": c["stress_net_pnl_pct_delta"],
            "base_delta_krw": c["base_net_delta_krw"],
            "stress_delta_krw": c["stress_net_delta_krw"],
            "winner_damage": "HIGH" if c["removed_winners"] else "LOW",
            "loser_removal": c["loser_removal_rate"],
            "stability": "LOW(단일 종목/거래일 의존 가능성 — 본문 leave-one-out 참고)",
            "tail_risk": c["single_large_winner_or_loser_flag"] or "명시적 tail 악화 신호 없음",
            "implementation_complexity": "LOW(기존 upside_to_recent_high_pct 필드 재사용, 단일 threshold)",
            "verdict": tier,
        })
    for r in extension_rules:
        rows.append({
            "study": "5-Min Exit",
            "candidate": r["rule"] + ("" if r["valid_evidence"] else " [INVALID EVIDENCE]"),
            "n_affected": r["extension_qualified_trades"],
            "removed_w_b_l": f"helped={r['helped']}/hurt={r['hurt']}/neutral={r['neutral']}",
            "base_delta_pct": r["base_delta_pct"],
            "stress_delta_pct": r["stress_delta_pct"],
            "base_delta_krw": "",  # 2026-08-24: Study B는 아직 %p 기준만 — 표본이 1~2건뿐이라
                                    # KRW 환산까지 더하면 정밀해 보이는 착시만 커짐(민우님 원칙: "가짜 정밀도 금지")
            "stress_delta_krw": "",
            "winner_damage": "해당없음(제거가 아니라 연장 실험)",
            "loser_removal": "해당없음(제거가 아니라 연장 실험)",
            "stability": "LOW(표본 매우 작음)",
            "tail_risk": f"worst-case degradation {r['worst_case_degradation_pct']}",
            "implementation_complexity": "MEDIUM(entry_watch 청산 로직 분기 추가 필요)",
            "verdict": r["sample_tier"] if r["valid_evidence"] else (
                "INVALID(feature 시점 불일치 — 전략 후보 근거로 사용 안 함; 판단 보류, 데이터부터 재수집)"
            ),
        })
    for g in gate_study:
        rows.append({
            "study": "Entry Quality",
            "candidate": g["gate"],
            "n_affected": g["would_block_trade_count"],
            "removed_w_b_l": f"{g['blocked_winners']}/{g['blocked_breakevens']}/{g['blocked_losers']}",
            "base_delta_pct": g["base_net_pnl_pct_delta_if_enforced"],
            "stress_delta_pct": g["stress_net_pnl_pct_delta_if_enforced"],
            "base_delta_krw": "",
            "stress_delta_krw": "",
            "winner_damage": "LOW" if g["blocked_winners"] == 0 else "MEDIUM",
            "loser_removal": g["loser_removal_rate"],
            "stability": "판단불가(n=0)" if g["would_block_trade_count"] == 0 else "LOW(표본 작음)",
            "tail_risk": "해당없음",
            "implementation_complexity": "LOW(이미 shadow로 계산되는 값, threshold만 enforce로 전환)",
            "verdict": "OBSERVE(효과 없음/threshold too weak)" if g["would_block_trade_count"] == 0 else "OBSERVE",
        })
    return rows


# ── CSV 출력 ──────────────────────────────────────────────────────
def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ── main ──────────────────────────────────────────────────────────
def run(bundle_dirs: list[str], out_dir: str) -> dict:
    days = []
    for d in bundle_dirs:
        days.append(load_bundle_day(Path(d)))
    days.sort(key=lambda d: d.date)

    feature_rows, warnings = build_trade_features(days)
    low_upside_buckets = low_upside_bucket_study(feature_rows)
    low_upside_candidates = low_upside_filter_candidates(feature_rows)
    # 2026-08-24 (민우님 리뷰 2번): MIN_PROFIT_5M과 EARLY_VWAP_EXIT을
    # 분리 — 조건부 연장 후보(extension_rule_candidates)는 반드시
    # MIN_PROFIT_5M만 사용합니다. EARLY_VWAP_EXIT은 진단용으로만 반환.
    extension_per_trade_min_profit, extension_per_trade_early_vwap, extension_excluded = (
        exit_extension_study(feature_rows)
    )
    extension_rules = extension_rule_candidates(extension_per_trade_min_profit)
    # 2026-08-26 (Sprint v1.3, Candidate M1, 민우님 지시): R1~R3/M1을
    # historical/forward로 분리 집계 — Candidate A의 cost-aware regime
    # 분리와 동일한 원칙(forward 표본을 historical과 섞어 부풀리지 않음).
    # 2026-08-27 재closure(Sprint v1.3.1, methodology closure): M1 고유의
    # 3-way 경계(CANDIDATE_REGIME_BOUNDARIES["M1"])를 씁니다 — Candidate A
    # 경계(20260826)를 그대로 쓰면 M1을 만드는 데 실제로 쓰인 8/26
    # 데이터(003490/006360)가 forward 증거로 잘못 섞입니다.
    extension_rules_by_regime = extension_rule_candidates_by_regime(
        extension_per_trade_min_profit,
        CANDIDATE_REGIME_BOUNDARIES["M1"]["hypothesis_forming_start"],
        CANDIDATE_REGIME_BOUNDARIES["M1"]["true_forward_start"],
    )
    gate_study = entry_quality_gate_study(feature_rows)
    scorecard = build_scorecard(low_upside_candidates, extension_rules, gate_study)

    # 2026-08-24 (민우님 확정): F2가 주 후보이므로 leave-one-out도 F2를
    # 먼저 계산 — F1/F3은 계속 보조 비교용으로 남깁니다.
    loo_f2 = candidate_leave_one_out(
        feature_rows, lambda r: r["upside_to_recent_high_pct"] < 0.50, "F2_skip_upside<0.50%(주 후보)")
    loo_f1 = candidate_leave_one_out(
        feature_rows, lambda r: r["upside_to_recent_high_pct"] < 1.00, "F1_skip_upside<1.00%(보조 비교용)")
    loo_f3 = candidate_leave_one_out(
        feature_rows, lambda r: r["upside_to_recent_high_pct"] < 0.25, "F3_skip_upside<0.25%(보조 비교용)")

    # 2026-08-26 (Sprint v1.2, 민우님 GPT 검토 경유 지시): F2 baseline
    # 대비 2-condition 후보(Candidate A/B) 비교 — 정확히 이 둘만,
    # 최대 2개 조건까지만(민우님 명시 지시). enforce는 이번 세션에
    # 하지 않고 결과만 계산합니다.
    two_condition_candidates = two_condition_low_upside_candidates(feature_rows)

    def _rvs_false(r):
        return r["upside_to_recent_high_pct"] < 0.50 and r.get("rebound_volume_spike") is False

    def _pr_false(r):
        return r["upside_to_recent_high_pct"] < 0.50 and r.get("is_pulldown_recovery") is False

    loo_cand_a = candidate_leave_one_out(feature_rows, _rvs_false, "CandidateA(F2 AND rebound_volume_spike==False)")
    loo_cand_b = candidate_leave_one_out(feature_rows, _pr_false, "CandidateB(F2 AND PR==False)")

    # 2026-08-27 (Candidate A forward shadow, 민우님 지시 #6): historical
    # (Candidate A를 도출하는 데 쓰인 8/20~8/25 근방 표본)과 forward(
    # CANDIDATE_A_FORWARD_START_DATE 이후 실제 shadow 관측 표본)를
    # 분리해서 별도 집계합니다 — 절대 하나로 합쳐서 표본 수를 부풀리지
    # 않습니다. COMBINED는 참고용으로만 별도 표기(enforce 판단에는
    # forward 증거를 최우선으로 둡니다).
    historical_rows, forward_rows = split_historical_forward(feature_rows, CANDIDATE_A_FORWARD_START_DATE)
    # 2026-08-27 재closure(Sprint v1.3.1, methodology closure, 민우님 지시):
    # build_cost_aware_report()는 F2_baseline/CandidateA/CandidateB/
    # CandidateG 네 후보를 한 번에 계산하는데, 넷 다 Candidate A의 forward
    # 경계(CANDIDATE_A_FORWARD_START_DATE=20260826)를 공유하는 건 F2/A/B
    # 에는 맞지만(셋 다 Sprint v1.2에서 8/26 이전에 조건이 고정됨)
    # CandidateG에는 틀립니다(G는 8/26 Sprint v1.3 구현 도중 분류 코드가
    # 만들어짐 — 위 CANDIDATE_REGIME_BOUNDARIES 주석 참고). 그래서
    # 이 A-경계 3종 리포트에서는 CandidateG를 걸러내고, 바로 아래에서
    # CandidateG 고유의 3-way 경계로 따로 계산합니다(함수 자체는 손대지
    # 않음 — build_cost_aware_report()의 직접 단위 테스트는 그대로 4개
    # 후보를 반환하는 순수 계산 함수로 남아있습니다).
    cost_aware_historical = [
        c for c in build_cost_aware_report(historical_rows, "HISTORICAL") if not c["candidate"].startswith("CandidateG")
    ]
    cost_aware_forward = [
        c for c in build_cost_aware_report(forward_rows, "FORWARD") if not c["candidate"].startswith("CandidateG")
    ]
    cost_aware_combined = [
        c for c in build_cost_aware_report(feature_rows, "COMBINED(참고용)") if not c["candidate"].startswith("CandidateG")
    ]
    cost_aware_candidate_g = build_candidate_regime_cost_aware_report(
        feature_rows, _candidate_g_pred, "CandidateG",
        CANDIDATE_REGIME_BOUNDARIES["CandidateG"]["hypothesis_forming_start"],
        CANDIDATE_REGIME_BOUNDARIES["CandidateG"]["true_forward_start"],
    )
    cost_aware_summary_rows, cost_aware_per_day_rows = flatten_cost_aware_reports(
        cost_aware_historical + cost_aware_forward + cost_aware_combined + cost_aware_candidate_g
    )

    out = Path(out_dir)
    write_pnl_terminology_md(out)
    write_csv(out / "trade_feature_table.csv", feature_rows)
    write_csv(out / "low_upside_study.csv", low_upside_buckets + low_upside_candidates)
    write_csv(out / "low_upside_two_condition_study.csv", two_condition_candidates)
    write_csv(
        out / "exit_extension_study.csv",
        extension_per_trade_min_profit + extension_per_trade_early_vwap + extension_rules,
    )
    write_csv(out / "exit_extension_study_by_regime.csv", extension_rules_by_regime)
    write_csv(out / "entry_quality_study.csv", gate_study)
    write_csv(out / "candidate_scorecard.csv", scorecard)
    write_csv(out / "candidate_cost_aware_summary.csv", cost_aware_summary_rows)
    write_csv(out / "candidate_cost_aware_per_day.csv", cost_aware_per_day_rows)

    return {
        "days": [d.date for d in days],
        "availability": {d.date: d.availability for d in days},
        "quality_notes": {d.date: d.quality_notes for d in days},
        "warnings": warnings,
        "feature_rows": feature_rows,
        "low_upside_buckets": low_upside_buckets,
        "low_upside_candidates": low_upside_candidates,
        "extension_per_trade_min_profit": extension_per_trade_min_profit,
        "extension_per_trade_early_vwap": extension_per_trade_early_vwap,
        "extension_excluded": extension_excluded,
        "extension_rules": extension_rules,
        "extension_rules_by_regime": extension_rules_by_regime,
        "gate_study": gate_study,
        "scorecard": scorecard,
        "leave_one_out_all": leave_one_out_report(feature_rows, "전체 포트폴리오(참고용)"),
        "leave_one_out_f2": loo_f2,
        "leave_one_out_f1": loo_f1,
        "leave_one_out_f3": loo_f3,
        "two_condition_candidates": two_condition_candidates,
        "leave_one_out_candidate_a": loo_cand_a,
        "leave_one_out_candidate_b": loo_cand_b,
        "historical_rows": historical_rows,
        "forward_rows": forward_rows,
        "cost_aware_historical": cost_aware_historical,
        "cost_aware_forward": cost_aware_forward,
        "cost_aware_combined": cost_aware_combined,
        "cost_aware_candidate_g": cost_aware_candidate_g,
        "cost_aware_summary_rows": cost_aware_summary_rows,
        "cost_aware_per_day_rows": cost_aware_per_day_rows,
        "pnl_terminology": PNL_TERMINOLOGY,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", action="append", required=True, help="daily bundle 디렉터리 (반복 가능)")
    ap.add_argument("--out-dir", default="diagnostics/profitability")
    args = ap.parse_args(argv)

    result = run(args.bundle, args.out_dir)
    print(f"분석 완료: {len(result['feature_rows'])}건 round-trip trade, 대상 날짜: {result['days']}")
    for w in result["warnings"]:
        print(f"[경고] {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
