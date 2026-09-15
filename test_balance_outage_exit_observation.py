# -*- coding: utf-8 -*-
"""180초 감시 공백 대응 3단계 (2026-09-15) — 관측 경로 연결.

배경: 1~2단계(exit_calc.py의 순수 함수 추출, 전략별 trailing_params()
공유)까지 완료된 뒤 GPT 재검토에서 지적된 세 가지를 이번 단계에서
연결한다.

1. 관측 입력 검증: 무효 가격·평균단가·오래된 시세를 "평가 보류"로
   구분한다(domain/strategy/exit_calc.py의 classify_exit_observation_
   readiness()). `evaluate_exit_candidate()`가 돌려주는 `None`("유효한
   입력을 평가했지만 후보 없음")과 절대 같은 값으로 기록하지 않는다.
2. 장애 경로 연결: 잔고 API 장애(429 등) 재시도 대기 중에도 관측은
   지속하되(TradingService.observe_exit_candidates_during_outage()),
   주문 제출·체결 확정·highest_price의 운영 병합은 활성화하지 않는다.
3. 복구 통합 검증: 실제 run_once()에 429→대기→복구를 주입해 정상
   경로 복귀와 부수 효과(중복 관측 로그, 중복 알림 등) 중복 방지를
   확인한다.

매수/매도/보유 판단 기준(전략 파라미터)은 이번에도 전혀 건드리지
않는다 — 잔고 조회 실패 시 무엇을 "관측"할지에 대한 순수 관측 기능
추가다.
"""
from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.models import AccountBalance, MarketPrice, MarketRegime, Position
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.exit_calc import classify_exit_observation_readiness
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomHttpError
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import (
    ExitCandidateOutageLogger, TradeCsvLogger, SignalCsvLogger, build_app_logger,
)
from infra.storage.state_store import JsonStateStore


def _make_service(tmpdir: str) -> TradingService:
    """test_balance_429_fallback.py의 _make_service()와 동일한 최소 조립."""
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


