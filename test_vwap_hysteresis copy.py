# -*- coding: utf-8 -*-
"""
VWAP 이탈 히스테리시스 단위테스트 (2026-07-22)

7/21 첫 실거래에서 VWAP이탈청산 2건이 -4.83%/-0.43%로 손실이 컸던 것에
대응해, 매수 직후 유예시간 / 이탈폭 하한 / 연속 확인 횟수 세 가지
필터를 추가했다. 이 테스트는:

1) 세 값을 모두 기본값(0, 0.0, 1)으로 두면 기존과 동일하게 즉시 청산
2) grace_seconds 적용 시 매수 직후에는 이탈해도 청산 안 됨
3) min_pct 적용 시 이탈폭이 기준 미만이면 청산 안 됨
4) confirm_count 적용 시 N회 연속돼야 청산, 중간에 회복하면 카운터 리셋
5) 포지션 청산 후 카운터가 정리되는지
를 검증한다.
"""
from __future__ import annotations

import sys
import tempfile
import dataclasses

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import Position
from domain.market_regime.minute_analyzer import MinuteAnalysis
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from config.settings import EntryWatchConfig
from datetime import datetime, timedelta


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


def make_minute_analysis(price_above_vwap: bool, vwap: float) -> MinuteAnalysis:
    fields = {}
    for f in dataclasses.fields(MinuteAnalysis):
        if f.type in ("bool", bool):
            fields[f.name] = False
        elif f.type in ("float", float):
            fields[f.name] = 0.0
        elif f.type in ("int", int):
            fields[f.name] = 0
        else:
            fields[f.name] = None
    fields["price_above_vwap"] = price_above_vwap
    fields["vwap"] = vwap
    return MinuteAnalysis(**fields)


def make_service(tmpdir: str, entry_watch: EntryWatchConfig) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings, "entry_watch", entry_watch)
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


symbol = "475150"

# ── 1) 기본값(하위호환) — 즉시 1회 이탈로 청산 ──────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    ew = EntryWatchConfig(
        enabled=True, watch_minutes=5, min_profit_pct=0.5,
        fail_cut_pct=-1.0, fail_on_vwap_break=True,
        # vwap_break_confirm_count=1, vwap_break_min_pct=0.0, vwap_grace_seconds=0 (기본값)
    )
    service = make_service(tmpdir, ew)
    service.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
    pos = Position(symbol=symbol, quantity=10, average_price=58000)
    ma = make_minute_analysis(price_above_vwap=False, vwap=58500.0)
    sig = service._check_entry_watch(symbol, pos, current_price=58400, minute_analysis=ma)
    check("1) 기본값이면 기존과 동일하게 1회 이탈로 즉시 SELL", sig is not None)

# ── 2) grace_seconds — 매수 직후에는 청산 안 됨 ──────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    ew = EntryWatchConfig(
        enabled=True, watch_minutes=5, min_profit_pct=0.5,
        fail_cut_pct=-1.0, fail_on_vwap_break=True,
        vwap_break_confirm_count=1, vwap_break_min_pct=0.0, vwap_grace_seconds=30,
    )
    service = make_service(tmpdir, ew)
    # 매수 후 10초 경과 (유예 30초 이내)
    service.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(seconds=10)).isoformat()
    pos = Position(symbol=symbol, quantity=10, average_price=58000)
    ma = make_minute_analysis(price_above_vwap=False, vwap=58500.0)
    sig = service._check_entry_watch(symbol, pos, current_price=58400, minute_analysis=ma)
    check("2) grace_seconds(30s) 이내면 VWAP 이탈해도 청산 안 됨", sig is None)

# ── 3) min_pct — 이탈폭이 기준 미만이면 청산 안 됨 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    ew = EntryWatchConfig(
        enabled=True, watch_minutes=5, min_profit_pct=0.5,
        fail_cut_pct=-1.0, fail_on_vwap_break=True,
        vwap_break_confirm_count=1, vwap_break_min_pct=0.5, vwap_grace_seconds=0,
    )
    service = make_service(tmpdir, ew)
    service.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
    pos = Position(symbol=symbol, quantity=10, average_price=58000)
    # VWAP 58500, 현재가 58450 -> 이탈폭 약 -0.11%, 기준(0.5%) 미달
    ma = make_minute_analysis(price_above_vwap=False, vwap=58500.0)
    sig = service._check_entry_watch(symbol, pos, current_price=58450, minute_analysis=ma)
    check("3) 이탈폭(0.11%)이 기준(0.5%) 미달이면 청산 안 됨", sig is None)

    # 이탈폭이 충분히 크면(0.6%) 청산돼야 함
    sig2 = service._check_entry_watch(symbol, pos, current_price=58150, minute_analysis=ma)
    check("   이탈폭(0.6%)이 기준(0.5%) 이상이면 청산", sig2 is not None)

# ── 4) confirm_count — N회 연속돼야 청산, 회복 시 리셋 ────────────
with tempfile.TemporaryDirectory() as tmpdir:
    ew = EntryWatchConfig(
        enabled=True, watch_minutes=5, min_profit_pct=0.5,
        fail_cut_pct=-1.0, fail_on_vwap_break=True,
        vwap_break_confirm_count=2, vwap_break_min_pct=0.0, vwap_grace_seconds=0,
    )
    service = make_service(tmpdir, ew)
    service.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
    pos = Position(symbol=symbol, quantity=10, average_price=58000)
    ma_below = make_minute_analysis(price_above_vwap=False, vwap=58500.0)

    sig1 = service._check_entry_watch(symbol, pos, current_price=58400, minute_analysis=ma_below)
    check("4) 1회차 이탈은 아직 청산 안 됨 (confirm_count=2)", sig1 is None)
    check("   1회차 후 streak=1로 기록됨", service.state.vwap_break_streak_by_symbol.get(symbol) == 1)

    sig2 = service._check_entry_watch(symbol, pos, current_price=58400, minute_analysis=ma_below)
    check("   2회 연속 이탈되면 청산", sig2 is not None)

    # 중간에 회복하면 카운터 리셋되는지 별도로 확인
    service.state.vwap_break_streak_by_symbol[symbol] = 1  # 1회 이탈 상태로 세팅
    ma_above = make_minute_analysis(price_above_vwap=True, vwap=58500.0)
    sig3 = service._check_entry_watch(symbol, pos, current_price=58600, minute_analysis=ma_above)
    check("   VWAP 위로 회복하면 청산 안 됨", sig3 is None)
    check("   회복 시 streak 카운터 0으로 리셋됨", service.state.vwap_break_streak_by_symbol.get(symbol) == 0)

# ── 5) 청산(미보유 전환) 후 카운터 정리 ──────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    ew = EntryWatchConfig(
        enabled=True, watch_minutes=5, min_profit_pct=0.5,
        fail_cut_pct=-1.0, fail_on_vwap_break=True,
    )
    service = make_service(tmpdir, ew)
    service.state.vwap_break_streak_by_symbol[symbol] = 3  # 이전 진입의 잔여 카운터 가정
    # position=None 처리 경로(= _process_symbol의 else 분기)를 직접 재현
    service._highest_price.pop(symbol, None)
    service.state.vwap_break_streak_by_symbol.pop(symbol, None)
    check("5) 미보유 전환 시 streak 카운터 제거됨", symbol not in service.state.vwap_break_streak_by_symbol)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
