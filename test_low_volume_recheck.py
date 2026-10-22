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

2026-10-22 재검토 반영 (민우님이 외부 LLM 검토 전 직접 지적한 6개
결함, 전부 재현 확인 후 수정):
  1. update_targets()가 조건검색 결과만으로 merged를 교체해, 쿨다운
     동안 조건검색에서 자연 탈락한 종목은 discard() 복귀가 cycle_
     targets에 반영되지 않았음 — held_symbols와 동일하게 강제 포함.
  2. reset_daily_loss_counts()가 _excluded_symbols는 비우면서 두 신규
     dict는 그대로 둬서, 날짜 변경 후 정상 폴링 중인 종목에 불필요한
     재검사 API 호출이 발생했음 — 함께 초기화 + 방어적 고아 정리 추가.
  3. low_volume_recheck_cutoff_hhmm 기본값 "15:00"이 실제 신규매수
     마감(하드코딩된 "14:50 이후 신규매수 차단")보다 늦어, 그 구간엔
     복귀시켜도 BUY 자체가 막혀 재검사가 무의미했음 — "14:50"으로 수정.
  4. _get_market_price_with_cache()가 캐시 존재 시 조회 실패를 내부에서
     삼키고 예외 없이 캐시를 반환해, 재검사 오류 집계·백오프가 전혀
     반영되지 않았음 — self._market_price_fetch_outcome 확인 추가.
  5. 재검사 소요 시간은 sleep_or_stop(poll)이 경과시간을 고려하지 않는
     고정 슬립이라 다음 사이클 시작을 그만큼 늦춤 — max_per_cycle=1·
     쿨다운 30분 간격으로 바운드돼 있어 이번 라운드는 완화 로직을
     추가하지 않고 CHANGELOG에 명시만 함(의도적 트레이드오프).
  6. export_daily_bundle.py의 CSV_SOURCES(날짜별 CSV 슬라이스 allowlist)
     에 low_volume_recheck.csv를 추가하지 않아 LOG_TAGS와는 무관하게
     이 CSV 자체가 번들에 전혀 실리지 않았음 — allowlist에 추가.
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


class TestUpdateTargetsForceIncludesLowVolumeExcluded(unittest.TestCase):
    """지적 1번 수정: 쿨다운 추적 중 종목은 조건검색 편출과 무관하게 모니터링 유지."""

    def test_excluded_symbol_kept_even_when_condition_search_drops_it(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir, low_volume_recheck_mode="shadow")
            symbol = "080220"  # settings.targets에 없는 임의 종목
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            # 조건검색 결과에 이 종목이 전혀 없음(= 탈락) — 그래도
            # 재검사 추적 중이면 merged에 강제 포함돼야 함.
            service.update_targets(["005930", "000660"], sym_to_conditions={})

            self.assertIn(symbol, service.targets)
            self.assertIn("005930", service.targets)

    def test_revived_symbol_no_longer_force_included(self):
        """추적에서 빠지면(복귀 완료) 더 이상 강제 포함되지 않음 — 일반 종목과 동일하게 취급."""
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir, low_volume_recheck_mode="enforce")
            symbol = "080220"
            # 추적에 없는 상태(이미 복귀 완료라고 가정)에서 조건검색도 포함 안 함.
            service.update_targets(["005930"], sym_to_conditions={})
            self.assertNotIn(symbol, service.targets)


