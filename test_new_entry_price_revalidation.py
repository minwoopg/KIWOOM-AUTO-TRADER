# -*- coding: utf-8 -*-
"""2026-09-23 (신규 진입 가격·분봉 정합성 검증, GPT 재검토 반영).

배경: 9/23 삼성전자(005930) 매수 재현 — 세션 시작 직후(09:00:02)
캐시된 시세(276,500원)가 실제로는 283,500원대였던 것이, 매수 판단
(09:00:30, 캐시가 아직 재조회 주기 안이라 그대로 재사용됨)까지
28초간 이어졌고, 매수 직후 다음 폴링(보유로 전환되어 캐시를 우회하는
직접조회 경로, trading_service.py 2329행 부근)에서야 283,500원으로
점프한 것이 확인됨(즉, 매수 자체가 부정확한 가격 위에서 판단된
것으로 재현). `signal_log_20260923.csv`를 09:00:02~09:00:30 구간까지
행 단위로 대조해 확인했고, 분봉 분석은 이미 09:00:30 시점에 당일
09:00봉으로 정상 갱신되어 있었음에도 시세 캐시는 여전히 09:00:02의
값을 쓰고 있었다는 점에서 "시세 캐시"와 "분봉 분석"이 서로 독립된
경로라 개장 직후 구간에서 desync될 수 있음을 확인.

수정 내용(trading_service.py `_process_symbol()`, config/settings.py
`TradingConfig.new_entry_price_revalidation_tolerance_pct`): 신규
진입(position is None) BUY 신호가 나오면 `_try_buy()` 호출 직전에
`self.broker.get_market_price()`로 한 번 더 직접 재조회하여, 판단에
쓰인 현재가와 비교한다. 재조회 자체가 실패하면(예: 429) 이번 폴링의
매수는 보류(`PRICE_REVALIDATION_FAILED`)하고, 재조회는 성공했지만
차이가 허용치(`new_entry_price_revalidation_tolerance_pct`, 기본
0.5%)를 넘으면 역시 보류(`PRICE_STALE_VS_LIVE`)한다. 두 경우 모두
`Signal`을 HOLD로 전환할 뿐 전략 임계값·entry_watch·손익 확정
로직은 건드리지 않는다. 보유 종목(position is not None)의 매도
감시는 이 검사 이전 코드 경로(2329행 부근, 이미 캐시를 거치지 않는
직접조회)라 이 신규 블록 자체를 타지 않으므로 위험 축소 행동이
막히지 않는다.

이 테스트는 사용자가 명시한 5가지 사례를 검증합니다.

1. 장전 캐시가 남은 상태에서 개장 후 가격이 크게 달라짐 → 신규
   진입 보류(HOLD, PRICE_STALE_VS_LIVE), 캐시는 재조회한 값으로
   갱신됨.
2. 분봉은 최신(entry_safe=True)이지만 현재가가 오래됨(같은 메커니즘,
   분봉 신선도와 무관하게 독립적으로 동작함을 확인) → 동일하게 보류.
3. 재조회 자체가 429 등으로 실패함 → 예외 없이 보류
   (PRICE_REVALIDATION_FAILED), 크래시 없음.
4. 정상 입력(재조회 결과가 판단 시점 가격과 일치)에서는 기존 주문
   판단(_try_buy 호출)이 그대로 유지됨 — 회귀 없음.
5. 신규 진입 보류가 보유 종목의 청산(SELL) 감시를 막지 않음 — 이
   신규 블록은 position is None일 때만 실행되므로, 보유 종목
   처리에는 재조회 자체가 끼어들지 않음을 확인.

전략 임계값·entry_watch·손익 확정 로직은 전혀 건드리지 않았습니다 —
신규 진입 직전에 시세를 한 번 더 확인할지에 대한 순수 안전장치입니다.
"""
from __future__ import annotations

import asyncio
import csv
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.minute_analyzer import MinuteAnalysis, MinuteDataResult
from domain.models import AccountBalance, MarketPrice, MarketRegime, Position, Signal, SignalType
from domain.risk.risk_manager import RiskManager
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
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


SYMBOL = "005930"


