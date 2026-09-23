# -*- coding: utf-8 -*-
"""2026-09-22 (조건검색 콜백의 잔고 백오프 우회 수정, GPT 재검토 반영).

배경: 9/21·9/22 번들 분석 요청("21일 22일 데이터입니다. 분석하고
개선점 잡아봅시다")에 대한 재검토에서, `balance_freshness.csv`의
`fetch_failed_429_fallback_disabled` 건수(9/21 19건, 9/22 40건)와
`trigger_reason=unresolved_orders`로 짧은 간격에 반복되는 429가 다수
확인됐습니다. 코드를 대조한 결과, 실시간 조건검색 콜백
(`app/main.py`의 `on_symbols_changed()`)이 호출하는
`TradingService.update_targets()`가 `app/main.py`의 `trading_loop()`가
`is_in_balance_outage()`로 지키는 독립 재시도 경로
(`handle_balance_outage_tick()`)와 완전히 별개로, 미해결 주문이 있을
때마다 조건 없이 `_get_balance_with_cache()`를 호출하고 있었습니다.
`_get_balance_with_cache()`는 미해결 주문이 있으면 캐시와 무관하게
매번 실제 API를 재조회하므로, 잔고 장애로 재시도 백오프가 걸려 있는
동안에도 조건검색 이벤트가 들어올 때마다 잔고 API를 추가로 두드려
백오프를 사실상 무력화시킬 수 있는 경로였습니다.

이 테스트는 다음을 검증합니다.

1. 장애 상태(`is_in_balance_outage()==True`)에서는 미해결 주문이
   있어도 `update_targets()`가 `broker.get_account_balance()`를 전혀
   호출하지 않는다 — 반복 호출해도 마찬가지(백오프 우회 재현 시나리오
   자체가 더는 재현되지 않음을 회귀로 고정).
2. 장애 상태에서도 마지막으로 성공한 잔고(`cached_balance`)가 있으면
   그 보유 종목은 여전히 감시 목록에 유지된다 — "보유 종목은 조건검색
   편출과 무관하게 항상 포함" 원칙 자체는 깨지지 않음.
3. 장애 상태이면서 `cached_balance`가 아예 없으면(예: 프로세스 시작
   직후 첫 조회부터 429) 예외 없이 조용히 held_symbols=빈 목록으로
   처리된다.
4. 장애 상태가 아닐 때(`is_in_balance_outage()==False`)의 동작은 이전과
   동일하게 유지된다 — 정상 조회 성공 시 보유 종목 병합, 조회 실패
   시(outage 진입 전의 최초 예외 등) 예외를 삼키고 빈 목록 처리.

매수/매도/보유 판단 기준(전략 파라미터)은 전혀 건드리지 않았습니다 —
조건검색 콜백이 잔고를 "언제" 조회할지에 대한 순수 인프라 처리입니다.
"""
from __future__ import annotations

import sys
import tempfile
import unittest

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, Position
from domain.position.lifecycle import PositionLifecycle as L
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore


class _CountingBroker(MockBroker):
    """get_account_balance() 호출 횟수를 세는 테스트 전용 더블."""

    def __init__(self) -> None:
        super().__init__()
        self.balance_call_count = 0

    def get_account_balance(self) -> AccountBalance:
        self.balance_call_count += 1
        return super().get_account_balance()


def _make_service(tmpdir: str, broker) -> TradingService:
    settings = build_minimal_settings(tmpdir)
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


def _mark_unresolved_buy_pending(service: TradingService, symbol: str) -> None:
    """`_has_unresolved_orders()`가 True가 되도록 포지션 상태를 조작."""
    state = service._position_state_machine.get(symbol)
    state.lifecycle = L.BUY_PENDING
    state.pending_order_id = "555555"


class TestUpdateTargetsBalanceOutageGuard(unittest.TestCase):

    def test_outage_active_with_unresolved_orders_never_calls_broker(self):
        """장애 상태 + 미해결 주문이 있어도 update_targets()가
        broker.get_account_balance()를 호출하지 않아야 한다(백오프
        우회 재현 시나리오의 핵심 회귀)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = _CountingBroker()
            service = _make_service(tmpdir, broker)
            _mark_unresolved_buy_pending(service, "005930")
            service.enter_balance_outage()

            for _ in range(3):
                service.update_targets(["010170"])

            self.assertEqual(
                broker.balance_call_count, 0,
                "장애 상태에서는 update_targets()가 잔고 API를 전혀 호출하지 않아야 합니다",
            )

    def test_outage_active_keeps_last_known_holding_symbols(self):
        """장애 상태여도 마지막으로 성공한 잔고의 보유 종목은 감시
        목록에서 계속 유지돼야 한다(조건검색 편출과 무관하게 보유
        종목은 항상 포함한다는 기존 원칙 유지)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = _CountingBroker()
            service = _make_service(tmpdir, broker)
            service.cached_balance = AccountBalance(
                cash=1_000_000, total_asset=1_000_000,
                positions=[Position(symbol="005930", quantity=10, average_price=70000)],
            )
            _mark_unresolved_buy_pending(service, "005930")
            service.enter_balance_outage()

            service.update_targets(["010170"])

            self.assertEqual(broker.balance_call_count, 0)
            self.assertIn("005930", service._dynamic_targets)
            self.assertIn("010170", service._dynamic_targets)

    def test_outage_active_without_cached_balance_yields_empty_holding(self):
        """장애 상태 + cached_balance가 아예 없으면(프로세스 시작
        직후 첫 조회부터 429) 예외 없이 held_symbols=빈 목록으로
        처리돼야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = _CountingBroker()
            service = _make_service(tmpdir, broker)
            self.assertIsNone(service.cached_balance)
            _mark_unresolved_buy_pending(service, "005930")
            service.enter_balance_outage()

            service.update_targets(["010170"])

            self.assertEqual(broker.balance_call_count, 0)
            self.assertEqual(service._dynamic_targets, ["010170"])

    def test_no_outage_behavior_unchanged_success(self):
        """장애 상태가 아닐 때는 이전과 동일하게 정상 조회 후 보유
        종목을 병합해야 한다(회귀 없음)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = _CountingBroker()
            broker._positions["005930"] = Position(
                symbol="005930", quantity=5, average_price=70000,
            )
            service = _make_service(tmpdir, broker)
            self.assertFalse(service.is_in_balance_outage())

            service.update_targets(["010170"])

            self.assertEqual(broker.balance_call_count, 1)
            self.assertIn("005930", service._dynamic_targets)
            self.assertIn("010170", service._dynamic_targets)

    def test_no_outage_broker_exception_still_swallowed(self):
        """장애 상태가 아닌데 잔고 조회가 예외를 던지면(예: outage
        진입 직전의 최초 429) 이전과 동일하게 조용히 삼키고
        held_symbols=빈 목록으로 처리해야 한다(회귀 없음)."""

        class _RaisingBroker(MockBroker):
            def get_account_balance(self):
                raise RuntimeError("simulated failure")

        with tempfile.TemporaryDirectory() as tmpdir:
            service = _make_service(tmpdir, _RaisingBroker())
            self.assertFalse(service.is_in_balance_outage())

            service.update_targets(["010170"])

            self.assertEqual(service._dynamic_targets, ["010170"])


if __name__ == "__main__":
    unittest.main()