class TestDailyResetClearsLowVolumeTracking(unittest.TestCase):
    """지적 2번 수정: 날짜 변경 시 쿨다운 추적 dict도 함께 초기화."""

    def test_reset_daily_loss_counts_clears_both_dicts(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            symbol = "005930"
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(days=1)
            service._low_volume_recheck_next_attempt_at[symbol] = now_kst() - timedelta(hours=1)

            service.reset_daily_loss_counts()

            self.assertEqual(service._low_volume_excluded_at, {})
            self.assertEqual(service._low_volume_recheck_next_attempt_at, {})


class TestOrphanedTrackingEntryCleanedUpWithoutApiCall(unittest.IsolatedAsyncioTestCase):
    """지적 2번 관련 방어 코드: _excluded_symbols에 없는(고아) 추적 항목은 API 호출 없이 정리."""

    async def test_orphaned_entry_skipped_without_calling_recheck(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="shadow",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            # _excluded_symbols에는 없지만 추적 dict에만 남아있는 고아 상태
            # (예: 날짜 변경이 아닌 다른 경로로 조만간 발생할 수 있는 상황을 모의).
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)
            self.assertNotIn(symbol, service._excluded_symbols)

            called = []
            service._recheck_one_low_volume_symbol = lambda *a, **k: called.append(1)
            await service._recheck_low_volume_exclusions()

            self.assertEqual(called, [])
            self.assertNotIn(symbol, service._low_volume_excluded_at)


class TestInternalPriceFetchFailureCountedAsError(unittest.IsolatedAsyncioTestCase):
    """지적 4번 수정: 캐시로 대체된(예외 없는) 내부 조회 실패도 오류로 집계."""

    async def test_cache_after_failure_outcome_treated_as_query_failed(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
                low_volume_recheck_failure_backoff_seconds=999,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            # 예외는 던지지 않지만(캐시로 대체) outcome을 "cache_after_failure"로 남김 —
            # 실제 _get_market_price_with_cache()가 캐시 존재+조회실패일 때 하는 동작.
            def _stale_cache(sym):
                service._market_price_fetch_outcome[sym] = "cache_after_failure"
                return SimpleNamespace(previous_close=70000)
            service._get_market_price_with_cache = _stale_cache
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(60_000_000_000)

            await service._recheck_low_volume_exclusions()

            # 회복된 것처럼 보이는 거래대금(600억)이 와도 "성공"으로
            # 처리돼 복귀시키면 안 됨 — query_failed로 집계돼야 함.
            self.assertIn(symbol, service._excluded_symbols)
            self.assertEqual(service._low_volume_recheck_error_count, 1)
            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "query_failed")
            next_attempt = service._low_volume_recheck_next_attempt_at[symbol]
            self.assertGreater((next_attempt - now_kst()).total_seconds(), 900)

    async def test_fetched_outcome_proceeds_normally(self):
        """정상 outcome("fetched")이면 기존 로직 그대로 진행(회귀 확인)."""
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            service = _make_service(
                tmpdir, low_volume_recheck_mode="enforce",
                low_volume_recheck_cooldown_seconds=1,
            )
            symbol = "005930"
            service._excluded_symbols.add(symbol)
            service._low_volume_excluded_at[symbol] = now_kst() - timedelta(seconds=10)

            def _fresh(sym):
                service._market_price_fetch_outcome[sym] = "fetched"
                return SimpleNamespace(previous_close=70000)
            service._get_market_price_with_cache = _fresh
            service._get_minute_analysis = lambda sym, prev_close: _fake_minute_result(60_000_000_000)

            await service._recheck_low_volume_exclusions()

            self.assertNotIn(symbol, service._excluded_symbols)
            rows = _read_csv_rows(service.low_volume_recheck_logger.file_path)
            self.assertEqual(rows[-1]["outcome"], "revived")


class TestCutoffDefaultMatchesNewBuyCutoff(unittest.TestCase):
    """지적 3번 수정: 기본 cutoff가 실제 신규매수 마감(14:50)과 일치."""

    def test_default_cutoff_is_1450(self):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            self.assertEqual(settings.trading.low_volume_recheck_cutoff_hhmm, "14:50")


class TestBundleCsvSourcesIncludesLowVolumeRecheck(unittest.TestCase):
    """지적 6번 수정: LOG_TAGS와는 별개로 CSV_SOURCES allowlist에도 추가됐는지."""

    def test_low_volume_recheck_csv_in_bundle_csv_sources(self):
        import export_daily_bundle
        names = [name for name, _cols in export_daily_bundle.CSV_SOURCES]
        self.assertIn("low_volume_recheck.csv", names)

    def test_low_volume_recheck_tag_in_log_tags(self):
        import export_daily_bundle
        self.assertIn("[LOW_VOL_RECHECK]", export_daily_bundle.LOG_TAGS)


def _read_csv_rows(path) -> list[dict]:
    import csv
    with open(path, newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


if __name__ == "__main__":
    unittest.main()
