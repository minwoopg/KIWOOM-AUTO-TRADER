# -*- coding: utf-8 -*-
"""Profitability Sprint v1 (tools/profitability_sprint.py) 회귀 테스트.

이 도구는 analysis-only입니다 — TradingService/Broker/API를 전혀
import하지 않고 synthetic CSV fixture로만 검증합니다. 요구사항
(민우님 지시 section 14) 10개 항목을 각각 최소 1건씩 검증합니다:

  1. WIN/LOSS/BREAKEVEN cost 계산
  2. upside=0.0 포함(결측 아님)
  3. non-finite 제외(nan/inf/-inf)
  4. Low Upside bucket 경계(0.25/0.50/0.75/1.00/1.50/2.00)
  5. filter가 winner를 제거한 경우 winner damage 정확히 계산
  6. all-breakeven 그룹 승률 "해당없음"
  7. +5/+10/+20 counterfactual 결측 처리(entry_watch_shadow 없음/불일치)
  8. Base 0.35% / Stress 0.90% 비용 반영
  9. leave-one-trade-out / leave-one-day-out
  10. 입력 bundle 일부 파일 누락 시 조용히 넘어가지 않고 데이터 품질 경고

2026-08-24 (민우님 코드/CSV 직접 대조 리뷰 반영, 추가 검증):
  11. Low Upside 후보 순서 — F2(주 후보)가 F1/F3(보조 비교용)보다 먼저 반환됨
  12. entry_watch_trigger_type 분류 — 급락청산/VWAP이탈청산/최소수익미달청산/
      entry_watch 아닌 청산을 exit_reason 텍스트만으로 정확히 구분
  13. exit_extension_study()가 3-tuple을 반환하고, MIN_PROFIT_5M만 첫 번째
      리스트에, EARLY_VWAP_EXIT만 두 번째 리스트에 들어가며, CRASH_CUT은
      유효한 counterfactual이 있어도 둘 중 어디에도 들어가지 않음
  14. extension_rule_candidates()의 R1/R2가 valid_evidence=False(feature
      시점 불일치)로 표시되고, build_scorecard()가 이를 candidate 문자열과
      verdict에 명시적으로 반영함(R3는 valid_evidence=True 유지)
  15. trade_feature_table의 KRW 컬럼(entry_notional_krw/base_net_pnl_krw/
      stress_net_pnl_krw)이 domain.cost_model 기준으로 정확히 계산됨
  16. Low Upside 후보의 base_net_delta_krw/stress_net_delta_krw가 %p 델타와
      부호가 일치함
  17. 모든 feature row의 data_quality_flag에 "SELL가는 proxy(참조가)이지
      실제 체결가가 아니다"라는 PnL price-source 감사 caveat이 포함됨
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from tools.profitability_sprint import (  # noqa: E402
    COST_MODEL, BASE_SCENARIO, STRESS_SCENARIO, WIN, LOSS, BREAKEVEN,
    classify_outcome, win_rate_str, safe_float,
    load_bundle_day, build_trade_features,
    low_upside_bucket_study, low_upside_filter_candidates,
    two_condition_low_upside_candidates,
    exit_extension_study, extension_rule_candidates, entry_quality_gate_study,
    candidate_leave_one_out, leave_one_out_report, build_scorecard,
    classify_entry_watch_trigger,
    TRIGGER_MIN_PROFIT_5M, TRIGGER_EARLY_VWAP_EXIT, TRIGGER_CRASH_CUT, TRIGGER_NOT_ENTRY_WATCH,
    CANDIDATE_A_FORWARD_START_DATE,
    candidate_cost_aware_metrics, split_historical_forward,
    build_cost_aware_report, flatten_cost_aware_reports,
    # 2026-08-26 (Sprint v1.3, Candidate G/M1):
    parse_gap_pullback_d, _candidate_g_pred, extension_rule_candidates_by_regime,
    run,
)
from domain.cost_model import load_cost_model as _load_cost_model_directly  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


TRADES_HEADER = [
    "timestamp", "symbol", "side", "quantity", "price", "accepted", "message",
    "order_id", "entry_strategy", "market_regime", "entry_score", "entry_reason",
    "is_v_rebound", "is_pulldown_recovery", "v_drop_pct", "v_rise_pct", "v_low_age",
    "current_vs_vwap_pct", "volume_ratio", "bar_amount", "rebound_volume_spike",
    "v_bottom_spike", "upside_to_recent_high_pct", "exit_reason", "hold_minutes",
    "avg_buy_price", "condition_name",
]


def _write_csv(path: Path, header: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def _buy(ts, sym, price, qty, order_id, upside="", entry_score="5",
         rebound_volume_spike="False", is_pulldown_recovery="False", entry_reason=""):
    return {
        "timestamp": ts, "symbol": sym, "side": "BUY", "quantity": str(qty),
        "price": str(price), "accepted": "True", "message": "체결", "order_id": order_id,
        "entry_strategy": "breakout", "market_regime": "BULLISH", "entry_score": entry_score,
        "entry_reason": entry_reason, "is_v_rebound": "False", "is_pulldown_recovery": is_pulldown_recovery,
        "v_drop_pct": "0.0", "v_rise_pct": "0.0", "v_low_age": "1",
        "current_vs_vwap_pct": "1.0", "volume_ratio": "1.0", "bar_amount": "1000",
        "rebound_volume_spike": rebound_volume_spike, "v_bottom_spike": "False",
        "upside_to_recent_high_pct": str(upside),
    }


def _sell(ts, sym, price, qty, order_id, exit_reason, hold_minutes, avg_buy_price):
    return {
        "timestamp": ts, "symbol": sym, "side": "SELL", "quantity": str(qty),
        "price": str(price), "accepted": "True", "message": "체결", "order_id": order_id,
        "exit_reason": exit_reason, "hold_minutes": str(hold_minutes),
        "avg_buy_price": str(avg_buy_price),
    }


MIN_PROFIT_SHADOW_HEADER = [
    "timestamp", "symbol", "entry_time", "holding_minutes", "pnl_pct", "price", "vwap",
    "price_vs_vwap_pct", "macd", "macd_signal", "macd_above_signal", "rsi", "ma5", "ma20",
    "peak_pnl_pct", "drawdown_from_peak_pct", "upside_to_recent_high_pct", "minute_data_stale",
]


def make_bundle(tmpdir: Path, date: str, trades_rows: list[dict],
                 entry_watch_rows: list[dict] | None = None,
                 include_entry_watch_file: bool = True,
                 min_profit_shadow_rows: list[dict] | None = None,
                 include_min_profit_shadow_file: bool = False) -> Path:
    bundle_dir = tmpdir / f"bundle_{date}"
    _write_csv(bundle_dir / "raw" / f"trades_{date}.csv", TRADES_HEADER, trades_rows)
    if include_entry_watch_file:
        ew_header = ["trigger_at", "symbol", "trigger_type", "entry_price", "trigger_price",
                     "actual_pnl_pct", "checkpoint_min", "checkpoint_price",
                     "counterfactual_pnl_pct", "entry_watch_effect_pct"]
        _write_csv(bundle_dir / "raw" / f"entry_watch_shadow_{date}.csv", ew_header,
                   entry_watch_rows or [])
    # entry_quality_shadow / position_lifecycle / signal_log는 의도적으로 생략
    # (요구사항 10: 일부 파일 누락 시 UNAVAILABLE + 경고, 조용히 넘어가지 않음)
    # min_profit_extension_shadow도 기본은 생략(2026-08-26, Sprint v1.3) —
    # 이 파일이 필요한 Candidate M1 관련 테스트에서만 명시적으로
    # include_min_profit_shadow_file=True로 켭니다. 나머지 모든 기존
    # 테스트는 이 파일 없이도(=UNAVAILABLE) 그대로 동작해야 합니다.
    if include_min_profit_shadow_file:
        _write_csv(bundle_dir / "raw" / f"min_profit_extension_shadow_{date}.csv",
                   MIN_PROFIT_SHADOW_HEADER, min_profit_shadow_rows or [])
    return bundle_dir


tmp_root = Path(tempfile.mkdtemp(prefix="profitability_sprint_test_"))

try:
    # ── 1. WIN/LOSS/BREAKEVEN cost 계산 + 8. Base/Stress 비용 ────────
    date1 = "20260901"
    rows1 = [
        _buy("2026-09-01T09:00:00", "AAA111", 10000, 10, "B001", upside="2.0"),
        _sell("2026-09-01T09:10:00", "AAA111", 10100, 10, "S001", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-01T09:20:00", "BBB222", 10000, 10, "B002", upside="2.0"),
        _sell("2026-09-01T09:30:00", "BBB222", 9900, 10, "S002", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-01T09:40:00", "CCC333", 10000, 10, "B003", upside="2.0"),
        _sell("2026-09-01T09:50:00", "CCC333", 10000, 10, "S003", "트레일링 스탑", "10.0", 10000),
    ]
    bundle1 = make_bundle(tmp_root, date1, rows1)
    day1 = load_bundle_day(bundle1)
    features1, warnings1 = build_trade_features([day1])
    check("1) 3건 모두 pair 매칭됨(WIN/LOSS/BREAKEVEN 각 1건)", len(features1) == 3)
    outcomes = {r["symbol"]: r["outcome"] for r in features1}
    check("1) AAA111(가격 상승) → WIN", outcomes.get("AAA111") == WIN)
    check("1) BBB222(가격 하락) → LOSS", outcomes.get("BBB222") == LOSS)
    check("1) CCC333(가격 불변) → BREAKEVEN", outcomes.get("CCC333") == BREAKEVEN)

    aaa = next(r for r in features1 if r["symbol"] == "AAA111")
    check("8) gross_pnl_pct 계산 정확(+1.0%)", abs(aaa["gross_pnl_pct"] - 1.0) < 1e-9)
    # 이 도구는 비용 값을 자체 하드코딩하지 않고 domain.cost_model을 단일
    # 출처로 씁니다(1J/1J.1 정책, test_cost_model.py가 저장소 전체의
    # 하드코딩을 스캔합니다) — 그래서 여기서도 리터럴 0.35/0.90을 직접
    # 비교하지 않고, 독립적으로 다시 로드한 CostModel과 구조적으로
    # 비교합니다.
    _direct_model = _load_cost_model_directly()
    check("8) tools.profitability_sprint.COST_MODEL이 domain.cost_model과 동일한 Base 비용을 씀",
          COST_MODEL.base_roundtrip_pct == _direct_model.base_roundtrip_pct)
    check("8) tools.profitability_sprint.COST_MODEL이 domain.cost_model과 동일한 Stress 비용을 씀",
          COST_MODEL.stress_roundtrip_pct == _direct_model.stress_roundtrip_pct)
    check("8) base_net_pnl_pct = gross - Base 비용(COST_MODEL.net()과 일치)",
          abs(aaa["base_net_pnl_pct"] - COST_MODEL.net(1.0, BASE_SCENARIO)) < 1e-9)
    check("8) stress_net_pnl_pct = gross - Stress 비용(COST_MODEL.net()과 일치)",
          abs(aaa["stress_net_pnl_pct"] - COST_MODEL.net(1.0, STRESS_SCENARIO)) < 1e-9)
    check("8) Stress 비용이 Base 비용보다 큼(비용 시나리오 순서 보존)",
          COST_MODEL.cost_pct(STRESS_SCENARIO) > COST_MODEL.cost_pct(BASE_SCENARIO))

    # ── 2. upside=0.0 포함(결측 아님) ────────────────────────────────
    date2 = "20260902"
    rows2 = [
        _buy("2026-09-02T09:00:00", "ZZZ999", 10000, 10, "B010", upside="0.0"),
        _sell("2026-09-02T09:10:00", "ZZZ999", 10050, 10, "S010", "트레일링 스탑", "10.0", 10000),
    ]
    bundle2 = make_bundle(tmp_root, date2, rows2)
    day2 = load_bundle_day(bundle2)
    features2, _ = build_trade_features([day2])
    zzz = features2[0]
    check("2) upside_to_recent_high_pct=0.0이 None(결측)이 아니라 0.0으로 보존됨",
          zzz["upside_to_recent_high_pct"] == 0.0 and zzz["upside_to_recent_high_pct"] is not None)
    buckets2 = low_upside_bucket_study(features2)
    lt025 = next(b for b in buckets2 if b["bucket"] == "<0.25%")
    check("2) upside=0.0 거래가 '<0.25%' bucket에 정확히 집계됨(trades=1)", lt025["trades"] == 1)

    # ── 3. non-finite 제외(nan/inf/-inf) ─────────────────────────────
    check("3) safe_float('nan') → None", safe_float("nan") is None)
    check("3) safe_float('inf') → None", safe_float("inf") is None)
    check("3) safe_float('-inf') → None", safe_float("-inf") is None)
    check("3) safe_float('1e309')(오버플로 → inf) → None", safe_float("1e309") is None)
    check("3) safe_float('0.0') → 0.0 (유효값 유지, 결측 아님)", safe_float("0.0") == 0.0)

    date3 = "20260903"
    rows3 = [
        _buy("2026-09-03T09:00:00", "NAN111", 10000, 10, "B020", upside="nan"),
        _sell("2026-09-03T09:10:00", "NAN111", 10050, 10, "S020", "트레일링 스탑", "10.0", 10000),
    ]
    bundle3 = make_bundle(tmp_root, date3, rows3)
    features3, _ = build_trade_features([load_bundle_day(bundle3)])
    check("3) upside='nan'인 거래는 upside_to_recent_high_pct가 None(결측)으로 처리됨",
          features3[0]["upside_to_recent_high_pct"] is None)
    buckets3 = low_upside_bucket_study(features3)
    check("3) non-finite upside 거래는 어떤 bucket에도 집계되지 않음(0으로 추정하지 않음)",
          all(b["trades"] == 0 for b in buckets3))

    # ── 4. Low Upside bucket 경계 ─────────────────────────────────────
    date4 = "20260904"
    boundary_values = ["0.24", "0.25", "0.49", "0.50", "0.74", "0.75", "0.99", "1.00",
                        "1.49", "1.50", "1.99", "2.00", "2.50"]
    rows4 = []
    for i, v in enumerate(boundary_values):
        sym = f"BND{i:03d}"
        rows4.append(_buy(f"2026-09-04T09:{i:02d}:00", sym, 10000, 10, f"B0{i:02d}", upside=v))
        rows4.append(_sell(f"2026-09-04T09:{i:02d}:30", sym, 10010, 10, f"S0{i:02d}", "트레일링 스탑", "0.5", 10000))
    bundle4 = make_bundle(tmp_root, date4, rows4)
    features4, _ = build_trade_features([load_bundle_day(bundle4)])
    buckets4 = {b["bucket"]: b["trades"] for b in low_upside_bucket_study(features4)}
    check("4) <0.25%: 0.24만 포함(경계 0.25 미포함) → 1건", buckets4["<0.25%"] == 1)
    check("4) 0.25~<0.50%: 0.25,0.49 포함(경계 0.50 미포함) → 2건", buckets4["0.25~<0.50%"] == 2)
    check("4) 0.50~<0.75%: 0.50,0.74 포함 → 2건", buckets4["0.50~<0.75%"] == 2)
    check("4) 0.75~<1.00%: 0.75,0.99 포함 → 2건", buckets4["0.75~<1.00%"] == 2)
    check("4) 1.00~<1.50%: 1.00,1.49 포함 → 2건", buckets4["1.00~<1.50%"] == 2)
    check("4) 1.50~<2.00%: 1.50,1.99 포함 → 2건", buckets4["1.50~<2.00%"] == 2)
    check("4) >=2.00%: 2.00,2.50 포함 → 2건", buckets4[">=2.00%"] == 2)
    check("4) 전체 13건이 정확히 7개 bucket에 분배됨(중복/누락 없음)",
          sum(buckets4.values()) == 13)

    # ── 5. filter가 winner를 제거한 경우 winner damage 정확히 계산 ────
    date5 = "20260905"
    rows5 = [
        _buy("2026-09-05T09:00:00", "WIN001", 10000, 10, "B030", upside="0.1"),   # low-upside WIN
        _sell("2026-09-05T09:10:00", "WIN001", 10500, 10, "S030", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-05T09:20:00", "LOS001", 10000, 10, "B031", upside="0.1"),   # low-upside LOSS
        _sell("2026-09-05T09:30:00", "LOS001", 9800, 10, "S031", "entry_watch 최소수익미달청산", "5.0", 10000),
        _buy("2026-09-05T09:40:00", "WIN002", 10000, 10, "B032", upside="3.0"),   # high-upside WIN (영향 없어야 함)
        _sell("2026-09-05T09:50:00", "WIN002", 10300, 10, "S032", "트레일링 스탑", "10.0", 10000),
    ]
    bundle5 = make_bundle(tmp_root, date5, rows5)
    features5, _ = build_trade_features([load_bundle_day(bundle5)])
    cands5 = low_upside_filter_candidates(features5)
    f1_5 = next(c for c in cands5 if c["candidate"].startswith("F1"))
    check("5) F1(upside<1.0% skip)이 low-upside WIN 1건을 제거함", f1_5["removed_winners"] == 1)
    check("5) F1이 low-upside LOSS 1건도 제거함", f1_5["removed_losers"] == 1)
    check("5) F1이 high-upside WIN(WIN002)은 건드리지 않음(removed_trades==2)", f1_5["removed_trades"] == 2)
    total_wins5 = sum(1 for r in features5 if r["outcome"] == WIN)
    expected_preservation = f"{(total_wins5 - f1_5['removed_winners'])/total_wins5*100:.0f}%"
    check("5) winner_preservation_rate가 실제 (총승수-제거승수)/총승수와 일치",
          f1_5["winner_preservation_rate"] == expected_preservation)
    check("5) single_large_winner_or_loser_flag가 제거된 승자(WIN001)를 언급함",
          "WIN001" in f1_5["single_large_winner_or_loser_flag"])

    # ── 6. all-breakeven 그룹 승률 '해당없음' ─────────────────────────
    check("6) win_rate_str(0,0) → '해당없음'(0%가 아님)", win_rate_str(0, 0) == "해당없음")
    check("6) win_rate_str(1,0) → '100.0%'", win_rate_str(1, 0) == "100.0%")
    date6 = "20260906"
    rows6 = [
        _buy("2026-09-06T09:00:00", "BE001", 10000, 10, "B040", upside="0.1"),
        _sell("2026-09-06T09:10:00", "BE001", 10000, 10, "S040", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-06T09:20:00", "BE002", 10000, 10, "B041", upside="0.1"),
        _sell("2026-09-06T09:30:00", "BE002", 10000, 10, "S041", "트레일링 스탑", "10.0", 10000),
    ]
    bundle6 = make_bundle(tmp_root, date6, rows6)
    features6, _ = build_trade_features([load_bundle_day(bundle6)])
    buckets6 = next(b for b in low_upside_bucket_study(features6) if b["bucket"] == "<0.25%")
    check("6) all-BREAKEVEN bucket(2건 모두 무승부)의 win_rate가 '해당없음'",
          buckets6["win_rate"] == "해당없음")
    check("6) all-BREAKEVEN bucket의 wins/losses가 둘 다 0",
          buckets6["wins"] == 0 and buckets6["losses"] == 0 and buckets6["breakevens"] == 2)

    # ── 7. +5/+10/+20 counterfactual 결측 처리 ───────────────────────
    date7 = "20260907"
    rows7 = [
        # entry_watch 청산인데 entry_watch_shadow 파일 자체가 아예 없는 경우
        _buy("2026-09-07T09:00:00", "NOEW001", 10000, 10, "B050", upside="0.1"),
        _sell("2026-09-07T09:10:00", "NOEW001", 9900, 10, "S050", "entry_watch VWAP이탈청산", "3.0", 10000),
        # entry_watch 청산이 아닌 경우(트레일링) — 애초에 counterfactual 대상 아님
        _buy("2026-09-07T09:20:00", "TRAIL01", 10000, 10, "B051", upside="0.1"),
        _sell("2026-09-07T09:30:00", "TRAIL01", 10100, 10, "S051", "트레일링 스탑", "10.0", 10000),
    ]
    bundle7 = make_bundle(tmp_root, date7, rows7, entry_watch_rows=[], include_entry_watch_file=False)
    features7, warnings7 = build_trade_features([load_bundle_day(bundle7)])
    noew = next(r for r in features7 if r["symbol"] == "NOEW001")
    trail = next(r for r in features7 if r["symbol"] == "TRAIL01")
    check("7) entry_watch_shadow 파일 자체가 없으면 fwd5m 값이 빈 값(NA)",
          noew["fwd5m_price_return_pct"] == "")
    check("7) entry_watch 청산이 아닌 거래는 애초에 fwd_data_source가 UNAVAILABLE로 명시됨",
          "UNAVAILABLE" in trail["fwd_data_source"])
    check("7) entry_watch_shadow 파일 부재가 경고 목록에 기록됨(조용히 넘어가지 않음)",
          any("entry_watch_shadow 없음" in w for w in warnings7))

    # entry_watch_shadow는 있지만 가격/시각이 실제 SELL과 불일치하는 경우(OBS.2-A 유형 오염)
    date7b = "20260908"
    rows7b = [
        _buy("2026-09-08T09:00:00", "MISM001", 10000, 10, "B060", upside="0.1"),
        _sell("2026-09-08T09:10:00", "MISM001", 9900, 10, "S060", "entry_watch VWAP이탈청산", "3.0", 10000),
    ]
    mismatched_ew = [
        {"trigger_at": "2026-09-08T09:15:00", "symbol": "MISM001", "trigger_type": "VWAP이탈청산",
         "entry_price": "10000", "trigger_price": "9950",  # 실제 SELL가(9900)와 다름 → 불일치
         "actual_pnl_pct": "-0.5", "checkpoint_min": "5", "checkpoint_price": "9980",
         "counterfactual_pnl_pct": "-0.2", "entry_watch_effect_pct": "0.3"},
    ]
    bundle7b = make_bundle(tmp_root, date7b, rows7b, entry_watch_rows=mismatched_ew)
    features7b, _ = build_trade_features([load_bundle_day(bundle7b)])
    mism = features7b[0]
    check("7) trigger_price가 실제 SELL가와 다른 entry_watch_shadow 후보는 사용하지 않음(추정 금지)",
          mism["fwd5m_price_return_pct"] == "")
    check("7) 불일치 후보는 UNAVAILABLE + 사유가 fwd_data_source에 명시됨",
          "UNAVAILABLE" in mism["fwd_data_source"] and "불일치" in mism["fwd_data_source"])
    check("7) 불일치 사유가 data_quality_flag에도 기록됨",
          "불일치" in mism["data_quality_flag"])

    # ── 9. leave-one-trade-out / leave-one-day-out ───────────────────
    date9a = "20260910"
    date9b = "20260911"
    rows9a = [
        _buy("2026-09-10T09:00:00", "D1A", 10000, 10, "B070", upside="0.1"),
        _sell("2026-09-10T09:10:00", "D1A", 10500, 10, "S070", "트레일링 스탑", "10.0", 10000),  # 큰 승자
        _buy("2026-09-10T09:20:00", "D1B", 10000, 10, "B071", upside="0.1"),
        _sell("2026-09-10T09:30:00", "D1B", 9900, 10, "S071", "트레일링 스탑", "10.0", 10000),
    ]
    rows9b = [
        _buy("2026-09-11T09:00:00", "D2A", 10000, 10, "B072", upside="0.1"),
        _sell("2026-09-11T09:10:00", "D2A", 9800, 10, "S072", "트레일링 스탑", "10.0", 10000),
    ]
    bundle9a = make_bundle(tmp_root, date9a, rows9a)
    bundle9b = make_bundle(tmp_root, date9b, rows9b)
    features9, _ = build_trade_features([load_bundle_day(bundle9a), load_bundle_day(bundle9b)])
    loo = leave_one_out_report(features9, "테스트 후보")
    check("9) leave-one-out 결과에 best/worst trade 제거 시나리오가 모두 존재",
          "base_net_pnl_pct_sum_excl_best_trade" in loo and "base_net_pnl_pct_sum_excl_worst_trade" in loo)
    check("9) best trade(D1A) 제거 시 합계가 낮아짐",
          loo["base_net_pnl_pct_sum_excl_best_trade"] < loo["base_net_pnl_pct_sum_full"])
    check("9) worst trade 제거 시 합계가 높아짐",
          loo["base_net_pnl_pct_sum_full"] < loo["base_net_pnl_pct_sum_excl_worst_trade"])
    check("9) 거래일별 제거(leave-one-day-out) 결과가 날짜 수만큼 존재",
          len(loo["per_day_exclusion"]) == 2)

    loo_candidate = candidate_leave_one_out(
        features9, lambda r: r["upside_to_recent_high_pct"] < 1.0, "테스트 필터")
    check("9) 후보별 leave-one-out도 거래별/거래일별 delta를 모두 제공",
          len(loo_candidate["per_trade_excluded_delta"]) == 3
          and len(loo_candidate["per_day_excluded_delta"]) == 2)

    # ── 10. bundle 일부 파일 누락 시 데이터 품질 경고(침묵 금지) ──────
    date10 = "20260912"
    bundle10_dir = tmp_root / f"bundle_{date10}"
    _write_csv(bundle10_dir / "raw" / f"trades_{date10}.csv", TRADES_HEADER, [
        _buy("2026-09-12T09:00:00", "MISS001", 10000, 10, "B080", upside="0.1"),
        _sell("2026-09-12T09:10:00", "MISS001", 10100, 10, "S080", "트레일링 스탑", "10.0", 10000),
    ])
    # entry_watch_shadow / entry_quality_shadow / position_lifecycle / signal_log /
    # min_profit_extension_shadow 전부 미생성
    day10 = load_bundle_day(bundle10_dir)
    check("10) trades 외 5개 raw 파일이 전부 UNAVAILABLE로 명시적 표시됨(존재하는 척 안 함) — "
          "2026-08-26 Sprint v1.3에서 min_profit_extension_shadow가 RAW_FILES에 추가되며 4개→5개",
          all(day10.availability[k] == "UNAVAILABLE"
              for k in ("entry_watch_shadow", "entry_quality_shadow", "position_lifecycle",
                        "signal_log", "min_profit_extension_shadow")))
    check("10) 누락된 5개 파일에 대한 quality_notes가 각각 기록됨(조용히 넘어가지 않음)",
          len(day10.quality_notes) == 5)
    features10, warnings10 = build_trade_features([day10])
    check("10) trades.csv만 있어도 거래 자체는 pnl/outcome 계산까지는 정상 진행(부분 분석 가능)",
          len(features10) == 1 and features10[0]["outcome"] == WIN)
    check("10) entry_quality_shadow 매칭 실패가 해당 거래의 data_quality_flag에 남음(값을 지어내지 않음)",
          "entry_quality_shadow" in features10[0]["data_quality_flag"])
    check("10) signal_log 매칭 실패도 data_quality_flag에 남음",
          "signal_log" in features10[0]["data_quality_flag"])
    check("10) 존재하지 않는 bundle 디렉터리(trades.csv조차 없음)는 예외로 실패함(조용한 오분석 금지)",
          True)
    raised = False
    try:
        load_bundle_day(tmp_root / "no_such_bundle")
    except FileNotFoundError:
        raised = True
    check("10) raw/trades_*.csv가 아예 없는 디렉터리는 FileNotFoundError로 명시 실패(빈 결과로 조용히 넘어가지 않음)",
          raised)

    # ── end-to-end: run()이 실제 파일을 정상적으로 출력하는지 ────────
    out_dir = tmp_root / "out"
    result = run([str(bundle1), str(bundle5)], str(out_dir))
    check("E2E) run()이 여러 bundle을 합쳐 feature_rows를 생성함",
          len(result["feature_rows"]) == 3 + 3)
    for fname in ("trade_feature_table.csv", "low_upside_study.csv", "exit_extension_study.csv",
                  "entry_quality_study.csv", "candidate_scorecard.csv"):
        check(f"E2E) {fname} 출력 파일이 실제로 생성됨", (out_dir / fname).is_file())

    # ── 11. Low Upside 후보 순서 — F2(주 후보)가 F1/F3보다 먼저 ──────
    non_baseline = [c["candidate"] for c in result["low_upside_candidates"] if not c["candidate"].startswith("F0")]
    idx_f2 = next(i for i, c in enumerate(non_baseline) if c.startswith("F2"))
    idx_f1 = next(i for i, c in enumerate(non_baseline) if c.startswith("F1"))
    idx_f3 = next(i for i, c in enumerate(non_baseline) if c.startswith("F3"))
    check("11) F2가 F1보다 먼저 반환됨(주 후보 우선순위)", idx_f2 < idx_f1)
    check("11) F2가 F3보다 먼저 반환됨", idx_f2 < idx_f3)
    check("11) F2 candidate 라벨에 '주 후보' 표기 포함", "주 후보" in non_baseline[idx_f2])
    check("11) F1/F3 candidate 라벨에 '보조 비교용' 표기 포함",
          "보조 비교용" in non_baseline[idx_f1] and "보조 비교용" in non_baseline[idx_f3])

    # ── 12/13. entry_watch_trigger_type 분류 + Study B 3-way 분리 ────
    date12 = "20260912"
    rows12 = [
        _buy("2026-09-12T09:00:00", "MINPROF1", 10000, 10, "B080", upside="2.0"),
        _sell("2026-09-12T09:06:00", "MINPROF1", 10020, 10, "S080",
              "entry_watch 최소수익미달청산 — 매수 후 5.5분, 수익률 +0.20% (기준 +0.5% 미달)",
              "5.5", 10000),
        _buy("2026-09-12T09:10:00", "VWAPEX1", 10000, 10, "B081", upside="2.0"),
        _sell("2026-09-12T09:11:30", "VWAPEX1", 9950, 10, "S081",
              "entry_watch VWAP이탈청산 — 매수 후 1.5분, 수익률 -0.50%, VWAP 10100원 아래 "
              "(-1.50%, 1/1회 연속 확인)", "1.5", 10000),
        _buy("2026-09-12T09:15:00", "CRASH1", 10000, 10, "B082", upside="2.0"),
        _sell("2026-09-12T09:16:00", "CRASH1", 9880, 10, "S082",
              "entry_watch 급락청산 — 매수 후 1.0분, 수익률 -1.20% (기준 -1.0% 이하)",
              "1.0", 10000),
        _buy("2026-09-12T09:20:00", "TRAIL1", 10000, 10, "B083", upside="2.0"),
        _sell("2026-09-12T09:40:00", "TRAIL1", 10200, 10, "S083", "트레일링 스탑", "20.0", 10000),
    ]
    ew12 = [
        {"trigger_at": "2026-09-12T09:06:00", "symbol": "MINPROF1", "trigger_type": "최소수익미달청산",
         "entry_price": "10000", "trigger_price": "10020", "actual_pnl_pct": "0.2",
         "checkpoint_min": "5", "checkpoint_price": "10100",
         "counterfactual_pnl_pct": "1.0", "entry_watch_effect_pct": "-0.8"},
        {"trigger_at": "2026-09-12T09:11:30", "symbol": "VWAPEX1", "trigger_type": "VWAP이탈청산",
         "entry_price": "10000", "trigger_price": "9950", "actual_pnl_pct": "-0.5",
         "checkpoint_min": "5", "checkpoint_price": "9900",
         "counterfactual_pnl_pct": "-1.0", "entry_watch_effect_pct": "0.5"},
        {"trigger_at": "2026-09-12T09:16:00", "symbol": "CRASH1", "trigger_type": "급락청산",
         "entry_price": "10000", "trigger_price": "9880", "actual_pnl_pct": "-1.2",
         "checkpoint_min": "5", "checkpoint_price": "9800",
         "counterfactual_pnl_pct": "-2.0", "entry_watch_effect_pct": "0.8"},
    ]
    bundle12 = make_bundle(tmp_root, date12, rows12, entry_watch_rows=ew12)
    features12, _ = build_trade_features([load_bundle_day(bundle12)])
    by_sym12 = {r["symbol"]: r for r in features12}

    check("12) classify_entry_watch_trigger — 최소수익미달청산 문구 → MIN_PROFIT_5M",
          classify_entry_watch_trigger("entry_watch 최소수익미달청산 — 매수 후 5.5분, 수익률 +0.20%")
          == TRIGGER_MIN_PROFIT_5M)
    check("12) classify_entry_watch_trigger — VWAP이탈청산 문구 → EARLY_VWAP_EXIT",
          classify_entry_watch_trigger("entry_watch VWAP이탈청산 — 매수 후 1.5분") == TRIGGER_EARLY_VWAP_EXIT)
    check("12) classify_entry_watch_trigger — 급락청산 문구 → CRASH_CUT",
          classify_entry_watch_trigger("entry_watch 급락청산 — 매수 후 1.0분") == TRIGGER_CRASH_CUT)
    check("12) classify_entry_watch_trigger — entry_watch 접두사 없으면 NOT_ENTRY_WATCH",
          classify_entry_watch_trigger("트레일링 스탑") == TRIGGER_NOT_ENTRY_WATCH)
    check("12) build_trade_features가 만든 실제 row의 entry_watch_trigger_type도 동일하게 분류됨",
          by_sym12["MINPROF1"]["entry_watch_trigger_type"] == TRIGGER_MIN_PROFIT_5M
          and by_sym12["VWAPEX1"]["entry_watch_trigger_type"] == TRIGGER_EARLY_VWAP_EXIT
          and by_sym12["CRASH1"]["entry_watch_trigger_type"] == TRIGGER_CRASH_CUT
          and by_sym12["TRAIL1"]["entry_watch_trigger_type"] == TRIGGER_NOT_ENTRY_WATCH)

    min_profit_list, early_vwap_list, excluded12 = exit_extension_study(features12)
    check("13) MIN_PROFIT_5M 거래만 min_profit_list에 들어감(정확히 1건, MINPROF1)",
          len(min_profit_list) == 1 and min_profit_list[0]["symbol"] == "MINPROF1")
    check("13) EARLY_VWAP_EXIT 거래만 early_vwap_list에 들어감(정확히 1건, VWAPEX1)",
          len(early_vwap_list) == 1 and early_vwap_list[0]["symbol"] == "VWAPEX1")
    check("13) CRASH1은 유효한 counterfactual이 있어도 두 리스트 어디에도 없음(급락청산은 두 연구 대상 아님)",
          all(r["symbol"] != "CRASH1" for r in min_profit_list)
          and all(r["symbol"] != "CRASH1" for r in early_vwap_list))
    check("13) TRAIL1(entry_watch 아님)도 excluded_note에 나타나지 않음(애초에 대상 아님)",
          all(r["symbol"] != "TRAIL1" for r in excluded12))

    # ── 14. R1/R2 valid_evidence=False, R3 valid_evidence=True + scorecard 반영 ──
    ext_rules12 = extension_rule_candidates(min_profit_list)
    r1 = next(r for r in ext_rules12 if r["rule"].startswith("R1"))
    r2 = next(r for r in ext_rules12 if r["rule"].startswith("R2"))
    r3 = next(r for r in ext_rules12 if r["rule"].startswith("R3"))
    check("14) R1(price>VWAP)은 valid_evidence=False(feature 시점 불일치 — 진입시점 값 사용)",
          r1["valid_evidence"] is False)
    check("14) R2(MACD)도 valid_evidence=False(동일 사유)", r2["valid_evidence"] is False)
    check("14) R3(entry_score)는 valid_evidence=True(entry_score는 원래 진입시점 값이라 문제 없음)",
          r3["valid_evidence"] is True)

    scorecard12 = build_scorecard([], ext_rules12, [])
    sc_r1 = next(r for r in scorecard12 if r["candidate"].startswith("R1"))
    sc_r3 = next(r for r in scorecard12 if r["candidate"].startswith("R3"))
    check("14) scorecard의 R1 candidate 라벨에 [INVALID EVIDENCE] 표기됨",
          "[INVALID EVIDENCE]" in sc_r1["candidate"])
    check("14) scorecard의 R1 verdict가 INVALID(...)로 시작함(전략 후보 근거로 사용 안 함 명시)",
          sc_r1["verdict"].startswith("INVALID("))
    check("14) scorecard의 R3는 INVALID 표기 없음(유효한 근거로 유지)",
          "[INVALID EVIDENCE]" not in sc_r3["candidate"] and not sc_r3["verdict"].startswith("INVALID("))

    # ── 15. KRW 컬럼 — domain.cost_model 기준 정확한 계산 ────────────
    date15 = "20260913"
    rows15 = [
        _buy("2026-09-13T09:00:00", "KRW001", 10000, 10, "B090", upside="2.0"),
        _sell("2026-09-13T09:30:00", "KRW001", 10300, 10, "S090", "트레일링 스탑", "30.0", 10000),
    ]
    bundle15 = make_bundle(tmp_root, date15, rows15, entry_watch_rows=[])
    features15, _ = build_trade_features([load_bundle_day(bundle15)])
    row15 = features15[0]
    _direct_model = _load_cost_model_directly()
    expected_notional = 10000 * 10  # buy_price(avg_buy_price 우선, 여기선 BUY가와 동일) * qty
    expected_gross_krw = (10300 - 10000) * 10
    expected_base_cost_krw = _direct_model.cost_amount(expected_notional, BASE_SCENARIO)
    expected_stress_cost_krw = _direct_model.cost_amount(expected_notional, STRESS_SCENARIO)
    check("15) entry_notional_krw = buy_price * qty", row15["entry_notional_krw"] == expected_notional)
    check("15) modeled_base_cost_krw가 domain.cost_model.cost_amount(base)와 일치",
          abs(row15["modeled_base_cost_krw"] - expected_base_cost_krw) < 0.01)
    check("15) modeled_stress_cost_krw가 domain.cost_model.cost_amount(stress)와 일치",
          abs(row15["modeled_stress_cost_krw"] - expected_stress_cost_krw) < 0.01)
    check("15) base_net_pnl_krw = gross_pnl_krw - modeled_base_cost_krw",
          abs(row15["base_net_pnl_krw"] - (expected_gross_krw - expected_base_cost_krw)) < 0.01)
    check("15) stress_net_pnl_krw = gross_pnl_krw - modeled_stress_cost_krw",
          abs(row15["stress_net_pnl_krw"] - (expected_gross_krw - expected_stress_cost_krw)) < 0.01)

    # ── 16. Low Upside 후보 KRW 델타 — %p 델타와 부호 일치 ───────────
    f2_candidate = next(c for c in result["low_upside_candidates"] if c["candidate"].startswith("F2"))
    check("16) F2 candidate에 base_net_delta_krw 키 존재", "base_net_delta_krw" in f2_candidate)
    check("16) F2 candidate에 stress_net_delta_krw 키 존재", "stress_net_delta_krw" in f2_candidate)
    pct_sign = f2_candidate["base_net_pnl_pct_delta"] >= 0
    krw_sign = f2_candidate["base_net_delta_krw"] >= 0
    check("16) base_net_delta_krw 부호가 base_net_pnl_pct_delta 부호와 일치",
          pct_sign == krw_sign)

    # ── 17. PnL price-source 감사 caveat이 모든 feature row에 포함됨 ──
    check("17) run() 결과의 모든 feature row에 proxy 관련 data_quality_flag가 포함됨",
          len(result["feature_rows"]) > 0
          and all("proxy" in r["data_quality_flag"] for r in result["feature_rows"]))

    # ══════════════════════════════════════════════════════════════
    # 18. Sprint v1.2 (2026-08-26): F2 baseline vs Candidate A/B
    # (F2 + rebound_volume_spike==False / F2 + PR==False) — 최대
    # 2-조건까지만, 정확히 이 3개 후보만 계산됨을 검증
    # ══════════════════════════════════════════════════════════════
    date18 = "20260913"
    rows18 = [
        # F2 대상(upside<0.50%)이면서 rebound_volume_spike=True,
        # is_pulldown_recovery=False → CandidateA는 살리고(spike=True라
        # F2 AND spike==False에 해당 안 함), CandidateB는 제거(PR==False)
        _buy("2026-09-13T09:00:00", "V2A1", 10000, 10, "B090", upside="0.20",
             rebound_volume_spike="True", is_pulldown_recovery="False"),
        _sell("2026-09-13T09:10:00", "V2A1", 10100, 10, "S090", "트레일링 스탑", "10.0", 10000),
        # F2 대상, rebound_volume_spike=False, is_pulldown_recovery=True
        # → CandidateA는 제거, CandidateB는 살림
        _buy("2026-09-13T09:20:00", "V2A2", 10000, 10, "B091", upside="0.20",
             rebound_volume_spike="False", is_pulldown_recovery="True"),
        _sell("2026-09-13T09:30:00", "V2A2", 9900, 10, "S091", "트레일링 스탑", "10.0", 10000),
        # F2 대상, 두 불리언 플래그 전부 결측(빈 문자열) → 두 후보 모두
        # 이 거래를 제거하지 않아야 함(결측을 False로 추정 금지)
        _buy("2026-09-13T09:40:00", "V2A3", 10000, 10, "B092", upside="0.20",
             rebound_volume_spike="", is_pulldown_recovery=""),
        _sell("2026-09-13T09:50:00", "V2A3", 10050, 10, "S092", "트레일링 스탑", "10.0", 10000),
        # F2 밖(upside 높음) → 어느 후보에도 제거 대상 아님(baseline 비교용)
        _buy("2026-09-13T10:00:00", "V2A4", 10000, 10, "B093", upside="3.00",
             rebound_volume_spike="False", is_pulldown_recovery="False"),
        _sell("2026-09-13T10:10:00", "V2A4", 10200, 10, "S093", "트레일링 스탑", "10.0", 10000),
    ]
    bundle18 = make_bundle(tmp_root, date18, rows18)
    features18, _ = build_trade_features([load_bundle_day(bundle18)])
    tc = two_condition_low_upside_candidates(features18)
    check("18) 정확히 3개 후보만 반환됨(F2_baseline/CandidateA/CandidateB, 추가 조합 없음)",
          len(tc) == 3)
    names18 = [c["candidate"] for c in tc]
    check("18) 후보 이름이 F2_baseline/CandidateA/CandidateB로 명확히 라벨됨",
          names18[0].startswith("F2_baseline") and names18[1].startswith("CandidateA")
          and names18[2].startswith("CandidateB"))
    f2_18, candA_18, candB_18 = tc
    check("18) F2_baseline은 upside<0.50% 3건(V2A1/V2A2/V2A3) 모두 제거",
          f2_18["removed_trades"] == 3)
    check("18) CandidateA는 spike=True(V2A1)를 살리고 spike=False(V2A2)만 제거 — removed_trades==1",
          candA_18["removed_trades"] == 1 and "V2A2" in candA_18["removed_symbols"]
          and "V2A1" not in candA_18["removed_symbols"])
    check("18) CandidateB는 PR=False(V2A1)만 제거하고 PR=True(V2A2)는 살림 — removed_trades==1",
          candB_18["removed_trades"] == 1 and "V2A1" in candB_18["removed_symbols"]
          and "V2A2" not in candB_18["removed_symbols"])
    check("18) 두 불리언 모두 결측인 V2A3는 CandidateA/B 어느 쪽에서도 제거되지 않음(추정 금지)",
          "V2A3" not in candA_18["removed_symbols"] and "V2A3" not in candB_18["removed_symbols"])
    check("18) CandidateA의 missing_boolean_feature_count==1(F2 대상 중 spike값 결측은 V2A3 하나)",
          candA_18["missing_boolean_feature_count"] == 1)
    check("18) CandidateB의 missing_boolean_feature_count도 1(F2 대상 중 PR값 결측은 V2A3 하나)",
          candB_18["missing_boolean_feature_count"] == 1)
    check("18) F0/F1/F3 기존 계산은 리팩터 후에도 동일 로직(_simulate_skip_candidate 공유) 사용 — "
          "기존 low_upside_filter_candidates()가 정상 동작(회귀 없음)",
          any(c["candidate"].startswith("F2") for c in low_upside_filter_candidates(features18)))

    # ── candidate_leave_one_out()의 leave-one-best/worst-trade-out 신규 필드 ──
    loo_candA18 = candidate_leave_one_out(
        features18,
        lambda r: r["upside_to_recent_high_pct"] < 0.50 and r.get("rebound_volume_spike") is False,
        "CandidateA(테스트)")
    check("18) leave_one_best_trade_out/leave_one_worst_trade_out 필드가 존재함",
          "leave_one_best_trade_out" in loo_candA18 and "leave_one_worst_trade_out" in loo_candA18)
    check("18) leave_one_best_trade_out이 gross_pnl_pct 최댓값 거래(V2A4, +2.0%)를 가리킴",
          "V2A4" in loo_candA18["leave_one_best_trade_out"]["excluded_trade"])
    check("18) leave_one_worst_trade_out이 gross_pnl_pct 최솟값 거래(V2A2, -1.0%)를 가리킴",
          "V2A2" in loo_candA18["leave_one_worst_trade_out"]["excluded_trade"])
    check("18) leave_one_best_trade_out의 delta_after_exclusion이 per_trade_excluded_delta의 동일 거래 값과 일치",
          loo_candA18["leave_one_best_trade_out"]["delta_after_exclusion"]
          == loo_candA18["per_trade_excluded_delta"]["exclude_V2A4_20260913"])

    # ── run()이 Sprint v1.2 결과와 새 CSV를 함께 출력하는지(E2E) ──────
    out_dir18 = tmp_root / "out_v1_2"
    result18 = run([str(bundle18)], str(out_dir18))
    check("18) run() 결과에 two_condition_candidates/leave_one_out_candidate_a/b가 포함됨",
          "two_condition_candidates" in result18
          and "leave_one_out_candidate_a" in result18
          and "leave_one_out_candidate_b" in result18)
    check("18) low_upside_two_condition_study.csv가 실제로 생성됨",
          (out_dir18 / "low_upside_two_condition_study.csv").is_file())

    # ══════════════════════════════════════════════════════════════
    # 19. Candidate A forward shadow (2026-08-27, 민우님 지시 #6):
    # base_outcome/stress_outcome, candidate_cost_aware_metrics(),
    # split_historical_forward(), run()이 historical/forward/combined
    # 세 regime을 분리해서 계산·CSV로 내보내는지 검증
    # ══════════════════════════════════════════════════════════════

    # ── 19-A) base_outcome/stress_outcome — gross WIN이 비용 반영 후
    # base/stress에서 LOSS로 바뀌는 케이스(민우님 지적 그대로 재현) ─────
    # 참고: base_outcome/stress_outcome은 기존 outcome/gross_pnl_pct와
    # 동일한 기존 관례대로 반올림 전 raw float 값으로 판정합니다(이
    # 관례 자체는 이번 변경으로 새로 만든 게 아니라 기존 코드를 그대로
    # 따른 것). 그래서 여기서는 정확히 0(BREAKEVEN)이 되는 가격 조합
    # 대신, 부동소수점 나눗셈 오차와 무관하게 항상 명확한 부호가 나오는
    # 여유 있는 값을 씁니다 — tri-state 경계(0.0) 자체는 classify_outcome()
    # 직접 호출로 별도 고정합니다.
    date19a = "20260914"
    rows19a = [
        # gross +0.50%(WIN) > Base 비용(0.35%) → base=+0.15%(WIN 유지)
        # 하지만 Stress 비용(0.90%)보다는 작음 → stress=-0.40%(LOSS).
        # "gross WIN·base WIN인데 stress에서만 LOSS로 전환"되는 케이스.
        _buy("2026-09-14T09:00:00", "COST01", 10000, 10, "B100", upside="2.0"),
        _sell("2026-09-14T09:10:00", "COST01", 10050, 10, "S100", "트레일링 스탑", "10.0", 10000),
        # gross +0.20% < Base 비용(0.35%) → base_net_pnl_pct=-0.15% →
        # base_outcome=LOSS. gross는 WIN인데 base는 LOSS인 핵심 케이스.
        _buy("2026-09-14T09:20:00", "COST02", 10000, 10, "B101", upside="2.0"),
        _sell("2026-09-14T09:30:00", "COST02", 10020, 10, "S101", "트레일링 스탑", "10.0", 10000),
    ]
    bundle19a = make_bundle(tmp_root, date19a, rows19a)
    features19a, _ = build_trade_features([load_bundle_day(bundle19a)])
    cost01 = next(r for r in features19a if r["symbol"] == "COST01")
    cost02 = next(r for r in features19a if r["symbol"] == "COST02")
    check("19-A) COST01: gross_pnl_pct=+0.50%(WIN)", cost01["outcome"] == WIN)
    check("19-A) COST01: base_outcome도 WIN 유지(0.50%-0.35%=+0.15%>0, Base 비용은 버텨냄)",
          cost01["base_outcome"] == WIN and cost01["base_net_pnl_pct"] > 0)
    check("19-A) COST01: stress_outcome은 LOSS로 전환(0.50%-0.90%=-0.40%<0) — "
          "gross/base가 WIN이어도 Stress에서는 손실로 바뀔 수 있음을 보여줌",
          cost01["stress_outcome"] == LOSS and cost01["stress_net_pnl_pct"] < 0)
    check("19-A) classify_outcome() 경계 자체는 정확히 고정됨: 0.0→BREAKEVEN",
          classify_outcome(0.0) == BREAKEVEN)
    check("19-A) classify_outcome() 경계: 미세 양수→WIN, 미세 음수→LOSS",
          classify_outcome(1e-9) == WIN and classify_outcome(-1e-9) == LOSS)
    check("19-A) COST02: gross_pnl_pct=+0.20%(WIN)인데 base_outcome은 LOSS(비용 반영 후 손실로 전환)",
          cost02["outcome"] == WIN and cost02["base_outcome"] == LOSS)
    check("19-A) COST02: stress_outcome도 LOSS(Stress 비용이 Base보다 커서 gross WIN → 손실 폭 더 큼)",
          cost02["stress_outcome"] == LOSS
          and cost02["stress_net_pnl_pct"] < cost02["base_net_pnl_pct"])

    # ── 19-B) candidate_cost_aware_metrics() — skip_precision_base /
    # loss_recall_base / winner_damage_base / per_day base delta 계산
    # 자체를 손으로 설계한 synthetic row로 정확히 검증 ─────────────────
    rows_cam = [
        {"trade_date": "20260826", "symbol": "M1", "upside_to_recent_high_pct": 0.20,
         "rebound_volume_spike": False, "base_outcome": LOSS,
         "base_net_pnl_pct": -0.5, "base_net_pnl_krw": -500},
        {"trade_date": "20260826", "symbol": "M2", "upside_to_recent_high_pct": 0.20,
         "rebound_volume_spike": True, "base_outcome": LOSS,
         "base_net_pnl_pct": -0.3, "base_net_pnl_krw": -300},
        {"trade_date": "20260826", "symbol": "M3", "upside_to_recent_high_pct": 3.0,
         "rebound_volume_spike": False, "base_outcome": WIN,
         "base_net_pnl_pct": 1.0, "base_net_pnl_krw": 1000},
        {"trade_date": "20260827", "symbol": "M4", "upside_to_recent_high_pct": 0.20,
         "rebound_volume_spike": False, "base_outcome": WIN,
         "base_net_pnl_pct": 0.1, "base_net_pnl_krw": 100},
    ]
    cand_a_skip = lambda r: r["upside_to_recent_high_pct"] < 0.50 and r.get("rebound_volume_spike") is False
    cam = candidate_cost_aware_metrics(rows_cam, cand_a_skip, "TestCandidateA")
    check("19-B) n_trades_in_scope==4(upside 결측 없음, 4건 전부 집계)", cam["n_trades_in_scope"] == 4)
    check("19-B) removed_trades==2(M1, M4 — spike==False AND upside<0.50%)", cam["removed_trades"] == 2)
    check("19-B) skip_precision_base==50%(제거 2건 중 base LOSS는 M1 하나)", cam["skip_precision_base"] == "50%")
    check("19-B) loss_recall_base==50%(base 손실 2건[M1,M2] 중 제거된 건 M1 하나)",
          cam["loss_recall_base"] == "50%")
    check("19-B) winner_damage_base==50%(base 승자 2건[M3,M4] 중 잘못 제거된 건 M4 하나)",
          cam["winner_damage_base"] == "50%")
    check("19-B) per_day_base_delta_pct[20260826]==0.5(M1 제거로 그 날 base net이 0.2%→0.7%)",
          abs(cam["per_day_base_delta_pct"]["20260826"] - 0.5) < 1e-9)
    check("19-B) per_day_base_delta_krw[20260826]==500", abs(cam["per_day_base_delta_krw"]["20260826"] - 500) < 1e-6)
    check("19-B) per_day_base_delta_pct[20260827]==-0.1(M4가 제거되며 그 날 유일한 거래가 사라짐)",
          abs(cam["per_day_base_delta_pct"]["20260827"] - (-0.1)) < 1e-9)
    check("19-B) per_day_base_delta_krw[20260827]==-100", abs(cam["per_day_base_delta_krw"]["20260827"] - (-100)) < 1e-6)

    # 제거 대상 0건 / base 손실 0건 / base 승자 0건 각각의 '해당없음' 처리
    cam_empty = candidate_cost_aware_metrics(rows_cam, lambda r: False, "NeverSkip")
    check("19-B) 제거 대상 0건이면 skip_precision_base가 '해당없음(제거 대상 0건)'",
          cam_empty["skip_precision_base"] == "해당없음(제거 대상 0건)")
    all_wins_rows = [r for r in rows_cam if r["base_outcome"] == WIN]
    cam_no_loss = candidate_cost_aware_metrics(all_wins_rows, lambda r: True, "AllWinsRemoved")
    check("19-B) base 손실 0건이면 loss_recall_base가 '해당없음(base 손실 0건)'",
          cam_no_loss["loss_recall_base"] == "해당없음(base 손실 0건)")
    all_loss_rows = [r for r in rows_cam if r["base_outcome"] == LOSS]
    cam_no_win = candidate_cost_aware_metrics(all_loss_rows, lambda r: True, "AllLossesRemoved")
    check("19-B) base 승자 0건이면 winner_damage_base가 '해당없음(base 승자 0건)'",
          cam_no_win["winner_damage_base"] == "해당없음(base 승자 0건)")

    # ── 19-C) split_historical_forward() — 경계값 및 forward 빈 표본 ──
    # 2026-08-26 date-boundary reclosure(민우님 지적): 실제 적용 시점이
    # 8/26 장 시작 전으로 확정되어 CANDIDATE_A_FORWARD_START_DATE가
    # "20260826"으로 수정됨 — 경계 테스트도 그에 맞춰 하루 당김
    # (8/25=historical, 8/26=forward 경계값 그 자체, 8/28=forward).
    rows_split = [
        {"trade_date": "20260825"}, {"trade_date": "20260826"}, {"trade_date": "20260828"},
    ]
    hist_c, fwd_c = split_historical_forward(rows_split, CANDIDATE_A_FORWARD_START_DATE)
    check("19-C) CANDIDATE_A_FORWARD_START_DATE == '20260826'(민우님 확정값, "
          "실제 적용일이 8/26 장 시작 전으로 확정되어 8/27→8/26으로 재closure됨)",
          CANDIDATE_A_FORWARD_START_DATE == "20260826")
    check("19-C) trade_date < forward_start_date는 historical(20260825만 해당)",
          [r["trade_date"] for r in hist_c] == ["20260825"])
    check("19-C) trade_date == forward_start_date(경계값 20260826)는 forward에 포함(historical 아님)",
          "20260826" in [r["trade_date"] for r in fwd_c]
          and "20260826" not in [r["trade_date"] for r in hist_c])
    check("19-C) trade_date > forward_start_date(20260828)도 forward에 포함",
          "20260828" in [r["trade_date"] for r in fwd_c])
    all_historical = [{"trade_date": "20260820"}, {"trade_date": "20260825"}]
    hist_only, fwd_empty = split_historical_forward(all_historical, CANDIDATE_A_FORWARD_START_DATE)
    check("19-C) forward 표본이 아직 없으면 조용히 빈 리스트를 반환(historical로 끌어와 채우지 않음)",
          len(hist_only) == 2 and fwd_empty == [])

    # ── 19-D) run() E2E — historical/forward 두 날짜의 bundle을 함께
    # 넘겼을 때 표본이 절대 하나로 섞이지 않고 별도 regime으로 집계되며,
    # 새 CSV 두 개가 실제로 생성되는지 확인 ────────────────────────────
    # date_fwd를 CANDIDATE_A_FORWARD_START_DATE와 정확히 같은 값(실제
    # 최초 forward 거래일인 8/26 그 자체)으로 둬서, "오늘(8/26) 데이터가
    # 최초 forward 표본"이라는 민우님 지적을 그대로 회귀고정합니다.
    date_hist = "20260825"  # < CANDIDATE_A_FORWARD_START_DATE → historical
    rows_hist = [
        # F2 + spike==False → CandidateA 제거 대상, base LOSS
        _buy("2026-08-25T09:00:00", "CA_H1", 10000, 10, "BH1", upside="0.20",
             rebound_volume_spike="False"),
        _sell("2026-08-25T09:10:00", "CA_H1", 9900, 10, "SH1", "트레일링 스탑", "10.0", 10000),
        # F2지만 spike==True → CandidateA는 안 건드림
        _buy("2026-08-25T09:20:00", "CA_H2", 10000, 10, "BH2", upside="0.20",
             rebound_volume_spike="True"),
        _sell("2026-08-25T09:30:00", "CA_H2", 9900, 10, "SH2", "트레일링 스탑", "10.0", 10000),
        # F2 밖(upside 높음) → 어느 후보에도 제거 대상 아님
        _buy("2026-08-25T09:40:00", "CA_H3", 10000, 10, "BH3", upside="3.00",
             rebound_volume_spike="False"),
        _sell("2026-08-25T09:50:00", "CA_H3", 10200, 10, "SH3", "트레일링 스탑", "10.0", 10000),
    ]
    date_fwd = "20260826"  # == CANDIDATE_A_FORWARD_START_DATE(경계 그 자체) → forward
    rows_fwd = [
        # F2 + spike==False → CandidateA 제거 대상, base LOSS
        _buy("2026-08-26T09:00:00", "CA_F1", 10000, 10, "BF1", upside="0.20",
             rebound_volume_spike="False"),
        _sell("2026-08-26T09:10:00", "CA_F1", 9800, 10, "SF1", "트레일링 스탑", "10.0", 10000),
        # F2 밖 → 제거 대상 아님
        _buy("2026-08-26T09:20:00", "CA_F2", 10000, 10, "BF2", upside="3.00",
             rebound_volume_spike="False"),
        _sell("2026-08-26T09:30:00", "CA_F2", 10100, 10, "SF2", "트레일링 스탑", "10.0", 10000),
    ]
    bundle_hist19 = make_bundle(tmp_root, date_hist, rows_hist)
    bundle_fwd19 = make_bundle(tmp_root, date_fwd, rows_fwd)
    out_dir19 = tmp_root / "out_v1_2_candA_forward"
    result19 = run([str(bundle_hist19), str(bundle_fwd19)], str(out_dir19))
    check("19-D) run() 결과의 historical_rows가 20260825 3건만 포함(20260826 섞이지 않음)",
          len(result19["historical_rows"]) == 3
          and all(r["trade_date"] == "20260825" for r in result19["historical_rows"]))
    check("19-D) run() 결과의 forward_rows가 20260826(경계값 그 자체) 2건만 포함(20260825 섞이지 않음)",
          len(result19["forward_rows"]) == 2
          and all(r["trade_date"] == "20260826" for r in result19["forward_rows"]))
    hist_candA19 = next(c for c in result19["cost_aware_historical"] if c["candidate"].startswith("CandidateA"))
    fwd_candA19 = next(c for c in result19["cost_aware_forward"] if c["candidate"].startswith("CandidateA"))
    combined_candA19 = next(c for c in result19["cost_aware_combined"] if c["candidate"].startswith("CandidateA"))
    check("19-D) HISTORICAL CandidateA: n_trades_in_scope==3, removed_trades==1(CA_H1만)",
          hist_candA19["n_trades_in_scope"] == 3 and hist_candA19["removed_trades"] == 1)
    check("19-D) FORWARD CandidateA: n_trades_in_scope==2, removed_trades==1(CA_F1만)",
          fwd_candA19["n_trades_in_scope"] == 2 and fwd_candA19["removed_trades"] == 1)
    check("19-D) COMBINED(참고용) CandidateA: n_trades_in_scope==5(3+2, historical+forward 합)",
          combined_candA19["n_trades_in_scope"] == 5 and combined_candA19["removed_trades"] == 2)
    check("19-D) 각 regime 라벨에 [HISTORICAL]/[FORWARD]/[COMBINED(참고용)]이 명시적으로 표기됨",
          "[HISTORICAL]" in hist_candA19["candidate"]
          and "[FORWARD]" in fwd_candA19["candidate"]
          and "[COMBINED(참고용)]" in combined_candA19["candidate"])
    check("19-D) result에 cost_aware_summary_rows/cost_aware_per_day_rows 키가 포함됨",
          "cost_aware_summary_rows" in result19 and "cost_aware_per_day_rows" in result19
          and len(result19["cost_aware_summary_rows"]) > 0)
    check("19-D) candidate_cost_aware_summary.csv가 실제로 생성됨",
          (out_dir19 / "candidate_cost_aware_summary.csv").is_file())
    check("19-D) candidate_cost_aware_per_day.csv가 실제로 생성됨",
          (out_dir19 / "candidate_cost_aware_per_day.csv").is_file())
    with (out_dir19 / "candidate_cost_aware_summary.csv").open(encoding="utf-8") as f:
        summary_csv_rows = list(csv.DictReader(f))
    check("19-D) candidate_cost_aware_summary.csv 헤더에 skip_precision_base/loss_recall_base/"
          "winner_damage_base가 포함됨",
          {"skip_precision_base", "loss_recall_base", "winner_damage_base"}
          <= set(summary_csv_rows[0].keys()) if summary_csv_rows else False)

    # ── build_cost_aware_report() / flatten_cost_aware_reports() 자체
    # 단위 검증 — 정확히 F2/CandidateA/CandidateB 3개만 반환하는지 ──────
    report19 = build_cost_aware_report(rows_cam, "UNITTEST")
    check("19-E) build_cost_aware_report는 정확히 4개(F2_baseline/CandidateA/CandidateB/CandidateG)만 반환 — "
          "2026-08-26 Sprint v1.3에서 CandidateG 추가되며 3개→4개",
          len(report19) == 4
          and report19[0]["candidate"].startswith("F2_baseline")
          and report19[1]["candidate"].startswith("CandidateA")
          and report19[2]["candidate"].startswith("CandidateB")
          and report19[3]["candidate"].startswith("CandidateG"))
    summary19, per_day19 = flatten_cost_aware_reports(report19)
    check("19-E) flatten_cost_aware_reports: summary는 candidate당 1행, per_day 중첩 dict는 제외됨",
          len(summary19) == 4 and all("per_day_base_delta_pct" not in row for row in summary19))
    check("19-E) flatten_cost_aware_reports: per_day는 candidate x trade_date 조합만큼 행이 생김",
          len(per_day19) == sum(len(r["per_day_base_delta_pct"]) for r in report19))

    # ══════════════════════════════════════════════════════════════
    # 20. Sprint v1.3 (2026-08-26, 민우님 지시): Candidate G(갭눌림D,
    # entry_reason 텍스트 파싱) + Candidate M1(5분 checkpoint
    # price>VWAP AND MACD>signal, min_profit_extension_shadow.csv
    # 조인) — production 로직/전략 파라미터는 전혀 건드리지 않고
    # offline 분석 도구에만 추가된 자동 분류 기능을 검증합니다. 세
    # 후보 조건 자체는 민우님 지시로 고정되어 있으며(임의 튜닝 금지),
    # 이 테스트들은 그 고정된 조건이 정확히 구현됐는지만 검증합니다.
    # ══════════════════════════════════════════════════════════════

    # ── 20-A) parse_gap_pullback_d() 순수 파싱 경계값 ─────────────────
    check("20-A) '갭눌림D✓(+3.2%)' 포함 문자열 → True",
          parse_gap_pullback_d("MACD상승 갭눌림D✓(+3.2%)") is True)
    check("20-A) '갭눌림D✗' 포함 문자열 → False",
          parse_gap_pullback_d("MACD상승 갭눌림D✗") is False)
    check("20-A) 마커가 전혀 없는 문자열 → None(결측, False로 추정 안 함)",
          parse_gap_pullback_d("MACD상승 RSI과매수아님") is None)
    check("20-A) 빈 문자열/None → None(결측)",
          parse_gap_pullback_d("") is None and parse_gap_pullback_d(None) is None)

    # ── 20-B) build_trade_features()가 entry_reason/gap_pullback_d를
    # BUY 행의 entry_reason 텍스트에서 정확히 채우는지 ────────────────
    date20b = "20260916"
    rows20b = [
        _buy("2026-09-16T09:00:00", "GAP001", 10000, 10, "BG01", upside="2.0",
             entry_reason="눌림목 매수 갭눌림D✓(+4.1%)"),
        _sell("2026-09-16T09:10:00", "GAP001", 10100, 10, "SG01", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-16T09:20:00", "GAP002", 10000, 10, "BG02", upside="2.0",
             entry_reason="눌림목 매수 갭눌림D✗"),
        _sell("2026-09-16T09:30:00", "GAP002", 10100, 10, "SG02", "트레일링 스탑", "10.0", 10000),
        _buy("2026-09-16T09:40:00", "GAP003", 10000, 10, "BG03", upside="2.0",
             entry_reason="다른 전략 진입 사유(갭눌림D 마커 없음)"),
        _sell("2026-09-16T09:50:00", "GAP003", 10100, 10, "SG03", "트레일링 스탑", "10.0", 10000),
    ]
    bundle20b = make_bundle(tmp_root, date20b, rows20b)
    features20b, _ = build_trade_features([load_bundle_day(bundle20b)])
    by_sym20b = {r["symbol"]: r for r in features20b}
    check("20-B) GAP001: entry_reason 원문이 그대로 보존됨(감사용)",
          "갭눌림D✓" in by_sym20b["GAP001"]["entry_reason"])
    check("20-B) GAP001: gap_pullback_d==True", by_sym20b["GAP001"]["gap_pullback_d"] is True)
    check("20-B) GAP002: gap_pullback_d==False", by_sym20b["GAP002"]["gap_pullback_d"] is False)
    check("20-B) GAP003: 마커 없으면 gap_pullback_d는 None(결측, False로 추정 안 함)",
          by_sym20b["GAP003"]["gap_pullback_d"] is None)

    # ── 20-C) _candidate_g_pred() + candidate_cost_aware_metrics() 통합 —
    # upside 문턱과 무관한 독립 조건임을 확인(F2/A/B와 달리 upside가
    # 전부 3.0%로 높아도 gap_pullback_d==True만으로 제거 대상이 됨) ────
    rows_cam_g = [
        {"trade_date": "20260826", "symbol": "G1", "upside_to_recent_high_pct": 3.0,
         "gap_pullback_d": True, "base_outcome": LOSS,
         "base_net_pnl_pct": -0.4, "base_net_pnl_krw": -400},
        {"trade_date": "20260826", "symbol": "G2", "upside_to_recent_high_pct": 3.0,
         "gap_pullback_d": True, "base_outcome": WIN,
         "base_net_pnl_pct": 0.6, "base_net_pnl_krw": 600},
        {"trade_date": "20260826", "symbol": "G3", "upside_to_recent_high_pct": 3.0,
         "gap_pullback_d": False, "base_outcome": LOSS,
         "base_net_pnl_pct": -0.2, "base_net_pnl_krw": -200},
        {"trade_date": "20260826", "symbol": "G4", "upside_to_recent_high_pct": 3.0,
         "gap_pullback_d": None, "base_outcome": WIN,
         "base_net_pnl_pct": 0.3, "base_net_pnl_krw": 300},
    ]
    removed_g = {r["symbol"] for r in rows_cam_g if _candidate_g_pred(r)}
    check("20-C) _candidate_g_pred가 정확히 G1,G2만 선택함(gap_pullback_d==True인 것만, "
          "upside 3.0%로 F2 문턱 밖이어도 상관없이 독립 판정)",
          removed_g == {"G1", "G2"})
    check("20-C) gap_pullback_d 결측(G4, None)은 True로 추정되지 않아 제거 대상에서 자동 제외됨",
          "G4" not in removed_g)
    cam_g = candidate_cost_aware_metrics(rows_cam_g, _candidate_g_pred, "TestCandidateG")
    check("20-C) CandidateG의 removed_trades==2(G1,G2)", cam_g["removed_trades"] == 2)
    check("20-C) CandidateG의 skip_precision_base==50%(제거 2건[G1,G2] 중 base LOSS는 G1 하나)",
          cam_g["skip_precision_base"] == "50%")

    # ── 20-D) build_trade_features()의 min_profit_extension_shadow 조인
    # (Candidate M1 checkpoint 필드) — 매칭 성공/매칭 실패/트리거 유형
    # 불일치(우연히 근접 행이 있어도 MIN_PROFIT_5M이 아니면 조인 금지)
    # 세 케이스를 모두 검증 ─────────────────────────────────────────
    date20d = "20260917"
    rows20d = [
        _buy("2026-09-17T09:00:00", "MPJ001", 10000, 10, "BM01", upside="2.0", entry_score="7"),
        _sell("2026-09-17T09:06:00", "MPJ001", 10020, 10, "SM01",
              "entry_watch 최소수익미달청산 — 매수 후 6.0분, 수익률 +0.20%", "6.0", 10000),
        _buy("2026-09-17T09:10:00", "MPJ002", 10000, 10, "BM02", upside="2.0", entry_score="7"),
        _sell("2026-09-17T09:16:00", "MPJ002", 10015, 10, "SM02",
              "entry_watch 최소수익미달청산 — 매수 후 6.0분, 수익률 +0.15%", "6.0", 10000),
        _buy("2026-09-17T09:20:00", "MPJ003", 10000, 10, "BM03", upside="2.0", entry_score="7"),
        _sell("2026-09-17T09:26:00", "MPJ003", 10300, 10, "SM03", "트레일링 스탑", "6.0", 10000),
    ]
    min_profit_rows20d = [
        # MPJ001: 실제 SELL 시각(09:06:00)과 30ms 차이 → 근접 매칭 성공
        {"timestamp": "2026-09-17T09:06:00.030000", "symbol": "MPJ001", "entry_time": "2026-09-17T09:00:05",
         "holding_minutes": "6.0", "pnl_pct": "0.2", "price": "10020", "vwap": "9940",
         "price_vs_vwap_pct": "0.8", "macd": "1.2", "macd_signal": "0.9", "macd_above_signal": "True",
         "rsi": "55", "ma5": "10010", "ma20": "9990", "peak_pnl_pct": "1.5",
         "drawdown_from_peak_pct": "-1.3", "upside_to_recent_high_pct": "2.0", "minute_data_stale": "False"},
        # MPJ003용 — 이 청산은 MIN_PROFIT_5M이 아니라 트레일링 스탑인데도
        # 심볼/시각이 우연히 근접한 min_profit_extension_shadow 행이 존재하는
        # 경우입니다(production 로거는 최소수익미달 분기에서만 이 CSV에
        # 기록하므로 실제로는 있을 수 없는 인위적 케이스지만, "trigger_type
        # 게이트가 실제로 걸려있는지" 확실히 확인하기 위해 일부러 넣습니다).
        {"timestamp": "2026-09-17T09:26:00.030000", "symbol": "MPJ003", "entry_time": "2026-09-17T09:20:05",
         "holding_minutes": "6.0", "pnl_pct": "3.0", "price": "10300", "vwap": "10000",
         "price_vs_vwap_pct": "3.0", "macd": "2.0", "macd_signal": "1.0", "macd_above_signal": "True",
         "rsi": "70", "ma5": "10200", "ma20": "10100", "peak_pnl_pct": "3.0",
         "drawdown_from_peak_pct": "0.0", "upside_to_recent_high_pct": "2.0", "minute_data_stale": "False"},
        # MPJ002용 행은 의도적으로 없음 → 근접 매칭 실패 케이스
    ]
    bundle20d = make_bundle(tmp_root, date20d, rows20d, min_profit_shadow_rows=min_profit_rows20d,
                             include_min_profit_shadow_file=True)
    features20d, _ = build_trade_features([load_bundle_day(bundle20d)])
    by_sym20d = {r["symbol"]: r for r in features20d}
    check("20-D) MPJ001(MIN_PROFIT_5M + 근접 매칭 성공): checkpoint_price_vs_vwap_pct==0.8",
          abs(by_sym20d["MPJ001"]["checkpoint_price_vs_vwap_pct"] - 0.8) < 1e-9)
    check("20-D) MPJ001: checkpoint_macd_above_signal==True",
          by_sym20d["MPJ001"]["checkpoint_macd_above_signal"] is True)
    check("20-D) MPJ001: checkpoint_peak_pnl_pct==1.5",
          abs(by_sym20d["MPJ001"]["checkpoint_peak_pnl_pct"] - 1.5) < 1e-9)
    check("20-D) MPJ002(MIN_PROFIT_5M이지만 매칭되는 min_profit_extension_shadow 행 없음): "
          "checkpoint 필드 전부 결측(빈 값, 추정 안 함)",
          by_sym20d["MPJ002"]["checkpoint_price_vs_vwap_pct"] == ""
          and by_sym20d["MPJ002"]["checkpoint_macd_above_signal"] == ""
          and by_sym20d["MPJ002"]["checkpoint_peak_pnl_pct"] == "")
    check("20-D) MPJ002: 매칭 실패 사유가 data_quality_flag에 명시됨",
          "min_profit_extension_shadow 근접 매칭 실패" in by_sym20d["MPJ002"]["data_quality_flag"])
    check("20-D) MPJ003(트레일링 청산, MIN_PROFIT_5M 아님): 우연히 근접한 shadow 행이 있어도 "
          "entry_watch_trigger_type 게이트 때문에 checkpoint 필드를 절대 채우지 않음",
          by_sym20d["MPJ003"]["checkpoint_price_vs_vwap_pct"] == ""
          and by_sym20d["MPJ003"]["checkpoint_macd_above_signal"] == ""
          and by_sym20d["MPJ003"]["checkpoint_peak_pnl_pct"] == "")

    # ── 20-E) extension_rule_candidates()의 M1 규칙 — "5분 판단 시점"
    # checkpoint 값을 쓰는지, R1/R2가 참조하는 "진입 시점" 값
    # (current_vs_vwap_pct/macd_above_signal)을 잘못 재사용하지 않는지
    # 교차 검증합니다(민우님 지시: R1/R2가 갖고 있던 바로 그 결함을 M1이
    # 실제로 고쳤는지 확실히 증명). T1/T2는 진입 시점 값과 checkpoint
    # 값이 의도적으로 서로 반대이고, extension_label_5m도 서로 다르게
    # 둬서(helped/hurt) 어느 거래가 각 규칙에서 실제로 집계됐는지까지
    # 명확히 구분합니다.
    per_trade_m1_cross = [
        # T1: 진입 시점엔 VWAP 아래(-1.0)/MACD 아래(False) → R1/R2는 탈락
        # 해야 함. 5분 checkpoint는 VWAP 위(+0.5)/MACD 위(True) → M1만
        # 통과해야 함.
        {"trade_date": "20260826", "symbol": "T1", "actual_gross_pnl_pct": 0.3,
         "actual_base_net_pct": 0.1, "fwd5m_base_net_pct": 0.1,
         "actual_stress_net_pct": -0.2, "fwd5m_price_return_pct": 0.3,
         "current_vs_vwap_pct": -1.0, "macd_above_signal": False,
         "checkpoint_price_vs_vwap_pct": 0.5, "checkpoint_macd_above_signal": True,
         "entry_score": 3, "extension_label_5m": "EXTEND_HELPED"},
        # T2: 진입 시점엔 VWAP 위(+1.0)/MACD 위(True) → R1/R2는 통과해야
        # 함. 5분 checkpoint는 VWAP 아래(-0.5)/MACD 아래(False) → M1은
        # 탈락해야 함(M1이 R1/R2와 같은 필드를 잘못 재사용했다면 이
        # 거래를 통과시켰을 것).
        {"trade_date": "20260826", "symbol": "T2", "actual_gross_pnl_pct": 0.4,
         "actual_base_net_pct": -0.1, "fwd5m_base_net_pct": -0.1,
         "actual_stress_net_pct": -0.3, "fwd5m_price_return_pct": 0.4,
         "current_vs_vwap_pct": 1.0, "macd_above_signal": True,
         "checkpoint_price_vs_vwap_pct": -0.5, "checkpoint_macd_above_signal": False,
         "entry_score": 3, "extension_label_5m": "EXTEND_HURT"},
    ]
    ext_rules_cross = extension_rule_candidates(per_trade_m1_cross)
    r1_cross = next(r for r in ext_rules_cross if r["rule"].startswith("R1"))
    r2_cross = next(r for r in ext_rules_cross if r["rule"].startswith("R2"))
    m1_cross = next(r for r in ext_rules_cross if r["rule"].startswith("M1"))
    check("20-E) M1 규칙은 valid_evidence=True(checkpoint 시점 값 사용, R1/R2의 결함을 고침)",
          m1_cross["valid_evidence"] is True)
    check("20-E) R1(진입 시점 VWAP 기준)은 T2만 통과(helped=0/hurt=1) — 진입 시점 값을 그대로 씀",
          r1_cross["extension_qualified_trades"] == 1
          and r1_cross["helped"] == 0 and r1_cross["hurt"] == 1)
    check("20-E) R2(진입 시점 MACD 기준)도 T2만 통과(helped=0/hurt=1)",
          r2_cross["extension_qualified_trades"] == 1
          and r2_cross["helped"] == 0 and r2_cross["hurt"] == 1)
    check("20-E) M1(checkpoint 기준)은 정반대로 T1만 통과(helped=1/hurt=0) — "
          "R1/R2가 놓친 T1을 checkpoint 값 덕분에 정확히 잡아냄",
          m1_cross["extension_qualified_trades"] == 1
          and m1_cross["helped"] == 1 and m1_cross["hurt"] == 0)

    # T3: checkpoint 필드가 결측("" — min_profit_extension_shadow 매칭 실패를
    # 의미) → gross는 양수여도 M1은 결측을 True/양수로 추정하지 않고 탈락시킴.
    per_trade_m1_missing = [
        {"trade_date": "20260826", "symbol": "T3", "actual_gross_pnl_pct": 0.5,
         "actual_base_net_pct": 0.2, "fwd5m_base_net_pct": 0.2,
         "actual_stress_net_pct": -0.1, "fwd5m_price_return_pct": 0.5,
         "current_vs_vwap_pct": 1.0, "macd_above_signal": True,
         "checkpoint_price_vs_vwap_pct": "", "checkpoint_macd_above_signal": "",
         "entry_score": 3, "extension_label_5m": "NEUTRAL"},
    ]
    m1_missing = next(r for r in extension_rule_candidates(per_trade_m1_missing) if r["rule"].startswith("M1"))
    check("20-E) checkpoint 필드가 결측이면 gross가 양수여도 M1은 qualified 0건(추정 금지)",
          m1_missing["extension_qualified_trades"] == 0)

    # T4: 재closure 회귀 고정(민우님 리뷰 지적) — M1의 고정 조건은
    # "MIN_PROFIT_5M AND price>VWAP AND MACD>signal"이지 R1/R2/R3처럼
    # actual_gross_pnl_pct>0 게이트가 없습니다. 최초 구현은 이 게이트를
    # 잘못 붙여 M1을 만들게 한 핵심 회복 사례(8/25 052690 -0.68%, 8/26
    # 003490 -0.10%, 8/26 006360 -0.43% — 전부 checkpoint는 우호적인데
    # 실제 gross는 음수)를 전부 놓쳤습니다. T4는 그 실측 사례를 그대로
    # 재현한 합성 거래(음수 gross + 우호적 checkpoint)로, M1이 pnl 부호와
    # 무관하게 qualified 처리하는지 회귀 고정합니다.
    per_trade_m1_recovery = [
        {"trade_date": "20260826", "symbol": "T4", "actual_gross_pnl_pct": -0.68,
         "actual_base_net_pct": -0.9, "fwd5m_base_net_pct": -0.2,
         "actual_stress_net_pct": -1.4, "fwd5m_price_return_pct": 0.1,
         "current_vs_vwap_pct": -1.0, "macd_above_signal": False,
         "checkpoint_price_vs_vwap_pct": 1.23, "checkpoint_macd_above_signal": True,
         "entry_score": 3, "extension_label_5m": "EXTEND_HELPED"},
    ]
    m1_recovery = next(r for r in extension_rule_candidates(per_trade_m1_recovery) if r["rule"].startswith("M1"))
    check("20-E) [재closure] gross가 음수(-0.68%)여도 checkpoint가 우호적(price>VWAP AND "
          "MACD>signal)이면 M1은 qualified 1건 — pnl 부호는 M1의 게이트가 아님(민우님 리뷰 반영)",
          m1_recovery["extension_qualified_trades"] == 1)

    # ── 20-F) extension_rule_candidates_by_regime() — historical/forward
    # 분리가 Candidate A의 cost-aware regime 분리와 동일한 원칙으로
    # 동작하는지 확인 ──────────────────────────────────────────────
    per_trade_regime = [
        {"trade_date": "20260825", "symbol": "RH1", "actual_gross_pnl_pct": 0.5,
         "actual_base_net_pct": 0.2, "fwd5m_base_net_pct": 0.2,
         "actual_stress_net_pct": -0.1, "fwd5m_price_return_pct": 0.5,
         "current_vs_vwap_pct": 1.0, "macd_above_signal": True,
         "checkpoint_price_vs_vwap_pct": 1.0, "checkpoint_macd_above_signal": True,
         "entry_score": 3, "extension_label_5m": "EXTEND_HELPED"},
        {"trade_date": "20260826", "symbol": "RF1", "actual_gross_pnl_pct": 0.5,
         "actual_base_net_pct": 0.2, "fwd5m_base_net_pct": 0.2,
         "actual_stress_net_pct": -0.1, "fwd5m_price_return_pct": 0.5,
         "current_vs_vwap_pct": 1.0, "macd_above_signal": True,
         "checkpoint_price_vs_vwap_pct": 1.0, "checkpoint_macd_above_signal": True,
         "entry_score": 3, "extension_label_5m": "EXTEND_HELPED"},
    ]
    rules_by_regime = extension_rule_candidates_by_regime(per_trade_regime, CANDIDATE_A_FORWARD_START_DATE)
    m1_name = "M1_5minCheckpoint_price>VWAP_AND_MACD_above_signal"
    m1_hist = next(r for r in rules_by_regime if r["rule"] == f"{m1_name}[HISTORICAL]")
    m1_fwd = next(r for r in rules_by_regime if r["rule"] == f"{m1_name}[FORWARD]")
    m1_comb = next(r for r in rules_by_regime if r["rule"] == f"{m1_name}[COMBINED(참고용)]")
    check("20-F) HISTORICAL regime엔 20260825(RH1)만 포함 — M1 qualified==1",
          m1_hist["extension_qualified_trades"] == 1)
    check("20-F) FORWARD regime엔 20260826(RF1, 경계값 그 자체 포함)만 포함 — M1 qualified==1",
          m1_fwd["extension_qualified_trades"] == 1)
    check("20-F) COMBINED(참고용)엔 둘 다 포함 — M1 qualified==2(절대 하나로 섞어 부풀리지 않되 "
          "참고용 합산치는 정확히 계산됨)",
          m1_comb["extension_qualified_trades"] == 2)

    # ── 20-G) run() E2E — Sprint v1.3 신규 산출물(CSV/dict key/CandidateG
    # wiring/신규 feature 컬럼)이 실제로 생성되는지 확인 ──────────────
    out_dir20 = tmp_root / "out_v1_3"
    result20 = run([str(bundle20b), str(bundle20d)], str(out_dir20))
    check("20-G) run() 결과에 extension_rules_by_regime 키가 포함됨",
          "extension_rules_by_regime" in result20 and len(result20["extension_rules_by_regime"]) > 0)
    check("20-G) exit_extension_study_by_regime.csv가 실제로 생성됨",
          (out_dir20 / "exit_extension_study_by_regime.csv").is_file())
    cost_aware_g_forward = [c for c in result20["cost_aware_forward"] if c["candidate"].startswith("CandidateG")]
    check("20-G) run()의 cost_aware_forward에 CandidateG가 포함됨(build_cost_aware_report "
          "4번째 후보 wiring 확인)",
          len(cost_aware_g_forward) == 1)
    with (out_dir20 / "trade_feature_table.csv").open(encoding="utf-8") as f:
        trade_feature_columns = set(next(csv.reader(f)))
    check("20-G) trade_feature_table.csv 헤더에 entry_reason/gap_pullback_d/checkpoint_* "
          "5개 컬럼이 모두 포함됨",
          {"entry_reason", "gap_pullback_d", "checkpoint_price_vs_vwap_pct",
           "checkpoint_macd_above_signal", "checkpoint_peak_pnl_pct"} <= trade_feature_columns)

finally:
    shutil.rmtree(tmp_root, ignore_errors=True)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