def build_service(tmpdir: str, broker=None) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    broker = broker if broker is not None else MockBroker()
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
        vwap=283000.0, price_above_vwap=True, low_rising=False,
        pullback_pct=0.0, is_valid_pullback=False,
        change_rate_pct=0.0, is_valid_change_rate=False,
        rebound_pct=0.0, is_valid_rebound=False,
        trading_value=1_000_000_000, is_valid_trading_value=True,
        day_high=284500, day_low=276000, is_valid_pulldown=False,
        ma5_above_ma20=True,
        is_v_rebound=False, v_fail_reason="", v_bottom_k=0,
        v_drop_pct=0.0, v_rise_pct=0.0, v_volume_ratio=0.0,
        v_bottom_spike=False, v_ma5_rising=False,
        rebound_volume_spike=False, rebound_volume_ratio=0.0,
        upside_to_recent_high_pct=5.0,
        is_pulldown_recovery=False, pr_low_turning=False, pr_volume_expanding=False,
        is_slow_v_rebound=False, slow_v_drop_pct=0.0, slow_v_rise_pct=0.0, slow_v_bottom_k=0,
    )


def _seed_stale_cache(service: TradingService, symbol: str, price: int) -> None:
    """개장 직후 09:00:02에 캐시된 가격을 흉내냅니다 — 방금 채워진
    (=재조회 주기 안이라 elapsed < price_refresh_seconds) '신선한'
    캐시지만, 실제 값 자체가 틀린 상황(9/23 재현의 핵심)."""
    market_price = MarketPrice(
        symbol=symbol, current_price=price, reference_price=price,
        previous_close=int(price * 0.985), timestamp=datetime.now(),
    )
    service.cached_market_prices[symbol] = market_price
    service.cached_market_price_loaded_at[symbol] = datetime.now()


def _run_process_symbol_with_forced_buy(
    service: TradingService, balance: AccountBalance,
    minute_entry_safe: bool = True,
    try_buy_side_effect=None,
) -> None:
    fake_analysis = make_minimal_minute_analysis()
    with patch.object(service, "_get_minute_analysis") as mock_get_analysis:
        mock_get_analysis.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=minute_entry_safe,
            source="LIVE" if minute_entry_safe else "CACHE",
            reason="" if minute_entry_safe else "STALE_MINUTE_DATA",
            latest_bar_timestamp="20260923090000", age_seconds=5.0,
        )
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
        ), patch.object(
            service, "_try_buy",
            side_effect=try_buy_side_effect if try_buy_side_effect is not None else (lambda *a, **kw: ""),
        ) as mock_try_buy:
            asyncio.run(service._process_symbol(SYMBOL, balance))
            return mock_try_buy


