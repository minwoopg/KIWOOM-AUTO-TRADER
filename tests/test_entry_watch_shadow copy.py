# -*- coding: utf-8 -*-
"""
entry_watch counterfactual(반사실적) 비교 로깅 단위테스트 (2026-07-22)

entry_watch가 SELL을 낸 시점 이후에도 해당 종목 가격을 계속 관찰해서
"청산 안 했다면 어떻게 됐을지"를 5/10/20분 체크포인트마다 기록하는
기능을 검증한다.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import Position
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import (
    TradeCsvLogger, SignalCsvLogger, build_app_logger, EntryWatchShadowLogger,
)
from infra.storage.state_store import JsonStateStore
from config.settings import EntryWatchConfig


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
    shadow_logger = EntryWatchShadowLogger(f"{tmpdir}/entry_watch_shadow.csv")
    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    return TradingService(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
        entry_watch_shadow_logger=shadow_logger,
    )


symbol = "475150"
ew = EntryWatchConfig(
    enabled=True, watch_minutes=5, min_profit_pct=0.5,
    fail_cut_pct=-1.0, fail_on_vwap_break=True,
)

# ── 1) 추적 시작 시 필드가 정확히 채워지는지 ──────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")

    tracking = service._entry_watch_shadow_tracking.get(symbol)
    check("1) 추적 시작 시 entry_price 기록", tracking is not None and tracking["entry_price"] == 58000)
    check("   trigger_price 기록", tracking["trigger_price"] == 57500)
    check("   actual_pnl_pct 계산 정확 (57500/58000-1)*100 ≈ -0.86%",
          abs(tracking["actual_pnl_pct"] - ((57500 - 58000) / 58000 * 100)) < 0.001)
    check("   trigger_type 기록", tracking["trigger_type"] == "급락청산")
    check("   checkpoints_done 빈 set으로 시작", tracking["checkpoints_done"] == set())

# ── 2) 5분 미경과 시 체크포인트 기록 안 됨 ────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")
    # trigger_at을 3분 전으로 조작
    service._entry_watch_shadow_tracking[symbol]["trigger_at"] = datetime.now() - timedelta(minutes=3)
    service._check_entry_watch_shadow_checkpoints(symbol, current_price=58200)
    check("2) 5분 미경과(3분) -> 체크포인트 기록 안 됨",
          service._entry_watch_shadow_tracking[symbol]["checkpoints_done"] == set())

# ── 3) 5분 경과 시 체크포인트 기록되고 로그에 남음 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")
    service._entry_watch_shadow_tracking[symbol]["trigger_at"] = datetime.now() - timedelta(minutes=6)
    # 6분 시점 현재가 58500 -> counterfactual: (58500-58000)/58000*100 ≈ +0.86%
    service._check_entry_watch_shadow_checkpoints(symbol, current_price=58500)

    check("3) 5분 체크포인트 기록됨",
          5 in service._entry_watch_shadow_tracking[symbol]["checkpoints_done"])

    with open(f"{tmpdir}/entry_watch_shadow.csv", encoding="utf-8") as f:
        content = f.read()
    check("   CSV 파일에 실제로 기록됨", symbol in content and "5" in content)

# ── 4) entry_watch_effect_pct 부호 검증: 도움된 경우(계속 하락) ───
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    # 청산가 57500 (실제 손실 -0.86%), 그 이후 계속 하락해서 5분 뒤 56000
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")
    service._entry_watch_shadow_tracking[symbol]["trigger_at"] = datetime.now() - timedelta(minutes=6)

    import csv
    service._check_entry_watch_shadow_checkpoints(symbol, current_price=56000)
    with open(f"{tmpdir}/entry_watch_shadow.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    effect = float(row["entry_watch_effect_pct"])
    check("4) 계속 하락했을 시나리오 -> effect가 음수(entry_watch가 손실 줄임=도움)",
          effect < 0)

# ── 5) entry_watch_effect_pct 부호 검증: 손해본 경우(청산 후 반등) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    # 청산가 57500 (실제 손실 -0.86%), 그 이후 반등해서 5분 뒤 59000(+1.7%)
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")
    service._entry_watch_shadow_tracking[symbol]["trigger_at"] = datetime.now() - timedelta(minutes=6)
    service._check_entry_watch_shadow_checkpoints(symbol, current_price=59000)

    import csv
    with open(f"{tmpdir}/entry_watch_shadow.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    effect = float(row["entry_watch_effect_pct"])
    check("5) 청산 후 반등 시나리오 -> effect가 양수(entry_watch가 좋은 거래를 잘라냄=손해)",
          effect > 0)

# ── 6) 20분(모든 체크포인트) 경과 시 추적 자동 종료 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    service._start_entry_watch_shadow_tracking(symbol, pos, 58000, 57500, ew, "급락청산")
    service._entry_watch_shadow_tracking[symbol]["trigger_at"] = datetime.now() - timedelta(minutes=25)
    service._check_entry_watch_shadow_checkpoints(symbol, current_price=58300)

    check("6) 20분 경과(모든 체크포인트 기록) -> 추적 딕셔너리에서 제거됨",
          symbol not in service._entry_watch_shadow_tracking)

    with open(f"{tmpdir}/entry_watch_shadow.csv", encoding="utf-8") as f:
        content = f.read()
    lines = [l for l in content.strip().split("\n") if symbol in l]
    check("   5/10/20분 세 체크포인트 전부 기록됨(3줄)", len(lines) == 3)

# ── 7) 추적 대상이 아닌 종목은 체크해도 아무 일 없음 ──────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._check_entry_watch_shadow_checkpoints("999999", current_price=10000)
    check("7) 추적 대상 아닌 종목 체크 -> 예외 없이 조용히 반환", True)

# ── 8) _check_entry_watch가 실제로 SELL 낼 때 추적이 자동 시작되는지 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.settings = type(service.settings)(**{**service.settings.__dict__})  # no-op, keep settings
    object.__setattr__(service.settings, "entry_watch", ew)
    service.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
    pos = Position(symbol=symbol, quantity=100, average_price=58000)
    sig = service._check_entry_watch(symbol, pos, current_price=57000, minute_analysis=None)  # -1.7% 급락
    check("8) _check_entry_watch가 SELL 낼 때 실제로 추적 시작됨",
          sig is not None and symbol in service._entry_watch_shadow_tracking)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
