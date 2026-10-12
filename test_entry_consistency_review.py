"""Offline regression tests for entry consistency and evidence/report integrity."""
import asyncio
import csv
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from test_run_once_integration import build_minimal_settings
from config.settings import load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import Signal, SignalType, MarketRegime
from domain.market_regime.minute_analyzer import MinuteAnalysis, MinuteDataResult
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.strategy.entry_vwap_guard import entry_vwap_block_reason
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.storage.run_baseline import capture_source_fingerprint, capture_run_baseline, save_effective_config_snapshot
from utils.time_utils import KST_TZ
from domain.cost_model import CostModel
from analyze_trades import analyze, pair_trades

FIXED_MARKET_TIME = datetime(2026, 10, 8, 10, 0, tzinfo=KST_TZ)

def build_service(tmpdir: str) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    # MockBroker의 초기 현금(1,000,000)과 order_cash_per_trade(1,000,000)가
    # 같아서 기본 min_cash_buffer(100,000)로는 어떤 주문이든 RISK_LIMIT에
    # 걸려버림 — 이 파일은 Candidate A gate 자체의 동작을 보고 싶은
    # 것이므로 buffer를 0으로 낮춤(5부 precedence 테스트는 이 값을
    # 호출부에서 다시 크게 덮어써 의도적으로 RISK_LIMIT을 유도함).
    object.__setattr__(settings.risk, "min_cash_buffer", 0)
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

def make_ma(**overrides) -> MinuteAnalysis:
    defaults = dict(
        vwap=100000.0, price_above_vwap=True, low_rising=False,
        pullback_pct=0.0, is_valid_pullback=False,
        change_rate_pct=0.0, is_valid_change_rate=False,
        rebound_pct=0.0, is_valid_rebound=False,
        trading_value=1_000_000_000, is_valid_trading_value=True,
        day_high=101000, day_low=99000, is_valid_pulldown=False,
        ma5_above_ma20=True, ma5=100500.0, ma20=99800.0,
        is_v_rebound=False, v_fail_reason="", v_bottom_k=0,
        v_drop_pct=0.0, v_rise_pct=0.0, v_volume_ratio=0.0,
        v_bottom_spike=False, v_ma5_rising=False,
        rebound_volume_spike=False, rebound_volume_ratio=0.0,
        upside_to_recent_high_pct=5.0,
        is_pulldown_recovery=False, pr_low_turning=False, pr_volume_expanding=False,
        is_slow_v_rebound=False, slow_v_drop_pct=0.0, slow_v_rise_pct=0.0, slow_v_bottom_k=0,
    )
    defaults.update(overrides)
    return MinuteAnalysis(**defaults)

class EntryGuardTests(unittest.TestCase):
    def test_price_boundaries_and_invalid_evidence(self):
        self.assertEqual(entry_vwap_block_reason(100, 101, 101), "")
        for prices in ((100, 101), (101, 100), (99, 101), (101, 99)):
            self.assertEqual(entry_vwap_block_reason(100, *prices), "ENTRY_BELOW_VWAP")
        for bad in (None, 0, -1, float("nan"), float("inf"), "100", True):
            for i in range(3):
                values = [100, 101, 101]
                values[i] = bad
                self.assertEqual(entry_vwap_block_reason(*values), "ENTRY_VWAP_UNAVAILABLE")

    def test_config_default_rollout_and_type_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_minimal_settings(tmp).trading
            self.assertFalse(cfg.require_entry_above_vwap)
            for bad in ("false", 1, None):
                with self.assertRaises(ValueError):
                    replace(cfg, require_entry_above_vwap=bad)
        self.assertTrue(load_settings().trading.require_entry_above_vwap)

    def test_order_gate_including_sub_tolerance_vwap_crossing(self):
        for enabled, decision, live, expected in (
            (True, 100100, 99900, "ENTRY_BELOW_VWAP"),
            (True, 99900, 100100, "ENTRY_BELOW_VWAP"),
            (True, 100100, None, "ENTRY_VWAP_UNAVAILABLE"),
            (True, 100100, 100100, ""),
            (False, 99900, None, ""),
        ):
            with self.subTest(enabled=enabled, decision=decision, live=live):
                with tempfile.TemporaryDirectory() as tmp:
                    service = build_service(tmp)
                    object.__setattr__(service.settings.trading, "require_entry_above_vwap", enabled)
                    symbol = "005930"
                    service.broker._prices[symbol] = live or decision
                    if live is not None:
                        service.cached_market_prices[symbol] = service.broker.get_market_price(symbol)
                        service.cached_market_price_loaded_at[symbol] = datetime.now()
                    balance = service.broker.get_account_balance()
                    before = copy.deepcopy(service.state)
                    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME), patch.object(service.broker, "place_order", wraps=service.broker.place_order) as order:
                        result = service._try_buy(symbol, decision, balance, minute_analysis=make_ma())
                    self.assertEqual(result or "", expected)
                    if expected:
                        order.assert_not_called()
                        self.assertEqual(service.state, before)
                        self.assertFalse(service._pending_buy_side_effects)
                        self.assertNotIn(symbol, service.broker._positions)
                    else:
                        order.assert_called_once()


    def test_process_symbol_revalidation_crossing_logs_block_without_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = build_service(tmp)
            object.__setattr__(service.settings.trading, "require_entry_above_vwap", True)
            symbol = "005930"
            service.broker._prices[symbol] = 100100
            service.cached_market_prices[symbol] = service.broker.get_market_price(symbol)
            service.cached_market_price_loaded_at[symbol] = datetime.now()
            service.broker._prices[symbol] = 99900  # only 0.20%, below existing 0.5% tolerance
            data = MinuteDataResult(analysis=make_ma(), entry_safe=True, source="LIVE",
                                    reason="", latest_bar_timestamp="20261008100000", age_seconds=5)
            signal = Signal(type=SignalType.BUY, reason="test BUY")
            before = copy.deepcopy(service.state)
            with patch.object(service, "_get_minute_analysis", return_value=data), patch.object(service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "test")), patch("domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal", return_value=signal), patch("domain.strategy.neutral_strategy.NeutralStrategy.generate_signal", return_value=signal), patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME), patch.object(service.broker, "place_order", wraps=service.broker.place_order) as order:
                asyncio.run(service._process_symbol(symbol, service.broker.get_account_balance()))
            order.assert_not_called()
            self.assertEqual(service.cached_market_prices[symbol].current_price, 99900)
            self.assertEqual(service.state, before)
            with open(service.settings.storage.signal_log_file, encoding="utf-8-sig") as f:
                row = list(csv.DictReader(f))[-1]
            self.assertEqual(row["order_block_reason"], "ENTRY_BELOW_VWAP")
            self.assertEqual(row["final_decision"], "BLOCKED")


