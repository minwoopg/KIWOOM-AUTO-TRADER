# -*- coding: utf-8 -*-
"""2026-09-11 (P0-1) → 2026-09-14 (안전 복구, GPT 재검토 20260914 반영):
잔고 조회 429 처리 테스트.

배경: P0-1은 broker.get_account_balance()가 HTTP 429로 실패하면 예외를
삼키고 직전 캐시 잔고로 이번 사이클을 계속 진행하게 했습니다(트레일링/
손절 감시가 180초 동안 멈추는 것을 막기 위함). 그런데 재검토(GPT
20260914)에서, 미해결 주문(BUY_PENDING 등)이 있어 "반드시 최신 잔고가
필요한" 바로 그 분기에서 429가 나면, 이 캐시 대체값이 run_once() →
_sync_position_state_machine_shadow()로 그대로 전달되고
confirm_buy_from_broker()에 임의로 오래된 캐시 기준 수량(예: 0주)이
"이번 체결 확인 결과"처럼 들어가는 경로가 실제로 재현됐습니다.

이 테스트는 그 위험을 제거하기 위해 캐시 대체를 되돌린(2026-08-10
이전과 동일한 동작으로 복귀) 다음 내용을 검증합니다.

1. 캐시된 잔고가 있어도 429가 나면 캐시로 대체하지 않고 예외를 그대로
   올린다 — 미해결 주문 유무와 무관하게.
2. 캐시가 전혀 없는 상태(예: 프로세스 시작 직후)에서 429가 나도
   (대체할 값이 원래 없었으므로) 마찬가지로 예외를 그대로 올린다.
3. 429가 아닌 다른 예외(전송 실패, 인증 오류 등)도 캐시가 있어도
   조용히 삼키지 않고 그대로 올린다 — 기존과 동일.
4. run_once() 레벨: 429가 나면 run_once() 자체가 예외를 그대로
   전파한다 — 즉 trading_loop()의 180초 전체 백오프가 다시 걸릴 수
   있는 상태로 돌아갔음을 회귀로 고정한다(이 되돌림이 180초 공백
   문제를 해결하는 것이 아니라는 사실 자체를 테스트로 남김).

매수/매도/보유 판단 기준(전략 파라미터, RSI/MACD/진입점수 등)은
전혀 건드리지 않았습니다 — 잔고 조회 실패 시 어떤 잔고 값을 쓸지에
대한 순수 인프라 처리입니다.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, Position
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomHttpError
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
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
    """kiwoom_broker.py의 실제 429 예외와 동일한 메시지 형태를 재현."""
    return KiwoomHttpError(
        "kiwoom request failed: api_id=kt00001, http=429, "
        "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}",
        status_code=429,
        body={"return_msg": "허용된 요청 개수를 초과하였습니다"},
    )


class TestBalanceRateLimitFallbackDisabled(unittest.TestCase):

    def test_429_with_existing_cache_still_raises(self):
        """2026-09-14 안전 복구: 캐시가 있어도 429는 캐시로 대체하지
        않고 예외를 그대로 올려야 합니다(P0-1의 캐시 대체 동작을
        되돌림 — confirm_buy_from_broker() 등에 스테일 수량이 흘러드는
        경로를 재현한 뒤 제거한 조치)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)

            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            # balance_refresh_seconds(180초)를 넘겨서 "캐시가 있지만
            # 갱신 시점이 됨" 분기를 타게 만듦.
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)

            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

    def test_429_with_unresolved_orders_still_raises(self):
        """미해결 주문이 있어 반드시 최신 잔고가 필요한 분기 — 이
        경우에도 캐시로 대체하지 않고 예외를 그대로 올려야 합니다.
        (재검토에서 재현된 문제: 여기서 캐시로 대체하면 스테일 수량이
        confirm_buy_from_broker()에 "체결 확인 결과"처럼 들어갈 수
        있었습니다.)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)

            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            service.cached_balance_loaded_at = datetime.now()  # 방금 갱신 — routine refresh 아님
            service.state.unresolved_order_intents = ["dummy-order-id"]
            self.assertTrue(service._has_unresolved_orders())

            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

    def test_429_without_any_cache_still_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            self.assertIsNone(service.cached_balance, "이 테스트는 캐시가 없는 최초 호출 상황을 가정합니다.")

            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                service._get_balance_with_cache()

    def test_non_rate_limit_error_still_propagates_even_with_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)

            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)

            other_error = RuntimeError("kiwoom business error: api_id=kt00001, body={'return_code': -1}")
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(other_error)

            with self.assertRaises(RuntimeError):
                service._get_balance_with_cache()

    def test_run_once_propagates_balance_429(self):
        """run_once() 레벨: 429가 나면 예외가 그대로 전파돼야 합니다
        — 이는 trading_loop()의 180초 전체 백오프가 다시 걸릴 수 있는
        상태로 돌아갔음을 뜻합니다(이번 되돌림이 해결하는 문제가
        아니라는 점을 회귀로 고정)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            service.broker._positions["000660"] = Position(
                symbol="000660", quantity=10, average_price=180000,
            )

            # 1회차: 정상 호출로 캐시를 채움.
            asyncio.run(service.run_once())
            self.assertIsNotNone(service.cached_balance)

            # 2회차 준비: 캐시 유효기간을 지나게 만들고, 다음
            # get_account_balance() 호출은 429를 던지도록 교체.
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            with self.assertRaises(KiwoomHttpError):
                asyncio.run(service.run_once())


if __name__ == "__main__":
    unittest.main()
