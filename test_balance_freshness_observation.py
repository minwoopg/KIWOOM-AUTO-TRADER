# -*- coding: utf-8 -*-
"""2026-09-11 (S01 관측 1단계, GPT 5차 검토 반영)
→ 2026-09-14 (안전 복구, GPT 재검토 20260914 반영) — 잔고 조회 신선도
관측 로그.

배경: 5차 설계 문서에서, 429를 얼마나 자주/어떤 사유로 만나는지
실측 없이 캐시 유효기간(360/540초) 연장이나 폴백 재도입 여부를 정할
수 없다고 정리했습니다. 이 라운드는 그 실측을 위한 순수 관측 로그
(BalanceFreshnessLogger)를 추가합니다.

2026-09-14 갱신: P0-1의 429 캐시 대체(폴백) 동작 자체가, 미해결 주문이
있어 최신 잔고가 필요한 분기에서 스테일 수량을 confirm_buy_from_
broker()에 흘려보내는 위험이 재현되어 되돌려졌습니다(test_balance_429_
fallback.py 참고). 이 파일의 관측 로그 자체(기록 항목·필드)는 그대로
유지하되, 429 발생 시 실제로 어떤 값이 기록되는지를 되돌려진 동작
기준으로 갱신합니다.

이 테스트가 확인하는 것:
1. `_get_balance_with_cache()`의 각 분기(fetch_success/cache_reuse/
   fetch_failed_429_fallback_disabled/fetch_failed_no_fallback)에서 로그
   행이 정확한 필드로 기록됩니다.
2. 로거가 없거나(None) append 자체가 예외를 던져도 `_get_balance_with_
   cache()`의 반환값·예외 전파는 test_balance_429_fallback.py가 이미
   고정한 계약과 완전히 동일하게 유지됩니다(fail-open, 회귀 없음).
3. 로거 생성 자체가 실패해도 TradingService 생성이 막히지 않습니다.

매수/매도/보유 판단 기준, 잔고 조회 주기(balance_refresh_seconds)는 이
라운드에서 단 한 줄도 바뀌지 않았습니다 — 이 테스트는 그 사실을
회귀로 고정하는 목적도 겸합니다.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomHttpError
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import BalanceFreshnessLogger, TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore


def _make_service(tmpdir: str) -> TradingService:
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


def _make_429_error() -> KiwoomHttpError:
    return KiwoomHttpError(
        "kiwoom request failed: api_id=kt00001, http=429, "
        "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}",
        status_code=429,
        body={"return_msg": "허용된 요청 개수를 초과하였습니다"},
    )


def _read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


class TestBalanceFreshnessLoggerCreation(unittest.TestCase):

    def test_logger_created_by_default_when_not_injected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            self.assertIsNotNone(
                service.balance_freshness_logger,
                "생성자 기본 경로에서 BalanceFreshnessLogger가 자동 생성돼야 합니다.",
            )

    def test_logger_creation_failure_does_not_block_service_construction(self):
        """디스크 문제 등으로 로거 생성 자체가 실패해도 TradingService는
        정상적으로 만들어져야 합니다(fail-open, low_upside_shadow_logger와
        동일한 계약)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            # 파일을 디렉터리처럼 부모 경로에 두어 mkdir(parents=True)가
            # 실패하도록 유도 (그 경로의 부모가 실제로는 일반 파일).
            blocker_file = f"{tmpdir}/blocker"
            with open(blocker_file, "w", encoding="utf-8") as fp:
                fp.write("not a directory")
            broken_path = f"{blocker_file}/balance_freshness.csv"

            import dataclasses
            broken_storage = dataclasses.replace(
                settings.storage, balance_freshness_log_file=broken_path
            )
            settings = dataclasses.replace(settings, storage=broken_storage)

            broker = MockBroker()
            app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
            trade_logger = TradeCsvLogger(settings.storage.trade_log_file)
            signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
            state_store = JsonStateStore(settings.storage.state_file)
            strategy_router = StrategyRouter(settings.strategy)
            regime_classifier = MarketRegimeClassifier(settings.market_regime)
            risk_manager = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)

            try:
                service = TradingService(
                    settings=settings, broker=broker, strategy_router=strategy_router,
                    regime_classifier=regime_classifier, risk_manager=risk_manager,
                    app_logger=app_logger, trade_logger=trade_logger,
                    signal_logger=signal_logger, state_store=state_store,
                )
            except Exception as exc:  # pragma: no cover - 실패하면 테스트가 바로 알려줌
                self.fail(
                    f"BalanceFreshnessLogger 생성 실패가 TradingService 생성 전체를 "
                    f"막으면 안 됩니다(fail-open 계약 위반): {type(exc).__name__}: {exc}"
                )
            self.assertIsNone(
                service.balance_freshness_logger,
                "생성 실패 시 None으로 남아 관측만 건너뛰어야 합니다.",
            )


