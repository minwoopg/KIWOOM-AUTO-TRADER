# -*- coding: utf-8 -*-
"""2026-09-11 (P0-1, GPT 종합검토 20260911 반영): 잔고 조회 429 완화 테스트.

배경: 이전엔 TradingService._get_balance_with_cache()가
broker.get_account_balance()를 아무 보호 없이 호출해서, HTTP 429가
나면 그 예외가 run_once() 밖까지 전파됐습니다. run_once()는 이 호출
직후에 보유 종목 손절/트레일링 감시를 하므로, 이 예외가
app/main.py의 trading_loop()까지 올라가면 거기서 180초 전체 루프
백오프가 걸려 그 동안 이미 보유 중인 포지션의 리스크 감시까지
전부 멈췄습니다(재현 확인, GPT 종합검토 P0-1).

이 테스트는 다음을 검증합니다.
1. 캐시된 잔고가 있는 상태에서 429가 나면 예외를 삼키고 캐시로 대체.
2. 캐시가 전혀 없는 상태(예: 프로세스 시작 직후)에서 429가 나면
   대체할 안전한 값이 없으므로 기존과 동일하게 예외를 그대로 올림.
3. 429가 아닌 다른 예외(전송 실패, 인증 오류 등)는 캐시가 있어도
   조용히 삼키지 않고 그대로 올림 — "429만 좁게" 처리한다는 계약을
   깨지 않는지 확인.
4. run_once() 레벨 통합 테스트: 캐시가 있는 상태에서 두 번째
   run_once() 호출이 잔고 429를 만나도 run_once() 자체는 예외 없이
   끝까지 돌고(=trading_loop()의 180초 백오프를 유발하지 않음),
   그 사이클의 보유 종목 처리(_process_symbol)도 정상적으로
   호출됨을 확인합니다 — "리스크 감시가 멈추지 않는다"는 실제
   효과를 회귀로 고정.

매수/매도/보유 판단 기준(전략 파라미터, RSI/MACD/진입점수 등)은
전혀 건드리지 않았습니다 — 잔고 조회 실패 시 어떤 잔고 값을 쓸지에
대한 순수 인프라 복원력 처리입니다.
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


class TestBalanceRateLimitFallback(unittest.TestCase):

    def test_429_with_existing_cache_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)

            good_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            service.cached_balance = good_balance
            # balance_refresh_seconds(180초)를 넘겨서 "캐시가 있지만
            # 갱신 시점이 됨" 분기를 타게 만듦 — 이 분기에서 429가
            # 나는 경로를 재현하려는 것.
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)

            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            result = service._get_balance_with_cache()

            self.assertIs(
                result, good_balance,
                "429가 나면 직전 캐시 잔고를 그대로 반환해야 합니다(예외를 올리면 안 됨).",
            )

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

    def test_run_once_survives_balance_429_and_still_monitors_holdings(self):
        """run_once() 레벨: 429가 나도 예외 없이 끝까지 돌고, 보유종목 처리도 계속됨."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir)
            service.broker._positions["000660"] = Position(
                symbol="000660", quantity=10, average_price=180000,
            )

            # 1회차: 정상 호출로 캐시를 채움 (첫 호출은 캐시가 없어
            # 무조건 fresh fetch가 필요한 분기라 429 fallback을
            # 검증하는 게 아니라 그냥 정상 시나리오로 캐시를 확보).
            asyncio.run(service.run_once())
            self.assertIsNotNone(service.cached_balance)

            # 2회차 준비: 캐시 유효기간을 지나게 만들고, 다음
            # get_account_balance() 호출은 429를 던지도록 교체.
            service.cached_balance_loaded_at = datetime.now() - timedelta(seconds=999)
            service.broker.get_account_balance = lambda: (_ for _ in ()).throw(_make_429_error())

            processed: list[str] = []
            original_process_symbol = service._process_symbol

            async def tracking_process_symbol(symbol, balance):
                processed.append(symbol)
                return await original_process_symbol(symbol, balance)

            service._process_symbol = tracking_process_symbol

            # 429가 여기서 예외로 올라오면 이 테스트가 바로 실패함 —
            # 곧 app/main.py의 trading_loop()까지 전파돼 180초 전체
            # 백오프를 유발했을 상황과 동일.
            asyncio.run(service.run_once())

            self.assertIn(
                "000660", processed,
                "잔고 조회가 429여도 보유 종목(000660) 손절/트레일링 감시는 계속 수행돼야 합니다.",
            )


if __name__ == "__main__":
    unittest.main()
