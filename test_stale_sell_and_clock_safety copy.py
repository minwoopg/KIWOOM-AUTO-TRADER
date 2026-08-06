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
hold_strategy의 SELL은 전부 가격 또는 일봉 지표
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


def build_service(tmpdir: str, fixed_price: int | None = None) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    broker = MockBroker()
    if fixed_price is not None:
        # 2026-07-28 (entry_watch stale VWAP 핫픽스): MockBroker의
        # 기본가(475150은 목록에 없어 10000원)를 명시적으로 고정 —
        # entry_watch 급락청산(fail_cut_pct)이 우연히 먼저 발동하지
        # 않도록 pnl_pct를 정확히 계산해 시나리오를 설계하기 위함.
        broker._prices["475150"] = fixed_price
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


def make_minimal_minute_analysis(**overrides) -> MinuteAnalysis:
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

# ══════════════════════════════════════════════════════════════
# 4부: entry_watch stale VWAP 핫픽스 (2026-07-28, GPT 7차 지적)
#
# 배경: _check_entry_watch()는 정규 strategy.generate_signal()보다
# 먼저 실행되는 별도 경로인데, 여기에는 minute_analysis를 무조건
# 그대로 넘기고 있었음 — 3부(2부 코드 기준)의 SELL 세분화 테스트는
# 전부 _check_entry_watch를 patch(return_value=None)해서 이 경로
# 자체를 검증하지 못했음. 재현 확인: entry_time 2분 전 + stale +
# price_above_vwap=False 조합에서 실제로 SELL 신호와
# vwap_break_streak=1이 발생.
#
# 수정: entry_watch에 넘기는 minute_analysis를 entry_safe일 때만
# 전달(entry_safe=False면 None), stale이면 vwap_break_streak도
# 명시적으로 리셋. entry_watch의 급락청산(fail_cut_pct)·시간초과
# 청산(watch_minutes)은 가격/시간 기반이라 계속 허용.
# ══════════════════════════════════════════════════════════════

# ── 11) stale + entry_watch VWAP 이탈 -> SELL 없음, streak 없음 ──
# (_check_entry_watch를 patch하지 않은 진짜 통합 테스트 — GPT 지시대로 필수)
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, fixed_price=10000)
    now = datetime.now()
    service.state.entry_time_by_symbol[symbol] = (now - timedelta(minutes=2)).isoformat()
    fake_analysis = make_minimal_minute_analysis(price_above_vwap=False, vwap=10020.0)
    # 현재가 10000, average_price 9980 -> pnl 약 +0.2%(fail_cut_pct -1.0% 안 걸림)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=9980)])
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            # _check_entry_watch는 patch하지 않음 — 실제 경로 그대로 검증
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("11) stale + entry_watch VWAP 이탈(patch 없는 진짜 통합 테스트) -> "
          "_try_sell 호출 안 됨(차단)", len(sell_calls) == 0)
    check("    vwap_break_streak_by_symbol이 갱신되지 않음(None 유지)",
          service.state.vwap_break_streak_by_symbol.get(symbol) is None)

# ── 12) fresh + entry_watch VWAP 이탈 -> SELL 유지(회귀 확인) ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, fixed_price=10000)
    now = datetime.now()
    service.state.entry_time_by_symbol[symbol] = (now - timedelta(minutes=2)).isoformat()
    fake_analysis = make_minimal_minute_analysis(price_above_vwap=False, vwap=10020.0)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=9980)])
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=True, source="LIVE",
            reason="", latest_bar_timestamp="20260728120000", age_seconds=5.0,
        )
        with patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("12) fresh + entry_watch VWAP 이탈 -> _try_sell 정상 호출됨(회귀 없음)",
          len(sell_calls) == 1)

# ── 13) stale + fail_cut_pct 급락 -> SELL 유지(가격 기반) ────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, fixed_price=10000)
    now = datetime.now()
    service.state.entry_time_by_symbol[symbol] = (now - timedelta(minutes=1)).isoformat()
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=11000)])  # 약 -9% 급락
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("13) stale + entry_watch 급락청산(fail_cut_pct, 가격 기반) -> "
          "_try_sell 호출됨(허용)", len(sell_calls) == 1)

# ── 14) stale + watch_minutes 경과 후 최소수익 미달 -> SELL 유지 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, fixed_price=10000)
    now = datetime.now()
    # watch_minutes(5분)는 넘었지만 +1분 버퍼(6분) 안쪽으로 안전하게 5.5분
    service.state.entry_time_by_symbol[symbol] = (now - timedelta(minutes=5, seconds=30)).isoformat()
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=9990)])  # +0.1%, min_profit(0.5%) 미달
    sell_calls = []

    with patch.object(service, "_get_minute_analysis") as mock_get:
        mock_get.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="CACHE_STALE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch.object(
            service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=lambda *a, **kw: sell_calls.append("sold"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    check("14) stale + entry_watch 최소수익미달청산(시간+가격 기반) -> "
          "_try_sell 호출됨(허용)", len(sell_calls) == 1)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
