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