class FingerprintTests(unittest.TestCase):
    def make_root(self, root):
        for name in ("app", "config", "domain", "infra", "utils"):
            (root / name).mkdir()
        (root / "domain/main.py").write_text("x = 1\n")
        (root / "requirements.txt").write_text("requests==2.32.3\n")

    def test_change_add_delete_and_no_config_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            first = capture_source_fingerprint(tmp)
            self.assertTrue(first["complete"])
            (root / "config/settings.yaml").write_text("secret: NEVER_EXPORT_THIS")
            self.assertEqual(first, capture_source_fingerprint(tmp))
            (root / "domain/main.py").write_text("x = 2\n")
            changed = capture_source_fingerprint(tmp)
            self.assertNotEqual(first["sha256"], changed["sha256"])
            (root / "domain/local.py").write_text("y = 2\n")
            self.assertNotEqual(changed["sha256"], capture_source_fingerprint(tmp)["sha256"])
            (root / "domain/local.py").unlink()
            self.assertEqual(changed, capture_source_fingerprint(tmp))
            settings = build_minimal_settings(tmp)
            snap = save_effective_config_snapshot(capture_run_baseline(settings), settings, str(root / "snap"), repo_root=tmp)
            payload = json.loads(snap.read_text())
            self.assertEqual(payload["source_fingerprint"], changed)
            self.assertNotIn("NEVER_EXPORT_THIS", snap.read_text())

    def test_missing_file_or_symlink_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            (root / "requirements.txt").unlink()
            self.assertFalse(capture_source_fingerprint(tmp)["complete"])
            (root / "requirements.txt").write_text("")
            try:
                (root / "domain/link.py").symlink_to(root / "domain/main.py")
            except OSError:
                self.skipTest("symlinks unavailable")
            result = capture_source_fingerprint(tmp)
            self.assertFalse(result["complete"])
            self.assertNotIn("domain/link.py", result["files"])


class ReportTests(unittest.TestCase):
    def row(self, side, price, time, qty=10):
        return dict(symbol="005930", side=side, price=str(price), quantity=str(qty),
                    timestamp=f"2026-10-08T{time}:00", accepted="True")

    def test_costs_use_buy_notional_and_label_order_estimates(self):
        rows = [self.row("BUY", 10000, "10:00"), self.row("SELL", 10100, "10:05")]
        text = analyze(rows, date(2026, 10, 8), date(2026, 10, 8), cost_model=CostModel())
        self.assertIn("체결 확정이 아닙니다", text)
        self.assertIn("+650원", text)
        self.assertIn("+100원", text)
        self.assertIn("Base (0.35%)", text)
        self.assertNotIn("총 체결 건수", text)

    def test_pairing_never_uses_later_buy_or_mismatched_quantity(self):
        buy = self.row("BUY", 10000, "10:00")
        sell = self.row("SELL", 10100, "10:05")
        orphan = self.row("SELL", 12000, "09:30")
        pairs = pair_trades([sell, orphan, buy])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["sell_price"], 10100)
        for qty in (5, 0, -10, "bad"):
            self.assertEqual(pair_trades([buy, dict(sell, quantity=qty)]), [])
        self.assertEqual(pair_trades([buy, dict(sell, timestamp="2026-10-09T10:05:00")]), [])
        self.assertEqual(pair_trades([dict(buy, accepted="False"), sell]), [])
        self.assertEqual(pair_trades([dict(buy, timestamp="bad"), sell]), [])


if __name__ == "__main__":
    unittest.main()
