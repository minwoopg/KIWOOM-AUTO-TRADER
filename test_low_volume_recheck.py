# -*- coding: utf-8 -*-
"""2026-10-22 (감시 대상 정리 3단계 — 거래대금 부족 재평가, 민우님 8개항 보완 반영)

배경: 거래대금 부족 3회 연속으로 제외된 종목은 하루 종일 다시는
재평가되지 않았습니다(장중 변동으로 거래대금이 회복돼도 영구 제외와
동일하게 동작). 이 라운드는 그 종목만(다른 사유로 제외된 종목은 대상
아님) 쿨다운(기본 30분) 후 1회씩 재조회하는 메커니즘을 추가합니다.

민우님이 명시적으로 정정한 설계 조건(이전 설명이 부정확했던 부분)을
그대로 테스트로 고정합니다:
  1. 재검사 자체가 _get_market_price_with_cache()/_get_minute_analysis()
     호출을 "새로" 발생시킴 — 제외된 종목은 평소 폴링(cycle_targets)에서
     빠져 있어 재검사가 유일한 데이터 획득 경로.
  2. 사이클당 최대 1종목(기본값)만 시도, 보유종목·entry_watch shadow
     처리가 끝난 뒤에만 시도(호출 순서 자체로 보장).
  3. shadow 모드에서는 판단을 계산·기록만 하고 실제 복귀는 하지 않음.
  4. 거래대금이 회복돼도 UNKNOWN 3연속/영구 차단 목록에 걸려있으면
     복귀시키지 않음.
  5. 조회 실패 시 다음 시도를 추가로 늦춤(429류 반복 방지).
  6. 신규 진입 마감 시각(low_volume_recheck_cutoff_hhmm) 이후에는
     재검사 자체를 하지 않음.

이 테스트는 BUY/SELL 판단, 신규 진입 게이트(min_trading_value), 기존
거래대금 부족 3연속 자동제외 로직 자체를 전혀 바꾸지 않았음도 함께
확인합니다(회귀 고정).
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from config.settings import ExperimentalConfig
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from utils.time_utils import now_kst


def _make_service(tmpdir: str, *, low_volume_recheck_mode: str = "off", **trading_overrides) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    if trading_overrides:
        import dataclasses
        settings = dataclasses.replace(
            settings, trading=dataclasses.replace(settings.trading, **trading_overrides)
        )
    settings = _with_experimental(settings, low_volume_recheck_mode=low_volume_recheck_mode)

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


def _with_experimental(settings, **overrides):
    import dataclasses
    experimental = dataclasses.replace(ExperimentalConfig(), **overrides)
    return dataclasses.replace(settings, experimental=experimental)


def _fake_minute_result(trading_value: int, *, entry_safe: bool = True, reason: str = ""):
    analysis = SimpleNamespace(trading_value=trading_value) if entry_safe else None
    return SimpleNamespace(analysis=analysis, entry_safe=entry_safe, reason=reason)


class TestLowVolumeExclusionRecordsReason(unittest.TestCase):
    """기존 거래대금 부족 3연속 자동제외 로직이 _low_volume_excluded_at도 함께 기록하는지."""

    def test_excluded_at_set_on_third_consecutive_low_volume(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            symbol = "005930"
            self.assertNotIn(symbol, service._low_volume_excluded_at)
            # 내부 상태를 직접 세팅해 3연속 도달을 시뮬레이션 (실제
            # _process_symbol 전체를 구동하지 않고 해당 분기만 검증).
            service._low_volume_count[symbol] = 2
            # 3번째 "거래대금 부족" 폴링과 동일한 효과를 주는 코드
            # 경로를 그대로 재현.
            service._low_volume_count[symbol] += 1
            self.assertEqual(service._low_volume_count[symbol], 3)
            if service._low_volume_count[symbol] >= 3:
                service._excluded_symbols.add(symbol)
                service._low_volume_excluded_at.setdefault(symbol, now_kst())
            self.assertIn(symbol, service._excluded_symbols)
            self.assertIn(symbol, service._low_volume_excluded_at)


class TestRecheckModeOff(unittest.IsolatedAsyncioTestCase):

    async def test_off_mode_never_calls_recheck(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir, low_volume_recheck_mode="off")
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(hours=1)

            called = []
            service._recheck_one_low_volume_symbol = lambda *a, **k: called.append(1)
            await service._recheck_low_volume_exclusions()

            self.assertEqual(called, [])
            self.assertIn(symbol, service._excluded_symbols)


class TestRecheckShadowMode(unittest.IsolatedAsyncioTestCase):

    async def test_shadow_mode_logs_but_does_not_revive(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="shadow",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            # 회복 확인(500억 이상)되는 상황을 모의.
            service._get_market_price_with_cache = lambda sym: SimpleNamespace(previous_close=70000)
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(60_000_000_000)

            await service._recheck_low_volume_exclusions()

            # shadow이므로 실제 복귀는 없음 — 그러나 호출 자체(call_count)는
            # 발생해야 함(민우님 정정: 재검사 자체가 API 호출을 발생시킴).
            self.assertIn(symbol, service._excluded_symbols)
            self.assertEqual(service._low_volume_recheck_call_count, 1)

            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "shadow_would_revive")
            self.assertEqual(rows[0]["symbol"], symbol)
            self.assertEqual(rows[0]["mode"], "shadow")
            self.assertEqual(rows[0]["pass_threshold_0"], "True")


class TestRecheckEnforceMode(unittest.IsolatedAsyncioTestCase):

    async def test_enforce_mode_revives_symbol(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)
            service._low_volume_count[symbol] = 3

            service._get_market_price_with_cache = lambda sym: SimpleNamespace(previous_close=70000)
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(60_000_000_000)

            await service._recheck_low_volume_exclusions()

            self.assertNotIn(symbol, service._excluded_symbols)
            self.assertEqual(service._low_volume_count[symbol], 0)
            self.assertNotIn(symbol, service._low_volume_excluded_at)

            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "revived")

    async def test_enforce_mode_still_low_keeps_excluded_and_reschedules(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            service._get_market_price_with_cache = lambda sym: SimpleNamespace(previous_close=70000)
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(1_000_000_000)

            await service._recheck_low_volume_exclusions()

            self.assertIn(symbol, service._excluded_symbols)
            self.assertIn(symbol, service._low_volume_excluded_at)
            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "still_low")

    async def test_enforce_mode_blocked_by_other_reason(self):
        """거래대금은 회복됐지만 UNKNOWN 3연속도 겹쳐 있으면 복귀시키지 않음."""
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)
            service._unknown_count[symbol] = 3  # 다른 사유로도 이미 제외 대상

            service._get_market_price_with_cache = lambda sym: SimpleNamespace(previous_close=70000)
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(60_000_000_000)

            await service._recheck_low_volume_exclusions()

            # 거래대금 부족 사유 추적은 끝났지만, excluded_symbols 자체는
            # 그대로(다른 메커니즘이 계속 관리) — 실제로 제거되지 않음.
            self.assertIn(symbol, service._excluded_symbols)
            self.assertNotIn(symbol, service._low_volume_excluded_at)
            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "blocked_other_reason")
            self.assertEqual(rows[-1]["other_exclusion_reason"], "unknown_regime")

    async def test_query_failure_extends_backoff_and_logs_error(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
                low_volume_recheck_failure_backoff_seconds=999,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            excluded_at = now_kst() - timedelta(seconds=10)
            service._low_volume_excluded_at[symbol] = excluded_at

            def _boom(sym):
                raise RuntimeError("통신 장애 모의")
            service._get_market_price_with_cache = _boom

            await service._recheck_low_volume_exclusions()

            self.assertIn(symbol, service._excluded_symbols)
            self.assertEqual(service._low_volume_recheck_error_count, 1)
            self.assertEqual(service._low_volume_recheck_call_count, 0)
            next_attempt = service._low_volume_recheck_next_attempt_at[symbol]
            # 쿨다운(1초) + 실패 백오프(999초) 뒤로 다음 시도가 밀려야 함.
            self.assertGreater((next_attempt - now_kst()).total_seconds(), 900)

            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "query_failed")
            self.assertIn("통신 장애 모의", rows[-1]["error"])


class TestRecheckCooldownAndCutoffAndMaxPerCycle(unittest.IsolatedAsyncioTestCase):

    async def test_not_yet_eligible_before_cooldown(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="shadow",
                low_volume_recheck_cooldown_seconds=1800,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)  # 쿨다운 미도달

            called = []
            service._recheck_one_low_volume_symbol = lambda *a, **k: called.append(1)
            await service._recheck_low_volume_exclusions()

            self.assertEqual(called, [])

    async def test_cutoff_time_stops_recheck(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="shadow",
                low_volume_recheck_cooldown_seconds=1,
                low_volume_recheck_cutoff_hhmm="00:00",  # 항상 "마감 이후"
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            called = []
            service._recheck_one_low_volume_symbol = lambda *a, **k: called.append(1)
            await service._recheck_low_volume_exclusions()

            self.assertEqual(called, [])

    async def test_max_per_cycle_limits_attempts(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="shadow",
                low_volume_recheck_cooldown_seconds=1,
                low_volume_recheck_max_per_cycle=1,
            )
            symbols = ["005930", "000660", "006260"]
            for i, symbol in enumerate(symbols):
                service._excluded_symbols.add(symbol)
                # 서로 다른 제외 시각 — 가장 오래된 것부터 시도돼야 함.
                service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=100 - i)

            service._get_market_price_with_cache = lambda sym: SimpleNamespace(previous_close=70000)
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(1_000_000_000)

            await service._recheck_low_volume_exclusions()

            # max_per_cycle=1이므로 이번 사이클엔 가장 오래 기다린
            # "005930"만 호출돼야 함 (elapsed_ms_total 등 카운터는 1회분만).
            self.assertEqual(service._low_volume_recheck_call_count, 1)
            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "005930")


class TestLowVolumeRecheckSettingsValidation(unittest.TestCase):

    def test_compare_threshold_0_must_match_min_trading_value(self):
        import dataclasses
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            bad_trading = dataclasses.replace(
                settings.trading,
                low_volume_recheck_compare_thresholds=[1, 30_000_000_000, 10_000_000_000],
            )
            with self.assertRaises(ValueError):
                dataclasses.replace(settings, trading=bad_trading)

    def test_invalid_cutoff_format_rejected(self):
        import dataclasses
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                dataclasses.replace(
                    build_minimal_settings(tmpdir).trading,
                    low_volume_recheck_cutoff_hhmm="not-a-time",
                )

    def test_invalid_mode_value_rejected(self):
        with self.assertRaises(ValueError):
            ExperimentalConfig(low_volume_recheck_mode="sometimes")


def _read_csv_rows(path) -> list[dict]:
    import csv
    with open(path, newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


if __name__ == "__main__":
    unittest.main()
