# -*- coding: utf-8 -*-
"""
order_block_reason 정확성 검증 (2026-07-22, GPT 검토 3번 지적)

_try_buy()가 반환하는 차단 사유 문자열이 실제 차단 사유와 일치하는지
확인한다. 기존엔 다음 두 지점이 잘못된 고정 문자열을 반환하고 있었음:

- 최대 보유 종목 수 초과 -> "STOPLOSS_COOLDOWN" (엉뚱한 사유)
- RiskManager 거부 전체 -> "TRAIL_LOSS_COOLDOWN" (실제 reason 무시)
- BUY 신호 쿨다운 -> bare return(None) (다른 사유들과 계약 불일치,
  if block: 체크에서 차단없음으로 오인되어 signal_log에 BLOCKED로
  기록조차 안 됐을 가능성)
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import AccountBalance, Position
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.storage.skip_reason import SkipReason
from utils.time_utils import KST_TZ

# 2026-07-28 (GPT 코드리뷰 지적, 8차): 이 파일은 TradingService.
# _try_buy()를 직접 호출하는데, 그 안의 14:50 KST 게이트가(1B.10에서
# datetime.now()->now_kst()로 교체됨) 시각을 고정하지 않으면 실행
# 시각(KST 14:50 이후)에 따라 항상 AFTER_1450으로 먼저 막혀버림
# — 실제로 테스트 실행 시각이 KST 16:28이었을 때 1~4번이 전부
# 이 이유로 실패하는 것을 재현 확인. 이 파일이 검증하려는 건 각
# 시나리오의 차단 사유(SkipReason 등)이지 시간 게이트가 아니므로,
# 모든 _try_buy() 호출을 장중 KST(10:00)로 고정해 실행 시각과
# 무관하게 항상 같은 결과를 내도록 함.
FIXED_MARKET_TIME = datetime(2026, 7, 28, 10, 0, 0, tzinfo=KST_TZ)


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


symbol = "475150"

# ── 1) 최대 보유 종목 수 초과 -> SKIP_MAX_POSITIONS ──────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.trading, "max_positions", 1)
    # 다른 종목을 이미 1개 보유 중으로 세팅 (max_positions=1이므로 초과)
    service.broker._positions["000660"] = Position(symbol="000660", quantity=10, average_price=10000)

    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(
            symbol, 58000, balance, signal=None, regime=None, minute_analysis=None,
        )
    check("1) 최대보유종목수 초과 -> SKIP_MAX_POSITIONS 반환 (예전엔 STOPLOSS_COOLDOWN 오반환)",
          block == SkipReason.MAX_POSITIONS)

# ── 2) RiskManager 거부 시 실제 reason 그대로 반환 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    # 최소현금버퍼를 매우 크게 설정해 RISK_LIMIT으로 거부되도록 유도
    object.__setattr__(service.settings.risk, "min_cash_buffer", 999_999_999_999)

    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(
            symbol, 58000, balance, signal=None, regime=None, minute_analysis=None,
        )
    check("2) RiskManager RISK_LIMIT 거부 -> SKIP_RISK_LIMIT 그대로 반환 (예전엔 TRAIL_LOSS_COOLDOWN 오반환)",
          block == SkipReason.RISK_LIMIT)

# ── 3) RiskManager의 ALREADY_HOLDING 게이트도 정확히 전달되는지 ───
# (_try_buy 자체의 1일1회 게이트는 symbol_entry_count_today 기반이고,
#  RiskManager.can_place_order()의 ALREADY_HOLDING은 bought_symbols_today
#  기반 — 서로 다른 경로. 여기서는 RiskManager 쪽 게이트만 걸리도록
#  _try_buy의 1일1회 게이트는 allow_multi=True로 비활성화한 채,
#  RiskManager 생성 시에는 allow_multi=False로 줘서 그쪽 게이트만 작동시킴)
with tempfile.TemporaryDirectory() as tmpdir:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.trading, "allow_multiple_entries_per_symbol_per_day", True)
    broker = MockBroker()
    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store = JsonStateStore(settings.storage.state_file)
    strategy_router = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    service = TradingService(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
    )
    # RiskManager 쪽 게이트만 강제로 걸기 위해 can_place_order를 직접 확인
    balance = service.broker.get_account_balance()
    service.state.bought_symbols_today.add(symbol)
    object.__setattr__(risk_manager.trading_config, "allow_multiple_entries_per_symbol_per_day", False)

    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(
            symbol, 58000, balance, signal=None, regime=None, minute_analysis=None,
        )
    check("3) RiskManager의 ALREADY_HOLDING 사유도 정확히 그대로 전달됨",
          block == SkipReason.ALREADY_HOLDING)

# ── 4) BUY 신호 쿨다운 -> 명시적 문자열 반환 (기존엔 bare return=None) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service._last_buy_signal_at[symbol] = datetime.now()  # 방금 신호 발생 -> 10분 쿨다운 중

    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(
            symbol, 58000, balance, signal=None, regime=None, minute_analysis=None,
        )
    check("4) BUY 신호 쿨다운 -> 명시적 문자열 반환 (예전엔 None)",
          block == "BUY_SIGNAL_COOLDOWN")
    check("   if block: 체크에서 True로 평가됨 (signal_log에 BLOCKED로 정확히 기록될 것)",
          bool(block) is True)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
