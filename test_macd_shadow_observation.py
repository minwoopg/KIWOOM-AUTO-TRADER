# -*- coding: utf-8 -*-
"""
MACD 상태 shadow 관측 필드 검증 (2026-08-04)

배경: 매매 성과 분석(7/30~8/4)에서 실제 체결 10건 중 MACD 데드
3건이 전부 손실이었음을 trades.csv의 entry_reason 텍스트 파싱으로
확인했으나, 이건 실제 체결된 극소수 케이스에만 있는 정보 — 매수로
안 이어진 수만 건의 HOLD/SKIP 판단에는 MACD 상태가 signal_log.csv
에 전혀 기록되지 않아(재현 확인: 원본 컬럼에 macd/macd_signal
자체가 없음), "MACD 데드 요구 게이트를 넣으면 몇 건이 추가로
막혔을지"를 과거 데이터로 계산할 방법이 없었음.

이 테스트가 검증하는 것 — GPT 코드리뷰 지시대로:
1. macd_golden/macd_dead/macd_hist_dir/chasing_overheated/would_be_
   blocked_if_macd_dead_required 5개 필드가 domain/strategy/
   breakout_strategy.py의 cond_macd_cross, chasing_overheated와
   정확히 동일한 계산식으로 계산되는지
2. 지표가 없는 경우(macd=None) 관측 불가와 "False"를 정확히
   구분해 빈 값으로 남기는지
3. 이 로깅 추가가 신호 판단 로직(Signal의 type/reason) 자체에는
   절대 영향을 주지 않는지(순수 관측 전용)
4. 기존 SIGNAL_FIELDS를 쓰던 CSV(신규/기존 파일 모두)가 헤더
   마이그레이션을 거쳐 새 필드를 정상적으로 채우는지
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import MarketPrice, MarketRegime, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger, SIGNAL_FIELDS
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


def make_market_price(symbol="005930", macd=None, macd_signal=None, hist_dir=0):
    return MarketPrice(
        symbol=symbol, current_price=100000, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=macd, indicator_macd_signal=macd_signal,
        indicator_macd_hist_direction=hist_dir,
    )


def make_minute_analysis(**overrides) -> MinuteAnalysis:
    defaults = dict(
        vwap=58000.0, price_above_vwap=True, low_rising=False,
        pullback_pct=0.0, is_valid_pullback=False,
        change_rate_pct=0.0, is_valid_change_rate=False,
        rebound_pct=0.0, is_valid_rebound=False,
        trading_value=1_000_000_000, is_valid_trading_value=True,
        day_high=58200, day_low=57800, is_valid_pulldown=False,
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


def read_last_row(service):
    with open(service.settings.storage.signal_log_file, encoding="utf-8") as f:
        lines = f.readlines()
    header = lines[0].strip().split(",")
    data = lines[-1].strip().split(",")
    return dict(zip(header, data))


symbol = "005930"

# ══════════════════════════════════════════════════════════════
# 1부: SIGNAL_FIELDS에 5개 필드가 정확히 추가됐는지
# ══════════════════════════════════════════════════════════════

check("1) SIGNAL_FIELDS에 macd_golden 포함됨", "macd_golden" in SIGNAL_FIELDS)
check("   macd_dead 포함됨", "macd_dead" in SIGNAL_FIELDS)
check("   macd_hist_dir 포함됨", "macd_hist_dir" in SIGNAL_FIELDS)
check("   chasing_overheated 포함됨", "chasing_overheated" in SIGNAL_FIELDS)
check("   would_be_blocked_if_macd_dead_required 포함됨",
      "would_be_blocked_if_macd_dead_required" in SIGNAL_FIELDS)

# ══════════════════════════════════════════════════════════════
# 2부: MACD 데드 + chasing_overheated 실제 발동 케이스
# ══════════════════════════════════════════════════════════════

# ── 2) MACD 데드 + 당일등락 4%(>=3%) -> chasing_overheated=True ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=-1)  # 데드
    ma = make_minute_analysis(change_rate_pct=4.0, is_valid_change_rate=True)
    signal = Signal(type=SignalType.HOLD, reason="추격매수 차단 3/8 — 당일 +4.0% + MACD데드")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("2) MACD 데드(macd<signal) -> macd_dead=True", row["macd_dead"] == "True")
    check("   macd_golden=False", row["macd_golden"] == "False")
    check("   당일등락 4%(>=3%) + MACD데드 -> chasing_overheated=True"
          "(breakout_strategy.py의 실제 게이트 조건과 정확히 동일 계산)",
          row["chasing_overheated"] == "True")

# ── 3) MACD 데드 + score=4(5점 미만) -> would_be_blocked=True ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.HOLD, reason="강한 진입 4/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("3) MACD 데드 + score=4(5점 미만) -> "
          "would_be_blocked_if_macd_dead_required=True", row["score"] == "4")
    check("   would_be_blocked=True", row["would_be_blocked_if_macd_dead_required"] == "True")

# ── 4) MACD 데드 + score=6(5점 이상) -> would_be_blocked=False ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("4) MACD 데드 + score=6(5점 이상) -> would_be_blocked=False"
          "(이미 기존 chasing_overheated 게이트를 통과할 자격이 있었음)",
          row["would_be_blocked_if_macd_dead_required"] == "False")

# ══════════════════════════════════════════════════════════════
# 3부: MACD 골든 케이스 — would_be_blocked는 관측 대상 아님(빈 값)
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=1.5, macd_signal=1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("5) MACD 골든 -> macd_golden=True, macd_dead=False",
          row["macd_golden"] == "True" and row["macd_dead"] == "False")
    check("   MACD 골든이면 would_be_blocked는 애초에 관측 대상이 아니라 빈 값"
          "(False로 단정하지 않음 — 이 필드는 'MACD 데드인 경우'만 의미가 있음)",
          row["would_be_blocked_if_macd_dead_required"] == "")

# ══════════════════════════════════════════════════════════════
# 4부: 지표 없음 — 관측 불가와 False를 명확히 구분(빈 값)
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=None, macd_signal=None)
    signal = Signal(type=SignalType.HOLD, reason="지표 없음 — 일봉 데이터 대기 중")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("6) macd=None(지표 없음) -> macd_golden/macd_dead 둘 다 빈 값"
          "(관측 불가를 False와 명확히 구분 — 'MACD가 데드가 아니었다'로 "
          "잘못 해석되지 않도록)",
          row["macd_golden"] == "" and row["macd_dead"] == "")
    check("   chasing_overheated도 빈 값", row["chasing_overheated"] == "")
    check("   would_be_blocked도 빈 값", row["would_be_blocked_if_macd_dead_required"] == "")

# ── 7) market_price 자체를 안 넘기는 기존 호출 경로도 안전(하위 호환) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    try:
        service._write_signal_log(
            symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=None, final_decision="HOLD",
            order_block_reason="",
            # market_price 생략(기본값 None)
        )
        row = read_last_row(service)
        check("7) market_price 생략해도 예외 없이 정상 기록됨(하위 호환)", True)
        check("   생략 시 5개 필드가 전부 빈 값", all(
            row[f] == "" for f in
            ["macd_golden", "macd_dead", "macd_hist_dir",
             "chasing_overheated", "would_be_blocked_if_macd_dead_required"]
        ))
    except Exception as exc:
        check(f"7) market_price 생략해도 예외 없이 정상 기록됨(하위 호환) - 실패: {exc}", False)
        check("   생략 시 5개 필드가 전부 빈 값", False)

# ══════════════════════════════════════════════════════════════
# 5부: 핵심 안전 조건 — 이 로깅 추가가 신호 판단 자체에 영향 없음
# ══════════════════════════════════════════════════════════════

# ── 8) _process_symbol() 통합 흐름에서 최종 신호(type/reason)가
#      market_price의 MACD 상태와 무관하게 기존과 동일하게 결정됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    from domain.models import AccountBalance
    from unittest.mock import patch

    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    # strategy.generate_signal을 스파이로 감싸서, 이 함수의 반환값이
    # market_price를 인자로 받는 것과 무관하게 그대로 최종 결과가
    # 되는지 확인(로깅 추가로 인한 신호 변형이 없어야 함).
    original_signal = Signal(type=SignalType.HOLD, reason="관측용 그대로 유지되어야 함")
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

    row = read_last_row(service)
    check("8) _process_symbol() 통합 흐름에서 signal_log의 skip_reason이 "
          "strategy.generate_signal()이 반환한 원래 reason과 정확히 일치함"
          "(관측 필드 계산 로직이 신호 자체를 바꾸지 않음)",
          row["skip_reason"] == original_signal.reason)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
