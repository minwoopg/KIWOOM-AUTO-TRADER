# -*- coding: utf-8 -*-
"""
분봉 데이터 신선도 기반 신규매수 차단 검증 (2026-07-27, 1B.6 안전 핫픽스)

배경: TradingService._get_minute_analysis()가 get_minute_bars()
실패 시 오래된 캐시를 그대로 분석에 넘기면서, 이 데이터가 신선한지
아닌지 호출부에 전혀 알리지 않았음 — 실운영에서 분봉 조회 실패
직후 오래된 캐시 기반 신규 매수(039980)가 발생한 원인. GPT
코드리뷰 지적으로 발견.

이 테스트는 다음을 검증합니다:
1. _get_minute_analysis()가 (analysis, is_fresh, reason) 튜플을
   정확히 반환하는지 (정상/실패+캐시있음/실패+캐시없음 3가지 케이스)
2. 미보유 종목에서 stale 데이터로 BUY 신호가 나오면 강제로 HOLD로
   전환되는지 (_process_symbol 전체 흐름에서)
3. 보유 종목의 SELL(손절 등)은 stale 데이터여도 차단되지 않는지
4. 신선한 데이터에서는 아무 영향이 없는지(회귀 확인)
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import AccountBalance, MarketRegime, MinuteBar, Position, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore


def make_minimal_minute_analysis() -> MinuteAnalysis:
    """테스트용 최소 MinuteAnalysis — score() 등 실제 메서드 호출이
    가능하도록 object() 대신 진짜 인스턴스를 만듦(2026-07-27, 이전
    버전은 object()를 써서 AttributeError로 조기 실패했었음)."""
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

# ══════════════════════════════════════════════════════════════
# 1부: _get_minute_analysis() 반환값 검증
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)

    # ── 1) 정상 조회 성공 -> fresh=True, reason='' ────────────────
    analysis1, fresh1, reason1 = service._get_minute_analysis(symbol, 58000)
    check("1) 정상 조회 성공 시 fresh=True", fresh1 is True)
    check("   reason은 빈 문자열", reason1 == "")

    # ── 2) 조회 실패 + 캐시 있음 -> fresh=False, STALE_MINUTE_DATA ──
    bars = [
        MinuteBar(cntr_tm=f"2026072109{i:02d}00", open_price=58000, high_price=58100,
                  low_price=57900, close_price=58000, volume=1000, acc_volume=50000)
        for i in range(60)
    ]
    service.cached_minute_bars[symbol] = bars
    service.cached_minute_bars_loaded_at[symbol] = datetime(2026, 7, 21, 9, 0, 0)  # 오래된 시각

    def failing_get_minute_bars(*a, **kw):
        raise RuntimeError("API 실패 시뮬레이션")

    service.broker.get_minute_bars = failing_get_minute_bars
    analysis2, fresh2, reason2 = service._get_minute_analysis(symbol, 58000)
    check("2) 조회 실패 + 캐시 있음 -> fresh=False", fresh2 is False)
    check("   reason=STALE_MINUTE_DATA", reason2 == "STALE_MINUTE_DATA")
    check("   analysis는 캐시 기반으로 여전히 계산됨(기존 동작 유지, "
          "보유종목 손절 판단이 끊기지 않도록)", analysis2 is not None)

    # ── 3) 조회 실패 + 캐시도 없음 -> fresh=False, MINUTE_DATA_UNAVAILABLE ──
    symbol_no_cache = "999999"
    analysis3, fresh3, reason3 = service._get_minute_analysis(symbol_no_cache, 58000)
    check("3) 조회 실패 + 캐시도 없음 -> fresh=False", fresh3 is False)
    check("   reason=MINUTE_DATA_UNAVAILABLE", reason3 == "MINUTE_DATA_UNAVAILABLE")
    check("   analysis는 None", analysis3 is None)

# ══════════════════════════════════════════════════════════════
# 2부: _process_symbol() 통합 흐름에서 실제 BUY 차단 검증
# ══════════════════════════════════════════════════════════════

# ── 4) 미보유 종목 + stale 데이터 + BUY 신호 -> HOLD로 강제 전환 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)

    # _get_minute_analysis를 강제로 (실제analysis, fresh=False, STALE) 반환하도록 patch
    fake_analysis = make_minimal_minute_analysis()

    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(
        service, "_get_minute_analysis",
        return_value=(fake_analysis, False, "STALE_MINUTE_DATA"),
    ):
        with patch.object(
            service._minute_analyzer, "analyze", return_value=fake_analysis,
        ):
            # strategy.generate_signal이 무조건 BUY를 내도록 강제 -
            # 실제 전략 조건을 다 만족시키기보다 신호 자체를 조작해
            # "만약 BUY가 나왔다면"이라는 조건을 명확히 재현
            with patch(
                "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
                return_value=Signal(type=SignalType.BUY, reason="테스트용 강제 BUY"),
            ), patch(
                "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
                return_value=Signal(type=SignalType.BUY, reason="테스트용 강제 BUY"),
            ), patch.object(
                service, "_check_entry_watch", return_value=None,
            ), patch.object(
                service.regime_classifier, "classify",
                return_value=(MarketRegime.BULLISH, "테스트"),
            ):
                await_result = None
                import asyncio
                asyncio.run(service._process_symbol(symbol, balance))

    # signal_log.csv에서 실제로 어떤 판단이 기록됐는지 확인
    import csv
    with open(service.settings.storage.signal_log_file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    matching = [r for r in rows if r["symbol"] == symbol]
    check("4) 미보유+stale+BUY신호 상황에서 signal_log에 기록이 남음", len(matching) >= 1)
    if matching:
        last_row = matching[-1]
        check("   final_decision이 BUY가 아니라 HOLD로 강제 전환됨(핵심 안전장치 검증)",
              last_row["final_decision"] != "BUY")
        check("   skip_reason 또는 관련 필드에 STALE_MINUTE_DATA 사유가 남음",
              "STALE_MINUTE_DATA" in str(last_row))

# ── 5) 보유 종목 + stale 데이터 + SELL 신호 -> 차단 없이 그대로 SELL 진행 ──
# (위험 축소 행동을 stale 데이터라고 막으면 안 됨 — GPT 지시 원칙)
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis2 = make_minimal_minute_analysis()
    balance_with_position = AccountBalance(
        cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=60000)],
    )

    sell_calls = []

    def fake_try_sell(sym, qty, price, **kwargs):
        sell_calls.append((sym, qty, price))

    with patch.object(
        service, "_get_minute_analysis",
        return_value=(fake_analysis2, False, "STALE_MINUTE_DATA"),
    ):
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 테스트"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 테스트"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=fake_try_sell,
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance_with_position))

    check("5) 보유종목 + stale 데이터 + SELL 신호 -> _try_sell이 실제로 호출됨"
          "(stale이어도 위험축소 SELL은 차단되지 않음)",
          len(sell_calls) == 1)

# ── 6) 미보유 종목 + 신선한(fresh) 데이터 + BUY 신호 -> 정상적으로 BUY 진행(회귀 확인) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis3 = make_minimal_minute_analysis()
    balance_no_position = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    buy_calls = []

    def fake_try_buy(sym, price, bal, **kwargs):
        buy_calls.append(sym)
        return ""  # 차단 사유 없음 = 매수 진행

    with patch.object(
        service, "_get_minute_analysis",
        return_value=(fake_analysis3, True, ""),  # fresh=True
    ):
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="신선한 데이터 BUY"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="신선한 데이터 BUY"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_buy", side_effect=fake_try_buy,
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance_no_position))

    check("6) (회귀) 미보유+fresh데이터+BUY신호 -> _try_buy가 정상적으로 호출됨"
          "(신선한 데이터에서는 안전장치가 개입하지 않음)",
          len(buy_calls) == 1)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