class TestBalanceFreshnessLogging(unittest.TestCase):
    """_get_balance_with_cache()의 각 분기에서 로그 행이 정확히 기록되는지,
    그리고 그 반환값/예외 전파가 test_balance_429_fallback.py가 이미
    고정한 기존 계약과 완전히 동일하게 유지되는지 확인합니다."""

    def test_fresh_fetch_when_no_cache_logs_fetch_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.broker.get_account_balance = lambda: balance

            result = service._get_balance_with_cache()

            self.assertIs(result, balance)
            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "fetch_success")
            self.assertEqual(rows[0]["trigger_reason"], "no_cache_yet")
            self.assertEqual(rows[0]["prior_loaded_at"], "")
            self.assertEqual(rows[0]["cache_age_seconds"], "")
            self.assertEqual(rows[0]["error_type"], "")

    def test_429_with_cache_logs_fallback_disabled_and_raises(self):
        """2026-09-14 안전 복구: 캐시가 있어도 429는 더 이상 캐시로
        대체되지 않고 예외가 그대로 전파됩니다. 관측 로그에는 그
        시점에 캐시가 있었다는 사실(cache_age_seconds)과 함께
        outcome=fetch_failed_429_fallback_disabled로 기록됩니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            loaded_at = datetime.now() - timedelta(seconds=999)
            service.cached_balance_loaded_at = loaded_at
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "fetch_failed_429_fallback_disabled")
            self.assertEqual(rows[0]["trigger_reason"], "routine_refresh_due")
            self.assertEqual(rows[0]["prior_loaded_at"], loaded_at.isoformat())
            self.assertNotEqual(rows[0]["cache_age_seconds"], "")
            self.assertIn("KiwoomHttpError", rows[0]["error_type"])

    def test_429_without_cache_still_raises_and_logs_fetch_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            self.assertIsNone(service.cached_balance)
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "fetch_failed_429_fallback_disabled")
            self.assertEqual(rows[0]["trigger_reason"], "no_cache_yet")

    def test_non_rate_limit_error_still_propagates_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)
            other_error = RuntimeError("kiwoom business error")
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(other_error)

            with self.assertRaises(RuntimeError):
                service._get_balance_with_cache()

            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(rows[0]["outcome"], "fetch_failed_no_fallback")
            self.assertEqual(rows[0]["error_type"], "RuntimeError")

    def test_cache_reuse_within_refresh_window_logs_cache_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            loaded_at = datetime.now() - timedelta(seconds=1)
            service.cached_balance_loaded_at = loaded_at
            # 아직 balance_refresh_seconds(180초)에 못 미쳤으므로 조회 시도
            # 자체가 없어야 함 — get_account_balance가 호출되면 즉시 실패.
            def _should_not_be_called():
                raise AssertionError("갱신 주기 전인데 조회를 시도했습니다(회귀).")
            service.broker.get_account_balance = _should_not_be_called

            result = service._get_balance_with_cache()

            self.assertIs(result, good_balance)
            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "cache_reuse")
            self.assertEqual(rows[0]["trigger_reason"], "cache_still_fresh")

    def test_unresolved_orders_forces_refresh_and_logs_that_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=1)
            new_balance = AccountBalance(cash=2_000_000, total_asset=2_000_000, positions=[])
            service.broker.get_account_balance = lambda: new_balance
            service._has_unresolved_orders = lambda: True

            result = service._get_balance_with_cache()

            self.assertIs(result, new_balance, "미해결 주문이 있으면 캐시 나이와 무관하게 강제 재조회해야 합니다(기존 계약).")
            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(rows[0]["outcome"], "fetch_success")
            self.assertEqual(rows[0]["trigger_reason"], "unresolved_orders")
            self.assertEqual(rows[0]["has_unresolved_orders"], "True")

    def test_unresolved_orders_429_still_raises_no_fallback(self):
        """2026-09-14 안전 복구 — 가장 위험했던 조합: 미해결 주문이
        있어 최신 잔고가 반드시 필요한 상황에서 429가 나면, 캐시가
        있어도 대체하지 않고 예외를 그대로 올려야 합니다(스테일
        수량이 confirm_buy_from_broker()에 흘러들지 않도록). 관측
        로그에는 trigger_reason=unresolved_orders +
        outcome=fetch_failed_429_fallback_disabled가 함께 남습니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            service.cached_balance_loaded_at = datetime.now()
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())
            service._has_unresolved_orders = lambda: True

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

            rows = _read_rows(service.settings.storage.balance_freshness_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "fetch_failed_429_fallback_disabled")
            self.assertEqual(rows[0]["trigger_reason"], "unresolved_orders")
            self.assertEqual(rows[0]["has_unresolved_orders"], "True")

    def test_logger_append_failure_does_not_break_balance_fetch(self):
        """관측 로그 기록이 실패해도(디스크 full 등) 잔고 조회 자체는
        영향받지 않아야 합니다(fail-open)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)

            class _ExplodingLogger:
                def append(self, row):
                    raise OSError("simulated disk full")

            service.balance_freshness_logger = _ExplodingLogger()
            balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.broker.get_account_balance = lambda: balance

            result = service._get_balance_with_cache()
            self.assertIs(result, balance, "관측 로그 실패가 잔고 조회 반환값에 영향을 주면 안 됩니다.")

    def test_none_logger_is_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            service.balance_freshness_logger = None
            balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.broker.get_account_balance = lambda: balance

            result = service._get_balance_with_cache()
            self.assertIs(result, balance)


if __name__ == "__main__":
    unittest.main()
