# -*- coding: utf-8 -*-
"""
Profitability Shadow v2 검증 (2026-08-24)

배경: Profitability Sprint v1(analysis-only 도구, tools/profitability_
sprint.py)이 실거래 9건(8/20+8/21) 사후 분석으로 찾은 후보들을 민우님이
직접 코드/CSV까지 대조 검토한 결과, 실시간으로 제대로 된 표본을 빠르게
쌓기 위한 두 가지 순수 관측(shadow) 로그가 필요하다고 지시:

1. LOW_UPSIDE_F2_SHADOW — BUY 후보 시점에 upside_to_recent_high_pct가
   임계값(F1<1.00%/F2<0.50%/F3<0.25%) 미만이면 "스킵했을 것"이라는
   가상 판정만 기록. entry_quality_guard_mode와 무관하게 항상 기록되고,
   어떤 경우에도 BUY를 막지 않음.

2. MIN_PROFIT_EXTENSION_SHADOW — entry_watch "최소수익미달청산"(5분
   시점) 판단이 실제로 내려지는 그 순간의 feature snapshot(pnl, VWAP,
   MACD, RSI, MA5/MA20, peak/drawdown, upside)을 기록. Sprint v1의
   Study B가 VWAP 조기청산과 최소수익미달청산을 섞고 진입 시점 feature를
   썼다는 리뷰 지적에 대한 직접적 대응 — 이 로그는 오직 "최소수익미달
   청산" 유형만, 오직 판단 순간의 값만 기록. 급락청산/VWAP이탈청산은
   이 로그에 절대 나타나지 않아야 함(entry_watch_shadow.csv가 계속 다룸).

이 테스트가 검증하는 것:
  1부: 두 로거 클래스(LowUpsideShadowLogger/MinProfitExtensionShadowLogger)
       자체의 CSV 기록/헤더/중복방지 동작
  2부: _write_signal_log()의 Low Upside shadow 기록 — F1/F2/F3 판정,
       guard_mode 무관, legacy_buy_candidate=False/minute_analysis=None
       일 때 기록 안 함, 중복 방지, signal_log.csv 자체 필드는 무변경
  3부: _check_entry_watch()의 MIN_PROFIT_EXTENSION_SHADOW 기록 —
       최소수익미달청산(3번 분기)에서만 기록되고 급락청산(1번)/
       VWAP이탈청산(2번)에서는 기록되지 않음, stale(minute_analysis=
       None)이어도 청산 자체는 그대로 허용되며 stale 플래그만 남음,
       market_price=None이어도 안전, 이 두 신규 인자가 반환되는 Signal
       자체(type/reason)에는 전혀 영향을 주지 않음(순수 관측 증명)
  4부: MinuteAnalysis.ma5/ma20 원시값이 실제 분봉 종가 평균과 일치
  5부: 기존 __new__ 스텁 패턴(legacy_tests/test_entry_watch.py 스타일)
       으로 만든 인스턴스에서도 예외 없이 동작(방어적 가드)
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
import domain.service.trading_service as ts_module
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis, MinuteAnalyzer
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import (
    MarketPrice, MarketRegime, MinuteBar, OrderResult, OrderSide,
    Position, RuntimeState, Signal, SignalType,
)
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import (
    TradeCsvLogger, SignalCsvLogger, build_app_logger,
    LowUpsideShadowLogger, MinProfitExtensionShadowLogger,
    LOW_UPSIDE_SHADOW_FIELDS, MIN_PROFIT_EXTENSION_SHADOW_FIELDS,
)
from infra.storage.state_store import JsonStateStore

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


def build_service(tmpdir: str) -> TradingService:
    settings = build_minimal_settings(tmpdir)
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
        ma5_above_ma20=True, ma5=100500.0, ma20=99800.0,
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


def make_market_price(macd=None, macd_signal=None, rsi=None) -> MarketPrice:
    return MarketPrice(
        symbol="005930", current_price=10020, reference_price=9800,
        previous_close=9800, timestamp=datetime.now(),
        indicator_macd=macd, indicator_macd_signal=macd_signal, indicator_rsi=rsi,
    )


def read_rows(path: str):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


symbol = "005930"

# ══════════════════════════════════════════════════════════════
# 1부: 로거 클래스 자체 — 헤더/기록/중복방지
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    path = f"{tmpdir}/low_upside_shadow.csv"
    logger1 = LowUpsideShadowLogger(path)
    rows = read_rows(path)
    check("1-1) 생성 즉시 헤더만 있는 빈 파일 생성", rows == [])
    check("   헤더 필드가 LOW_UPSIDE_SHADOW_FIELDS와 일치",
          list(csv.reader(open(path, encoding="utf-8")))[0] == LOW_UPSIDE_SHADOW_FIELDS)

    ok1 = logger1.append_if_new({
        "symbol": "005930", "latest_bar_timestamp": "20260824090000",
        "detected_patterns": "PR", "score": "6",
        "upside_to_recent_high_pct": 0.3,
        "would_skip_low_upside_f1": True, "would_skip_low_upside_f2": True,
        "would_skip_low_upside_f3": False,
    })
    ok2 = logger1.append_if_new({
        "symbol": "005930", "latest_bar_timestamp": "20260824090000",
        "detected_patterns": "PR", "score": "6",
        "upside_to_recent_high_pct": 0.3,
        "would_skip_low_upside_f1": True, "would_skip_low_upside_f2": True,
        "would_skip_low_upside_f3": False,
    })
    check("1-2) 첫 기록은 append_if_new=True", ok1 is True)
    check("   같은 (symbol,latest_bar_timestamp,detected_patterns,score) 재기록은 False(중복 차단)",
          ok2 is False)
    check("   실제로 파일에는 1행만 남음", len(read_rows(path)) == 1)

# ── 1-2b~e) 주문 상태 signature — 같은 base key(symbol/latest_bar_
# timestamp/detected_patterns/score)라도 주문 상태가 바뀌면 새 행,
# 완전히 동일하면 계속 중복 차단 (2026-08-24, Shadow v2 재closure,
# 민우님 코드리뷰 P1 지적: "같은 1분봉 안에서 rejected→accepted로
# 바뀌어도 base 4필드 키만으로는 두 번째(accepted) 행이 중복으로
# 버려져, F2가 True였고 실제 accepted된 거래를 놓친다") ──
def _base_row(**overrides):
    row = {
        "symbol": "005930", "latest_bar_timestamp": "20260824093000",
        "detected_patterns": "PR", "score": "6",
        "upside_to_recent_high_pct": 0.3,
        "would_skip_low_upside_f2": True,
    }
    row.update(overrides)
    return row


with tempfile.TemporaryDirectory() as tmpdir:
    logger1b = LowUpsideShadowLogger(f"{tmpdir}/low_upside_shadow.csv")
    ok_rejected = logger1b.append_if_new(_base_row(
        final_decision="BUY", order_block_reason="",
        order_attempted=True, order_accepted=False, order_id="",
    ))
    ok_accepted = logger1b.append_if_new(_base_row(
        final_decision="BUY", order_block_reason="",
        order_attempted=True, order_accepted=True, order_id="REAL_ORD_1",
    ))
    check("1-2b) 같은 분봉에서 rejected(order_accepted=False) 기록", ok_rejected is True)
    check("   같은 분봉·같은 base key라도 accepted=True로 상태가 바뀌면 새 행으로 기록됨",
          ok_accepted is True)
    rows_1b = read_rows(f"{tmpdir}/low_upside_shadow.csv")
    check("   실제로 2행이 남고, 두 번째 행에 order_accepted=True + 실제 order_id가 보존됨",
          len(rows_1b) == 2
          and rows_1b[-1]["order_accepted"] == "True"
          and rows_1b[-1]["order_id"] == "REAL_ORD_1")

with tempfile.TemporaryDirectory() as tmpdir:
    logger1c = LowUpsideShadowLogger(f"{tmpdir}/low_upside_shadow.csv")
    logger1c.append_if_new(_base_row(
        final_decision="BLOCKED", order_block_reason="DAILY_ENTRY_LIMIT",
        order_attempted=False, order_accepted="", order_id="",
    ))
    ok_after_block = logger1c.append_if_new(_base_row(
        final_decision="BUY", order_block_reason="",
        order_attempted=True, order_accepted=True, order_id="REAL_ORD_2",
    ))
    check("1-2c) BLOCKED(주문 시도 자체 없음) → 이후 실제 accepted로 전환되면 새 행 허용",
          ok_after_block is True and len(read_rows(f"{tmpdir}/low_upside_shadow.csv")) == 2)

with tempfile.TemporaryDirectory() as tmpdir:
    logger1d = LowUpsideShadowLogger(f"{tmpdir}/low_upside_shadow.csv")
    results = [
        logger1d.append_if_new(_base_row(
            final_decision="BUY", order_block_reason="",
            order_attempted=True, order_accepted=True, order_id="REAL_ORD_3",
        ))
        for _ in range(5)
    ]
    check("1-2d) 완전히 동일한 accepted 상태(같은 order_id 포함) 5회 반복 폴링 → 1행만 남음",
          results == [True, False, False, False, False]
          and len(read_rows(f"{tmpdir}/low_upside_shadow.csv")) == 1)

with tempfile.TemporaryDirectory() as tmpdir:
    path1e = f"{tmpdir}/low_upside_shadow.csv"
    logger1e_a = LowUpsideShadowLogger(path1e)
    logger1e_a.append_if_new(_base_row(
        final_decision="BUY", order_block_reason="",
        order_attempted=True, order_accepted=True, order_id="REAL_ORD_4",
    ))
    logger1e_b = LowUpsideShadowLogger(path1e)  # 프로세스 재시작 모사
    ok_restart = logger1e_b.append_if_new(_base_row(
        final_decision="BUY", order_block_reason="",
        order_attempted=True, order_accepted=True, order_id="REAL_ORD_4",
    ))
    check("1-2e) 재시작 후에도 signature(주문 상태 포함) 전체를 복원해 동일 accepted 상태 재기록을 막음",
          ok_restart is False and len(read_rows(path1e)) == 1)

with tempfile.TemporaryDirectory() as tmpdir:
    path = f"{tmpdir}/min_profit_extension_shadow.csv"
    logger2 = MinProfitExtensionShadowLogger(path)
    rows = read_rows(path)
    check("1-3) 생성 즉시 헤더만 있는 빈 파일 생성", rows == [])
    check("   헤더 필드가 MIN_PROFIT_EXTENSION_SHADOW_FIELDS와 일치(entry_time 포함)",
          list(csv.reader(open(path, encoding="utf-8")))[0] == MIN_PROFIT_EXTENSION_SHADOW_FIELDS
          and "entry_time" in MIN_PROFIT_EXTENSION_SHADOW_FIELDS)
    # 2026-08-24 (Shadow v2 closure, 민우님 코드리뷰 지적): "청산 이벤트당
    # 정확히 한 번만 호출된다"는 최초 전제가 틀렸음이 확인됨(balance lag
    # 동안 동일 판단이 반복될 수 있음, OBS.2-A와 동일 유형) — 이제
    # (symbol, entry_time) 기준 append_if_new()로 중복을 막는다.
    ok_a = logger2.append_if_new({"symbol": "005935", "entry_time": "T1", "pnl_pct": 0.2})
    ok_b = logger2.append_if_new({"symbol": "005935", "entry_time": "T1", "pnl_pct": 0.3})
    check("1-3b) 첫 기록은 append_if_new=True", ok_a is True)
    check("   같은 (symbol, entry_time) 재기록은 False(중복 차단, 값이 달라도)", ok_b is False)
    check("   실제로 파일에는 1행만 남음(balance lag로 반복 판단돼도 1건)", len(read_rows(path)) == 1)
    ok_c = logger2.append_if_new({"symbol": "005935", "entry_time": "T2", "pnl_pct": 0.4})
    check("   다른 entry_time(재진입)은 새 행으로 기록됨", ok_c is True and len(read_rows(path)) == 2)

# ── 1-4) 재시작 시 기존 CSV에서 (symbol, entry_time) 키를 복원 ──
with tempfile.TemporaryDirectory() as tmpdir:
    path = f"{tmpdir}/min_profit_extension_shadow.csv"
    logger2a = MinProfitExtensionShadowLogger(path)
    logger2a.append_if_new({"symbol": "005935", "entry_time": "T1", "pnl_pct": 0.2})
    logger2b = MinProfitExtensionShadowLogger(path)  # 프로세스 재시작 모사
    ok_restart = logger2b.append_if_new({"symbol": "005935", "entry_time": "T1", "pnl_pct": 0.99})
    check("1-4) 재시작 후에도 기존 CSV에서 키를 복원해 같은 episode 재기록을 막음",
          ok_restart is False and len(read_rows(path)) == 1)

# ══════════════════════════════════════════════════════════════
# 2부: _write_signal_log() — LOW_UPSIDE_F2_SHADOW
# ══════════════════════════════════════════════════════════════

# ── 2-1) upside=0.3% → F1/F2 True, F3 False (F3 임계값 0.25% 미만) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    rows = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("2-1) upside=0.3% → 정확히 1행 기록됨", len(rows) == 1)
    r = rows[0]
    check("   would_skip_low_upside_f1(<1.00%)=True", r["would_skip_low_upside_f1"] == "True")
    check("   would_skip_low_upside_f2(<0.50%, 주 후보)=True", r["would_skip_low_upside_f2"] == "True")
    check("   would_skip_low_upside_f3(<0.25%)=False(0.3은 0.25 미만이 아님)",
          r["would_skip_low_upside_f3"] == "False")
    check("   upside_to_recent_high_pct 원값 보존", r["upside_to_recent_high_pct"] == "0.3")

# ── 2-2) upside=0.7% → F1 True, F2/F3 False ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.7)
    signal = Signal(type=SignalType.BUY, reason="테스트 5/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824091000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-2) upside=0.7% → F1=True, F2=False, F3=False",
          r["would_skip_low_upside_f1"] == "True"
          and r["would_skip_low_upside_f2"] == "False"
          and r["would_skip_low_upside_f3"] == "False")

# ── 2-3) upside=1.5% → 셋 다 False ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=1.5)
    signal = Signal(type=SignalType.BUY, reason="테스트 5/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824092000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-3) upside=1.5% → F1/F2/F3 모두 False",
          r["would_skip_low_upside_f1"] == "False"
          and r["would_skip_low_upside_f2"] == "False"
          and r["would_skip_low_upside_f3"] == "False")

# ── 2-4) guard_mode="off"이어도 동일하게 기록됨(entry_quality_guard_mode와 무관) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    check("   전제: 기본 guard_mode=off", service.settings.experimental.entry_quality_guard_mode == "off")
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    rows_low_upside = read_rows(service.settings.storage.low_upside_shadow_log_file)
    rows_entry_quality = read_rows(service.settings.storage.entry_quality_shadow_log_file)
    check("2-4) guard_mode=off인데도 low_upside_shadow에는 기록됨(1행)",
          len(rows_low_upside) == 1)
    check("   반면 entry_quality_shadow.csv는 guard_mode=off라 기록 안 됨(0행) — 서로 독립적인 축 증명",
          len(rows_entry_quality) == 0)

# ── 2-5) legacy_buy_candidate=False(HOLD) → 기록 안 함 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.1)  # 아주 낮아도
    signal = Signal(type=SignalType.HOLD, reason="점수 부족 3/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="HOLD",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    rows = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("2-5) HOLD 신호는 legacy_buy_candidate=False라 low_upside_shadow에 기록 안 함(0행)",
          len(rows) == 0)

# ── 2-6) minute_analysis=None(stale) → 기록 안 함 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    rows = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("2-6) minute_analysis=None(stale)이면 upside 계산 불가 — 기록 안 함(0행), 빈 값으로 잘못 채우지 않음",
          len(rows) == 0)

# ── 2-7) 이 로깅 추가가 signal_log.csv 자체의 신호 판단 결과에는 영향 없음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.1)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    sig_rows = read_rows(service.settings.storage.signal_log_file)
    check("2-7) signal_log.csv에는 원래 signal/final_decision이 그대로(BUY) 기록됨 — 판단 로직 무변경",
          sig_rows[-1]["signal"] == "BUY" and sig_rows[-1]["final_decision"] == "BUY")

# ── 2-8) 실제 accepted 주문과 연결되는 order_attempted/order_accepted/order_id
# (2026-08-24, Shadow v2 closure, 민우님 코드리뷰 지적: entry_quality_shadow에는
# 이미 있던 이 필드들이 low_upside_shadow.csv에는 빠져 있었음) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="ORD_ABC123", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=True, message="OK",
        timestamp=datetime.now(),
    )
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-8) order_attempted=True 기록", r["order_attempted"] == "True")
    check("   order_accepted=True 기록", r["order_accepted"] == "True")
    check("   order_id가 실제 accepted SELL/BUY order_id와 일치",
          r["order_id"] == "ORD_ABC123")

# ── 2-9) order_attempt가 없는 경우(주문 시도 자체가 없었던 폴링) 공백 처리 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824090000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-9) order_attempt 없음 → order_attempted=False, order_accepted/order_id는 공백",
          r["order_attempted"] == "False" and r["order_accepted"] == "" and r["order_id"] == "")

# ── 2-10) 실제 _write_signal_log() 경로로 재현: 같은 분봉에서 첫 폴링은
# 거부(rejected), 두 번째 폴링은 재시도로 accepted — 두 행 모두 남고
# 두 번째 행에 실제 order_id가 보존됨 (2026-08-24, Shadow v2 재closure,
# 민우님 코드리뷰 P1 지적 재현 시나리오 그대로) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")

    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=False, message="REJECTED",
        timestamp=datetime.now(),
    )
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824093000",
    )
    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="REAL_ORD_9", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=True, message="OK",
        timestamp=datetime.now(),
    )
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260824093000",  # 같은 분봉
    )
    rows_210 = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("2-10) 같은 분봉에서 rejected→accepted 전환 시 두 행 모두 기록됨(하나로 뭉개지지 않음)",
          len(rows_210) == 2)
    check("   두 번째(accepted) 행에 order_accepted=True + 실제 order_id=REAL_ORD_9가 정확히 보존됨",
          rows_210[-1]["order_accepted"] == "True" and rows_210[-1]["order_id"] == "REAL_ORD_9")
    check("   첫 번째(rejected) 행은 order_accepted=False로 그대로 남아 있음(덮어쓰지 않음)",
          rows_210[0]["order_accepted"] == "False")

# ══════════════════════════════════════════════════════════════
# 2부-P0: fail-open — LOW_UPSIDE_SHADOW 기록 실패가 signal_log를
# 물귀신처럼 끌고 내려가면 안 됨 (2026-08-24, Shadow v2 closure,
# 민우님 코드리뷰 P0 지적)
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)

    class _BoomLowUpsideLogger:
        def append_if_new(self, row):
            raise IOError("디스크 오류 모사")

    service.low_upside_shadow_logger = _BoomLowUpsideLogger()
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    raised = False
    try:
        service._write_signal_log(
            symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma, final_decision="BUY",
            order_block_reason="", latest_bar_timestamp="20260824090000",
        )
    except Exception:
        raised = True
    check("2-P0-1) LowUpsideShadowLogger.append_if_new()가 IOError를 던져도 예외가 밖으로 전파되지 않음",
          raised is False)
    sig_rows = read_rows(service.settings.storage.signal_log_file)
    check("2-P0-2) 관측 로그 실패와 무관하게 signal_log.csv에는 정상적으로 행이 기록됨(BUY 판단 자체는 영향 없음)",
          len(sig_rows) == 1 and sig_rows[-1]["signal"] == "BUY")

# 2부-CA: Candidate A forward shadow (2026-08-27, Sprint v1.2
# historical 결과 승인 후 민우님 지시)
#
# Sprint v1.2가 8/20~8/25 historical 데이터로 F2(upside<0.50%) 대비
# 가장 강한 후보로 확인한 Candidate A(upside<0.50% AND
# rebound_volume_spike==False)를 이 시점부터 조건 고정하고,
# low_upside_shadow.csv에 두 필드(rebound_volume_spike,
# would_skip_low_upside_no_spike)를 추가해 forward 표본을 쌓는다.
# 새 logger는 만들지 않고 기존 LowUpsideShadowLogger row를 확장 —
# 기존 would_skip_f1/f2/f3, order_attempted/accepted/id 등은 무변경.

# ── 2-CA-1~4) 경계값 — upside<0.50% AND spike==False일 때만 True ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.49, rebound_volume_spike=False)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-1) upside=0.49% + spike=False → would_skip_low_upside_no_spike=True(Candidate A 성립)",
          r["would_skip_low_upside_no_spike"] == "True")
    check("   rebound_volume_spike 원시값도 함께 기록됨(False)", r["rebound_volume_spike"] == "False")

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.49, rebound_volume_spike=True)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090100",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-2) upside=0.49% + spike=True → would_skip_low_upside_no_spike=False(spike가 있으면 Candidate A는 스킵 안 함)",
          r["would_skip_low_upside_no_spike"] == "False")
    check("   rebound_volume_spike 원시값=True", r["rebound_volume_spike"] == "True")

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.50, rebound_volume_spike=False)  # 경계값(0.50은 <0.50 아님)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090200",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-3) upside=0.50%(경계값, F2와 동일하게 미만이 아니므로 미해당) + spike=False → would_skip=False",
          r["would_skip_low_upside_no_spike"] == "False"
          and r["would_skip_low_upside_f2"] == "False")  # F2 자체도 경계값에서 False임을 함께 확인(일관성)

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.51, rebound_volume_spike=False)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090300",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-4) upside=0.51%(F2 밖) + spike=False → would_skip_low_upside_no_spike=False",
          r["would_skip_low_upside_no_spike"] == "False")

# ── 2-CA-5) rebound_volume_spike가 결측(None)이면 False로 추정하지
# 않고 두 필드 모두 빈 값(unknown)으로 남김 — Sprint v1.2 분석 도구의
# "결측을 skip 대상으로 추정하지 않는다" 원칙과 동일해야 offline
# historical 결과와 forward shadow 결과를 1:1로 비교할 수 있음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.10, rebound_volume_spike=None)  # 극단적 결측 상황 모사
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090400",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-5) rebound_volume_spike=None(결측) → rebound_volume_spike 필드는 빈 값(공백), 'False' 아님",
          r["rebound_volume_spike"] == "")
    check("   would_skip_low_upside_no_spike도 빈 값(unknown) — upside=0.10%로 F2는 성립하지만 Candidate A는 True로 추정하지 않음",
          r["would_skip_low_upside_no_spike"] == "" and r["would_skip_low_upside_no_spike"] != "True")
    check("   (대조) 같은 행의 F2 자체는 spike와 무관하므로 정상적으로 True", r["would_skip_low_upside_f2"] == "True")

# ── 2-CA-6) 실제 accepted BUY 행에 order_accepted/order_id와
# rebound_volume_spike/would_skip_low_upside_no_spike가 함께 존재
# (E2E, 민우님 지시: "실제로 샀던 거래 중 Candidate A가 무엇을
# 막았을 것이며, 그 거래가 나중에 돈을 벌었는가"를 answer하려면
# order 연결과 Candidate A 플래그가 반드시 같은 행에 있어야 함) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="ORD_CANDA_1", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=True, message="OK",
        timestamp=datetime.now(),
    )
    ma = make_ma(upside_to_recent_high_pct=0.30, rebound_volume_spike=False)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827090500",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("2-CA-6) 실제 accepted BUY 행에 order_accepted=True + order_id + "
          "rebound_volume_spike + would_skip_low_upside_no_spike가 모두 같은 행에 존재",
          r["order_accepted"] == "True" and r["order_id"] == "ORD_CANDA_1"
          and r["rebound_volume_spike"] == "False"
          and r["would_skip_low_upside_no_spike"] == "True")

# ── 2-CA-7) dedup 계약 보존 — same-bar rejected→accepted가 신규
# 필드 추가 후에도 여전히 두 행 모두 보존됨(2-10과 동일 시나리오,
# Candidate A 필드가 dedup key에 없다는 것까지 함께 확인) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.30, rebound_volume_spike=False)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")

    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=False, message="REJECTED",
        timestamp=datetime.now(),
    )
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827093000",
    )
    service._last_order_attempt_by_symbol[symbol] = OrderResult(
        order_id="REAL_ORD_CANDA", symbol=symbol, side=OrderSide.BUY,
        requested_quantity=10, accepted=True, message="OK",
        timestamp=datetime.now(),
    )
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260827093000",  # 같은 분봉
    )
    rows_ca7 = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("2-CA-7) Candidate A 필드 추가 후에도 same-bar rejected→accepted 두 행이 그대로 보존됨(dedup key 무변경 확인)",
          len(rows_ca7) == 2
          and rows_ca7[0]["order_accepted"] == "False"
          and rows_ca7[-1]["order_accepted"] == "True" and rows_ca7[-1]["order_id"] == "REAL_ORD_CANDA")
    check("   두 행 모두 rebound_volume_spike/would_skip_low_upside_no_spike 값이 정상적으로 채워짐(신규 필드가 dedup을 깨지 않음)",
          all(row["rebound_volume_spike"] == "False" and row["would_skip_low_upside_no_spike"] == "True"
              for row in rows_ca7))

# ── 2-CA-8) fail-open — Candidate A 블록(신규 코드) 자체에서
# rebound_volume_spike 접근이 예외를 던져도 BUY 판단/signal_log에는
# 영향이 없어야 함. `rebound_volume_spike`는 _write_signal_log() 안에서
# 두 번 읽힘 — ① 기존 patterns 블록(이 closure 이전부터 있던 코드,
# 이번 변경 대상 아님), ② 신규 Candidate A 블록(이번에 추가한 코드,
# 기존 fail-open try 안에 위치). 이 테스트는 ①은 정상 통과시키고
# ②에서만 예외가 나도록 해, 신규 코드의 fail-open만 정확히 겨냥한다 ──
class _SpikeExplodesOnSecondAccess:
    """rebound_volume_spike 접근을 가로채 두 번째부터 예외를 던지는
    MinuteAnalysis 프록시. 나머지 속성은 실제 MinuteAnalysis로 위임."""

    def __init__(self, ma):
        object.__setattr__(self, "_ma", ma)
        object.__setattr__(self, "_spike_access_count", 0)

    def __getattr__(self, name):
        if name == "rebound_volume_spike":
            count = object.__getattribute__(self, "_spike_access_count")
            object.__setattr__(self, "_spike_access_count", count + 1)
            if count >= 1:
                raise RuntimeError("강제 예외 — Candidate A 블록의 2번째 rebound_volume_spike 접근 실패 모사")
        return getattr(object.__getattribute__(self, "_ma"), name)


with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma_real = make_ma(upside_to_recent_high_pct=0.30, rebound_volume_spike=False)
    ma_boom = _SpikeExplodesOnSecondAccess(ma_real)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    raised = False
    try:
        service._write_signal_log(
            symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma_boom, final_decision="BUY",
            order_block_reason="", latest_bar_timestamp="20260827090600",
        )
    except Exception:
        raised = True
    check("2-CA-8) Candidate A 블록에서 rebound_volume_spike 접근이 예외를 던져도 예외가 밖으로 전파되지 않음",
          raised is False)
    sig_rows_ca8 = read_rows(service.settings.storage.signal_log_file)
    check("   signal_log.csv에는 정상적으로 BUY 판단이 기록됨(Candidate A 관측 실패와 무관)",
          len(sig_rows_ca8) == 1 and sig_rows_ca8[-1]["signal"] == "BUY")
    rows_ca8 = read_rows(service.settings.storage.low_upside_shadow_log_file)
    check("   low_upside_shadow.csv에는 이 관측 실패로 행이 남지 않음(관측만 포기, 0행)",
          len(rows_ca8) == 0)

# ══════════════════════════════════════════════════════════════
# 3부: _check_entry_watch() — MIN_PROFIT_EXTENSION_SHADOW
# ══════════════════════════════════════════════════════════════

def make_position(avg: int = 10000) -> Position:
    return Position(symbol=symbol, quantity=10, average_price=avg)


def set_entry_time(service, minutes_ago: float) -> None:
    service.state.entry_time_by_symbol[symbol] = (
        datetime.now() - timedelta(minutes=minutes_ago)
    ).isoformat()


# ── 3-1) 최소수익미달청산(3번 분기) 발동 → feature snapshot 정확히 기록 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)  # watch_minutes(5) 경과, +1분 버퍼 이내
    pos = make_position(avg=10000)
    current_price = 10020  # pnl = +0.2% < min_profit_pct(0.5%), fail_cut_pct(-1.0%) 아님
    service._highest_price[symbol] = 10100  # 매수 후 한때 +1.0%까지 갔다가 밀림
    ma = make_ma(
        vwap=10000.0, price_above_vwap=True,  # VWAP 위 → 2번 분기(이탈) 미발동
        upside_to_recent_high_pct=2.5, ma5=10010.0, ma20=9990.0,
    )
    mp = make_market_price(macd=1.2, macd_signal=0.8, rsi=55.0)
    sig = service._check_entry_watch(
        symbol, pos, current_price, ma, market_price=mp, highest_price=10100,
    )
    check("3-1) 최소수익미달청산 SELL 신호 반환", sig is not None and sig.type == SignalType.SELL)
    check("   사유에 최소수익미달청산 문구 포함", "최소수익미달청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   feature snapshot 정확히 1행 기록됨", len(rows) == 1)
    r = rows[0]
    check("   pnl_pct=0.2 기록", r["pnl_pct"] == "0.2")
    check("   price=10020 기록", r["price"] == "10020")
    check("   vwap=10000.0 기록(stale 아님)", r["vwap"] == "10000.0")
    check("   price_vs_vwap_pct=0.2 기록", r["price_vs_vwap_pct"] == "0.2")
    check("   macd=1.2/macd_signal=0.8/macd_above_signal=True 기록",
          r["macd"] == "1.2" and r["macd_signal"] == "0.8" and r["macd_above_signal"] == "True")
    check("   rsi=55.0 기록", r["rsi"] == "55.0")
    check("   ma5=10010.0 / ma20=9990.0 기록(진입 시점 아닌 판단 순간 값)",
          r["ma5"] == "10010.0" and r["ma20"] == "9990.0")
    # peak_pnl_pct = (10100-10000)/10000*100 = 1.0, drawdown = (10020-10100)/10100*100 ≈ -0.792
    check("   peak_pnl_pct=1.0 기록(매수 후 최고가 기준)", r["peak_pnl_pct"] == "1.0")
    check("   drawdown_from_peak_pct가 음수로 정확히 계산됨(최고가 대비 현재 낙폭, 소수 3자리 반올림)",
          abs(float(r["drawdown_from_peak_pct"]) - round((10020 - 10100) / 10100 * 100, 3)) < 1e-9)
    check("   upside_to_recent_high_pct=2.5 기록", r["upside_to_recent_high_pct"] == "2.5")
    check("   minute_data_stale=False", r["minute_data_stale"] == "False")

# ── 3-2) 같은 상황이지만 minute_analysis=None(stale) → 청산은 그대로 허용, 지표는 공백+stale=True ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    service._highest_price[symbol] = 10100
    sig = service._check_entry_watch(
        symbol, pos, 10020, None, market_price=None, highest_price=10100,
    )
    check("3-2) minute_analysis=None(stale)이어도 최소수익미달청산은 그대로 발동(기존 동작 무변경)",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)
    r = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)[0]
    check("   minute_data_stale=True", r["minute_data_stale"] == "True")
    check("   vwap/price_vs_vwap_pct/ma5/ma20/upside 전부 공백(0으로 잘못 채우지 않음)",
          r["vwap"] == "" and r["price_vs_vwap_pct"] == "" and r["ma5"] == ""
          and r["ma20"] == "" and r["upside_to_recent_high_pct"] == "")
    check("   macd/macd_signal/rsi도 market_price=None이라 공백",
          r["macd"] == "" and r["macd_signal"] == "" and r["rsi"] == "")
    check("   반면 peak_pnl_pct/drawdown_from_peak_pct는 minute_analysis와 무관(highest_price 기반)이라 그대로 계산됨",
          r["peak_pnl_pct"] == "1.0" and r["drawdown_from_peak_pct"] != "")

# ── 3-3) 급락청산(1번 분기) → MIN_PROFIT_EXTENSION_SHADOW에는 기록 안 됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 1.0)  # watch_minutes 이내 — 급락 조건만으로 판단
    pos = make_position(avg=10000)
    sig = service._check_entry_watch(
        symbol, pos, 9880, None,  # -1.2% <= fail_cut_pct(-1.0%)
        market_price=make_market_price(), highest_price=9880,
    )
    check("3-3) 급락청산 SELL 신호 반환", sig is not None and "급락청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   급락청산은 MIN_PROFIT_EXTENSION_SHADOW 대상이 아님 — 0행(entry_watch_shadow.csv가 계속 담당)",
          len(rows) == 0)

# ── 3-4) VWAP이탈청산(2번 분기) → MIN_PROFIT_EXTENSION_SHADOW에는 기록 안 됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 1.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10100.0, price_above_vwap=False)  # VWAP 아래, 즉시 이탈 확정(confirm_count=1 기본)
    sig = service._check_entry_watch(
        symbol, pos, 10020, ma,  # +0.2%, VWAP(10100) 아래
        market_price=make_market_price(), highest_price=10050,
    )
    check("3-4) VWAP이탈청산 SELL 신호 반환", sig is not None and "VWAP이탈청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   VWAP이탈청산도 MIN_PROFIT_EXTENSION_SHADOW 대상이 아님 — 0행(다른 질문, 별도 연구 대상)",
          len(rows) == 0)

# ── 3-5) 신규 인자(market_price/highest_price) 유무가 반환 Signal 자체에는 영향 없음(순수 관측 증명) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    sig_without = service._check_entry_watch(symbol, pos, 10020, ma)  # 구 시그니처 그대로(위치 인자만)
    sig_with = service._check_entry_watch(
        symbol, pos, 10020, ma, market_price=make_market_price(macd=1, macd_signal=0.5), highest_price=10100,
    )
    check("3-5) market_price/highest_price 생략 여부와 무관하게 Signal.type 동일(SELL)",
          sig_without.type == sig_with.type == SignalType.SELL)
    check("   Signal.reason도 완전히 동일 — 새 인자가 판정 로직에 전혀 관여하지 않음",
          sig_without.reason == sig_with.reason)

# ── 3-6) entry_time이 실제 진입 시각(state.entry_time_by_symbol)과 일치 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    expected_entry_time = service.state.entry_time_by_symbol[symbol]
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    r = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)[0]
    check("3-6) 기록된 entry_time이 state.entry_time_by_symbol과 정확히 일치",
          r["entry_time"] == expected_entry_time)

# ── 3-7) balance lag로 같은 판단이 반복돼도(OBS.2-A형 실측 패턴) 1행만
# 기록됨 (2026-08-24, Shadow v2 closure, 민우님 코드리뷰 지적: "청산
# 이벤트당 정확히 한 번만 호출된다"는 최초 전제가 틀렸음 — SELL
# accepted 뒤 잔고 API가 2~3분 늦게 반영되는 동안 position이 여전히
# non-None으로 보여 같은 최소수익미달 판단이 여러 폴링에서 반복될 수
# 있음. PSM의 중복 SELL block보다 앞선 지점에서 로깅되므로 별도
# dedup이 필요) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    for _ in range(5):  # 같은 종목, 같은 entry_time으로 5회 반복 폴링 판단 모사
        sig = service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
        check_types_ok = sig is not None and sig.type == SignalType.SELL
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("3-7) 같은 (symbol, entry_time)으로 5회 반복 판단해도 CSV에는 1행만 남음",
          len(rows) == 1)

    # 재진입(entry_time 갱신) 후에는 새 episode로 다시 기록됨
    set_entry_time(service, 5.5)  # 실제로는 새 BUY가 entry_time_by_symbol을 갱신
    new_entry_time = service.state.entry_time_by_symbol[symbol]
    service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    rows2 = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   entry_time이 달라지면(재진입) 새 행으로 기록됨(총 2행)",
          len(rows2) == 2 and rows2[-1]["entry_time"] == new_entry_time)

# ══════════════════════════════════════════════════════════════
# 3부-P0: fail-open — MIN_PROFIT_EXTENSION_SHADOW 기록 실패가 실제
# SELL Signal 반환을 막으면 안 됨 (2026-08-24, Shadow v2 closure,
# 민우님 코드리뷰 P0 지적 — 가장 중요한 항목)
#
# 최초 배치본은 _log_min_profit_extension_shadow() 안에서 "예외를
# 삼키지 않는다"고 설계했는데, 이 함수가 _check_entry_watch()의
# SELL Signal 반환 *직전*에 호출되므로 CSV write가 IOError/
# PermissionError/disk full 등으로 실패하면 그 폴링에서 실제
# 청산이 아예 나가지 않을 수 있었다 — "관측 전용, SELL 정책에
# 영향 0"이라는 설계 조건을 정면으로 어기는 것이었다.
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)

    class _BoomMinProfitLogger:
        def append_if_new(self, row):
            raise PermissionError("디스크 permission 오류 모사")

    service.min_profit_extension_shadow_logger = _BoomMinProfitLogger()
    raised = False
    sig = None
    try:
        sig = service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    except Exception:
        raised = True
    check("3-P0-1) MinProfitExtensionShadowLogger.append_if_new()가 PermissionError를 던져도 "
          "예외가 밖으로 전파되지 않음",
          raised is False)
    check("3-P0-2) 관측 로그 실패와 무관하게 원래 최소수익미달청산 SELL Signal이 정상 반환됨"
          "(SELL 정책에 영향 0 확인)",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)

# ══════════════════════════════════════════════════════════════
# 3부-C: MIN_PROFIT_EXTENSION_SHADOW contamination closure
# (2026-08-26, 8/25 daily bundle 실측 발견 — 403870)
#
# 실제 타임라인: 10:26:32 VWAP이탈청산 SELL accepted → 잔고 반영
# 지연으로 SELL_PENDING → PENDING_TIMEOUT → SELL orphan(브로커
# 잔고 89주 그대로 관측) → 10:29:18(여전히 orphan 유지 중) 3번
# 분기(최소수익미달청산)가 다시 평가되어 이미 다른 사유로 청산
# 확정된 포지션에 대한 shadow row가 하나 더 기록됨. entry_watch_
# shadow.csv는 1P0.8-OBS.2-A(8/21)로 이미 "최초 SELL accepted
# 시점" 기준으로 이 유형의 오염을 막았는데, 이 로거(Shadow v2,
# OBS.2-A보다 나중 도입)에는 같은 보정이 없었음 — 이번 라운드에서
# 동일한 접근으로 닫음. `_check_entry_watch()`의 SELL 판정 로직·
# 반환 Signal·PSM 전이·실제 주문 로직은 전혀 건드리지 않음 —
# MIN_PROFIT_EXTENSION_SHADOW 기록 여부만 바뀜(observation-only).
# ══════════════════════════════════════════════════════════════
from domain.position.lifecycle import PositionLifecycle

# ── 3-C1) SELL_PENDING(타임아웃 전) 중 — 기록 건너뜀 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    state = service._position_state_machine.get(symbol)
    state.lifecycle = PositionLifecycle.SELL_PENDING
    state.pending_order_id = "0066415"
    sig = service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    check("3-C1) SELL_PENDING 중에도 최소수익미달청산 SELL 신호 자체는 그대로 반환됨(판정 로직 무변경)",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   MIN_PROFIT_EXTENSION_SHADOW에는 기록되지 않음(이미 SELL 진행 중) — 0행",
          len(rows) == 0)

# ── 3-C2) SELL orphan — 8/25 403870 실측 재현(PENDING_TIMEOUT 이후
# lifecycle은 OPEN으로 돌아가고 orphan_order_id/orphan_expected_delta
# <0으로 SELL orphan임을 표시) — 기록 건너뜀 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.19)  # 403870 실측(5.19분)과 동일
    pos = make_position(avg=45550)
    ma = make_ma(vwap=45477.0, price_above_vwap=True)  # VWAP 위로 회복 — 2번 분기 미발동(실측과 동일)
    state = service._position_state_machine.get(symbol)
    state.lifecycle = PositionLifecycle.OPEN
    state.orphan_order_id = "0066415"
    state.orphan_expected_delta = -89  # SELL orphan은 항상 음수(observe_for_orphan() 참고)
    sig = service._check_entry_watch(symbol, pos, 45700, ma, highest_price=45850)
    check("3-C2) SELL orphan 중에도 SELL 신호 자체는 그대로 반환됨(판정 로직 무변경)",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   MIN_PROFIT_EXTENSION_SHADOW에는 기록되지 않음(8/25 403870 오염 재현 방지) — 0행",
          len(rows) == 0)

# ── 3-C3) 대조군 — accepted SELL이 전혀 없는 정상 케이스(052690형)는
# 이번 closure와 무관하게 그대로 1행 기록(회귀 없음) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.2)
    pos = make_position(avg=105613)
    ma = make_ma(vwap=104000.0, price_above_vwap=True)
    # state는 기본값(FLAT, pending/orphan 전부 없음) — 아무것도 세팅하지 않음
    sig = service._check_entry_watch(symbol, pos, 104900, ma, highest_price=108300)
    check("3-C3) SELL이 전혀 진행 중이 아닌 정상 케이스는 최소수익미달청산 SELL 반환",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   정상 케이스는 그대로 1행 기록됨(052690형, 회귀 없음)", len(rows) == 1)

# ── 3-C4) BUY_PENDING/BUY orphan은 이 가드 대상이 아님(SELL이 아직
# 없다는 뜻이므로 오염 시나리오가 아님) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    state = service._position_state_machine.get(symbol)
    state.lifecycle = PositionLifecycle.BUY_PENDING
    state.pending_order_id = "0012345"
    service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("3-C4) BUY_PENDING 중에는 가드가 적용되지 않고 그대로 기록됨(SELL 없음)",
          len(rows) == 1)

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    state = service._position_state_machine.get(symbol)
    state.lifecycle = PositionLifecycle.OPEN
    state.orphan_order_id = "0012345"
    state.orphan_expected_delta = 5  # BUY orphan은 양수(목표수량-현재잔고, resolve_stale_pending() 참고)
    service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   BUY orphan(orphan_expected_delta>=0) 중에도 가드가 적용되지 않고 그대로 기록됨",
          len(rows) == 1)

# ── 3-C5) contamination guard 자체의 fail-open 보장 (2026-08-27 재closure,
# 민우님 GPT 코드리뷰 지적 반영). 최초 배치본은 새 guard
# (`_position_state_machine.get(symbol)` 및 state 속성 접근)가 기존
# try/except 블록 *앞*에 있었습니다 — 이 함수 전체가 지키는 fail-open
# 계약(2026-08-24 Shadow v2 closure, docstring 참고: "관측 전용,
# SELL 정책에 영향 0")을 어기는 것이었습니다. 이제 guard 전체가 try
# 안으로 옮겨졌으므로, PositionStateMachine.get()이 (극히 예외적인
# 상황에서) 예외를 던지더라도 그 예외가 `_check_entry_watch()` 밖으로
# 전파되어 실제 최소수익미달청산 SELL Signal 반환을 막아서는 안
# 됩니다. ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)

    class _ExplodingPositionStateMachine:
        """contamination guard 예외 강제용 — get()이 항상 예외를 던짐."""

        def get(self, _symbol):
            raise AttributeError("강제 예외 — contamination guard fail-open 검증용")

    service._position_state_machine = _ExplodingPositionStateMachine()
    sig = service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    check("3-C5) contamination guard(PositionStateMachine.get())가 예외를 던져도 "
          "최소수익미달청산 SELL Signal은 정상 반환됨(fail-open 계약 유지, 예외 전파 없음)",
          sig is not None and sig.type == SignalType.SELL and "최소수익미달청산" in sig.reason)
    rows = read_rows(service.settings.storage.min_profit_extension_shadow_log_file)
    check("   guard 예외로 이 폴링의 관측 기록은 남지 않지만(허용됨) SELL 판정에는 영향 없음 — 0행",
          len(rows) == 0)

# ══════════════════════════════════════════════════════════════
# 4부: MinuteAnalysis.ma5/ma20 원시값 — 실제 분봉 종가 평균과 일치
# ══════════════════════════════════════════════════════════════

closes = list(range(100, 125))  # 25개 종가: 100..124 (오름차순)
bars = [
    MinuteBar(
        cntr_tm=f"202608240900{i:02d}", open_price=c, high_price=c + 1,
        low_price=c - 1, close_price=c, volume=1000, acc_volume=1000 * (i + 1),
    )
    for i, c in enumerate(closes)
]
analyzer = MinuteAnalyzer(min_trading_value=0, low_rising_bars=3)
result = analyzer.analyze(bars, prev_close=99)
expected_ma5 = sum(closes[-5:]) / 5     # 120..124 평균 = 122.0
expected_ma20 = sum(closes[-20:]) / 20  # 105..124 평균 = 114.5

check("4-1) analyze() 결과 반환됨(bars 충분)", result is not None)
if result is not None:
    check("   ma5가 실제 마지막 5개 분봉 종가 평균과 일치", abs(result.ma5 - expected_ma5) < 1e-9)
    check("   ma20이 실제 마지막 20개 분봉 종가 평균과 일치", abs(result.ma20 - expected_ma20) < 1e-9)
    check("   ma5_above_ma20 불리언이 원시값(ma5>ma20)과 정합",
          result.ma5_above_ma20 == (result.ma5 > result.ma20))

check("4-2) MinuteAnalysis 기본값(ma5/ma20 미지정) — 하위호환 기본값 0.0",
      MinuteAnalysis(**{
          **{f: (False if t == "bool" else 0.0 if t == "float" else 0 if t == "int" else "")
             for f, t in [(f.name, f.type) for f in __import__("dataclasses").fields(MinuteAnalysis)]},
      }).ma5 == 0.0)

# ══════════════════════════════════════════════════════════════
# 5부: __init__을 거치지 않은 인스턴스(legacy_tests 스타일)에서도 안전
# ══════════════════════════════════════════════════════════════

svc_stub = TradingService.__new__(TradingService)


class _StubSettings:
    pass


svc_stub.settings = _StubSettings()
svc_stub.settings.entry_watch = None  # entry_watch 비활성 → 바로 None 반환(로깅 시도 자체가 없음)
svc_stub.state = RuntimeState()
sig_stub = svc_stub._check_entry_watch(symbol, make_position(), 10020, None)
check("5-1) __new__ 스텁 + entry_watch=None → None 반환(예외 없음)", sig_stub is None)

svc_stub2 = TradingService.__new__(TradingService)
svc_stub2.settings = _StubSettings()
from config.settings import EntryWatchConfig
svc_stub2.settings.entry_watch = EntryWatchConfig(
    enabled=True, watch_minutes=5, min_profit_pct=0.5, fail_cut_pct=-1.0, fail_on_vwap_break=True,
)
svc_stub2.state = RuntimeState()
svc_stub2.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5.5)).isoformat()
# min_profit_extension_shadow_logger 속성이 아예 없는 상태 — 방어적 가드가 없으면 AttributeError
sig_stub2 = svc_stub2._check_entry_watch(symbol, make_position(), 10020, None)
check("5-2) 로거 속성이 없는 __new__ 스텁에서도 최소수익미달청산 판정 자체는 정상 동작(방어적 가드)",
      sig_stub2 is not None and sig_stub2.type == SignalType.SELL)

# ══════════════════════════════════════════════════════════════
# 6부: TradingService.__init__ — profitability shadow logger 2종의
# 생성 자체가 실패해도 서비스 기동이 막히면 안 됨 (2026-08-24,
# Shadow v2 재closure, 민우님 코드리뷰 P0 지적)
#
# 최초 closure는 append()/append_if_new() 실패만 fail-open으로
# 막았는데, 두 로거의 __init__()이 mkdir/open/헤더쓰기를 즉시
# 수행하므로 permission/disk-full/잘못된 경로 등으로 생성자 자체가
# 실패하면 그 예외가 TradingService.__init__ 밖으로 전파돼
# 자동매매 프로그램 전체가 기동하지 못할 수 있었다. 관측용 CSV
# 하나 때문에 프로그램이 아예 시작 못 하는 것은 observation-only
# 설계 조건에 맞지 않는다.
# ══════════════════════════════════════════════════════════════


class _BoomOnInit:
    def __init__(self, *args, **kwargs):
        raise PermissionError("permission 오류 모사(로거 생성 자체 실패)")


class _BoomOnInitOSError:
    def __init__(self, *args, **kwargs):
        raise OSError("disk-full 오류 모사(로거 생성 자체 실패)")


# ── 6-A) LowUpsideShadowLogger 생성자가 실패해도 TradingService는 정상 생성됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    _orig = ts_module.LowUpsideShadowLogger
    ts_module.LowUpsideShadowLogger = _BoomOnInit
    try:
        service = build_service(tmpdir)
    finally:
        ts_module.LowUpsideShadowLogger = _orig
    check("6-A-1) LowUpsideShadowLogger 생성자가 PermissionError를 던져도 TradingService 생성 자체는 성공",
          service is not None)
    check("   실패한 로거는 None으로 안전하게 남음(속성 자체가 없는 게 아니라 명시적으로 None)",
          service.low_upside_shadow_logger is None)
    # BUY 판단 경로(_write_signal_log) 자체가 여전히 정상 동작하는지 확인
    ma = make_ma(upside_to_recent_high_pct=0.3)
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    raised = False
    try:
        service._write_signal_log(
            symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=ma, final_decision="BUY",
            order_block_reason="", latest_bar_timestamp="20260824090000",
        )
    except Exception:
        raised = True
    check("6-A-2) low_upside_shadow_logger=None이어도 _write_signal_log()는 예외 없이 정상 동작",
          raised is False)
    sig_rows_6a = read_rows(service.settings.storage.signal_log_file)
    check("   signal_log.csv에는 원래 BUY 판단이 정상적으로 기록됨(기존 기능 사용 가능)",
          len(sig_rows_6a) == 1 and sig_rows_6a[-1]["signal"] == "BUY")

# ── 6-B) MinProfitExtensionShadowLogger 생성자가 실패해도 entry_watch
# 최소수익미달 SELL이 정상 반환됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    _orig = ts_module.MinProfitExtensionShadowLogger
    ts_module.MinProfitExtensionShadowLogger = _BoomOnInitOSError
    try:
        service = build_service(tmpdir)
    finally:
        ts_module.MinProfitExtensionShadowLogger = _orig
    check("6-B-1) MinProfitExtensionShadowLogger 생성자가 OSError를 던져도 TradingService 생성 자체는 성공",
          service is not None)
    check("   실패한 로거는 None으로 안전하게 남음", service.min_profit_extension_shadow_logger is None)
    set_entry_time(service, 5.5)
    pos = make_position(avg=10000)
    ma = make_ma(vwap=10000.0, price_above_vwap=True)
    sig_6b = service._check_entry_watch(symbol, pos, 10020, ma, highest_price=10100)
    check("6-B-2) min_profit_extension_shadow_logger=None이어도 최소수익미달청산 SELL이 정상 반환됨",
          sig_6b is not None and sig_6b.type == SignalType.SELL and "최소수익미달청산" in sig_6b.reason)

# ── 6-C) 두 로거 모두 생성 실패해도 서비스가 기동하고 기존 핵심 로거는 영향 없음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    _orig_a = ts_module.LowUpsideShadowLogger
    _orig_b = ts_module.MinProfitExtensionShadowLogger
    ts_module.LowUpsideShadowLogger = _BoomOnInit
    ts_module.MinProfitExtensionShadowLogger = _BoomOnInitOSError
    try:
        service = build_service(tmpdir)
    finally:
        ts_module.LowUpsideShadowLogger = _orig_a
        ts_module.MinProfitExtensionShadowLogger = _orig_b
    check("6-C-1) 두 신규 shadow logger가 모두 생성 실패해도 서비스가 기동함",
          service is not None
          and service.low_upside_shadow_logger is None
          and service.min_profit_extension_shadow_logger is None)
    check("   기존 핵심 로거(trade_logger/signal_logger/entry_quality_shadow_logger/"
          "tracked_order_journal)는 전혀 영향받지 않고 정상 생성됨",
          service.trade_logger is not None
          and service.signal_logger is not None
          and service.entry_quality_shadow_logger is not None
          and service._tracked_order_journal is not None)


print(f"\n총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
