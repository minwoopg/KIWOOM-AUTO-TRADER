"""2026-09-07 audit: deterministic failure scenarios, no network/orders.

Run directly (official regression runner) or with pytest.
The broker accepts orders without filling them unless a test changes its balance.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, MarketPrice, MarketRegime, OrderResult, OrderSide, Position
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker, KiwoomApiResponse, KiwoomHttpError
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

    def test_balance_fetch_429_does_enter_balance_outage(self):
        """위 테스트의 대응쌍 — 잔고 조회 자체에서 난 429는(태그가
        붙으므로) 실제로 `enter_balance_outage()`를 타야 합니다. 둘을
        함께 둬야 `kiwoom_balance_fetch_failure` 태깅이 실제로 두
        경우를 가르고 있다는 것(태그를 아예 없애버려도 두 테스트 중
        하나는 반드시 깨짐)이 검증됩니다."""
        from app.main import trading_loop
        svc = self.service()

        balance_429 = KiwoomHttpError(
            "kiwoom request failed: api_id=kt00001, http=429, "
            "body={'return_code': 5, 'return_msg': '허용된 요청 개수(1700)를 초과하였습니다'}",
            status_code=429,
            body={"return_code": 5, "return_msg": "허용된 요청 개수(1700)를 초과하였습니다"},
        )
        self.broker.get_account_balance = Mock(side_effect=balance_429)
        object.__setattr__(svc.settings.broker, "use_mock", True)

        # 1번째 폴링에서 run_once()가 429로 실패 → except 블록에서
        # enter_balance_outage() 호출 → 그 블록 자체의 asyncio.sleep()은
        # 그대로 흘려보낸다(여기서 CancelledError를 내면 try 블록
        # 바깥이라 못 잡혀 바로 위 테스트와 같은 문제가 재현된다).
        # 2번째 폴링은 is_in_balance_outage()==True라 handle_balance_
        # outage_tick()으로 가고(재시도 시각 전이라 아직 broker를 다시
        # 부르지 않음), try 블록의 asyncio.sleep()에서 깨끗하게 종료.
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=True), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 10, 0)), \
             patch("app.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertTrue(
            svc.is_in_balance_outage(),
            "잔고 조회에서 난 429는 실제로 잔고 장애 상태로 들어가야 합니다.",
        )

    def test_other_api_429_does_not_enter_balance_outage(self):
        """2026-09-15 (독립 잔고 재시도 설계, GPT 6차 재검토 1번 지적):
        예전엔 예외 메시지에 "http=429"만 있으면 무조건 잔고 장애로
        취급했습니다 — 시세·주문상태 등 다른 API에서 난 429까지 잔고
        재시도 경로로 잘못 보낼 위험이 있었습니다. 이제는 `_get_
        balance_with_cache()`가 실패 시 표시하는 `kiwoom_balance_
        fetch_failure` 속성이 없는 예외는(다른 API에서 난 429든, 429가
        아닌 다른 예외든) `enter_balance_outage()`를 부르지 않고 예전
        그대로 poll 주기만큼 자고 다음 폴링에서 다시 시도해야 합니다."""
        from app.main import trading_loop
        svc = self.service()

        other_api_429 = RuntimeError(
            "kiwoom request failed: api_id=ka10086, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )
        # kiwoom_balance_fetch_failure 속성을 의도적으로 달지 않는다 —
        # 시세 조회 등 다른 API에서 난 429를 재현한다. 1번째 폴링만
        # 실패시키고(예외 분기 통과 확인용), 2번째는 정상 통과시켜
        # try 블록 안의 asyncio.sleep()에서 깨끗하게 종료시킨다 —
        # except 블록 자체에서 도는 asyncio.sleep()이 CancelledError를
        # 내면 그 예외는 (try 블록이 아니므로) trading_loop() 바깥으로
        # 그대로 전파돼 버려 정상 종료 시나리오를 재현할 수 없기 때문.
        svc.run_once = unittest.mock.AsyncMock(side_effect=[other_api_429, None])
        object.__setattr__(svc.settings.broker, "use_mock", True)

        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=True), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 10, 0)), \
             patch("app.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertFalse(
            svc.is_in_balance_outage(),
            "잔고 조회에서 난 429가 아니면 잔고 장애 상태로 들어가면 안 됩니다.",
        )

    def test_balance_outage_persists_across_market_close_uses_tick_both_sides(self):
        """2026-09-15 (독립 잔고 재시도 설계, GPT 6차 재검토 2번·6번
        지적) → 2026-09-16 (GPT 7차 재검토 지적 반영, 기대값 정정):
        구 `wait_out_balance_outage()`는 180초를 통째로 블로킹해 그
        사이 장이 마감돼도 `trading_loop()`의 장 종료 분기로 못
        돌아갔습니다. 새 설계는 장애 상태(`is_in_balance_outage()`)가
        여전히 True인 채로 장이 마감돼도 `reconcile_after_market_
        close()`로 "전환"하지 않습니다 — 장중·장외 어느 쪽이든 장애가
        계속되는 한 `handle_balance_outage_tick()`이 계속 처리하며,
        장외로 넘어간 뒤에는 `reconcile_only=True`로 호출돼 복구 시
        주문 제출 없는 대조·저장만 하도록 구분됩니다(GPT 7차 재검토
        지적 — 장중·장외 공통 백오프, 복구 후 처리 구분).
        """
        from app.main import trading_loop
        svc = self.service()
        svc.enter_balance_outage()
        self.assertTrue(svc.is_in_balance_outage())

        tick_calls = []

        async def _fake_tick(*, reconcile_only=False):
            tick_calls.append(reconcile_only)
            return False  # 두 tick 모두 아직 복구되지 않음(장애 유지)

        svc.handle_balance_outage_tick = _fake_tick
        svc.run_once = unittest.mock.AsyncMock(
            side_effect=AssertionError("장애 중에는 run_once()를 직접 부르면 안 됩니다"),
        )
        svc.reconcile_after_market_close = Mock(
            side_effect=AssertionError(
                "장애가 계속되는 한 reconcile_after_market_close()를 직접 부르면 안 됩니다"
                "(handle_balance_outage_tick(reconcile_only=True)로 위임해야 함)"
            ),
        )

        # 1번째 폴링: 장중 + 장애 상태 → handle_balance_outage_tick() 호출.
        # 2번째 폴링: 장 마감 + 장애 유지 → handle_balance_outage_tick(
        # reconcile_only=True) 호출(reconcile_after_market_close() 직접
        # 호출 아님).
        market_open_sequence = [True, False]

        def _is_market_open_side_effect():
            return market_open_sequence.pop(0)

        object.__setattr__(svc.settings.broker, "use_mock", False)
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", side_effect=_is_market_open_side_effect), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 15, 21)), \
             patch("app.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertEqual(
            tick_calls, [False, True],
            "1번째(장중)는 기본값(reconcile_only=False), 2번째(장외)는 "
            "reconcile_only=True로 handle_balance_outage_tick()이 호출돼야 합니다.",
        )

    def test_after_hours_repeated_balance_429_still_reaches_eod_report_and_date_change(self):
        """2026-09-16 (GPT 7차 재검토 지적 — 높음): 개정 전에는 장외
        시간에 `reconcile_after_market_close()`가 잔고 429로 실패하면
        그 예외가 그대로 올라가 같은 폴링의 날짜변경 확인·마감 리포트
        생성 코드에 전혀 도달하지 못했다(GPT 실측 재현: 연속 429 중
        마감 리포트 호출 0회, 날짜변경 검사 0회). 이 테스트는 미해결
        주문이 있는 상태에서 잔고 조회가 연속 실패해도 같은 폴링 안에
        날짜변경 확인과(시각 조건이 맞으면) 마감 리포트 생성이 항상
        실행되는지 확인한다."""
        from app.main import trading_loop
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}
        self.assertTrue(svc._has_unresolved_orders())

        balance_429 = RuntimeError(
            "kiwoom request failed: api_id=kt00001, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )

        def _always_fail():
            exc = balance_429
            exc.kiwoom_balance_fetch_failure = True
            raise exc

        self.broker.get_account_balance = _always_fail

        daily_reset_calls = []
        original_daily_reset = svc._check_and_handle_daily_reset

        def _track_daily_reset():
            daily_reset_calls.append(1)
            return original_daily_reset()

        svc._check_and_handle_daily_reset = _track_daily_reset

        eod_calls = []
        original_eod = svc._run_end_of_day_tasks

        def _track_eod(now):
            eod_calls.append(1)
            return original_eod(now)

        svc._run_end_of_day_tasks = _track_eod

        object.__setattr__(svc.settings.broker, "use_mock", False)
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=False), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 15, 26)), \
             patch("app.main.asyncio.sleep", side_effect=[None, None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertTrue(
            svc.is_in_balance_outage(),
            "잔고 조회가 계속 429로 실패하면 장애 상태로 전환돼야 합니다(장외에서도 동일).",
        )
        self.assertGreaterEqual(
            len(daily_reset_calls), 3,
            "잔고 조회 실패 여부와 무관하게 매 폴링마다 날짜변경 확인이 실행돼야 합니다.",
        )
        self.assertGreaterEqual(
            len(eod_calls), 3,
            "잔고 조회 실패 여부와 무관하게 시각 조건이 맞으면 매 폴링마다 마감 리포트 처리가 시도돼야 합니다"
            "(실제 리포트 1회 생성 여부와는 별개로, 이 훅 자체가 호출되는지를 확인합니다).",
        )

    def test_other_api_429_backs_off_instead_of_immediate_retry(self):
        """2026-09-16 (GPT 7차 재검토 지적 — 높음): 개정 전에는(라운드5)
        잔고 조회가 아닌 다른 API의 429가 나면 poll_interval_seconds
        (기본 10초)만 쉬고 바로 다음 폴링에서 run_once()를 다시
        호출했다 — 그 이전(라운드4까지)에는 이런 예외도 180초를
        통째로 블로킹해 최소한의 재진입 제한이 있었다. 이 테스트는
        다른 API의 429 이후 곧바로 이어지는 폴링들에서 run_once()가
        다시 호출되지 않는지(=쿨다운이 실제로 적용되는지) 확인한다."""
        from app.main import trading_loop
        svc = self.service()

        other_api_429 = RuntimeError(
            "kiwoom request failed: api_id=ka10086, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )
        run_once_calls = []

        async def _fake_run_once():
            run_once_calls.append(1)
            if len(run_once_calls) == 1:
                raise other_api_429
            # 이후 호출은 성공 — 쿨다운이 걸려 있다면 애초에 호출되지
            # 않아야 하므로, 만약 호출된다면 이 카운트로 드러난다.

        svc.run_once = _fake_run_once
        object.__setattr__(svc.settings.broker, "use_mock", True)

        # 1번째 폴링: run_once() 429 실패 → 쿨다운 시작, sleep(poll).
        # 2·3번째 폴링: 쿨다운 안 끝났으므로 run_once()를 건너뛰어야
        # 한다 — monotonic 시계를 거의 전진시키지 않는다(가짜 sleep이
        # 실제로 시간을 흘려보내지 않으므로 자연히 쿨다운 유지됨).
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=True), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 10, 0)), \
             patch("app.main.asyncio.sleep", side_effect=[None, None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertEqual(
            len(run_once_calls), 1,
            "다른 API의 429 이후 쿨다운 기간 안에는 run_once()가 다시 호출되면 안 됩니다.",
        )
        self.assertFalse(
            svc.is_in_balance_outage(),
            "다른 API의 429는 잔고 장애 상태로 들어가면 안 됩니다(기존 검증과 동일).",
        )

    def test_balance_recovery_after_hours_reconciles_without_submitting_orders(self):
        """2026-09-16 (GPT 7차 재검토 지적 — 복구 후 처리 구분): 장외
        시간에 잔고가 복구되면 정상 전략 처리(`_run_once_with_
        balance()`, 신규 주문 제출 가능)로 이어지면 안 되고, `reconcile_
        after_market_close()`와 동일하게 대조·저장만 수행해야 한다."""
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}
        svc.enter_balance_outage()
        clock = {"t": svc._balance_retry_next_attempt_at}
        svc._monotonic = lambda: clock["t"]

        run_once_with_balance_calls = []

        async def _fake_run_once_with_balance(balance):
            run_once_with_balance_calls.append(balance)

        svc._run_once_with_balance = _fake_run_once_with_balance

        sync_calls = []
        original_sync = svc._sync_position_state_machine_shadow

        def _track_sync(balance):
            sync_calls.append(balance)
            return original_sync(balance)

        svc._sync_position_state_machine_shadow = _track_sync

        recovered_balance = self.broker.balance
        recovered = asyncio.run(svc.handle_balance_outage_tick(reconcile_only=True))

        self.assertTrue(recovered)
        self.assertFalse(svc.is_in_balance_outage())
        self.assertEqual(
            run_once_with_balance_calls, [],
            "장외 복구는 정상 전략 처리(_run_once_with_balance)를 호출하면 안 됩니다.",
        )
        self.assertEqual(
            len(sync_calls), 1,
            "장외 복구는 reconcile_after_market_close()와 동일하게 PSM 대조는 수행해야 합니다.",
        )

    def test_after_hours_other_api_429_backs_off_instead_of_immediate_retry(self):
        """2026-09-16 (GPT 8차 재검토 지적 — 높음): 장외 대조(`reconcile_
        after_market_close()`)는 미해결 주문이 있으면 잔고 API뿐
        아니라 order-status 조회(다른 API, `_sync_position_state_
        machine_shadow()` 참고)도 함께 호출한다 — 그 429는 `kiwoom_
        balance_fetch_failure` 태그가 없다. 개정 전에는 이 블록의
        except가 잔고 태그만 확인하고 나머지는 로그만 남겨, 다른
        API의 429가 매 폴링(10초)마다 그대로 재시도되는 우회가
        있었다(GPT 실측 재현: 대조 재실행 0·10·20초). 이 테스트는
        장외에서 다른 API의 429가 나도 쿨다운 기간 동안 대조가 다시
        호출되지 않는지 확인한다."""
        from app.main import trading_loop
        svc = self.service()

        other_api_429 = RuntimeError(
            "kiwoom request failed: api_id=kt00005, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )
        reconcile_calls = []

        def _fake_reconcile():
            reconcile_calls.append(1)
            raise other_api_429

        svc.reconcile_after_market_close = _fake_reconcile
        object.__setattr__(svc.settings.broker, "use_mock", False)

        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=False), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 15, 21)), \
             patch("app.main.asyncio.sleep", side_effect=[None, None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertEqual(
            len(reconcile_calls), 1,
            "다른 API의 429 이후 쿨다운 기간 안에는 장외 대조가 다시 호출되면 안 됩니다.",
        )
        self.assertFalse(
            svc.is_in_balance_outage(),
            "다른 API의 429는 잔고 장애 상태로 들어가면 안 됩니다.",
        )

    def test_other_api_cooldown_carries_over_from_market_open_to_after_hours(self):
        """2026-09-16 (GPT 8차 재검토 지적 — 높음): 장중에 걸린 다른
        API 쿨다운이 장외로 전환된 뒤에도 그대로 유지돼, 분기 순서
        때문에 장외 대조가 쿨다운을 우회해 곧바로 재시도되면 안
        된다."""
        from app.main import trading_loop
        svc = self.service()

        other_api_429 = RuntimeError(
            "kiwoom request failed: api_id=ka10086, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )
        run_once_calls = []

        async def _fake_run_once():
            run_once_calls.append(1)
            raise other_api_429

        svc.run_once = _fake_run_once

        reconcile_calls = []

        def _fake_reconcile():
            reconcile_calls.append(1)

        svc.reconcile_after_market_close = _fake_reconcile

        # 1번째 폴링: 장중, run_once() 429 실패 → 쿨다운 시작.
        # 2번째 폴링: 장 마감 → 쿨다운이 이어져 대조를 건너뛰어야 함.
        market_open_sequence = [True, False]

        def _is_market_open_side_effect():
            return market_open_sequence.pop(0)

        object.__setattr__(svc.settings.broker, "use_mock", False)
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", side_effect=_is_market_open_side_effect), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 15, 21)), \
             patch("app.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertEqual(len(run_once_calls), 1)
        self.assertEqual(
            reconcile_calls, [],
            "장중에 걸린 다른 API 쿨다운이 장외로 넘어가도 그대로 유지돼 대조를 건너뛰어야 합니다.",
        )

    def test_eod_report_provisional_then_finalized_once_after_reconciliation(self):
        """2026-09-16 (GPT 8차 재검토 지적 — 중간, 9차에서 target_date
        전달 확인 추가, 10차에서 상태 구조 변경 반영): 미해결 주문이
        있는 채로 마감 리포트가 잠정 생성된 뒤, 대조가 끝나면(미해결
        주문 해소) 시각 제한 없이(15시 제한과 분리) 한 번 더 최종본
        으로 갱신돼야 하고, 이후 반복 호출에서는 추가로 생성되지
        않아야 한다. 리포트 파일 본문에도 잠정 여부가 남아야 하고,
        최종화로 리포트가 다시 저장됐으니 번들도 다시 생성돼야
        한다(10차 재검토 지적)."""
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}
        self.assertTrue(svc._has_unresolved_orders())

        generate_calls = []
        original_generate = svc._generate_daily_report

        def _track_generate(*, provisional=False, target_date=None):
            generate_calls.append((provisional, target_date))
            return original_generate(provisional=provisional, target_date=target_date)

        svc._generate_daily_report = _track_generate

        # 다른 부수 분석·번들 내보내기(subprocess 호출 포함)는 이
        # 테스트의 관심사가 아니므로 무력화한다.
        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today", "_export_daily_bundle_today",
        ):
            setattr(svc, attr, Mock())

        target = date(2026, 9, 7)

        # 1) 15:26, 미해결 주문 있음 → 잠정 생성.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26))
        self.assertEqual(generate_calls, [(True, target)])
        self.assertEqual(svc._report_generated_date, target)
        self.assertEqual(svc._eod_pending_dates.get(target), True)
        self.assertEqual(svc._export_daily_bundle_today.call_count, 1)

        # 2026-09-16 (GPT 9차 재검토 지적 반영): _generate_daily_
        # report()가 이제 target_date를 그대로 DailyReporter.generate()
        # 에 전달하므로, 파일 경로는 (실제 오늘 날짜가 아니라) 이
        # 테스트가 넘긴 거래일 그대로 찾는다.
        report_path = (
            Path(svc.settings.storage.trade_log_file).parent
            / f"daily_report_{target.strftime('%Y%m%d')}.txt"
        )
        self.assertTrue(report_path.exists())
        self.assertIn("잠정(대조 미완료)", report_path.read_text(encoding="utf-8"))

        # 2) 15:40, 여전히 미해결 → 반복 폴링에서 추가 생성 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 40))
        self.assertEqual(generate_calls, [(True, target)], "대조가 안 끝났으면 재생성하면 안 됩니다.")

        # 3) 16:05, 대조 완료(미해결 주문 해소) → 시각 제한 없이(now.hour==15
        # 제한과 분리) 최종본으로 정확히 한 번 갱신.
        svc.state.unresolved_order_intents = {}
        self.assertFalse(svc._has_unresolved_orders())
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 5))
        self.assertEqual(
            generate_calls, [(True, target), (False, target)],
            "대조 완료 후 시각 제한 없이, 원래 대상 거래일 그대로 최종본으로 정확히 한 번 더 갱신돼야 합니다.",
        )
        self.assertNotIn(target, svc._eod_pending_dates)
        self.assertNotIn("잠정(대조 미완료)", report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            svc._export_daily_bundle_today.call_count, 2,
            "최종본으로 갱신된 리포트를 반영하도록 번들도 다시 생성돼야 합니다(GPT 10차 재검토 지적).",
        )

        # 4) 16:10, 이후 반복 폴링 → 추가 생성 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 10))
        self.assertEqual(
            generate_calls, [(True, target), (False, target)],
            "최종본 갱신 후에는 추가로 생성되면 안 됩니다.",
        )
        self.assertEqual(svc._export_daily_bundle_today.call_count, 2)

    def test_balance_outage_and_other_api_cooldown_simultaneous_no_bypass(self):
        """2026-09-16 (GPT 9차 재검토 지적 — 높음): handle_balance_
        outage_tick()은 잔고 재시도가 성공하면 그 안에서 곧바로
        _run_once_with_balance()(신규 주문 제출 가능한 정상 처리)까지
        호출한다. 개정 전(8차까지)에는 장중 분기가 잔고 장애를 다른
        API 쿨다운보다 먼저 확인해서, 두 상태가 동시에 걸려 있을 때
        잔고 재시도가 먼저 성공하면(예: 30초) 다른 API 쿨다운이 아직
        남아있어도(예: 180초) handle_balance_outage_tick()이 호출돼
        정상 처리로 우회 진입할 수 있었다(GPT가 두 상태를 함께
        주입해 재현). 이 테스트는 그 두 상태를 함께 주입해, 다른
        API 쿨다운이 남아있는 동안에는 잔고 장애 상태여도
        handle_balance_outage_tick()이 호출되지 않는지 확인한다."""
        from app.main import trading_loop
        svc = self.service()

        other_api_429 = RuntimeError(
            "kiwoom request failed: api_id=ka10086, http=429, "
            "body={'return_msg': '허용된 요청 개수를 초과하였습니다'}"
        )
        run_once_calls = []
        call_count = {"n": 0}

        async def _fake_run_once():
            call_count["n"] += 1
            run_once_calls.append(1)
            if call_count["n"] == 1:
                # GPT 재현 방식과 동일하게 — 다른 API 429가 발생한
                # 시점에 잔고 장애도 함께(동시에) 걸려 있는 상태를
                # 주입한다. 재시도 시각은 이미 지난 것으로 설정해,
                # 다음 tick에 바로 성공할 수 있는 상태로 만든다.
                svc.enter_balance_outage()
                svc._balance_retry_next_attempt_at = svc._monotonic() - 1
                raise other_api_429

        svc.run_once = _fake_run_once

        tick_calls = []

        async def _fake_tick(*, reconcile_only=False):
            tick_calls.append(reconcile_only)
            return True

        svc.handle_balance_outage_tick = _fake_tick

        object.__setattr__(svc.settings.broker, "use_mock", False)
        with patch("app.main.seconds_until_market_open", return_value=0), \
             patch("app.main.is_market_open", return_value=True), \
             patch("app.main.now_local", return_value=datetime(2026, 9, 7, 10, 30)), \
             patch("app.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            asyncio.run(trading_loop(svc, svc.settings, Mock()))

        self.assertEqual(
            tick_calls, [],
            "다른 API 쿨다운이 남아있는 동안에는 잔고 장애 상태여도 "
            "handle_balance_outage_tick()을 호출해 정상 처리로 우회 진입하면 안 됩니다.",
        )
        self.assertEqual(
            len(run_once_calls), 1,
            "다른 API 쿨다운 중에는 run_once()도 다시 호출되면 안 됩니다.",
        )
        self.assertTrue(svc.is_in_balance_outage(), "잔고 장애 상태 자체는 그대로 유지돼야 합니다.")

    def test_eod_report_finalize_save_failure_keeps_provisional_and_retries_once_fixed(self):
        """2026-09-16 (GPT 9차 재검토 지적 — 중간, 10차에서 상태 구조
        변경 반영): `_generate_daily_report()`가 예외를 삼키고 항상
        성공한 것처럼 취급되면, 최종 갱신 저장이 실패해도 잠정 대기
        상태가 사라져 버려 다음 폴링에서 재시도하지 않았다(GPT 실측
        재현: 생성 시도가 한 번뿐이고 재시도 없음). 이 테스트는 최종
        저장이 실패하면 상태가 유지되고(기존 잠정 파일도 훼손 없이
        보존), 재시도 간격이 지난 뒤 재시도가 성공하면 정확히 한 번
        더 시도해 최종본으로 갱신되며 그 뒤 중복 생성이 없는지
        확인한다."""
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}

        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today", "_export_daily_bundle_today",
        ):
            setattr(svc, attr, Mock())

        save_attempts = []
        original_reporter_generate = svc._reporter.generate
        should_fail = {"value": False}

        def _flaky_generate(*args, **kwargs):
            save_attempts.append(kwargs.get("provisional"))
            if should_fail["value"]:
                raise RuntimeError("disk write failed (injected)")
            return original_reporter_generate(*args, **kwargs)

        svc._reporter.generate = _flaky_generate

        target = date(2026, 9, 7)
        report_path = (
            Path(svc.settings.storage.trade_log_file).parent
            / f"daily_report_{target.strftime('%Y%m%d')}.txt"
        )

        # 1) 15:26 잠정 생성 — 정상 성공.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26))
        self.assertEqual(save_attempts, [True])
        self.assertEqual(svc._eod_pending_dates.get(target), True)
        provisional_text = report_path.read_text(encoding="utf-8")
        self.assertIn("잠정(대조 미완료)", provisional_text)

        # 2) 대조 완료(미해결 주문 해소) — 이번엔 최종 저장 자체가
        # 실패하도록 주입.
        svc.state.unresolved_order_intents = {}
        should_fail["value"] = True
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 5))
        self.assertEqual(save_attempts, [True, False], "저장 실패라도 시도 자체는 이뤄져야 합니다.")
        self.assertEqual(
            svc._eod_pending_dates.get(target), True,
            "최종 저장이 실패하면 잠정 상태를 그대로 유지해야 합니다(성공한 것처럼 확정하면 안 됨).",
        )
        self.assertEqual(
            report_path.read_text(encoding="utf-8"), provisional_text,
            "저장 실패 시 기존 잠정 파일이 훼손되지 않고 그대로 보존돼야 합니다.",
        )

        # 3) 재시도 간격이 지나기 전 — 곧바로 다음 폴링에서 또 재시도하면 안 됨.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 5, 5))
        self.assertEqual(
            save_attempts, [True, False],
            "저장 실패 직후 재시도 간격이 지나기 전에는 다시 시도하면 안 됩니다.",
        )

        # 4) 재시도 간격 경과 + 이번엔 저장 성공 → 정확히 한 번 더 시도해 최종본으로 갱신.
        svc._eod_retry_at[target] = svc._monotonic() - 1
        should_fail["value"] = False
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 6))
        self.assertEqual(save_attempts, [True, False, False])
        self.assertNotIn(target, svc._eod_pending_dates)
        self.assertNotIn("잠정(대조 미완료)", report_path.read_text(encoding="utf-8"))

        # 5) 이후 반복 폴링 — 중복 생성 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 10))
        self.assertEqual(
            save_attempts, [True, False, False],
            "최종본 갱신 후에는 추가로 생성되면 안 됩니다.",
        )

    def test_eod_report_finalize_uses_original_target_date_after_crossing_midnight(self):
        """2026-09-16 (GPT 9차 재검토 지적 — 거래일 연결, 10차에서
        FIFO 우선순위로 변경): 잠정 생성 시점의 거래일과 최종화
        시점의 `now.date()`가 다르면(자정을 넘겨 계속 실행 중인
        프로세스에서 다음날 대조가 완료되는 경우), 리포트·분석·번들이
        전일이 아니라 다음날 거래일로 잘못 생성됐다(GPT 실측 재현:
        9/16 잠정 생성 → 9/17 대조 완료 → 번들 대상이 9/16이 아니라
        9/17로 바뀜). 이 테스트는 (1) 전일 잠정본의 최종화가 항상
        잠정 생성 당시의 거래일을 쓰는지, (2) 당일 등록과 전일
        최종화 조건이 같은 tick에 동시에 맞아떨어지면 오래된 거래일
        (전일 최종화)부터 처리되는지 확인한다.

        2026-09-17 (GPT 10차 재검토 지적 반영): 9차 구현은
        `_pending_provisional_date` 스칼라 하나로만 추적해 "당일 최초
        생성"을 항상 우선했다 — 그 결과 이틀 연속 잠정 상태가 되면
        두 번째 거래일이 첫 번째를 덮어써 최종화 대상에서 완전히
        누락되는 문제가 있었다(별도 테스트
        `test_eod_report_finalizes_multiple_pending_dates_in_order`
        참고). 이제 거래일별 dict를 오래된 순서로 순회해 처리하므로,
        같은 tick에 두 조건이 겹치면 전일 최종화가 먼저 처리되고
        당일 최초 생성은 등록만 이번 tick에 해둔 채 다음 tick으로
        넘어간다.
        """
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}

        generate_calls = []
        original_generate = svc._generate_daily_report

        def _track_generate(*, provisional=False, target_date=None):
            generate_calls.append((provisional, target_date))
            return original_generate(provisional=provisional, target_date=target_date)

        svc._generate_daily_report = _track_generate

        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today", "_export_daily_bundle_today",
        ):
            setattr(svc, attr, Mock())

        day1 = date(2026, 9, 16)
        day2 = date(2026, 9, 17)
        reports_dir = Path(svc.settings.storage.trade_log_file).parent
        day1_report_path = reports_dir / f"daily_report_{day1.strftime('%Y%m%d')}.txt"
        day2_report_path = reports_dir / f"daily_report_{day2.strftime('%Y%m%d')}.txt"

        # 1) 9/16 15:26 — 미해결 주문 있음 → 9/16 대상으로 잠정 생성.
        svc._run_end_of_day_tasks(datetime(2026, 9, 16, 15, 26))
        self.assertEqual(generate_calls, [(True, day1)])
        self.assertEqual(svc._eod_pending_dates, {day1: True})
        self.assertTrue(day1_report_path.exists())
        self.assertIn("잠정(대조 미완료)", day1_report_path.read_text(encoding="utf-8"))

        # 2) 자정을 넘겨 프로세스가 계속 떠 있고(재시작 없음), 9/17
        # 15:26에 9/16 주문이 마침 해소된 상태로 확인됨 — 9/17 자체의
        # 최초 등록 조건과 9/16 잠정본의 최종화 조건이 같은 tick에
        # 동시에 맞아떨어진다. 오래된 거래일(9/16)부터 처리하므로
        # 이번 tick엔 9/16이 원래 거래일 그대로 최종화되고, 9/17은
        # 등록만 되고 생성은 다음 tick으로 넘어간다.
        svc.state.unresolved_order_intents = {}
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26))
        self.assertEqual(
            generate_calls, [(True, day1), (False, day1)],
            "9/16 잠정본은 now.date()(9/17)가 아니라 원래 대상 거래일(9/16)로, 오래된 거래일부터 최종화돼야 합니다.",
        )
        self.assertEqual(svc._report_generated_date, day2, "9/17 작업은 등록은 이번 tick에 이뤄져야 합니다.")
        self.assertEqual(
            svc._eod_pending_dates, {day2: False},
            "9/16은 최종화 완료로 제거되고, 9/17은 등록만 된 채(아직 저장 전) 남아있어야 합니다.",
        )
        self.assertFalse(day2_report_path.exists(), "9/17 리포트는 이번 tick엔 아직 생성되지 않아야 합니다.")
        self.assertNotIn(
            "잠정(대조 미완료)", day1_report_path.read_text(encoding="utf-8"),
            "9/16 파일은 이번 tick에 최종본으로 갱신돼야 합니다.",
        )

        # 3) 다음 폴링(10초 뒤) — 9/17 자체 리포트가 그 시점엔 미해결
        # 주문이 없으므로 최종본으로 독립 생성된다.
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26, 10))
        self.assertEqual(
            generate_calls, [(True, day1), (False, day1), (False, day2)],
            "9/17 리포트는 이번 tick에 최종본으로 독립 생성돼야 합니다.",
        )
        self.assertEqual(svc._eod_pending_dates, {})
        self.assertTrue(day2_report_path.exists())
        self.assertNotIn("잠정(대조 미완료)", day2_report_path.read_text(encoding="utf-8"))

        # 4) 이후 반복 폴링 — 추가 생성 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26, 20))
        self.assertEqual(
            generate_calls, [(True, day1), (False, day1), (False, day2)],
            "두 거래일 모두 완료된 뒤에는 추가로 생성되면 안 됩니다.",
        )

    def test_eod_report_finalizes_multiple_pending_dates_in_order(self):
        """2026-09-17 (GPT 10차 재검토 지적 — 중간): 9차 구현은
        "최종화 대기 거래일"을 `_pending_provisional_date` 스칼라
        하나로만 추적했다 — 연속된 이틀이 모두 잠정 상태가 되면
        (예: 장외 장애가 이틀 넘게 이어짐) 두 번째 거래일이 그 값을
        덮어써, 이후 미해결 주문이 해소돼도 첫 번째 거래일만
        최종화되고 두 번째는 영원히 잠정으로 남았다(GPT 실측 재현:
        9/16 잠정 → 9/17도 잠정 → 대조 완료 후 9/16만 최종화). 이제
        거래일별 dict로 추적하므로 두 거래일 모두(오래된 순서로)
        최종화돼야 한다."""
        svc = self.service()
        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today", "_export_daily_bundle_today",
        ):
            setattr(svc, attr, Mock())

        day1 = date(2026, 9, 16)
        day2 = date(2026, 9, 17)
        reports_dir = Path(svc.settings.storage.trade_log_file).parent
        day1_report_path = reports_dir / f"daily_report_{day1.strftime('%Y%m%d')}.txt"
        day2_report_path = reports_dir / f"daily_report_{day2.strftime('%Y%m%d')}.txt"

        # 1) 9/16 15:26 — 미해결 주문 있음 → 9/16 잠정 생성.
        svc.state.unresolved_order_intents = {"dummy": "order"}
        svc._run_end_of_day_tasks(datetime(2026, 9, 16, 15, 26))
        self.assertEqual(svc._eod_pending_dates, {day1: True})

        # 2) 9/17 15:26 — 여전히 미해결 → 9/17도 잠정 생성. 두 거래일
        # 모두 최종화 대기 상태가 된다(9차 구현이라면 여기서 day1의
        # 대기 기록이 사라졌을 것).
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26))
        self.assertEqual(
            svc._eod_pending_dates, {day1: True, day2: True},
            "두 거래일 모두 잠정 최종화 대기 상태로 독립 추적돼야 합니다.",
        )
        self.assertIn("잠정(대조 미완료)", day1_report_path.read_text(encoding="utf-8"))
        self.assertIn("잠정(대조 미완료)", day2_report_path.read_text(encoding="utf-8"))

        # 3) 대조 완료(미해결 주문 해소) — 오래된 거래일(9/16)부터 최종화.
        svc.state.unresolved_order_intents = {}
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26, 10))
        self.assertEqual(
            svc._eod_pending_dates, {day2: True},
            "9/16이 먼저 최종화되고, 9/17은 아직 대기 상태로 남아있어야 합니다.",
        )
        self.assertNotIn("잠정(대조 미완료)", day1_report_path.read_text(encoding="utf-8"))
        self.assertIn("잠정(대조 미완료)", day2_report_path.read_text(encoding="utf-8"))

        # 4) 다음 폴링 — 9/17도 최종화된다(9차 구현이었다면 여기서
        # 영원히 잠정으로 남았을 것).
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26, 20))
        self.assertEqual(
            svc._eod_pending_dates, {},
            "두 거래일 모두 최종화돼야 합니다 — 두 번째 거래일이 누락되면 안 됩니다.",
        )
        self.assertNotIn("잠정(대조 미완료)", day2_report_path.read_text(encoding="utf-8"))

        # 5) 이후 반복 폴링 — 추가 생성 없음(예외 없이 조용히 반환).
        svc._run_end_of_day_tasks(datetime(2026, 9, 17, 15, 26, 30))
        self.assertEqual(svc._eod_pending_dates, {})

    def test_eod_report_initial_save_failure_retries_after_16_with_original_date(self):
        """2026-09-17 (GPT 10차 재검토 지적 — 중간): 9차 구현은 "최초
        생성" 조건 자체가 `now.hour == 15`를 요구했다 — 15:59대에
        최초 저장이 실패하면 등록된 거래일이 없는 채로 16시가 지나
        버려(요구 조건이 다시는 참이 되지 않음) 영원히 재시도하지
        않았다(GPT 실측 재현). 이제 시각 조건은 등록 시점에만 적용
        하고, 이미 등록된 거래일의 재시도는 시각과 무관하게 원래
        거래일 그대로 처리돼야 한다."""
        svc = self.service()
        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today", "_export_daily_bundle_today",
        ):
            setattr(svc, attr, Mock())

        target = date(2026, 9, 7)
        original_generate = svc._reporter.generate
        should_fail = {"value": True}
        save_calls = []

        def _flaky(*args, **kwargs):
            save_calls.append(kwargs.get("target_date"))
            if should_fail["value"]:
                raise RuntimeError("disk write failed (injected)")
            return original_generate(*args, **kwargs)

        svc._reporter.generate = _flaky

        # 1) 15:59:50 — 최초 저장 시도(미해결 주문 없음 → 최종 시도)
        # 자체가 실패.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 59, 50))
        self.assertEqual(save_calls, [target])
        self.assertEqual(svc._report_generated_date, target, "등록은 실패와 무관하게 이뤄져야 합니다.")
        self.assertEqual(
            svc._eod_pending_dates.get(target), False,
            "저장에 아직 성공한 적이 없으므로 False(미완료) 상태로 남아야 합니다.",
        )

        # 2) 16:00:00 — 재시도 간격(30초)이 지나기 전이므로 재시도 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 0, 0))
        self.assertEqual(save_calls, [target], "재시도 간격이 지나기 전에는 재시도하면 안 됩니다.")

        # 3) 16:05 — 재시도 간격 경과 + 이번엔 저장 성공. 16시가
        # 지났어도 새로 오늘 날짜를 등록하는 게 아니라, 원래 등록된
        # 거래일(target) 그대로 재시도돼야 한다.
        svc._eod_retry_at[target] = svc._monotonic() - 1
        should_fail["value"] = False
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 5))
        self.assertEqual(save_calls, [target, target])
        self.assertNotIn(
            target, svc._eod_pending_dates,
            "저장 성공(미해결 주문 없음 → 최종) 후에는 더 이상 추적하지 않아야 합니다.",
        )
        report_path = (
            Path(svc.settings.storage.trade_log_file).parent
            / f"daily_report_{target.strftime('%Y%m%d')}.txt"
        )
        self.assertTrue(report_path.exists())

        # 4) 16:10 — 이후 반복 폴링, 추가 등록·생성 없음(오늘 날짜는
        # 이미 등록됐던 그 날짜와 같으므로 재등록되지 않아야 한다).
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 16, 10))
        self.assertEqual(save_calls, [target, target])

    def test_eod_followup_step_failure_retries_only_failed_step(self):
        """2026-09-17 (GPT 10차 재검토 지적 — 중간): 리포트 저장 직후
        실행하는 후속 분석/번들 생성이 실패해도 예전엔 경고만 남기고
        재시도가 전혀 없었다(GPT 실측 재현: 번들 실행에 실패 코드를
        3번 연속 주입해도 시도는 한 번뿐). 이제 실패한 단계만 다음
        폴링에서 재시도하고, 이미 성공한 단계는 다시 실행되지 않아야
        한다."""
        svc = self.service()

        calls = {name: [] for name in (
            "validate", "signal", "trade", "indicator",
            "replay", "bb_block", "shadow", "bundle",
        )}
        bundle_should_fail = {"value": True}

        def _ok(name):
            def _fn(target_date):
                calls[name].append(target_date)
                return True
            return _fn

        svc._validate_logs_today = _ok("validate")
        svc._run_signal_analysis_today = _ok("signal")
        svc._run_trade_analysis_today = _ok("trade")
        svc._run_indicator_analysis_today = _ok("indicator")
        svc._run_replay_today = _ok("replay")
        svc._run_bb_block_impact_today = _ok("bb_block")
        svc._run_shadow_analysis_today = _ok("shadow")

        def _bundle(target_date):
            calls["bundle"].append(target_date)
            return not bundle_should_fail["value"]

        svc._export_daily_bundle_today = _bundle

        target = date(2026, 9, 7)

        # 1) 15:26 — 리포트 저장은 성공(미해결 주문 없음 → 최종),
        # 번들만 실패.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26))
        self.assertEqual({k: len(v) for k, v in calls.items()}, {
            "validate": 1, "signal": 1, "trade": 1, "indicator": 1,
            "replay": 1, "bb_block": 1, "shadow": 1, "bundle": 1,
        })
        self.assertEqual(svc._eod_followups_pending.get(target), {"bundle"})
        self.assertNotIn(
            target, svc._eod_pending_dates,
            "리포트 저장 자체는 성공했으므로 리포트 재저장 대상에는 없어야 합니다.",
        )

        # 2) 재시도 간격이 지나기 전 — 재시도 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26, 5))
        self.assertEqual(len(calls["bundle"]), 1)

        # 3) 재시도 간격 경과 + 번들 성공 → 번들만 다시 실행되고,
        # 이미 성공했던 다른 단계는 재실행되지 않아야 한다.
        svc._eod_retry_at[target] = svc._monotonic() - 1
        bundle_should_fail["value"] = False
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26, 40))
        self.assertEqual(len(calls["bundle"]), 2)
        for name in ("validate", "signal", "trade", "indicator", "replay", "bb_block", "shadow"):
            self.assertEqual(len(calls[name]), 1, f"{name}은 이미 성공했으므로 재실행되면 안 됩니다.")
        self.assertNotIn(target, svc._eod_followups_pending)

        # 4) 이후 폴링 — 추가 실행 없음.
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 27))
        self.assertEqual(len(calls["bundle"]), 2)

    def test_daily_reporter_atomic_write_preserves_file_on_replace_failure(self):
        """2026-09-17 (GPT 10차 재검토 지적 — 중간): `DailyReporter.
        generate()`가 `Path.write_text()`로 기존 파일에 직접 덮어써,
        쓰는 도중(또는 마지막 교체 도중) 실패하면 기존 잠정 파일이
        이미 훼손된 뒤였다(GPT 실측 재현: 파일을 일부 쓴 뒤 오류를
        주입하면 `_generate_daily_report()`는 정상적으로 False를
        반환하지만 기존 파일 내용은 이미 바뀌어 있었음). 이제
        임시 파일에 전체 내용을 쓰고 성공했을 때만 `os.replace()`로
        교체하므로, 마지막 교체가 실패해도 기존 파일은 전혀
        건드려지지 않고 임시 파일도 남지 않아야 한다."""
        from infra.storage.daily_reporter import DailyReporter

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "daily_report_20260907.txt"
            target.write_text("ORIGINAL CONTENT", encoding="utf-8")

            with patch("os.replace", side_effect=OSError("disk full (injected)")):
                with self.assertRaises(OSError):
                    DailyReporter._atomic_write_text(target, "NEW CONTENT (should not land)")

            self.assertEqual(
                target.read_text(encoding="utf-8"), "ORIGINAL CONTENT",
                "os.replace() 실패 시 기존 파일 내용이 전혀 바뀌면 안 됩니다.",
            )
            leftover = [p for p in Path(tmpdir).iterdir() if p.name.startswith(f".{target.name}.")]
            self.assertEqual(leftover, [], "실패한 임시 파일이 정리되지 않고 남아있으면 안 됩니다.")

    def test_daily_reporter_atomic_write_preserves_file_on_write_failure(self):
        """위 테스트와 동일한 취지 — 임시 파일에 '쓰는 도중'(교체 이전
        단계) 실패해도 기존 파일이 보존되고 임시 파일이 정리되는지
        확인한다(디스크가 쓰기 도중 가득 차는 경우를 모사)."""
        from infra.storage.daily_reporter import DailyReporter

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "daily_report_20260907.txt"
            target.write_text("ORIGINAL CONTENT", encoding="utf-8")

            class _FailingFile:
                def write(self, _data):
                    raise OSError("disk full (injected)")

                def flush(self):
                    pass

                def fileno(self):
                    return 0

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            with patch("os.fdopen", return_value=_FailingFile()), \
                 patch("os.fsync"):
                with self.assertRaises(OSError):
                    DailyReporter._atomic_write_text(target, "NEW CONTENT (should not land)")

            self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL CONTENT")
            leftover = [p for p in Path(tmpdir).iterdir() if p.name.startswith(f".{target.name}.")]
            self.assertEqual(leftover, [], "실패한 임시 파일이 정리되지 않고 남아있으면 안 됩니다.")

    def test_daily_reporter_generate_preserves_previous_report_on_save_failure(self):
        """`_atomic_write_text()` 단독 테스트와 별개로, `DailyReporter.
        generate()`가 실제로 그 헬퍼를 통해 저장하는지(배선이 빠지지
        않았는지) 종단으로 확인한다 — 첫 호출로 잠정 리포트를 저장한
        뒤, 두 번째 호출(최종 갱신)에서 저장 자체가 실패하도록
        주입하면 첫 번째로 저장된 잠정 리포트 파일 내용이 그대로
        보존돼야 한다."""
        svc = self.service()
        svc.state.unresolved_order_intents = {"dummy": "order"}
        target = date(2026, 9, 7)

        # 1) 잠정 리포트를 정상 저장.
        svc._generate_daily_report(provisional=True, target_date=target)
        report_path = (
            Path(svc.settings.storage.trade_log_file).parent
            / f"daily_report_{target.strftime('%Y%m%d')}.txt"
        )
        provisional_text = report_path.read_text(encoding="utf-8")
        self.assertIn("잠정(대조 미완료)", provisional_text)

        # 2) 최종 갱신 저장 도중(원자적 교체 단계) 실패를 주입.
        with patch("os.replace", side_effect=OSError("disk full (injected)")):
            saved = svc._generate_daily_report(provisional=False, target_date=target)

        self.assertFalse(saved, "저장 실패는 _generate_daily_report()가 False로 알려야 합니다.")
        self.assertEqual(
            report_path.read_text(encoding="utf-8"), provisional_text,
            "저장이 실패해도 이전에 저장된 리포트 파일 내용이 훼손되지 않고 그대로 보존돼야 합니다"
            "(DailyReporter.generate()가 임시 파일 → os.replace() 경로를 실제로 사용해야 함).",
        )

    def test_eod_bundle_waits_for_preceding_analysis_and_includes_latest_result(self):
        """2026-09-17 (GPT 11차 재검토 지적 — 중간): 10차 구현은 번들
        (`_export_daily_bundle_today()`)을 다른 분석 단계와 무관하게
        독립적으로 실행·재시도해, 앞선 분석(예: shadow)이 실패한
        채로도 번들이 "예전 분석 결과"를 담아 성공해버릴 수 있었다.
        이후 분석이 재시도로 성공해도 번들은 이미 완료 처리라 다시
        만들어지지 않았다(GPT 실측 재현: shadow 분석 실패 → 번들은
        예전 내용으로 생성 성공 → shadow 분석 재시도 성공 → 번들은
        재생성 안 됨, 호출은 분석 2회·번들 1회, 대기 목록은 빈
        상태였음). 이 테스트는 (1) 선행 분석이 실패한 동안 번들이
        아예 실행되지 않는지, (2) 분석 복구 후 번들이 최신 분석
        결과를 실제로 포함해 생성되는지(호출 횟수뿐 아니라 ZIP
        내부 내용까지) 확인한다."""
        svc = self.service()

        with tempfile.TemporaryDirectory() as tmpdir:
            shadow_report_path = Path(tmpdir) / "shadow_report.txt"
            bundle_zip_path = Path(tmpdir) / "bundle.zip"

            for attr in (
                "_validate_logs_today", "_run_signal_analysis_today",
                "_run_trade_analysis_today", "_run_indicator_analysis_today",
                "_run_replay_today", "_run_bb_block_impact_today",
            ):
                setattr(svc, attr, lambda target_date: True)

            shadow_calls = []
            shadow_should_fail = {"value": True}

            def _shadow(target_date):
                shadow_calls.append(target_date)
                if shadow_should_fail["value"]:
                    return False
                shadow_report_path.write_text("LATEST SHADOW ANALYSIS", encoding="utf-8")
                return True

            svc._run_shadow_analysis_today = _shadow

            bundle_calls = []

            def _bundle(target_date):
                bundle_calls.append(target_date)
                # 실제 export_daily_bundle.py처럼, 그 시점에 존재하는
                # 분석 리포트 파일들을 그대로 묶는다.
                with zipfile.ZipFile(bundle_zip_path, "w") as zf:
                    if shadow_report_path.exists():
                        zf.write(shadow_report_path, arcname="shadow_report.txt")
                return True

            svc._export_daily_bundle_today = _bundle

            target = date(2026, 9, 7)

            # 1) 15:26 — 리포트 저장 성공, shadow 분석 실패 → 번들은
            # 선행 단계가 안 끝났으므로 이번 폴링엔 실행되면 안 된다.
            svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26))
            self.assertEqual(len(shadow_calls), 1)
            self.assertEqual(bundle_calls, [], "선행 분석이 실패한 채로 번들이 실행되면 안 됩니다.")
            self.assertEqual(svc._eod_followups_pending.get(target), {"shadow", "bundle"})
            self.assertFalse(bundle_zip_path.exists())

            # 2) 재시도 간격이 지나기 전 — 재시도 없음.
            svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26, 5))
            self.assertEqual(len(shadow_calls), 1)

            # 3) 재시도 간격 경과 + shadow 분석 성공 → 이번엔 번들도
            # 같은 폴링에서 실행되고, 최신 shadow 분석 결과를 담아야
            # 한다.
            svc._eod_retry_at[target] = svc._monotonic() - 1
            shadow_should_fail["value"] = False
            svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26, 40))
            self.assertEqual(len(shadow_calls), 2)
            self.assertEqual(len(bundle_calls), 1)
            self.assertNotIn(target, svc._eod_followups_pending)
            self.assertTrue(bundle_zip_path.exists())
            with zipfile.ZipFile(bundle_zip_path) as zf:
                content = zf.read("shadow_report.txt").decode("utf-8")
            self.assertEqual(
                content, "LATEST SHADOW ANALYSIS",
                "번들 ZIP에는 재시도로 복구된 최신 분석 결과가 포함돼야 합니다.",
            )

            # 4) 이후 폴링 — 추가 실행 없음.
            svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 27))
            self.assertEqual(len(shadow_calls), 2)
            self.assertEqual(len(bundle_calls), 1)

    def test_eod_retry_timing_accounts_for_time_spent_during_failed_step(self):
        """2026-09-17 (GPT 11차 재검토 지적 — 중간): 재시도 대기
        시각을 `_run_end_of_day_tasks()` 시작 시점에 구한 `now_mono`
        기준으로 계산해, 보고서 저장이나 분석·번들 실행 자체(동기식
        subprocess 호출 포함)에 걸린 시간이 대기 간격에서 그대로
        깎여나갔다(GPT 실측 재현: 작업 시작 0초 → 번들 실패 확인
        40초 → 등록된 재시도 시각이 30초로 이미 과거 → 다음 폴링
        50초에 바로 재실행돼 실제 대기는 10초뿐). 이 테스트는 가짜
        monotonic 시계로 "실패 확인까지 40초가 걸리는" 상황을 만들어,
        재시도 시각이 작업 시작 시점이 아니라 실패를 확인한 시점
        기준으로 계산되는지 확인한다."""
        svc = self.service()

        class _FakeMonotonic:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        fake_mono = _FakeMonotonic()
        svc._monotonic = fake_mono

        for attr in (
            "_validate_logs_today", "_run_signal_analysis_today",
            "_run_trade_analysis_today", "_run_indicator_analysis_today",
            "_run_replay_today", "_run_bb_block_impact_today",
            "_run_shadow_analysis_today",
        ):
            setattr(svc, attr, lambda target_date: True)

        bundle_calls = []
        bundle_should_fail = {"value": True}

        def _bundle(target_date):
            bundle_calls.append(target_date)
            if bundle_should_fail["value"]:
                # 실제 subprocess 호출이 오래 걸려 실패를 "확인"하기
                # 까지 40초가 걸린 상황을 모사한다.
                fake_mono.value = 40.0
                return False
            return True

        svc._export_daily_bundle_today = _bundle

        target = date(2026, 9, 7)

        # 1) t=0 — 리포트 저장 성공, 번들 실패(확인 시점엔 시계가
        # 40으로 진행돼 있음).
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26))
        self.assertEqual(len(bundle_calls), 1)
        self.assertEqual(
            svc._eod_retry_at.get(target), 70.0,
            "재시도 시각은 실패를 확인한 시점(40)에 대기 간격(30)을 더해야 합니다"
            "(작업 시작 시점 0에 더하면 30이 돼 이미 지난 시각이 됩니다).",
        )

        # 2) t=50 — 등록된 재시도 시각(70)이 아직 지나지 않았으므로
        # 재시도하면 안 된다(작업 소요 시간을 반영하지 않으면 30초
        # 뒤인 t=30에 이미 지난 시각이 돼 여기서 재시도됐을 것이다).
        fake_mono.value = 50.0
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 26, 50))
        self.assertEqual(len(bundle_calls), 1)

        # 3) t=71 — 재시도 간격 경과, 이번엔 성공.
        fake_mono.value = 71.0
        bundle_should_fail["value"] = False
        svc._run_end_of_day_tasks(datetime(2026, 9, 7, 15, 27, 11))
        self.assertEqual(len(bundle_calls), 2)
        self.assertNotIn(target, svc._eod_followups_pending)


if __name__ == "__main__":
    unittest.main()
