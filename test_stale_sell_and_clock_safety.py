# -*- coding: utf-8 -*-
"""
stale SELL 세분화 및 KST Clock 게이트 검증 (2026-07-28, "1B Safety Closure")

배경: 1B.7부터 세 라운드째 이월되던 항목을 이번에 마무리:
1. entry_safe=False일 때 [MIN]/[V_FAIL]/low_volume_count/자동제외
   상태를 갱신하지 않음
2. stale 상태에서는 현재가 기반 hard-risk SELL(고정손절/트레일링/
   안전망 익절)만 허용하고, VWAP/MA 등 지표 기반 SELL(추세 꺾임)은
   fresh 데이터가 있을 때만 허용
3. _try_buy()의 14:50 게이트를 datetime.now()(naive)에서
   now_kst()(1B.8에서 신설, tzdata 비의존 고정 UTC+9)로 전환

Signal에 requires_fresh_minute_data 필드를 추가(기본 False) —
breakout_strategy.py와 neutral_strategy.py의 "추세 꺾임"(VWAP/MA5
이탈을 점수에 반영) SELL 신호에만 True로 표시. bottom_strategy,
hold_strategy, swing_strategy의 SELL은 전부 가격 또는 일봉 지표
(RSI/MACD, minute_analysis와 무관) 기반이라 건드리지 않음.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis, MinuteDataResult
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import AccountBalance, MarketRegime, Position, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.storage.skip_reason import SkipReason
from utils.time_utils import KST_TZ


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


def make_minimal_minute_analysis() -> MinuteAnalysis:
    return MinuteAnalysis(
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


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: stale 상태에서 진입 관련 카운터/로그 상태 불변
# ══════════════════════════════════════════════════════════════

# ── 1) entry_safe=False일 때 low_volume_count가 갱신되지 않음 ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    object.__setattr__(fake_analysis, "is_valid_trading_value", False)  # 거래대금 부족 조건
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.HOLD, reason="거래대금 부족"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.HOLD, reason="거래대금 부족"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))
            asyncio.run(service._process_symbol(symbol, balance))
            asyncio.run(service._process_symbol(symbol, balance))

    check("1) stale 상태로 3회 연속 거래대금부족 HOLD여도 low_volume_count가 0 유지"
          "(entry_safe=False라 카운터 갱신 자체를 안 함)",
          service._low_volume_count.get(symbol, 0) == 0)
    check("   자동제외 상태도 아님", symbol not in service._excluded_symbols)

# ── 2) (대조군) entry_safe=True면 정상적으로 카운터가 올라감(회귀 확인) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    object.__setattr__(fake_analysis, "is_valid_trading_value", False)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=True, source="LIVE",
            reason="", latest_bar_timestamp="20260728120000", age_seconds=5.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.HOLD, reason="거래대금 부족"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.HOLD, reason="거래대금 부족"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("2) (대조군) entry_safe=True면 low_volume_count가 정상적으로 1회 증가함"
          "(회귀 없음)", service._low_volume_count.get(symbol, 0) == 1)

# ══════════════════════════════════════════════════════════════
# 2부: stale SELL 세분화 (hard-risk 허용, indicator 차단)
# ══════════════════════════════════════════════════════════════

# ── 3) stale + hard-risk SELL(손절, requires_fresh=False 기본값) -> 허용 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=60000)])
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 — 평균단가 대비 -3.0%"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 — 평균단가 대비 -3.0%"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("3) stale + hard-risk SELL(손절) -> _try_sell 실제 호출됨(허용)",
          len(sell_calls) == 1)

# ── 4) stale + indicator SELL(추세꺾임, requires_fresh=True) -> 차단 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=60000)])
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="추세 꺾임 3/5점",
                                 requires_fresh_minute_data=True),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="추세 꺾임 3/5점",
                                 requires_fresh_minute_data=True),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("4) stale + indicator SELL(추세꺾임) -> _try_sell이 호출되지 않음(차단)",
          len(sell_calls) == 0)

# ── 5) (대조군) fresh + indicator SELL -> 정상 허용(회귀 확인) ──────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=60000)])
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=True, source="LIVE",
            reason="", latest_bar_timestamp="20260728120000", age_seconds=5.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="추세 꺾임 3/5점",
                                 requires_fresh_minute_data=True),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="추세 꺾임 3/5점",
                                 requires_fresh_minute_data=True),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("5) (대조군) fresh + indicator SELL -> _try_sell 정상 호출됨(회귀 없음)",
          len(sell_calls) == 1)

# ══════════════════════════════════════════════════════════════
# 3부: 14:50 게이트 KST Clock 검증
# ══════════════════════════════════════════════════════════════

# ── 6) 14:49:59 -> 허용(차단 안 됨) ───────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = service.broker.get_account_balance()
    fixed = datetime(2026, 7, 28, 14, 49, 59, tzinfo=KST_TZ)
    with patch("domain.service.trading_service.now_kst", return_value=fixed):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("6) 14:49:59 -> AFTER_1450으로 차단되지 않음", block != "AFTER_1450")

# ── 7) 14:50:00 -> 차단 ──────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = service.broker.get_account_balance()
    fixed = datetime(2026, 7, 28, 14, 50, 0, tzinfo=KST_TZ)
    with patch("domain.service.trading_service.now_kst", return_value=fixed):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("7) 14:50:00 -> AFTER_1450으로 정확히 차단됨", block == "AFTER_1450")

# ── 8) 14:50:01 -> 차단 ──────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = service.broker.get_account_balance()
    fixed = datetime(2026, 7, 28, 14, 50, 1, tzinfo=KST_TZ)
    with patch("domain.service.trading_service.now_kst", return_value=fixed):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("8) 14:50:01 -> AFTER_1450으로 차단됨", block == "AFTER_1450")

# ── 9) UTC 서버 환경(로컬시각이 UTC)에서도 정확히 KST 14:50 기준 판정 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = service.broker.get_account_balance()

    class FakeUTCDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            utc_now = datetime(2026, 7, 28, 5, 50, 0, tzinfo=timezone.utc)  # UTC 05:50 = KST 14:50
            if tz is not None:
                return utc_now.astimezone(tz)
            return utc_now.replace(tzinfo=None)

    with patch("utils.time_utils.datetime", FakeUTCDatetime):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("9) UTC 서버(로컬시각 05:50 UTC = 14:50 KST) -> 정확히 AFTER_1450으로 차단됨",
          block == "AFTER_1450")

# ── 10) UTC 서버 환경에서 14:49:59 KST(=UTC 05:49:59)는 허용 ────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = service.broker.get_account_balance()

    class FakeUTCDatetime2(datetime):
        @classmethod
        def now(cls, tz=None):
            utc_now = datetime(2026, 7, 28, 5, 49, 59, tzinfo=timezone.utc)  # UTC 05:49:59 = KST 14:49:59
            if tz is not None:
                return utc_now.astimezone(tz)
            return utc_now.replace(tzinfo=None)

    with patch("utils.time_utils.datetime", FakeUTCDatetime2):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("10) UTC 서버(로컬시각 05:49:59 UTC = 14:49:59 KST) -> 차단되지 않음",
          block != "AFTER_1450")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