def _last_signal_row(service: TradingService, symbol: str = SYMBOL) -> dict:
    with open(service.settings.storage.signal_log_file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    matching = [r for r in rows if r["symbol"] == symbol]
    return matching[-1] if matching else {}


# ══════════════════════════════════════════════════════════════
# 1) 장전 캐시가 남은 상태에서 개장 후 가격이 크게 달라짐 -> 보류
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    broker = MockBroker()
    service = build_service(tmpdir, broker)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    # 판단에 쓰인(캐시) 가격 276,500원 — 9/23 재현값
    _seed_stale_cache(service, SYMBOL, 276500)
    # 재조회(revalidation) 시 브로커가 돌려주는 실제 값 283,500원
    broker.get_market_price = lambda symbol: MarketPrice(
        symbol=symbol, current_price=283500, reference_price=283500,
        previous_close=280000, timestamp=datetime.now(),
    )

    mock_try_buy = _run_process_symbol_with_forced_buy(service, balance)

    check("1) 캐시(276,500) vs 재조회(283,500) 불일치 -> _try_buy() 호출 안 됨",
          mock_try_buy.call_count == 0)
    row = _last_signal_row(service)
    check("   final_decision이 BUY가 아님(HOLD로 전환됨)", row.get("final_decision") != "BUY")
    check("   캐시가 재조회한 실제 값(283,500)으로 갱신됨",
          service.cached_market_prices[SYMBOL].current_price == 283500)

# ══════════════════════════════════════════════════════════════
# 2) 분봉은 최신(entry_safe=True)이지만 현재가가 오래됨(같은
#    메커니즘, 분봉 신선도와 무관하게 독립 동작함을 확인)
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    broker = MockBroker()
    service = build_service(tmpdir, broker)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    _seed_stale_cache(service, SYMBOL, 276500)
    broker.get_market_price = lambda symbol: MarketPrice(
        symbol=symbol, current_price=284000, reference_price=284000,
        previous_close=280000, timestamp=datetime.now(),
    )

    mock_try_buy = _run_process_symbol_with_forced_buy(service, balance, minute_entry_safe=True)

    check("2) 분봉은 fresh(entry_safe=True)인데 현재가만 stale -> 그래도 보류됨"
          "(분봉 신선도 검사와 별개의 독립 경로임을 확인)",
          mock_try_buy.call_count == 0)
    row = _last_signal_row(service)
    check("   final_decision이 BUY가 아님", row.get("final_decision") != "BUY")

# ══════════════════════════════════════════════════════════════
# 3) 재조회 자체가 429 등으로 실패함 -> 예외 없이 보류
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    broker = MockBroker()
    service = build_service(tmpdir, broker)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    _seed_stale_cache(service, SYMBOL, 276500)

    def _raise_429(symbol):
        raise RuntimeError("429 Too Many Requests (simulated)")

    broker.get_market_price = _raise_429

    mock_try_buy = _run_process_symbol_with_forced_buy(service, balance)

    check("3) 재조회가 예외(429)로 실패 -> 예외 전파 없이 정상 종료되고 _try_buy() 호출 안 됨",
          mock_try_buy.call_count == 0)
    row = _last_signal_row(service)
    check("   final_decision이 BUY가 아님", row.get("final_decision") != "BUY")
    check("   기존 캐시(276,500)는 실패한 재조회로 덮어써지지 않고 보존됨",
          service.cached_market_prices[SYMBOL].current_price == 276500)

# ══════════════════════════════════════════════════════════════
# 4) (회귀) 정상 입력 -> 기존 주문 판단(_try_buy 호출)이 유지됨
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    broker = MockBroker()
    service = build_service(tmpdir, broker)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    _seed_stale_cache(service, SYMBOL, 283000)
    # 재조회 결과가 판단 시점 가격과 거의 동일(허용치 0.5% 이내)
    broker.get_market_price = lambda symbol: MarketPrice(
        symbol=symbol, current_price=283100, reference_price=283100,
        previous_close=280000, timestamp=datetime.now(),
    )

    mock_try_buy = _run_process_symbol_with_forced_buy(service, balance)

    check("4) (회귀) 재조회 결과가 판단 가격과 허용치 이내로 일치 -> _try_buy()가 정상 호출됨",
          mock_try_buy.call_count == 1)
    row = _last_signal_row(service)
    check("   final_decision이 BUY로 유지됨(정상 진행)", row.get("final_decision") == "BUY")

# ══════════════════════════════════════════════════════════════
# 5) 신규 진입 보류가 보유 종목의 청산(SELL) 감시를 막지 않음
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir:
    broker = MockBroker()
    service = build_service(tmpdir, broker)
    balance_with_position = AccountBalance(
        cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=SYMBOL, quantity=10, average_price=283905)],
    )
    fake_analysis = make_minimal_minute_analysis()
    sell_calls = []
    revalidate_calls = {"count": 0}

    def _counting_get_market_price(symbol):
        # 보유 종목 처리는 2329행 부근의 직접조회 경로를 타므로, 이번
        # 신규 블록(재검증)이 실행됐다면 추가 호출이 잡혔을 것 —
        # 그 여부로 "이 검사 자체가 실행되지 않았는지"를 판별.
        revalidate_calls["count"] += 1
        return MarketPrice(
            symbol=symbol, current_price=284250, reference_price=284250,
            previous_close=280000, timestamp=datetime.now(),
        )

    broker.get_market_price = _counting_get_market_price

    def fake_try_sell(sym, qty, price, **kwargs):
        sell_calls.append((sym, qty, price))

    with patch.object(service, "_get_minute_analysis") as mock_get_analysis:
        mock_get_analysis.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=True, source="LIVE",
            reason="", latest_bar_timestamp="20260923090500", age_seconds=5.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="entry_watch 최소수익미달청산 테스트"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="entry_watch 최소수익미달청산 테스트"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=fake_try_sell,
        ):
            asyncio.run(service._process_symbol(SYMBOL, balance_with_position))

    # 보유 종목 처리 경로는 _process_symbol() 진입 시 딱 1번(2329행)만
    # broker.get_market_price()를 호출함 — 신규 진입 재검증 블록은
    # position is None일 때만 실행되므로 추가 호출이 없어야 함.
    check("5-a) 보유 종목 처리에서 재조회 호출이 딱 1회(2329행 경로)만 발생 "
          "— 신규 진입 재검증 블록이 끼어들지 않음",
          revalidate_calls["count"] == 1)
    check("5-b) SELL 신호가 차단 없이 _try_sell()까지 정상 도달함", len(sell_calls) == 1)


print(f"\n총 {passed + failed}개 중 {passed}개 통과, {failed}개 실패")
if failed:
    sys.exit(1)
