# -*- coding: utf-8 -*-
"""
run_once() 통합 테스트 (2026-07-20)

MockBroker로 TradingService를 실제 조립해서 run_once()를 한 번 실행하고,
_process_symbol이 호출되는 실제 순서가 "보유 종목 먼저"인지 확인합니다.
동시에 run_once()가 예외 없이 끝까지 도는지(회귀 없음)도 함께 검증합니다.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from config.settings import (
    AppConfig, BrokerConfig, TradingConfig, StrategyConfig, RiskConfig,
    MarketRegimeConfig, StorageConfig, WebSocketConfig, EntryWatchConfig,
    KakaoConfig, Settings,
)
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import Position
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore


def build_minimal_settings(tmpdir: str) -> Settings:
    return Settings(
        app=AppConfig(name="test", env="local", log_level="INFO"),
        broker=BrokerConfig(
            provider="kiwoom", use_mock=True, base_url="", app_key="",
            secret_key="", account_number="", is_paper_trading=True,
        ),
        targets=["005930", "000660", "010170", "006260", "080220"],
        trading=TradingConfig(
            poll_interval_seconds=10, balance_refresh_seconds=180,
            price_refresh_seconds=60, order_cash_per_trade=1000000,
            max_positions=5, allow_multiple_entries_per_symbol_per_day=True,
            force_exit_before_market_close_minutes=20,
            reentry_cooldown_seconds=600, excluded_symbols=[],
        ),
        strategy=StrategyConfig(
            name="breakout", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.2, trend_reversal_rsi=70.0,
        ),
        risk=RiskConfig(
            max_daily_loss_amount=1000000, max_consecutive_losses=3,
            max_order_amount=6000000, min_cash_buffer=100000,
        ),
        market_regime=MarketRegimeConfig(
            short_ma_days=5, long_ma_days=20, history_days=50, rsi_period=14,
            rsi_overbought=80.0, rsi_oversold=20.0, history_refresh_seconds=3600,
            macd_fast=12, macd_slow=26, macd_signal=9, volume_surge_ratio=1.5,
            minute_tick_scope=1, minute_bar_count=60, minute_refresh_seconds=60,
            min_trading_value=50000000000, pullback_min_pct=-7.0,
            pullback_max_pct=-0.3, change_rate_min=2.0, change_rate_max=18.0,
            rebound_min_pct=2.0, gap_pullback_min_pct=5.0, gap_pullback_max_pct=10.0,
            v_bottom_lookback=30, v_low_min_age=1, v_low_max_age=8,
            v_drop_threshold_pct=-2.5, v_rebound_threshold_pct=0.5,
            v_max_rebound_pct=6.0, v_volume_ratio=1.2, v_min_bar_amount=30000000,
            v_bottom_spike_ratio=1.5, v_ma5_slope_bars=3,
        ),
        storage=StorageConfig(
            state_file=f"{tmpdir}/state.json",
            trade_log_file=f"{tmpdir}/trades.csv",
            signal_log_file=f"{tmpdir}/signal_log.csv",
            app_log_file=f"{tmpdir}/app.log",
            save_minute_bars=False, minute_bars_dir=f"{tmpdir}/minute_bars",
        ),
        websocket=WebSocketConfig(
            enabled=False, url="", condition_seqs=[], max_symbols=10,
            app_key="", secret_key="",
        ),
        entry_watch=EntryWatchConfig(
            enabled=True, watch_minutes=5, min_profit_pct=0.5,
            fail_cut_pct=-1.0, fail_on_vwap_break=True,
        ),
        kakao=KakaoConfig(),
    )


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = build_minimal_settings(tmpdir)

        broker = MockBroker()
        # targets 중 뒤쪽(4번째 인덱스, 5번째 종목) 010170을 보유 중으로 설정
        # -> 기존 로직이면 앞선 4종목(1초씩 sleep) 다 거친 뒤에야 처리됨
        broker._positions["010170"] = Position(symbol="010170", quantity=10, average_price=9500)
        broker._prices["010170"] = 9000  # -5.3% 손실 상태 (손절 기준 -1.5% 하회)

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

        # _process_symbol 호출 순서를 기록하기 위해 래핑
        call_order: list[str] = []
        original = service._process_symbol

        async def tracking_process_symbol(symbol, balance):
            call_order.append(symbol)
            return await original(symbol, balance)

        service._process_symbol = tracking_process_symbol

        # asyncio.sleep을 패치해서 테스트 속도를 올림 (실제 sleep 없이 순서만 검증)
        real_sleep = asyncio.sleep
        async def fast_sleep(_seconds):
            await real_sleep(0)
        asyncio.sleep = fast_sleep

        try:
            await service.run_once()
        finally:
            asyncio.sleep = real_sleep

        print("targets 원래 순서:", settings.targets)
        print("실제 _process_symbol 호출 순서:", call_order)

        ok = True
        if call_order[0] != "010170":
            print("[FAIL] 보유종목(010170)이 첫 번째로 처리되지 않음")
            ok = False
        else:
            print("[PASS] 보유종목(010170)이 첫 번째로 처리됨")

        if set(call_order) == set(settings.targets):
            print("[PASS] 모든 종목이 빠짐없이 처리됨 (누락/중복 없음)")
        else:
            print("[FAIL] 처리된 종목 집합이 targets와 불일치:", set(call_order), "vs", set(settings.targets))
            ok = False

        if len(call_order) == len(set(call_order)):
            print("[PASS] 중복 처리 없음")
        else:
            print("[FAIL] 종목이 중복 처리됨:", call_order)
            ok = False

        # 나머지(미보유) 종목 순서가 원래 targets 순서 그대로인지 확인
        remaining_expected = [s for s in settings.targets if s != "010170"]
        remaining_actual = call_order[1:]
        if remaining_actual == remaining_expected:
            print("[PASS] 나머지 미보유 종목은 원래 targets 순서 그대로 유지")
        else:
            print(f"[FAIL] 나머지 순서 불일치: {remaining_actual} vs {remaining_expected}")
            ok = False

        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
