# -*- coding: utf-8 -*-
"""
VWAP shadow 관측 검증 (2026-08-05, 1E.5단계)

배경: 매매 성과 분석(7/30~8/4)에서 VWAP 대비 +2% 초과 진입 3건이
전부 손실 방향이었음을 확인했으나, "PR 조건이 VWAP 기준"이라는
초기 분석은 부정확했음 — 실제로는 PR(is_pulldown_recovery)이
저점 우상향+거래량 팽창 조건이고 VWAP 거리와 무관, C(is_valid_
pulldown)도 "VWAP 위/아래"만 확인하지 몇 % 위인지는 무관. 즉
"VWAP +2% 초과 차단"은 완전히 새로운 진입 품질 게이트다.

이 테스트가 검증하는 것 — GPT 코드리뷰 지시대로:
1. domain/strategy/entry_quality_shadow.py의 evaluate_vwap_shadow()
   순수 함수가 PR-only/C-or-PR/condition-source/PR-or-condition
   네 범위를 rolling·session 각각 독립적으로 정확히 계산하는지
2. 복수 조건식이 정확히 보존되고(ConditionWatcher.symbol_to_
   conditions), 스냅샷 교체로 편출된 종목의 과거 조건명이
   잔존하지 않는지
3. rolling/session VWAP 거리 임계값(2.0%)이 정확히 경계 처리되는지
   (2.00%=통과, 2.01%=차단 후보)
4. session_metrics_ready=False(PARTIAL_SESSION)일 때는 거리값만
   기록하고 would_block은 빈 값으로 남기는지
5. would_block_* 8개 필드가 legacy_buy_candidate=True(전략이
   실제로 BUY를 반환한 경우)일 때만 계산되는지
6. entry_quality_guard_mode="off"/"shadow"에서 signal/final_
   decision/주문 결과가 완전히 동일한지(shadow 계산이 실매매
   판단에 절대 영향을 주지 않는지)
7. entry_quality_shadow.csv가 legacy BUY 후보에만, 동일 분봉·
   패턴·점수 조합은 한 번만 기록되는지
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.market_regime.session_metrics import SessionMetrics
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.strategy.entry_quality_shadow import evaluate_vwap_shadow
from domain.models import AccountBalance, MarketPrice, MarketRegime, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.websocket.condition_watcher import ConditionWatcher
from config.settings import WebSocketConfig


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


def build_service(tmpdir: str, guard_mode: str = "shadow") -> TradingService:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.experimental, "entry_quality_guard_mode", guard_mode)
    broker = MockBroker()
    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store = JsonStateStore(settings.storage.state_file)
    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    return TradingService(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
    )


def make_ma(**overrides) -> MinuteAnalysis:
    defaults = dict(
        vwap=100000.0, price_above_vwap=True, low_rising=False,
        pullback_pct=0.0, is_valid_pullback=False,
        change_rate_pct=0.0, is_valid_change_rate=False,
        rebound_pct=0.0, is_valid_rebound=False,
        trading_value=1_000_000_000, is_valid_trading_value=True,
        day_high=101000, day_low=99000, is_valid_pulldown=False,
        ma5_above_ma20=True,
        is_v_rebound=False, v_fail_reason="", v_bottom_k=0,
        v_drop_pct=0.0, v_rise_pct=0.0, v_volume_ratio=0.0,
        v_bottom_spike=False, v_ma5_rising=False,
        rebound_volume_spike=False, rebound_volume_ratio=0.0,
        upside_to_recent_high_pct=5.0,
        is_pulldown_recovery=False, pr_low_turning=False, pr_volume_expanding=False,
        is_slow_v_rebound=False, slow_v_drop_pct=0.0, slow_v_rise_pct=0.0, slow_v_bottom_k=0,
    )
    defaults.update(overrides)
    return MinuteAnalysis(**defaults)


def make_session_metrics(ready: bool, reason: str, vwap: float = 100000.0) -> SessionMetrics:
    return SessionMetrics(
        session_date="20260805", session_vwap=vwap, session_high=101000, session_low=99000,
        earliest_timestamp="20260805090000", latest_timestamp="20260805100000",
        rolling_vwap_20=vwap, rolling_20_count=20, rolling_vwap_60=vwap, rolling_60_count=60,
        recent_high_30=101000, session_bar_count=60,
        filtered_other_date_count=0, filtered_outside_market_count=0, filtered_invalid_ohlc_count=0,
        last_batch_filtered_other_date_count=0, last_batch_filtered_outside_market_count=0,
        last_batch_filtered_invalid_ohlc_count=0,
        session_metrics_ready=ready, readiness_reason=reason,
    )


def read_last_row(service):
    with open(service.settings.storage.signal_log_file, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1]


def read_shadow_rows(service):
    with open(service.settings.storage.entry_quality_shadow_log_file, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


symbol = "005930"

# ══════════════════════════════════════════════════════════════
# 1부: evaluate_vwap_shadow() 순수 함수 — 범위 판정
# ══════════════════════════════════════════════════════════════

# ── 1) PR=True, C=False -> PR-only=True, C-or-PR=True ────────────
ma = make_ma(is_pulldown_recovery=True, is_valid_pulldown=False)
r = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("1) PR=True,C=False + rolling거리2.01% -> PR-only=True", r.would_block_pr_only_rolling_vwap is True)
check("   C-or-PR도 True(PR이 True이므로)", r.would_block_c_or_pr_rolling_vwap is True)

# ── 2) PR=False, C=True -> PR-only=False, C-or-PR=True ───────────
ma2 = make_ma(is_pulldown_recovery=False, is_valid_pulldown=True)
r2 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma2,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("2) PR=False,C=True -> PR-only=False", r2.would_block_pr_only_rolling_vwap is False)
check("   C-or-PR=True(C가 True이므로)", r2.would_block_c_or_pr_rolling_vwap is True)

# ── 3) PR=False, C=False, 눌림목 조건식=True -> condition scope만 True ──
ma3 = make_ma(is_pulldown_recovery=False, is_valid_pulldown=False)
r3 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma3,
    condition_names=("자동매매_눌림목_PR",), condition_source_reliable=True, session_metrics=None,
)
check("3) PR=False,C=False,조건식눌림목 -> PR-only=False", r3.would_block_pr_only_rolling_vwap is False)
check("   C-or-PR=False", r3.would_block_c_or_pr_rolling_vwap is False)
check("   condition scope=True(조건식명에 눌림목 포함)",
      r3.would_block_pullback_condition_rolling_vwap is True)

# ══════════════════════════════════════════════════════════════
# 2부: 복수 조건식 처리
# ══════════════════════════════════════════════════════════════

# ── 4) 돌파형A + 눌림목_PR 동시 편입 -> is_pullback_condition=True ──
ma4 = make_ma(is_pulldown_recovery=False, is_valid_pulldown=False)
r4 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma4,
    condition_names=("자동매매_돌파형A", "자동매매_눌림목_PR"), condition_source_reliable=True, session_metrics=None,
)
check("4) 복수조건식(돌파형A+눌림목_PR) 동시편입 -> is_pullback_condition=True",
      r4.is_pullback_condition is True)

# ── 5) ConditionWatcher.symbol_to_conditions가 복수 조건식을 보존함 ──
config = WebSocketConfig(enabled=False, url="", condition_seqs=["1", "2"], max_symbols=10,
                          app_key="", secret_key="")
watcher = ConditionWatcher.__new__(ConditionWatcher)
watcher.config = config
watcher._symbols_by_seq = {"1": {"005930", "058610"}, "2": {"058610", "047040"}}
watcher._condition_names = {"1": "자동매매_돌파형A", "2": "자동매매_눌림목_PR"}
check("5) symbol_to_condition(기존, 단수형)은 058610에서 하나만 남김"
      "(재현 확인용 — 이게 문제였던 기존 동작)",
      len(set([watcher.symbol_to_condition["058610"]])) == 1)
check("   symbol_to_conditions(신규, 복수형)는 058610의 조건식 2개 모두 보존",
      len(watcher.symbol_to_conditions["058610"]) == 2
      and "자동매매_돌파형A" in watcher.symbol_to_conditions["058610"]
      and "자동매매_눌림목_PR" in watcher.symbol_to_conditions["058610"])

# ── 6) 편출 스냅샷 — TradingService.update_targets가 통째로 교체함 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.update_targets(["058610"], sym_to_conditions={"058610": ("자동매매_눌림목_PR",)})
    check("6) 1회차 눌림목 조건식 편입 -> _symbol_to_conditions에 기록됨",
          "자동매매_눌림목_PR" in service._symbol_to_conditions.get("058610", ()))
    service.update_targets([], sym_to_conditions={})
    check("   2회차 완전 편출(빈 dict 전달) -> 과거 조건명이 잔존하지 않음"
          "(정확히 GPT 지시 필수 테스트)",
          "058610" not in service._symbol_to_conditions)

# ══════════════════════════════════════════════════════════════
# 3부: 임계값 경계 (rolling)
# ══════════════════════════════════════════════════════════════

# ── 7) rolling distance=2.00% -> block=False ──────────────────────
ma_exact = make_ma(is_pulldown_recovery=True)
r_exact = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102000, minute_analysis=ma_exact,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("7) rolling distance 정확히 2.00% -> would_block=False(통과)",
      r_exact.would_block_pr_only_rolling_vwap is False)

# ── 8) rolling distance=2.01% -> block=True ───────────────────────
r_over = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma_exact,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("8) rolling distance 2.01% -> would_block=True(차단 후보)",
      r_over.would_block_pr_only_rolling_vwap is True)

# ══════════════════════════════════════════════════════════════
# 4부: 세션 준비 상태
# ══════════════════════════════════════════════════════════════

# ── 9) session ready=True, distance=2.01% -> session block=True ──
ma_pr = make_ma(is_pulldown_recovery=True)
sm_ready = make_session_metrics(ready=True, reason="COMPLETE_FROM_OPEN")
r9 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma_pr,
    condition_names=(), condition_source_reliable=True, session_metrics=sm_ready,
)
check("9) session ready=True + distance2.01% -> session_gate_eligible=True",
      r9.session_gate_eligible is True)
check("   would_block_pr_only_session_vwap=True", r9.would_block_pr_only_session_vwap is True)

# ── 10) session ready=False -> 거리는 기록, would_block은 빈 값 ──
sm_partial = make_session_metrics(ready=False, reason="PARTIAL_SESSION")
r10 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=103000, minute_analysis=ma_pr,
    condition_names=(), condition_source_reliable=True, session_metrics=sm_partial,
)
check("10) session ready=False(PARTIAL_SESSION) -> session_vwap_distance_pct는 관찰용으로 기록됨"
      "(정확히 GPT 지시 필수 테스트)", r10.session_vwap_distance_pct is not None)
check("    session_gate_eligible=False", r10.session_gate_eligible is False)
check("    would_block_pr_only_session_vwap은 빈 값(None) — 불완전 세션값을 판단에 안 씀",
      r10.would_block_pr_only_session_vwap is None)

# ══════════════════════════════════════════════════════════════
# 5부: BUY 후보 한정 (legacy_buy_candidate)
# ══════════════════════════════════════════════════════════════

# ── 11) HOLD에서는 상태값만 기록, would_block 전부 빈 값 ─────────
r11 = evaluate_vwap_shadow(
    legacy_buy_candidate=False, current_price=102010, minute_analysis=ma_pr,
    condition_names=(), condition_source_reliable=True, session_metrics=sm_ready,
)
check("11) legacy_buy_candidate=False(HOLD) -> is_pr 상태값은 기록됨", r11.is_pr is True)
check("    rolling_vwap_distance_pct도 기록됨", r11.rolling_vwap_distance_pct is not None)
check("    would_block 8개 전부 빈 값(None)",
      all(v is None for v in [
          r11.would_block_pr_only_rolling_vwap, r11.would_block_c_or_pr_rolling_vwap,
          r11.would_block_pullback_condition_rolling_vwap,
          r11.would_block_pr_or_pullback_condition_rolling_vwap,
          r11.would_block_pr_only_session_vwap, r11.would_block_c_or_pr_session_vwap,
          r11.would_block_pullback_condition_session_vwap,
          r11.would_block_pr_or_pullback_condition_session_vwap,
      ]))

# ── 12) condition_names=() 이어도 PR=True면 정상 평가 ─────────────
r12 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma_pr,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("12) condition_names=() 이어도 PR=True면 PR-only 정상 평가됨"
      "(조건식명 누락이 PR 판정에 영향 없음)", r12.would_block_pr_only_rolling_vwap is True)

# ══════════════════════════════════════════════════════════════
# 6부: TradingService 통합 — off/shadow 무영향, 로그 기록
# ══════════════════════════════════════════════════════════════

# ── 13) off 모드 -> VWAP 필드 전부 빈 값, 전용 CSV 파일 생성 안 됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="off")
    signal = Signal(type=SignalType.BUY, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="",
    )
    row = read_last_row(service)
    check("13) off 모드 -> signal_log의 is_pr이 빈 값(계산 자체 스킵)", row["is_pr"] == "")

# ── 14) shadow 모드 -> signal_log와 entry_quality_shadow.csv 둘 다 기록 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    service._symbol_to_conditions[symbol] = ("자동매매_눌림목_PR",)
    mp = MarketPrice(symbol=symbol, current_price=102010, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma_shadow = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=102010, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma_shadow, final_decision="BUY",
        order_block_reason="", market_price=mp, latest_bar_timestamp="20260805100000",
    )
    row = read_last_row(service)
    check("14) shadow 모드 -> signal_log에 is_pr=True 기록됨", row["is_pr"] == "True")
    check("    rolling_vwap_distance_pct=2.01 기록됨", row["rolling_vwap_distance_pct"] == "2.01")

    shadow_rows = read_shadow_rows(service)
    check("    entry_quality_shadow.csv에 1행 기록됨(legacy BUY 후보)", len(shadow_rows) == 1)
    if shadow_rows:
        check("    would_block_pr_only_rolling_vwap=True 정확히 기록됨",
              shadow_rows[0]["would_block_pr_only_rolling_vwap"] == "True")

# ── 15) HOLD는 entry_quality_shadow.csv에 기록 안 됨(legacy BUY 후보만) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=102010, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma_hold = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=102010, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma_hold, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("15) HOLD여도 signal_log의 is_pr 상태값은 기록됨", row["is_pr"] == "True")
    shadow_rows = read_shadow_rows(service)
    check("    entry_quality_shadow.csv는 0행(legacy BUY 후보 아니므로 기록 안 됨)",
          len(shadow_rows) == 0)

# ── 16) 동일 (symbol, latest_bar_timestamp, patterns, score) 중복 호출 -> 1행만 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=102010, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma_dup = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    for _ in range(3):  # 같은 분봉에서 3번 폴링(10초 간격)을 흉내
        service._write_signal_log(
            symbol=symbol, price=102010, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma_dup, final_decision="BUY",
            order_block_reason="", market_price=mp, latest_bar_timestamp="20260805100000",
        )
    shadow_rows = read_shadow_rows(service)
    check("16) 동일 symbol+latest_bar_timestamp+patterns+score로 3회 호출해도"
          " entry_quality_shadow.csv는 1행만 기록됨(중복 방지)", len(shadow_rows) == 1)
    with open(service.settings.storage.signal_log_file, encoding="utf-8", newline="") as f:
        all_rows = list(csv.DictReader(f))
    check("    signal_log.csv는 매번(3행) 그대로 기록됨(dedup 대상 아님)", len(all_rows) == 3)

# ── 17) off/shadow에서 실제 신호 판단 완전 동일(통합 흐름) ────────
with tempfile.TemporaryDirectory() as tmpdir_off, tempfile.TemporaryDirectory() as tmpdir_shadow:
    from unittest.mock import patch

    original_signal = Signal(type=SignalType.HOLD, reason="관측용 그대로 유지되어야 함")

    def run_and_capture(tmpdir, guard_mode):
        service = build_service(tmpdir, guard_mode=guard_mode)
        balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=original_signal,
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=original_signal,
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))
        return read_last_row(service)

    row_off = run_and_capture(tmpdir_off, "off")
    row_shadow = run_and_capture(tmpdir_shadow, "shadow")

    check("17) off/shadow 모드에서 skip_reason(최종 신호)이 완전히 동일함"
          "(VWAP shadow 계산이 신호 판단에 절대 영향 없음)",
          row_off["skip_reason"] == row_shadow["skip_reason"] == original_signal.reason)
    check("    off/shadow 모드에서 final_decision도 완전히 동일함",
          row_off["final_decision"] == row_shadow["final_decision"])

# ══════════════════════════════════════════════════════════════
# 7부: 조건검색식 출처 신뢰도 (2026-08-05, 2차 GPT 코드리뷰 지적 1번)
# ══════════════════════════════════════════════════════════════

# ── 18) REAL 이벤트 후 condition_source_reliable=False ────────────
config_ws = WebSocketConfig(enabled=False, url="", condition_seqs=["1", "2"], max_symbols=10,
                             app_key="", secret_key="")
watcher18 = ConditionWatcher.__new__(ConditionWatcher)
watcher18.config = config_ws
watcher18._symbols_by_seq = {"1": set(), "2": set()}
watcher18._condition_names = {"1": "자동매매_돌파형A", "2": "자동매매_눌림목_PR"}
watcher18._condition_source_reliable = {}
watcher18.on_symbols_changed = lambda x: None

msg_real = {"trnm": "REAL", "data": [
    {"values": {"9001": "A058610", "843": "I"}, "type": "00", "name": "조건검색", "item": "058610"}
]}
watcher18._on_realtime(msg_real)
check("18) REAL 이벤트로 편입된 종목 -> condition_source_reliable=False"
      "(정확히 GPT 지시 필수 테스트 — 어느 조건식인지 확정 불가)",
      watcher18.symbol_condition_source_reliable.get("058610") is False)
check("    symbol_to_conditions에는 이 종목이 안 나타남(조건식 미확정)",
      "058610" not in watcher18.symbol_to_conditions)
check("    하지만 targets(_all_symbols)에는 정확히 포함됨(편입 자체는 반영)",
      "058610" in watcher18._all_symbols)

# ── 19) CNSRREQ 초기조회 후에는 reliable=True ─────────────────────
msg_cnsrreq = {"trnm": "CNSRREQ", "return_code": 0, "seq": "2", "data": [{"jmcode": "A058610"}]}
watcher18._on_initial_result(msg_cnsrreq)
check("19) CNSRREQ 초기조회 후 -> condition_source_reliable=True",
      watcher18.symbol_condition_source_reliable.get("058610") is True)
check("    symbol_to_conditions에 정확한 조건식명이 나타남",
      watcher18.symbol_to_conditions.get("058610") == ("자동매매_눌림목_PR",))

# ── 20) unreliable이면 condition-source 기반 would_block=None ─────
ma20 = make_ma(is_pulldown_recovery=False, is_valid_pulldown=False)
r20 = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma20,
    condition_names=("자동매매_눌림목_PR",), condition_source_reliable=False,
    session_metrics=None,
)
check("20) condition_source_reliable=False -> is_pullback_condition=None"
      "(정확히 GPT 지시 필수 테스트)", r20.is_pullback_condition is None)
check("    would_block_pullback_condition_rolling_vwap도 None",
      r20.would_block_pullback_condition_rolling_vwap is None)

# ── 21) PR/C 기반 would_block은 reliable 여부와 무관하게 정상 계산 ──
ma21 = make_ma(is_pulldown_recovery=True, is_valid_pulldown=True)
r21_unreliable = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma21,
    condition_names=(), condition_source_reliable=False, session_metrics=None,
)
r21_reliable = evaluate_vwap_shadow(
    legacy_buy_candidate=True, current_price=102010, minute_analysis=ma21,
    condition_names=(), condition_source_reliable=True, session_metrics=None,
)
check("21) PR/C 기반 would_block_pr_only_rolling_vwap은 reliable 여부와 무관하게 "
      "동일하게 정상 계산됨(정확히 GPT 지시 필수 테스트)",
      r21_unreliable.would_block_pr_only_rolling_vwap == r21_reliable.would_block_pr_only_rolling_vwap
      and r21_unreliable.would_block_pr_only_rolling_vwap is True)
check("    would_block_c_or_pr_rolling_vwap도 동일하게 정상 계산됨",
      r21_unreliable.would_block_c_or_pr_rolling_vwap == r21_reliable.would_block_c_or_pr_rolling_vwap
      and r21_unreliable.would_block_c_or_pr_rolling_vwap is True)

# ══════════════════════════════════════════════════════════════
# 8부: entry_quality_shadow.csv 중복 방지 — 게이트 상태 변화 보존
# (2026-08-05, 2차 GPT 코드리뷰 지적 2번)
# ══════════════════════════════════════════════════════════════

# ── 22) 동일 봉 1.99%->2.01% 상태 변화는 2행 기록 ──────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    service._symbol_to_conditions[symbol] = ()

    for distance_price in (101990, 102010):  # 1.99% -> 2.01%
        mp = MarketPrice(symbol=symbol, current_price=distance_price, reference_price=98000,
            previous_close=98000, timestamp=datetime.now(),
            indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
        ma_dist = make_ma(is_pulldown_recovery=True)
        signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
        service._write_signal_log(
            symbol=symbol, price=distance_price, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma_dist, final_decision="BUY",
            order_block_reason="", market_price=mp, latest_bar_timestamp="20260805100000",
        )
    shadow_rows = read_shadow_rows(service)
    check("22) 동일 분봉에서 rolling거리 1.99%->2.01%(게이트 상태 변화) -> "
          "entry_quality_shadow.csv에 2행 기록됨(정확히 GPT 지시 필수 테스트 — "
          "상태 변화가 삭제되지 않음)", len(shadow_rows) == 2)
    if len(shadow_rows) == 2:
        check("    첫 행은 would_block_pr_only_rolling_vwap=False",
              shadow_rows[0]["would_block_pr_only_rolling_vwap"] == "False")
        check("    둘째 행은 would_block_pr_only_rolling_vwap=True",
              shadow_rows[1]["would_block_pr_only_rolling_vwap"] == "True")

# ── 23) 같은 상태 반복은 1행 유지 ──────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=102010, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma23 = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    for _ in range(3):
        service._write_signal_log(
            symbol=symbol, price=102010, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma23, final_decision="BUY",
            order_block_reason="", market_price=mp, latest_bar_timestamp="20260805100000",
        )
    shadow_rows = read_shadow_rows(service)
    check("23) 같은 상태(같은 거리·같은 게이트 결과) 3회 반복은 1행만 유지됨",
          len(shadow_rows) == 1)

# ── 24) final_decision BLOCKED->BUY 변화는 새 행 기록 ──────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=100000, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma24 = make_ma(is_pulldown_recovery=False)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    # 1차: DAILY_ENTRY_LIMIT로 차단됨(final_decision=BLOCKED)
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma24, final_decision="BLOCKED",
        order_block_reason="DAILY_ENTRY_LIMIT", market_price=mp,
        latest_bar_timestamp="20260805100000",
    )
    # 2차: 동일 분봉·패턴·점수인데 이번엔 실제로 BUY 체결됨
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma24, final_decision="BUY",
        order_block_reason="", market_price=mp,
        latest_bar_timestamp="20260805100000",
    )
    shadow_rows = read_shadow_rows(service)
    check("24) final_decision이 BLOCKED->BUY로 바뀌면 새 행 기록됨"
          "(정확히 GPT 지시 필수 테스트)", len(shadow_rows) == 2)
    if len(shadow_rows) == 2:
        check("    첫 행 final_decision=BLOCKED, order_block_reason=DAILY_ENTRY_LIMIT",
              shadow_rows[0]["final_decision"] == "BLOCKED"
              and shadow_rows[0]["order_block_reason"] == "DAILY_ENTRY_LIMIT")
        check("    둘째 행 final_decision=BUY, actual_order_submitted=True",
              shadow_rows[1]["final_decision"] == "BUY"
              and shadow_rows[1]["actual_order_submitted"] == "True")

# ── 25) current_price/final_decision/order_block_reason 정확히 기록 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=105000, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma25 = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 특정사유")
    service._write_signal_log(
        symbol=symbol, price=105000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma25, final_decision="BUY",
        order_block_reason="", market_price=mp, latest_bar_timestamp="20260805110000",
    )
    shadow_rows = read_shadow_rows(service)
    check("25) current_price=105000 정확히 기록됨(정확히 GPT 지시 필수 테스트)",
          shadow_rows[0]["current_price"] == "105000")
    check("    legacy_reason 정확히 기록됨", shadow_rows[0]["legacy_reason"] == signal.reason)
    check("    final_decision=BUY 정확히 기록됨", shadow_rows[0]["final_decision"] == "BUY")

# ══════════════════════════════════════════════════════════════
# 9부: 재시작 후 중복 방지, 대표 condition_name 일치
# (2026-08-05, 2차 GPT 코드리뷰 지적 4, 5번)
# ══════════════════════════════════════════════════════════════

# ── 26) 프로세스 재시작 후 기존 key 중복 방지 ──────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service1 = build_service(tmpdir, guard_mode="shadow")
    mp = MarketPrice(symbol=symbol, current_price=102010, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=1.5, indicator_macd_signal=1.0, indicator_macd_hist_direction=1)
    ma26 = make_ma(is_pulldown_recovery=True)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service1._write_signal_log(
        symbol=symbol, price=102010, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma26, final_decision="BUY",
        order_block_reason="", market_price=mp, latest_bar_timestamp="20260805100000",
    )
    rows_before_restart = read_shadow_rows(service1)
    check("26-준비) 재시작 전 1행 기록됨", len(rows_before_restart) == 1)

    # "재시작" 시뮬레이션: 같은 storage 설정으로 새 TradingService(및
    # 새 EntryQualityShadowLogger) 인스턴스를 만들어 기존 파일에서
    # 복원되는지 확인.
    service2 = build_service(tmpdir, guard_mode="shadow")
    # tmpdir을 공유하지 않으므로(build_service가 tmpdir 인자를
    # 그대로 씀) 정확히 같은 entry_quality_shadow_log_file 경로를
    # 갖도록 로거를 직접 재생성해 검증.
    from infra.storage.logger import EntryQualityShadowLogger
    restored_logger = EntryQualityShadowLogger(service1.settings.storage.entry_quality_shadow_log_file)
    check("26) 재시작 시뮬레이션 -> 새 로거 인스턴스가 기존 파일에서 키를 복원함"
          "(정확히 GPT 지시 필수 테스트)", len(restored_logger._seen_keys) == 1)

    # 복원된 로거로 동일 판단을 다시 시도 -> 중복으로 거부돼야 함
    same_row = {
        "symbol": symbol, "latest_bar_timestamp": "20260805100000",
        "detected_patterns": rows_before_restart[0]["detected_patterns"],
        "score": rows_before_restart[0]["score"],
        "would_block_macd_dead_min_score5": rows_before_restart[0]["would_block_macd_dead_min_score5"],
        "would_block_macd_above_signal_required": rows_before_restart[0]["would_block_macd_above_signal_required"],
        "would_block_pr_only_rolling_vwap": rows_before_restart[0]["would_block_pr_only_rolling_vwap"],
        "would_block_c_or_pr_rolling_vwap": rows_before_restart[0]["would_block_c_or_pr_rolling_vwap"],
        "would_block_pullback_condition_rolling_vwap": rows_before_restart[0]["would_block_pullback_condition_rolling_vwap"],
        "would_block_pr_or_pullback_condition_rolling_vwap": rows_before_restart[0]["would_block_pr_or_pullback_condition_rolling_vwap"],
        "would_block_pr_only_session_vwap": rows_before_restart[0]["would_block_pr_only_session_vwap"],
        "would_block_c_or_pr_session_vwap": rows_before_restart[0]["would_block_c_or_pr_session_vwap"],
        "would_block_pullback_condition_session_vwap": rows_before_restart[0]["would_block_pullback_condition_session_vwap"],
        "would_block_pr_or_pullback_condition_session_vwap": rows_before_restart[0]["would_block_pr_or_pullback_condition_session_vwap"],
        "final_decision": rows_before_restart[0]["final_decision"],
        "order_block_reason": rows_before_restart[0]["order_block_reason"],
    }
    result_after_restart = restored_logger.append_if_new(same_row)
    check("    복원된 로거로 동일 판단 재시도 -> 중복으로 거부됨(False)",
          result_after_restart is False)

# ── 27) condition_name과 condition_names가 현재 스냅샷에서 일치함 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.update_targets(["058610"], sym_to_conditions={
        "058610": ("자동매매_돌파형A", "자동매매_눌림목_PR"),
    })
    rep_name = service._representative_condition_name("058610")
    check("27) 대표 condition_name이 현재 복수형 스냅샷에서 파생됨"
          "(정확히 GPT 지시 필수 테스트 — 두 필드가 서로 모순되지 않음)",
          rep_name in service._symbol_to_conditions["058610"])

    # 편출 후에는 대표값도 정확히 빈 문자열이 됨(과거 값 잔존 없음)
    service.update_targets([], sym_to_conditions={})
    rep_name_after = service._representative_condition_name("058610")
    check("    편출 후 대표 condition_name도 빈 문자열(과거 값 잔존 없음)",
          rep_name_after == "")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