def _read_csv_rows(path: str) -> list[dict]:
    import csv
    with open(path, encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


# ─────────────────────────────────────────────────────────────────
# 1. classify_exit_observation_readiness() — 순수 함수 단위 테스트
# ─────────────────────────────────────────────────────────────────
class TestClassifyExitObservationReadiness(unittest.TestCase):

    def test_valid_input_returns_empty_string(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "",
        )

    def test_price_age_exactly_at_boundary_is_still_valid(self):
        """max_price_age_seconds와 정확히 같으면(초과가 아니면) 유효합니다."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=120.0, max_price_age_seconds=120,
            ),
            "",
        )

    def test_current_price_zero_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=0, average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )

    def test_current_price_negative_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=-100, average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )

    def test_current_price_nan_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=float("nan"), average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )

    def test_current_price_infinite_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=float("inf"), average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )

    def test_average_price_zero_is_invalid_and_checked_after_current_price(self):
        """current_price가 유효해도 average_price가 0이면 무효여야 합니다
        — 둘 다 독립적으로 검증합니다(현재가만 보고 통과시키지 않음)."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=0,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_average_price",
        )

    def test_bool_is_not_accepted_as_valid_price(self):
        """bool은 int의 서브클래스라 isinstance(True, int)가 True입니다 —
        가격 필드에 bool이 들어오는 것은 명백한 손상값이므로 명시적으로
        걸러냅니다."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=True, average_price=9500,
                price_age_seconds=10.0, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )

    def test_price_age_none_means_unknown(self):
        """캐시된 시세 자체가 없으면(나이를 계산할 기준이 없음) 별도
        사유로 보류합니다 — invalid_current_price와 구분됩니다(다만
        current_price 자체도 보통 None이라 실제로는 invalid_current_price가
        먼저 걸립니다 — 이 테스트는 current_price가 유효한데 나이만 모를 때)."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=None, max_price_age_seconds=120,
            ),
            "price_age_unknown",
        )

    def test_stale_price_beyond_max_age_is_deferred(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=121.0, max_price_age_seconds=120,
            ),
            "stale_price",
        )

    def test_missing_cached_price_reason_is_invalid_current_price(self):
        """관측 경로에서 캐시된 시세 자체가 없으면 current_price로 None을
        넘기게 되는데, 이때 실제로 invalid_current_price로 분류되는지
        (None이 양수·유한 검증에서 걸러지는지) 확인합니다 — 호출부
        (trading_service.py)가 기대하는 실제 사용 패턴입니다."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=None, average_price=9500,
                price_age_seconds=None, max_price_age_seconds=120,
            ),
            "invalid_current_price",
        )


# ─────────────────────────────────────────────────────────────────
# 2. TradingService.observe_exit_candidates_during_outage() — 통합 단위 테스트
# ─────────────────────────────────────────────────────────────────
class TestObserveExitCandidatesDuringOutage(unittest.TestCase):

    def test_no_op_when_no_cached_balance(self):
        """프로세스 시작 직후 등 성공 조회된 잔고가 아직 없으면 아무 것도
        기록하지 않고 예외 없이 반환합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            self.assertIsNone(svc.cached_balance)
            svc.observe_exit_candidates_during_outage()  # 예외 없어야 함
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(rows, [])

    def test_no_op_when_logger_is_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            svc.exit_candidate_outage_logger = None
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol="005930", quantity=10, average_price=10000)],
            )
            svc.observe_exit_candidates_during_outage()  # 예외 없어야 함

    def test_zero_quantity_positions_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol="005930", quantity=0, average_price=10000)],
            )
            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(rows, [])

    def test_missing_cached_price_is_deferred_not_none(self):
        """시세 캐시가 전혀 없으면 "평가 보류(invalid_current_price)"로
        기록해야지, evaluate_exit_candidate()의 None(후보 없음)과 같은
        의미로 취급하면 안 됩니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=10000)],
            )
            # cached_market_prices에 아무것도 없음 — 시세 캐시 없음 재현
            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], symbol)
            self.assertEqual(rows[0]["status"], "deferred")
            self.assertEqual(rows[0]["reason"], "invalid_current_price")

    def test_stale_cached_price_is_deferred(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=10000)],
            )
            old_loaded_at = datetime.now() - timedelta(seconds=999)
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=10500, reference_price=10000,
                previous_close=9800, timestamp=old_loaded_at,
            )
            svc.cached_market_price_loaded_at[symbol] = old_loaded_at
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "deferred")
            self.assertEqual(rows[0]["reason"], "stale_price")

    def test_regime_not_cached_is_deferred(self):
        """가격은 유효·신선하지만 regime이 아직 캐시된 적 없으면(UNKNOWN)
        새로 판정을 시도(브로커 호출)하지 않고 평가 보류로만 기록합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=10000)],
            )
            now = datetime.now()
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=10500, reference_price=10000,
                previous_close=9800, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            # cached_regime에 아무것도 없음 — regime 미확보 재현

            with mock.patch.object(
                svc.broker, "get_daily_prices",
                side_effect=AssertionError("장애 관측 경로는 브로커를 호출하면 안 됩니다"),
            ):
                svc.observe_exit_candidates_during_outage()

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "deferred")
            self.assertEqual(rows[0]["reason"], "regime_not_cached")

    def test_valid_input_with_stop_loss_triggered_is_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            avg = 10000
            current = int(avg * (1 - svc.settings.strategy.stop_loss_pct / 100)) - 10  # 손절가 하회
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=avg)],
            )
            now = datetime.now()
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=current, reference_price=avg,
                previous_close=avg, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "STOP_LOSS")
            self.assertEqual(rows[0]["stop_loss_triggered"], "True")

    def test_valid_input_no_candidate_is_distinguished_from_deferred(self):
        """유효한 입력을 평가했지만 손절·트레일링 모두 미충족이면
        status="no_candidate"로 기록해야 합니다 — "deferred"와 절대
        같은 값이면 안 됩니다(이번 단계의 핵심 요구사항)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            avg = 10000
            current = avg + 10  # 손절도 트레일링도 아닌 평범한 보합
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=avg)],
            )
            now = datetime.now()
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=current, reference_price=avg,
                previous_close=avg, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "no_candidate")
            self.assertNotEqual(rows[0]["status"], "deferred")

    def test_never_submits_orders_or_mutates_highest_price(self):
        """관측 경로는 순수 조회·기록만 수행합니다 — place_order 호출도,
        self._highest_price 갱신도 없어야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            avg = 10000
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=avg)],
            )
            now = datetime.now()
            # highest_price보다 훨씬 높은 시세 — 병합됐다면 값이 바뀌었을 것
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=avg + 5000, reference_price=avg,
                previous_close=avg, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL
            svc._highest_price[symbol] = 0  # 관측 전 상태 고정

            with mock.patch.object(
                svc.broker, "place_order",
                side_effect=AssertionError("장애 관측 경로는 주문을 제출하면 안 됩니다"),
            ):
                svc.observe_exit_candidates_during_outage()

            self.assertEqual(svc._highest_price.get(symbol, 0), 0, "highest_price가 관측만으로 갱신되면 안 됩니다.")

    def test_exception_in_one_symbol_does_not_stop_others(self):
        """한 종목의 관측이 실패해도(fail-open) 다른 종목 관측은 계속되고,
        예외가 밖으로 전파되지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            good_symbol, bad_symbol = "005930", "000660"
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[
                    Position(symbol=bad_symbol, quantity=10, average_price=10000),
                    Position(symbol=good_symbol, quantity=10, average_price=10000),
                ],
            )
            now = datetime.now()
            for sym in (good_symbol, bad_symbol):
                svc.cached_market_prices[sym] = MarketPrice(
                    symbol=sym, current_price=10500, reference_price=10000,
                    previous_close=9800, timestamp=now,
                )
                svc.cached_market_price_loaded_at[sym] = now
                svc.cached_regime[sym] = MarketRegime.NEUTRAL

            original = svc._observe_exit_candidate_for_symbol

            def _boom(symbol, position):
                if symbol == bad_symbol:
                    raise RuntimeError("의도적 장애 주입")
                return original(symbol, position)

            with mock.patch.object(svc, "_observe_exit_candidate_for_symbol", side_effect=_boom):
                svc.observe_exit_candidates_during_outage()  # 예외가 밖으로 나오면 안 됨

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            symbols_logged = {r["symbol"] for r in rows}
            self.assertIn(good_symbol, symbols_logged, "장애 종목과 무관하게 다른 종목은 계속 관측돼야 합니다.")
            self.assertNotIn(bad_symbol, symbols_logged)


# ─────────────────────────────────────────────────────────────────
# 3. wait_out_balance_outage() — 반복 관측 + 총 대기시간 보존
# ─────────────────────────────────────────────────────────────────
class TestWaitOutBalanceOutage(unittest.TestCase):

    def test_total_wait_time_unchanged_and_observes_repeatedly(self):
        """총 대기시간(기존 180초 상당)은 그대로 유지하되, 그 안에서
        여러 번 관측 기회가 있어야 합니다 — "재시도 대기 중 관측은
        지속한다"는 요구사항의 핵심 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=10000)],
            )
            now = datetime.now()
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=10500, reference_price=10000,
                previous_close=9800, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            slept_seconds = []

            async def _fake_sleep(seconds):
                slept_seconds.append(seconds)

            with mock.patch("asyncio.sleep", side_effect=_fake_sleep):
                asyncio.run(
                    svc.wait_out_balance_outage(total_seconds=40, observe_interval_seconds=15)
                )

            self.assertAlmostEqual(sum(slept_seconds), 40.0)
            self.assertEqual(slept_seconds, [15, 15, 10])

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            # 15초 간격 x 3구간(15,15,10) = 관측 3회
            self.assertEqual(len(rows), 3, "매 구간 시작마다 관측이 한 번씩 있어야 합니다.")

    def test_never_calls_broker_during_wait(self):
        """대기 중 잔고 API를 다시 호출하면 429를 재유발할 위험이 있어
        절대 호출하면 안 됩니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            svc.cached_balance = AccountBalance(cash=0, total_asset=0, positions=[])

            with mock.patch.object(
                svc.broker, "get_account_balance",
                side_effect=AssertionError("대기 중 잔고 API를 호출하면 안 됩니다"),
            ), mock.patch("asyncio.sleep", new=mock.AsyncMock()):
                asyncio.run(
                    svc.wait_out_balance_outage(total_seconds=20, observe_interval_seconds=10)
                )


# ─────────────────────────────────────────────────────────────────
# 4. run_once() 통합 검증 — 429 → (관측 지속) 대기 → 복구
# ─────────────────────────────────────────────────────────────────
class TestRunOnceOutageRecoveryIntegration(unittest.TestCase):
    """trading_loop()(app/main.py)의 except 분기가 실제로 하는 일을
    그대로 재현합니다: run_once()가 429로 실패 → wait_out_balance_
    outage() 호출(짧은 값으로 대체, sleep은 모킹) → 잔고 복구 후
    run_once() 재호출 → 정상 경로 복귀, 부수 효과 중복 없음.
    """

    def test_429_then_wait_with_observation_then_recovers_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = svc.targets[0]

            # 이 종목을 보유 중으로 만들고, 관측에 쓸 시세/장세/잔고
            # 캐시를 "장애 발생 직전 마지막 정상 사이클"처럼 미리
            # 채워둔다 — 실제로도 outage는 첫 폴링이 아니라 몇 차례
            # 정상 사이클 이후에 발생하는 것이 일반적인 시나리오다.
            # (svc.cached_balance는 TradingService 자신의 캐시이고,
            # svc.broker._positions는 MockBroker 내부 상태 — 다음 성공
            # 조회에서 무엇이 반환될지를 결정할 뿐 서로 자동 동기화되지
            # 않으므로 둘 다 채워야 한다.)
            svc.broker._positions[symbol] = Position(symbol=symbol, quantity=10, average_price=10000)
            now = datetime.now()
            svc.cached_balance = AccountBalance(
                cash=0, total_asset=0,
                positions=[Position(symbol=symbol, quantity=10, average_price=10000)],
            )
            # balance_refresh_seconds(기본 180초)보다 오래된 것으로 만들어
            # _get_balance_with_cache()가 실제로 새 조회를 시도하게 한다
            # (그래야 모킹한 429가 실제로 발동한다) — 시세 캐시는 별개로
            # "막 성공한 관측"처럼 신선하게 유지한다.
            svc.cached_balance_loaded_at = now - timedelta(seconds=200)
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=10500, reference_price=10000,
                previous_close=9800, timestamp=now,
            )
            svc.cached_market_price_loaded_at[symbol] = now
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            place_order_calls = []
            original_place_order = svc.broker.place_order

            def _track_place_order(*args, **kwargs):
                place_order_calls.append((args, kwargs))
                return original_place_order(*args, **kwargs)

            error = _make_429_error()
            with mock.patch.object(svc.broker, "get_account_balance", side_effect=error), \
                 mock.patch.object(svc.broker, "place_order", side_effect=_track_place_order):
                with self.assertRaises(KiwoomHttpError):
                    asyncio.run(svc.run_once())

            # 이 시점에는 아직 관측이 한 번도 안 일어남 — run_once() 자체는
            # 429를 그대로 전파할 뿐(1~2단계 회귀 유지, test_balance_429_
            # fallback.py와 동일한 계약), 대기·관측은 trading_loop()가
            # (여기서는 이 테스트가) 별도로 호출한다.
            rows_before = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(rows_before, [])

            with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
                asyncio.run(svc.wait_out_balance_outage(total_seconds=30, observe_interval_seconds=10))

            rows_during_outage = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows_during_outage), 3, "30초/10초 간격이면 관측 3회가 있어야 합니다.")
            self.assertTrue(
                all(r["symbol"] == symbol for r in rows_during_outage)
            )
            self.assertEqual(place_order_calls, [], "장애 대기 중에는 어떤 주문도 제출되면 안 됩니다.")

            # 복구: 잔고 API가 다시 정상 응답 — run_once()가 예외 없이
            # 끝까지 돌고, 관측 로그가 recovery 이후 중복으로 쌓이지
            # 않는지(다음 정상 사이클은 이 로거를 아예 건드리지 않음)도
            # 함께 확인한다.
            asyncio.run(svc.run_once())
            rows_after_recovery = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(
                len(rows_after_recovery), len(rows_during_outage),
                "정상 복구된 run_once()는 exit_candidate_outage 로그에 아무 것도 추가하지 않아야 합니다"
                "(이 로거는 장애 관측 전용 — 정상 경로는 기존 손절/트레일링 판정을 그대로 씁니다).",
            )


if __name__ == "__main__":
    unittest.main()
