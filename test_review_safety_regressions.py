"""2026-09-07 audit: deterministic failure scenarios, no network/orders.

Run directly (official regression runner) or with pytest.
The broker accepts orders without filling them unless a test changes its balance.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, MarketPrice, MarketRegime, OrderResult, OrderSide, Position
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker, KiwoomApiResponse
from infra.storage.logger import SignalCsvLogger, TradeCsvLogger
from infra.storage.state_store import JsonStateStore
from utils.time_utils import KST_TZ


class PendingBroker:
    """Account changes are explicit; accepted never implies filled."""
    def __init__(self):
        self.balance = AccountBalance(20_000_000, 20_000_000, [])
        self.orders = []
        self.price = 98_000

    def get_account_balance(self):
        return self.balance

    def get_market_price(self, symbol):
        return MarketPrice(symbol, self.price, 100_000, 100_000, datetime.now())

    def get_daily_prices(self, symbol, days):
        return []

    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(str(len(self.orders)), order.symbol, order.side,
                           order.quantity, True, "accepted, not filled", datetime.now())


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_minimal_settings(self.tmp.name)
        object.__setattr__(self.settings, "targets", [])
        object.__setattr__(self.settings.trading, "held_symbol_poll_gap_seconds", 0)
        object.__setattr__(self.settings.trading, "entry_poll_gap_seconds", 0)
        self.clock = patch("domain.service.trading_service.now_kst",
                           return_value=datetime(2026, 9, 7, 10, tzinfo=KST_TZ))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.broker = PendingBroker()

    def service(self):
        with patch("domain.service.trading_service.build_notifier", return_value=Mock()):
            svc = TradingService(
                self.settings, self.broker, StrategyRouter(self.settings.strategy),
                MarketRegimeClassifier(self.settings.market_regime),
                RiskManager(self.settings.trading, self.settings.risk, self.settings.storage.trade_log_file),
                Mock(), TradeCsvLogger(self.settings.storage.trade_log_file),
                SignalCsvLogger(self.settings.storage.signal_log_file),
                JsonStateStore(self.settings.storage.state_file),
            )
        svc._get_regime_with_cache = Mock(return_value=(MarketRegime.UNKNOWN, None))
        return svc

    def run_cycle(self, svc):
        with patch.object(svc, "_check_force_exit_overnight", new=unittest.mock.AsyncMock()):
            asyncio.run(svc.run_once())

    def test_holding_outside_targets_still_receives_stop_order(self):
        self.broker.balance = AccountBalance(10_000_000, 11_000_000, [Position("005930", 10, 100_000)])
        svc = self.service()
        self.run_cycle(svc)
        self.assertEqual([(o.side, o.symbol, o.quantity) for o in self.broker.orders],
                         [(OrderSide.SELL, "005930", 10)])

    def test_excluded_holding_still_receives_stop_order(self):
        self.broker.balance = AccountBalance(10_000_000, 11_000_000, [Position("005930", 10, 100_000)])
        svc = self.service()
        svc._last_reset_date = date(2026, 9, 7)
        svc._dynamic_targets = ["005930"]
        svc._excluded_symbols.add("005930")
        self.run_cycle(svc)
        self.assertEqual([(o.side, o.quantity) for o in self.broker.orders], [(OrderSide.SELL, 10)])

    def test_third_unknown_regime_does_not_skip_holding_stop(self):
        self.broker.balance = AccountBalance(10_000_000, 11_000_000, [Position("005930", 10, 100_000)])
        svc = self.service()
        svc._dynamic_targets = ["005930"]
        svc._unknown_count["005930"] = 2
        self.run_cycle(svc)
        self.assertEqual([(o.side, o.quantity) for o in self.broker.orders], [(OrderSide.SELL, 10)])

    def test_same_day_restart_preserves_risk_limits(self):
        svc = self.service()
        svc.state._last_run_date = "2026-09-07"
        svc.state.bought_symbols_today = {"005930"}
        svc.state.symbol_entry_count_today = {"005930": 1}
        svc.state.symbol_loss_count_today = {"005930": 2}
        svc.state.consecutive_losses = 3
        svc.state_store.save(svc.state, {})
        restarted = self.service()
        self.run_cycle(restarted)
        self.assertEqual(restarted.state.consecutive_losses, 3)
        self.assertEqual(restarted.state.symbol_entry_count_today, {"005930": 1})
        self.assertEqual(restarted.state.symbol_loss_count_today, {"005930": 2})
        self.assertEqual(restarted.state.bought_symbols_today, {"005930"})
        restarted._try_buy("000660", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 0)

    def test_new_day_resets_limits_once_and_persists_date(self):
        svc = self.service()
        svc.state._last_run_date = "2026-09-06"
        svc.state.consecutive_losses = 3
        svc.state.symbol_entry_count_today = {"005930": 1}
        svc.state_store.save(svc.state, {})
        restarted = self.service()
        self.run_cycle(restarted)
        self.assertEqual(restarted.state.consecutive_losses, 0)
        self.assertEqual(restarted.state.symbol_entry_count_today, {})
        saved, _ = restarted.state_store.load()
        self.assertEqual(getattr(saved, "_last_run_date", None), "2026-09-07")
        restarted.state.consecutive_losses = 1
        self.run_cycle(restarted)
        self.assertEqual(restarted.state.consecutive_losses, 1)

    def test_balance_failure_blocks_buy_without_using_old_cash(self):
        svc = self.service()
        old = self.broker.balance
        self.broker.get_account_balance = Mock(side_effect=RuntimeError("balance timeout"))
        reason = svc._try_buy("005930", 100_000, old)
        self.assertEqual(len(self.broker.orders), 0)
        self.assertEqual(reason, "ACCOUNT_BALANCE_UNAVAILABLE")

    def test_expired_price_cache_cannot_size_a_new_buy(self):
        svc = self.service()
        svc.cached_market_price_loaded_at["005930"] = datetime.now() - timedelta(hours=2)
        reason = svc._try_buy("005930", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 0)
        self.assertEqual(reason, "STALE_MARKET_PRICE")

    def test_unfilled_buy_reserves_capacity_for_other_symbols(self):
        object.__setattr__(self.settings.trading, "max_positions", 1)
        svc = self.service()
        svc._try_buy("005930", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 1)
        reason = svc._try_buy("000660", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(reason, "ACCOUNT_ORDER_UNRESOLVED")

    def test_symbol_loss_count_ignores_duplicate_observation_global_counter_kept_2026_06_15_policy(self):
        # 2026-09-07: 외부 감사는 이 테스트가 원래 consecutive_losses==2를
        # 기대하도록(같은 종목 반복손실도 전역 카운터 반영) 제안했으나,
        # 민우님이 2026-06-15 정책(전역 카운터는 종목별 "새로운" 첫 손실만
        # 반영 — 한 종목의 불운이 계좌 전체를 세우지 않도록)을 그대로
        # 유지하기로 결정. 종목별 한도(symbol_loss_count_today, 중복 관측
        # 방지 포함)만 이 fix의 범위로 남기고 전역 카운터는 원복.
        svc = self.service()
        for _ in range(2):
            svc._pending_sell_side_effects["005930"] = dict(
                exit_reason="손절", avg_buy_price=100_000, current_price=98_000, quantity=10)
            svc._apply_deferred_sell_side_effects("005930")
            svc._apply_deferred_sell_side_effects("005930")  # duplicate observation
        self.assertEqual(svc.state.symbol_loss_count_today["005930"], 2)
        self.assertEqual(svc.state.consecutive_losses, 1)

    def test_malformed_success_http_is_ambiguous_and_never_retried(self):
        from domain.models import OrderRequest
        for body in ({}, {"raw_text": "upstream truncated"}, [], None,
                     {"return_code": False}, {"return_code": "0"}):
            with self.subTest(body=body):
                broker = KiwoomBroker(self.settings.broker)
                broker._post = Mock(return_value=KiwoomApiResponse(200, {}, body))
                result = broker.place_order(OrderRequest("005930", OrderSide.BUY, 10))
                self.assertFalse(result.accepted)
                self.assertTrue(result.is_ambiguous)
                self.assertEqual(broker._post.call_count, 1)

    def test_restart_with_unfilled_order_blocks_duplicate_and_other_buys(self):
        svc = self.service()
        svc._try_buy("005930", 100_000, self.broker.balance)
        restarted = self.service()  # account still reports zero holdings
        self.run_cycle(restarted)
        restarted._try_buy("005930", 100_000, self.broker.balance)
        restarted._try_buy("000660", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 1)

    def test_ambiguous_placement_survives_restart_before_cycle_save(self):
        svc = self.service()
        def ambiguous(order):
            self.broker.orders.append(order)
            return OrderResult("", order.symbol, order.side, order.quantity,
                               False, "timeout", datetime.now(), is_ambiguous=True)
        self.broker.place_order = ambiguous
        svc._try_buy("005930", 100_000, self.broker.balance)
        restarted = self.service()
        restarted._try_buy("005930", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 1)

    def test_restart_sell_pending_blocks_duplicate_but_allows_other_exit(self):
        self.broker.balance = AccountBalance(10_000_000, 12_000_000,
            [Position("005930", 10, 100_000), Position("000660", 10, 100_000)])
        svc = self.service()
        svc._position_state_machine.sync_from_broker("005930", 10)
        svc._try_sell("005930", 10, 98_000, "손절", 100_000)
        restarted = self.service()
        self.run_cycle(restarted)
        self.assertEqual([(o.symbol, o.quantity) for o in self.broker.orders],
                         [("005930", 10), ("000660", 10)])

    def test_failed_durable_write_prevents_submission(self):
        svc = self.service()
        with patch.object(svc.state_store, "save", side_effect=OSError("disk full")):
            svc._try_buy("005930", 100_000, self.broker.balance)
        self.assertEqual(len(self.broker.orders), 0)

    def test_atomic_state_failure_preserves_previous_file(self):
        svc = self.service()
        svc.state.consecutive_losses = 3
        svc.state_store.save(svc.state, {})
        old = Path(svc.settings.storage.state_file).read_bytes()
        with patch("infra.storage.state_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                svc.state_store.save(svc.state, {})
        self.assertEqual(Path(svc.settings.storage.state_file).read_bytes(), old)

    def test_market_window_uses_kst_and_excludes_weekends(self):
        from utils.time_utils import is_market_open, seconds_until_market_open
        monday = datetime(2026, 9, 7, 9, 10, tzinfo=KST_TZ)
        with patch("utils.time_utils.now_kst", return_value=monday):
            self.assertTrue(is_market_open())
            self.assertEqual(seconds_until_market_open(), 0)
        with patch("utils.time_utils.now_kst", return_value=monday.replace(day=6)):
            self.assertFalse(is_market_open())

    def test_force_exit_uses_kst_when_host_clock_is_utc(self):
        svc = self.service()
        self.broker.balance = AccountBalance(10_000_000, 11_000_000, [Position("005930", 10, 100_000)])
        svc._position_state_machine.sync_from_broker("005930", 10)
        with patch("domain.service.trading_service.now_kst",
                   return_value=datetime(2026, 9, 7, 15, 10, tzinfo=KST_TZ)):
            asyncio.run(svc._check_force_exit_overnight(self.broker.balance))
        self.assertEqual([(o.side, o.quantity) for o in self.broker.orders], [(OrderSide.SELL, 10)])

    def test_holdings_follow_continuation_and_keep_all_quantities(self):
        broker = KiwoomBroker(self.settings.broker)
        responses = [
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"ord_alow_amt": "10000000"}),
            KiwoomApiResponse(200, {"cont-yn": "Y", "next-key": "page2"},
                {"prsm_dpst_aset_amt": "12000000", "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A005930", "rmnd_qty": "10", "pur_pric": "100000"}]}),
            KiwoomApiResponse(200, {"cont-yn": "N", "next-key": ""},
                {"acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A000660", "rmnd_qty": "5", "pur_pric": "200000"}]}),
        ]
        broker._post = Mock(side_effect=responses)
        balance = broker.get_account_balance()
        self.assertEqual([(p.symbol, p.quantity) for p in balance.positions], [("005930", 10), ("000660", 5)])
        self.assertEqual(broker._post.call_count, 3)
        self.assertEqual(broker._post.call_args.kwargs["next_key"], "page2")

    def test_missing_holdings_array_is_not_an_empty_account(self):
        broker = KiwoomBroker(self.settings.broker)
        broker._post = Mock(side_effect=[
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"ord_alow_amt": "10000000"}),
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"prsm_dpst_aset_amt": "12000000"}),
        ])
        with self.assertRaises(RuntimeError):
            broker.get_account_balance()

    def test_negative_available_cash_blocks_buying_power_but_preserves_holdings(self):
        broker = KiwoomBroker(self.settings.broker)
        broker._post = Mock(side_effect=[
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"ord_alow_amt": "-1000000"}),
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"prsm_dpst_aset_amt": "0", "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "rmnd_qty": "10", "pur_pric": "100000"}]}),
        ])
        balance = broker.get_account_balance()
        self.assertEqual(balance.cash, 0)
        self.assertEqual([(p.symbol, p.quantity) for p in balance.positions], [("005930", 10)])

    def test_decimal_average_price_keeps_existing_integer_model_contract(self):
        broker = KiwoomBroker(self.settings.broker)
        broker._post = Mock(side_effect=[
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"ord_alow_amt": "1000000"}),
            KiwoomApiResponse(200, {"cont-yn": "N"}, {"prsm_dpst_aset_amt": "2000000", "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "rmnd_qty": "10", "pur_pric": "100,000.50"}]}),
        ])
        balance = broker.get_account_balance()
        self.assertEqual(balance.positions[0].average_price, 100000)

    def test_mock_partial_sell_preserves_residual_and_rejects_oversell(self):
        from infra.broker.mock_broker import MockBroker
        from domain.models import OrderRequest
        broker = MockBroker()
        broker.place_order(OrderRequest("005930", OrderSide.BUY, 10))
        broker.place_order(OrderRequest("005930", OrderSide.SELL, 3))
        balance = broker.get_account_balance()
        self.assertEqual([(p.symbol, p.quantity) for p in balance.positions], [("005930", 7)])
        result = broker.place_order(OrderRequest("005930", OrderSide.SELL, 8))
        self.assertFalse(result.accepted)
        self.assertEqual(broker.get_account_balance(), balance)

    def test_second_process_cannot_acquire_same_lock_then_can_after_release(self):
        import subprocess
        import sys
        from infra.storage.process_lock import single_instance_lock
        lock = str(Path(self.tmp.name) / "state.lock")
        code = ("import sys\nfrom infra.storage.process_lock import single_instance_lock\n"
                "try:\n with single_instance_lock(sys.argv[1]): pass\n"
                "except RuntimeError: sys.exit(7)\n")
        with single_instance_lock(lock):
            result = subprocess.run([sys.executable, "-c", code, lock], capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr)
        result = subprocess.run([sys.executable, "-c", code, lock], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_order_rechecks_balance_before_180_second_cache_expires(self):
        svc = self.service()
        svc._position_state_machine_initialized = True
        svc._try_buy("005930", 100_000, self.broker.balance)
        svc._get_balance_with_cache()  # first post-order query still says zero
        self.broker.balance = AccountBalance(19_000_000, 20_000_000, [Position("005930", 10, 100_000)])
        svc._sync_position_state_machine_shadow(svc._get_balance_with_cache())
        self.assertEqual(svc._position_state_machine.get("005930").known_quantity, 10)
        self.assertEqual(svc.state.symbol_entry_count_today["005930"], 1)

    def test_after_hours_reconciles_last_sale_without_new_orders(self):
        from app.main import trading_loop
        svc = self.service()
        svc._position_state_machine_initialized = True
        svc._position_state_machine.sync_from_broker("005930", 10)
        svc._try_sell("005930", 10, 98_000, "손절", 100_000)
        # Broker confirms the last sale only after the application's order
        # window has ended. Exercise the real application loop for one pass.
        self.broker.balance = AccountBalance(20_000_000, 20_000_000, [])
        object.__setattr__(svc.settings.broker, "use_mock", False)
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=False), \
             patch("app.main.now_local", return_value=datetime(2026,9,7,15,21)), \
             patch("app.main.asyncio.sleep", side_effect=asyncio.CancelledError):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))
        self.assertEqual(svc._position_state_machine.get("005930").lifecycle.value, "FLAT")
        self.assertEqual(svc.state.symbol_loss_count_today["005930"], 1)
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(svc.state_store.load()[0].unresolved_order_intents, {})


if __name__ == "__main__":
    unittest.main()
