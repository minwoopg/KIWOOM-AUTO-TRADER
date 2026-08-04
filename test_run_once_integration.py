# -*- coding: utf-8 -*-
"""
run_once() 통합 테스트 + 공용 테스트 헬퍼 (재작성, 2026-08-04)

배경: 기존 test_run_once_integration.py가 프로젝트에서 유실되어
(경위 불명 — CHANGELOG_v1.6.md에는 0.5단계에서 legacy_tests/의
"중복 사본"만 옮겼다고 기록되어 있으나 실제로는 루트 원본도 함께
사라진 상태였음), 8개 이상의 최신 안전 테스트(test_stale_minute_
data_safety.py, test_session_metrics_shadow.py 등)가 여기서 가져다
쓰는 build_minimal_settings()를 이 시점의 config/settings.py 구조에
맞춰 새로 작성.

기존 legacy_tests/test_run_once_integration.py나 이전에 별도로
전달받았던 사본들은 minute_bar_min_count_for_entry/minute_bar_max_
age_seconds/minute_fetch_backoff_seconds(1B.9~1B.10 신설 필드)를
전혀 모르는 구버전이었음 — dataclass 기본값이 있어 즉시 에러는
안 나지만, "이 시점 실제 설정과 정확히 일치"를 보장하려면 새로
작성하는 편이 안전하다고 판단. config/settings.py를 직접 읽어
모든 dataclass 필드(필수/기본값 있는 것 포함)를 하나씩 대조해
작성함 — 필드가 하나라도 다르면 이후 이 헬퍼를 쓰는 다른 테스트가
전부 조용히 잘못된 기준선으로 검증될 위험이 있어, 정확성을
최우선으로 둠.

이 파일이 원래 갖고 있던 두 가지 역할을 그대로 유지:
1. build_minimal_settings(): 다른 테스트들이 import해서 쓰는 공용
   헬퍼 (MockBroker로 최소 동작 가능한 Settings 생성)
2. run_once() 통합 테스트 본체: MockBroker로 TradingService를 실제
   조립해서 run_once()를 한 번 실행하고, _process_symbol이 호출되는
   실제 순서가 "보유 종목 먼저"인지 확인. 동시에 run_once()가 예외
   없이 끝까지 도는지(회귀 없음)도 함께 검증.
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


def build_minimal_settings(tmpdir: str) -> Settings:
    """MockBroker와 함께 즉시 동작 가능한 최소 Settings를 생성합니다.

    2026-08-04: config/settings.py의 모든 dataclass 필드를 하나씩
    대조해서 작성 — MarketRegimeConfig의 1B.9~1B.10 신설 필드
    (minute_bar_min_count_for_entry=60, minute_bar_max_age_seconds
    =120, minute_fetch_backoff_seconds=20)는 dataclass 기본값을
    그대로 사용(명시 생략)해도 __post_init__ 검증을 통과함 — 하지만
    "이 시점 실제 운영값과 정확히 일치"를 위해 기본값과 동일한 값을
    확인차 그대로 유지.
    """
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
            rebound_min_pct=0.3, gap_pullback_min_pct=0.5, gap_pullback_max_pct=3.0,
            v_bottom_lookback=30, v_low_min_age=1, v_low_max_age=8,
            v_drop_threshold_pct=-1.5, v_rebound_threshold_pct=0.5,
            v_max_rebound_pct=5.0, v_volume_ratio=1.0, v_min_bar_amount=10_000_000,
            v_bottom_spike_ratio=1.5, v_ma5_slope_bars=3,
            # 1B.9~1B.10에서 신설된 필드 — dataclass 기본값(60/120/20)과
            # 동일한 값을 명시해 이 시점 실제 운영값과 일치함을 확인차 고정.
            minute_bar_min_count_for_entry=60,
            minute_bar_max_age_seconds=120,
            minute_fetch_backoff_seconds=20,
        ),
        storage=StorageConfig(
            state_file=f"{tmpdir}/state.json",
            trade_log_file=f"{tmpdir}/trades.csv",
            signal_log_file=f"{tmpdir}/signal_log.csv",
            app_log_file=f"{tmpdir}/app.log",
            save_minute_bars=False,
            minute_bars_dir=f"{tmpdir}/minute_bars",
            # 2026-07-27 (0.5단계에서 발견된 문제 재발 방지): 이 두
            # 필드를 명시하지 않으면 StorageConfig 기본값(상대경로
            # "logs/entry_watch_shadow.csv" 등)이 그대로 쓰여서, 이
            # 헬퍼를 재사용하는 테스트를 실행할 때마다 프로젝트 루트의
            # 실제 logs/ 디렉토리에 CSV가 새는 사고가 있었음(재현
            # 확인됨, CHANGELOG_v1.6.md 0.5단계 3번 참고). 반드시
            # tmpdir 기준 경로로 명시.
            entry_watch_shadow_log_file=f"{tmpdir}/entry_watch_shadow.csv",
            position_lifecycle_log_file=f"{tmpdir}/position_lifecycle.csv",
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

        from domain.market_regime.classifier import MarketRegimeClassifier
        from domain.risk.risk_manager import RiskManager
        from domain.service.trading_service import TradingService
        from domain.strategy.strategy_router import StrategyRouter
        from domain.models import Position
        from infra.broker.mock_broker import MockBroker
        from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
        from infra.storage.state_store import JsonStateStore

        broker = MockBroker()
        # "보유 종목 먼저" 처리 순서 검증을 위해 targets 중간에 있는
        # 종목(010170)을 미리 보유 상태로 만들어 둠 — 만약 순서가
        # targets 그대로라면 010170은 3번째로 처리되어야 정상이지만,
        # "보유 종목 우선" 로직이 있다면 항상 1번째로 처리돼야 함.
        broker._positions["010170"] = Position(
            symbol="010170", quantity=10, average_price=15000,
        )

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

        processed_order: list[str] = []
        original_process_symbol = service._process_symbol

        async def tracking_process_symbol(symbol, balance):
            processed_order.append(symbol)
            return await original_process_symbol(symbol, balance)

        service._process_symbol = tracking_process_symbol

        try:
            await service.run_once()
        except Exception as exc:
            print(f"[FAIL] run_once() 실행 중 예외 발생: {exc}")
            import traceback
            traceback.print_exc()
            return 1

        print("targets 원래 순서:", settings.targets)
        print("실제 _process_symbol 호출 순서:", processed_order)

        passed = 0
        failed = 0

        def check(label: str, condition: bool) -> None:
            nonlocal passed, failed
            status = "PASS" if condition else "FAIL"
            print(f"[{status}] {label}")
            if condition:
                passed += 1
            else:
                failed += 1

        check(
            "보유종목(010170)이 첫 번째로 처리됨",
            bool(processed_order) and processed_order[0] == "010170",
        )
        check(
            "모든 종목이 빠짐없이 처리됨 (누락/중복 없음)",
            sorted(processed_order) == sorted(settings.targets),
        )
        check(
            "중복 처리 없음",
            len(processed_order) == len(set(processed_order)),
        )
        remaining_expected = [s for s in settings.targets if s != "010170"]
        remaining_actual = [s for s in processed_order if s != "010170"]
        check(
            "나머지 미보유 종목은 원래 targets 순서 그대로 유지",
            remaining_actual == remaining_expected,
        )

        print()
        print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
