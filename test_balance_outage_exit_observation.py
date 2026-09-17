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
from domain.models import AccountBalance, MarketPrice, MarketRegime, OrderSide, Position
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


class _FrozenDateTime(datetime):
    """실제 `datetime.now()`를 오버라이드해 시간을 인위적으로
    전진시키는 테스트 전용 가짜 시계 — 2026-09-15 보완 (GPT 재검토
    4번 지적 반영: 기존 통합 테스트는 `asyncio.sleep`을 무력화만 하고
    시간 자체를 전진시키지 않아 "장애 대기 중 시세 신선도가 실제로
    달라지는지"를 전혀 검증하지 못했습니다).

    `domain.service.trading_service`의 `datetime` 이름만 이 클래스로
    바꿔치기하면(`mock.patch("domain.service.trading_service.datetime",
    _FrozenDateTime)`), 그 모듈 안에서 호출되는 `datetime.now()`만 이
    클래스의 `now()`를 타고, 나머지(뺄셈·비교·`isoformat()`,
    `fromisoformat()` 등)는 표준 `datetime`과 완전히 동일하게
    동작합니다 — `_frozen`에 담긴 값 자체가 평범한 실제 datetime
    인스턴스이기 때문입니다.
    """

    _frozen: "datetime | None" = None

    @classmethod
    def set_now(cls, value: datetime) -> None:
        cls._frozen = value

    @classmethod
    def advance(cls, seconds: float) -> None:
        cls._frozen = cls._frozen + timedelta(seconds=seconds)

    @classmethod
    def now(cls, tz=None) -> datetime:  # noqa: D102 - datetime.now() 표준 시그니처
        return cls._frozen


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

    def test_negative_price_age_is_invalid(self):
        """2026-09-15 보완 (GPT 재검토 3번 지적 반영): 시스템 시계가
        뒤로 보정되면(NTP 등) `(now - loaded_at)`이 음수가 될 수
        있습니다. 최초 구현은 `음수 > max_price_age_seconds`가 항상
        거짓이라 "유효"로 잘못 통과시켰습니다(재현: `-60`이 빈 문자열
        반환) — 미래 시각의 가격을 신선하다고 인정하면 안 되므로
        명시적으로 무효 처리해야 합니다."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=-60, max_price_age_seconds=120,
            ),
            "invalid_price_age",
        )

    def test_negative_infinite_price_age_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=float("-inf"), max_price_age_seconds=120,
            ),
            "invalid_price_age",
        )

    def test_bool_price_age_is_invalid(self):
        """bool은 int의 서브클래스라 `isinstance(True, int)`가 참입니다
        — 나이 필드에 bool이 들어오는 것도 가격 필드와 동일하게 명백한
        손상값이므로 명시적으로 걸러냅니다(재현: `True`가 빈 문자열
        반환했었음 — `True >= 0`이 참이라 통과됨)."""
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=True, max_price_age_seconds=120,
            ),
            "invalid_price_age",
        )

    def test_nan_price_age_is_invalid(self):
        self.assertEqual(
            classify_exit_observation_readiness(
                current_price=10000, average_price=9500,
                price_age_seconds=float("nan"), max_price_age_seconds=120,
            ),
            "invalid_price_age",
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

    def _seed_observed_position(self, svc, symbol, quantity, average_price):
        """`_last_observed_positions`를 실제 운영 경로(`_get_balance_
        with_cache()`가 성공할 때마다 호출하는 `_update_last_observed_
        positions()`)와 동일한 모양으로 직접 채웁니다.

        2026-09-15 보완 (GPT 재검토 2번 지적 반영): 이제 감시 대상
        종목은 `self.cached_balance`가 아니라 이 스냅샷과 로컬 상태
        (`entry_time_by_symbol` 등)로 결정됩니다 — 이전처럼
        `svc.cached_balance`만 채우면 `_symbols_needing_outage_
        observation()`이 이 종목을 아예 찾지 못해 관측 자체가 0건이
        되는, 바로 이번 재검토가 지적한 결함을 테스트가 재도입하게
        됩니다.
        """
        svc._last_observed_positions[symbol] = Position(
            symbol=symbol, quantity=quantity, average_price=average_price,
        )

    def test_no_op_when_no_cached_balance(self):
        """프로세스 시작 직후 등 성공 조회된 잔고가 아직 없고, 로컬에도
        추적 중인 종목이 전혀 없으면 아무 것도 기록하지 않고 예외 없이
        반환합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            self.assertIsNone(svc.cached_balance)
            self.assertEqual(svc._last_observed_positions, {})
            svc.observe_exit_candidates_during_outage()  # 예외 없어야 함
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(rows, [])

    def test_no_op_when_logger_is_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            svc.exit_candidate_outage_logger = None
            self._seed_observed_position(svc, "005930", 10, 10000)
            svc.observe_exit_candidates_during_outage()  # 예외 없어야 함

    def test_zero_quantity_positions_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            # 수량이 0인 포지션은 _update_last_observed_positions()의
            # 실제 필터(quantity > 0)와 동일하게 스냅샷에 아예 들어가지
            # 않습니다 — 로컬 추적 상태도 없으므로 감시 대상 자체가
            # 비어야 합니다.
            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(rows, [])

    def test_missing_cached_price_is_deferred_not_none(self):
        """2026-09-15 보완 (GPT 재검토 1번 지적 반영): 시세 캐시가 전혀
        없어도 이제는 `_get_market_price_with_cache()`를 통해 실제로
        새 시세를 조회합니다 — 그 조회가 성공하면(MockBroker는 항상
        성공) "평가 보류"가 아니라 정상적으로 평가되어야 합니다. 캐시
        부재 자체가 곧 무효 입력이라는 이전 가정은 "장애 중에도 새
        시세를 관측해야 한다"는 요구사항과 정면으로 배치되므로 이제는
        유효합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            self._seed_observed_position(svc, symbol, 10, 10000)
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL
            # cached_market_prices에 아무것도 없음 — 시세 캐시 없음 재현.
            # MockBroker.get_market_price()는 항상 성공하므로 실제로
            # 새로 조회되어 정상 평가로 이어져야 합니다.
            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], symbol)
            self.assertIn(rows[0]["status"], ("no_candidate", "STOP_LOSS", "TRAILING"))
            self.assertNotEqual(rows[0]["status"], "deferred")

    def test_price_fetch_failure_with_no_cache_is_deferred(self):
        """시세 캐시가 없는 상태에서 그 첫 조회 시도 자체가 실패하면
        (예: 브로커 장애가 잔고 API와 겹침) "평가 보류
        (price_fetch_failed)"로 기록해야지, evaluate_exit_candidate()의
        None(후보 없음)과 같은 의미로 취급하면 안 됩니다 — GPT 재검토
        1번 지적이 요구한 "시세 API 자체의 독립적 실패 처리"의 핵심
        회귀 방지 테스트입니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            self._seed_observed_position(svc, symbol, 10, 10000)
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            with mock.patch.object(
                svc.broker, "get_market_price",
                side_effect=RuntimeError("시세 API 장애"),
            ):
                svc.observe_exit_candidates_during_outage()

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "deferred")
            self.assertEqual(rows[0]["reason"], "price_fetch_failed")

    def test_position_unconfirmed_when_no_snapshot_available(self):
        """2026-09-15 보완 (GPT 재검토 2번 지적 반영): 주문 접수 직후처럼
        `_last_observed_positions`에 해당 종목의 마지막 성공 스냅샷이
        아직 없어도, 로컬에 이미 알려진 종목(`entry_time_by_symbol`)이면
        감시 대상에는 포함하되 평단을 지어내지 않고 "평가 보류
        (position_unconfirmed)"로만 기록해야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            svc.state.entry_time_by_symbol[symbol] = datetime.now().isoformat()
            self.assertNotIn(symbol, svc._last_observed_positions)

            svc.observe_exit_candidates_during_outage()

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], symbol)
            self.assertEqual(rows[0]["status"], "deferred")
            self.assertEqual(rows[0]["reason"], "position_unconfirmed")

    def test_stale_cached_price_is_deferred(self):
        """캐시가 오래됐어도(`price_refresh_seconds` 경과) 재조회
        시도는 반드시 이뤄집니다 — 다만 그 재조회 자체가 실패해야만
        (대체할 캐시로 폴백) 진짜 "오래된 시세"로 남습니다. 재조회가
        성공하면(정상 케이스) 캐시는 새로 갱신되어 더 이상 stale이
        아니므로, 이 테스트는 브로커 재조회를 의도적으로 실패시켜
        "재조회도 실패했고 대체할 캐시도 오래됐다"는 시나리오를
        재현합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            self._seed_observed_position(svc, symbol, 10, 10000)
            old_loaded_at = datetime.now() - timedelta(seconds=999)
            svc.cached_market_prices[symbol] = MarketPrice(
                symbol=symbol, current_price=10500, reference_price=10000,
                previous_close=9800, timestamp=old_loaded_at,
            )
            svc.cached_market_price_loaded_at[symbol] = old_loaded_at
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            with mock.patch.object(
                svc.broker, "get_market_price",
                side_effect=RuntimeError("재조회 실패 — 기존 캐시로 폴백"),
            ):
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
            self._seed_observed_position(svc, symbol, 10, 10000)
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
            self._seed_observed_position(svc, symbol, 10, avg)
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
            self._seed_observed_position(svc, symbol, 10, avg)
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
            self._seed_observed_position(svc, symbol, 10, avg)
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
            self._seed_observed_position(svc, bad_symbol, 10, 10000)
            self._seed_observed_position(svc, good_symbol, 10, 10000)
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
# 2.5. _get_market_price_with_cache() — 재조회 실패 백오프 +
#      price_source 구분 (GPT 3차 재검토 1번 지적, "우선 수정")
# ─────────────────────────────────────────────────────────────────
class TestMarketPriceFetchBackoff(unittest.TestCase):
    """GPT 3차 재검토 재현: 시세 재조회가 429로 계속 실패해도, 기존
    구현은 재조회 주기(예: 60초)가 지나기만 하면 매번 다시 브로커를
    호출했다 — 관측 간격(15초)마다 반복해서 429를 유발(재현: 시세
    나이 60/75/90초 세 번 모두 재호출·429). 이제는 실패 시각을
    기억해 `market_price_retry_backoff_seconds` 동안 재시도 자체를
    건너뛰어야 한다.

    2026-09-15 4차 보완 (GPT 재검토 2번 지적 반영): 백오프 게이트가
    monotonic(`svc._monotonic`) 기준으로 바뀌었으므로, 이 클래스의
    모든 테스트는 `_FrozenDateTime`(캐시 나이·로그 타임스탬프용)과는
    별도로 `clock` 딕셔너리로 가짜 monotonic 시계를 함께 제어한다 —
    `_FrozenDateTime.advance(N)`을 호출할 때마다 `clock["t"] += N`도
    함께 해 두 시계를 같은 속도로 전진시킨다(둘을 일부러 어긋나게
    만드는 시스템 시계 점프 테스트는 아래 별도 테스트로 분리).
    """

    def test_repeated_failures_within_backoff_do_not_retry_broker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            def _advance(seconds):
                _FrozenDateTime.advance(seconds)
                clock["t"] += seconds

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)
                svc.cached_market_prices[symbol] = MarketPrice(
                    symbol=symbol, current_price=10500, reference_price=10000,
                    previous_close=9800, timestamp=frozen_start,
                )
                svc.cached_market_price_loaded_at[symbol] = frozen_start - timedelta(
                    seconds=svc.settings.trading.price_refresh_seconds + 1
                )

                call_count = {"n": 0}

                def _always_429(_symbol):
                    call_count["n"] += 1
                    raise _make_429_error()

                with mock.patch.object(svc.broker, "get_market_price", side_effect=_always_429):
                    # 1회차 — 재조회 주기가 지났으므로 실제로 브로커를
                    # 호출하고 실패, 캐시로 폴백.
                    price1 = svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1)
                    self.assertEqual(svc._market_price_fetch_outcome[symbol], "cache_after_failure")
                    self.assertEqual(price1.current_price, 10500)

                    # 2, 3회차 — 관측 간격(15초)만큼만 흘러 백오프
                    # (기본 60초) 안이므로, 재조회 주기는 다시 지났어도
                    # 브로커를 또 호출하면 안 된다(재현: 기존엔 매번
                    # 호출·429).
                    _advance(15)
                    price2 = svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 재조회를 시도하면 안 됩니다.")
                    self.assertEqual(svc._market_price_fetch_outcome[symbol], "cache_after_failure")

                    _advance(15)
                    price3 = svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 재조회를 시도하면 안 됩니다.")

                    # 백오프(60초)가 지나면 다시 시도해야 한다.
                    _advance(40)  # 누적 70초 경과
                    svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 2, "백오프가 끝나면 다시 재시도해야 합니다.")

    def test_no_cache_and_in_backoff_raises_without_calling_broker(self):
        """캐시가 아예 없는 상태에서 첫 시도가 실패하면, 그 뒤 백오프
        기간 안의 재시도는 브로커를 다시 부르지 않고 바로 예외를
        올려야 한다(대체할 캐시가 없으므로 "조회 불가"는 동일하지만,
        불필요한 API 호출은 없어야 한다)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)
                call_count = {"n": 0}

                def _always_429(_symbol):
                    call_count["n"] += 1
                    raise _make_429_error()

                with mock.patch.object(svc.broker, "get_market_price", side_effect=_always_429):
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1)
                    self.assertEqual(svc._market_price_fetch_outcome[symbol], "unavailable")

                    _FrozenDateTime.advance(15)
                    clock["t"] += 15
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 캐시가 없어도 재호출하면 안 됩니다.")

    def test_success_after_failure_clears_backoff_and_records_fetched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)

                with mock.patch.object(
                    svc.broker, "get_market_price", side_effect=_make_429_error(),
                ):
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)

                # 백오프가 끝난 뒤 조회가 성공하면 즉시 "fetched"로
                # 정상 반영되고, 실패 기록은 지워져야 한다(같은 심볼에
                # 대한 다음 백오프 판정에 영향을 주면 안 됨).
                _FrozenDateTime.advance(
                    svc.settings.trading.market_price_retry_backoff_seconds + 1
                )
                clock["t"] += svc.settings.trading.market_price_retry_backoff_seconds + 1
                price = svc._get_market_price_with_cache(symbol)
                self.assertEqual(svc._market_price_fetch_outcome[symbol], "fetched")
                self.assertNotIn(symbol, svc._market_price_fetch_failed_at)
                self.assertNotIn(symbol, svc._market_price_fetch_next_retry_at)

    def test_slow_failure_schedules_retry_from_when_failure_was_caught(self):
        """GPT 4차 재검토 2번 지적("우선 수정") 재현: 조회 자체가
        느리게(예: 10초) 실패하면, 3차 보완은 그 호출을 "시작한
        시점"을 실패 시각으로 기록해 백오프가 그만큼 짧아졌다(재현:
        10초 걸려 실패 → 그 실패 이후 50초만 지나도 재시도 허용,
        설정 60초보다 짧음). 이제는 실패를 "잡은 시점"(느린 호출이
        실제로 끝난 뒤) 기준으로 monotonic 재시도 허용 시각을
        예약해야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            backoff = svc.settings.trading.market_price_retry_backoff_seconds

            call_count = {"n": 0}

            def _slow_429(_symbol):
                call_count["n"] += 1
                clock["t"] += 10  # 조회 자체가 10초 걸림을 재현
                raise _make_429_error()

            with mock.patch.object(svc.broker, "get_market_price", side_effect=_slow_429):
                # 호출 시작 시각 t=0, 실패를 "잡는" 시각은 10초 뒤인 t=10.
                with self.assertRaises(Exception):
                    svc._get_market_price_with_cache(symbol)
                self.assertEqual(call_count["n"], 1)

                # 실패를 잡은 시점(10) 기준으로 backoff가 아직 안
                # 끝난 시각(호출 시작 기준으로는 10+backoff-1) —
                # 재시도가 허용되면 안 된다. 이전 버그(호출 시작 시점
                # 기준)라면 여기서 이미 backoff가 끝난 것으로 오판된다
                # (0+backoff-1 < 10+backoff-1).
                clock["t"] = 10 + backoff - 1
                with self.assertRaises(Exception):
                    svc._get_market_price_with_cache(symbol)
                self.assertEqual(
                    call_count["n"], 1,
                    "실패를 '잡은 시점'(호출 시작이 아니라) 기준으로 아직 백오프가 끝나지 않았습니다.",
                )

                # 실패를 잡은 시점으로부터 정확히 backoff가 지나면 허용.
                clock["t"] = 10 + backoff + 1
                with self.assertRaises(Exception):
                    svc._get_market_price_with_cache(symbol)
                self.assertEqual(
                    call_count["n"], 2,
                    "실패를 잡은 시점 기준으로 백오프가 끝나면 재시도해야 합니다.",
                )

    def test_system_clock_jump_does_not_affect_backoff_duration(self):
        """GPT 4차 재검토 2번 지적("우선 수정") 재현: 시스템 시각
        (`datetime.now()`)을 5분 뒤로 조정해도(NTP 보정 등), monotonic
        기준 백오프는 그 영향을 받으면 안 된다 — 3차 보완은 `datetime.
        now()`로 실패 시각을 기록해 이 재현에서 백오프가 원치 않게
        끝나거나 반대로 끝나지 않을 수 있었다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            backoff = svc.settings.trading.market_price_retry_backoff_seconds

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)
                call_count = {"n": 0}

                def _always_429(_symbol):
                    call_count["n"] += 1
                    raise _make_429_error()

                with mock.patch.object(svc.broker, "get_market_price", side_effect=_always_429):
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1)

                    # 시스템 시계만 5분 앞으로 훌쩍 이동(NTP 보정 재현)
                    # — monotonic 시계(clock)는 전혀 흐르지 않았으므로
                    # 백오프는 여전히 유효해야 한다.
                    _FrozenDateTime.set_now(frozen_start + timedelta(minutes=5))
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(
                        call_count["n"], 1,
                        "시스템 시계가 앞으로 튀어도 monotonic 백오프는 영향받으면 안 됩니다.",
                    )

                    # monotonic 시계를 실제로 backoff만큼 전진시키면
                    # (시스템 시계는 그대로 둔 채) 재시도가 허용돼야 한다.
                    clock["t"] += backoff + 1
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(
                        call_count["n"], 2,
                        "monotonic 시계가 충분히 흐르면 시스템 시계와 무관하게 재시도해야 합니다.",
                    )

    def test_price_source_distinguishes_fresh_cache_from_failure_fallback(self):
        """관측 CSV의 `price_source` 필드가 "정상 캐시 재사용
        (cache_fresh)"과 "갱신 실패 후 대체(cache_after_failure)"를
        구분해야 한다 — 이전에는 둘 다 같은 current_price로만
        보여 구분이 불가능했다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)
                svc._last_observed_positions[symbol] = Position(
                    symbol=symbol, quantity=10, average_price=10000,
                )
                svc.cached_regime[symbol] = MarketRegime.NEUTRAL
                svc.cached_market_prices[symbol] = MarketPrice(
                    symbol=symbol, current_price=10500, reference_price=10000,
                    previous_close=9800, timestamp=frozen_start,
                )
                svc.cached_market_price_loaded_at[symbol] = frozen_start

                # 1) 재조회 주기(60초) 전 — 정상 캐시 재사용.
                svc.observe_exit_candidates_during_outage()

                # 2) 재조회 주기가 지나고 재조회가 실패 — 실패 후 대체.
                _FrozenDateTime.advance(61)
                clock["t"] += 61
                with mock.patch.object(
                    svc.broker, "get_market_price", side_effect=_make_429_error(),
                ):
                    svc.observe_exit_candidates_during_outage()

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["price_source"], "cache_fresh")
            self.assertEqual(rows[1]["price_source"], "cache_after_failure")


# ─────────────────────────────────────────────────────────────────
# 3. wait_out_balance_outage() — 반복 관측 + 총 대기시간 보존
# ─────────────────────────────────────────────────────────────────
class TestBalanceOutageRetry(unittest.TestCase):
    """독립 잔고 재시도 설계 (2026-09-15, GPT 재검토 반영) — 잔고 API
    장애를 하나의 긴 블로킹 함수(구 `wait_out_balance_outage()`, 이번에
    제거)가 아니라, `trading_loop()`가 매 폴링마다 짧게 호출하는
    `enter_balance_outage()`/`handle_balance_outage_tick()`으로 독립
    재시도한다. 구 `wait_out_balance_outage()`의 문제점 — (1) 180초
    동안 한 번도 반환하지 않아 장 종료·날짜변경 감지를 막았고, (2)
    잔고 API를 전혀 재시도하지 않아 실제 재시도 간격이 사실상 총
    대기시간(180초) 그 자체였음 — 은 새 설계의 독스트링(trading_
    service.py)에 정리돼 있다. 여기서는 새 메커니즘 자체를 검증한다.
    trading_loop() 레벨(다른 API의 429 구분, 장 종료·취소 처리가
    막히지 않음)은 test_review_safety_regressions.py가 별도로
    검증한다.
    """

    def test_enter_balance_outage_schedules_initial_backoff_and_immediate_observe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 100.0}
            svc._monotonic = lambda: clock["t"]

            svc.enter_balance_outage()

            self.assertTrue(svc.is_in_balance_outage())
            self.assertEqual(
                svc._balance_retry_next_attempt_at,
                100.0 + svc.settings.trading.balance_retry_backoff_min_seconds,
            )
            self.assertEqual(
                svc._next_balance_outage_observe_at, 100.0,
                "장애 진입 직후 관측은 지연 없이 1회 즉시 실행돼야 합니다.",
            )

    def test_enter_balance_outage_is_idempotent_does_not_reset_timer(self):
        """이미 장애 상태에서 또 호출돼도(trading_loop()가 연속 폴링에서
        다시 예외를 잡는 경우) 재시도 타이머를 리셋하면 안 됩니다 —
        그러면 매 폴링마다 백오프가 최솟값으로 계속 초기화돼 절대
        늘지 않는 버그가 됩니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            svc.enter_balance_outage()
            first_deadline = svc._balance_retry_next_attempt_at
            clock["t"] = 10.0
            svc.enter_balance_outage()
            self.assertEqual(svc._balance_retry_next_attempt_at, first_deadline)

    def test_tick_before_retry_time_does_not_call_broker_and_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()
            clock["t"] = svc._balance_retry_next_attempt_at - 1  # 아직 재시도 시각 전

            with mock.patch.object(
                svc.broker, "get_account_balance",
                side_effect=AssertionError("재시도 시각 전에는 잔고 API를 호출하면 안 됩니다"),
            ):
                recovered = asyncio.run(svc.handle_balance_outage_tick())

            self.assertFalse(recovered)
            self.assertTrue(svc.is_in_balance_outage())

    def test_observe_runs_on_its_own_cadence_independent_of_retry(self):
        """시세 관측과 잔고 재시도가 서로 다른 monotonic 타이머로
        독립 실행돼야 한다는 GPT 지적의 핵심 검증 — 재시도 시각이
        아직 멀었어도(30초 뒤) 관측은 즉시(0초) 예정돼 있어 이번
        tick에서 수행돼야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()

            with mock.patch.object(svc, "observe_exit_candidates_during_outage") as observe_mock, \
                 mock.patch.object(
                     svc.broker, "get_account_balance",
                     side_effect=AssertionError("아직 재시도 시각이 아닙니다"),
                 ):
                recovered = asyncio.run(svc.handle_balance_outage_tick())

            self.assertFalse(recovered)
            observe_mock.assert_called_once()
            self.assertEqual(
                svc._next_balance_outage_observe_at,
                svc.settings.trading.balance_outage_observe_interval_seconds,
            )

    def test_repeated_failures_double_backoff_up_to_cap(self):
        """GPT 필수 테스트 항목: "반복 실패와 재시도 간격 유지" —
        30→60→120→180초로 늘어나고, 180초에서 더 늘지 않아야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()

            expected_intervals = [60.0, 120.0, 180.0, 180.0]  # 최초(30) 다음 실패부터

            with mock.patch.object(
                svc.broker, "get_account_balance",
                side_effect=lambda: (_ for _ in ()).throw(_make_429_error()),
            ):
                for expected in expected_intervals:
                    clock["t"] = svc._balance_retry_next_attempt_at
                    recovered = asyncio.run(svc.handle_balance_outage_tick())
                    self.assertFalse(recovered)
                    self.assertEqual(svc._balance_retry_interval_seconds, expected)

    def test_non_rate_limit_failure_during_retry_reraises_but_still_schedules_backoff(self):
        """429가 아닌 예상 밖의 실패(예: 인증 만료)는 조용히 삼키지
        않고 올려야 하지만(trading_loop()의 일반 예외 로그를 태우기
        위함), 백오프는 이미 예약된 뒤라 재시도 간격 없이 매 폴링마다
        두드리지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()
            clock["t"] = svc._balance_retry_next_attempt_at

            other_error = RuntimeError("kiwoom business error: api_id=kt00001, body={'return_code': -1}")
            with mock.patch.object(svc.broker, "get_account_balance", side_effect=other_error):
                with self.assertRaises(RuntimeError):
                    asyncio.run(svc.handle_balance_outage_tick())

            self.assertTrue(svc.is_in_balance_outage(), "예상 밖 실패라도 장애 상태는 유지돼야 합니다.")
            self.assertEqual(
                svc._balance_retry_interval_seconds,
                svc.settings.trading.balance_retry_backoff_min_seconds * 2,
            )

    def test_tick_detects_date_change_during_outage(self):
        """GPT 필수 테스트 항목: "장 종료·날짜 변경·취소" 중 날짜변경
        부분 — 구 `wait_out_balance_outage()`는 180초를 통째로 블로킹해
        자정을 넘겨도 그 안에서는 날짜변경을 감지하지 못했습니다. 새
        `handle_balance_outage_tick()`은 매 tick마다 `_check_and_handle_
        daily_reset()`을 호출하므로, 장애가 자정을 넘겨 이어지더라도
        (재시도·관측 어느 쪽도 아직 예정 시각이 안 됐어도) 날짜변경
        자체는 놓치지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            with mock.patch(
                "domain.service.trading_service.now_kst",
                return_value=datetime(2026, 9, 15, 23, 59),
            ):
                svc.enter_balance_outage()
                svc._check_and_handle_daily_reset()  # enter_balance_outage() 전에 오늘 날짜를 기록해 둠
            svc.state.consecutive_losses = 3

            # 재시도·관측 어느 쪽도 아직 예정 시각이 안 된 시점에서
            # tick을 호출해도, 자정을 넘긴 날짜변경은 감지돼야 한다.
            clock["t"] = 1.0
            with mock.patch(
                "domain.service.trading_service.now_kst",
                return_value=datetime(2026, 9, 16, 0, 0),
            ), mock.patch.object(
                svc.broker, "get_account_balance",
                side_effect=AssertionError("아직 재시도 시각이 아닙니다"),
            ), mock.patch.object(svc, "observe_exit_candidates_during_outage"):
                recovered = asyncio.run(svc.handle_balance_outage_tick())

            self.assertFalse(recovered)
            self.assertEqual(svc._last_reset_date.isoformat(), "2026-09-16")
            self.assertEqual(
                svc.state.consecutive_losses, 0,
                "장애 중이라도 날짜변경이 감지되면 일별 상태가 초기화돼야 합니다.",
            )

    def test_recovery_success_with_unresolved_orders_processes_without_extra_fetch(self):
        """GPT 지적 재현: 미해결 주문(BUY_PENDING 등)이 있으면 `_get_
        balance_with_cache()`는 캐시가 있어도 항상 새로 조회한다 —
        그래서 "복구된 잔고를 캐시에 넣기만" 하면 다음 `run_once()`가
        또 잔고를 호출하게 된다(불필요한 재조회). `handle_balance_
        outage_tick()`은 이 복구 응답을 그대로 `_run_once_with_
        balance()`에 전달해 이번 폴링 안에서 정상 전략 처리까지
        마쳐야 하고, `get_account_balance()`는 이 tick 안에서 정확히
        1회만 호출돼야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            svc.state.unresolved_order_intents = {"dummy": "order"}
            self.assertTrue(svc._has_unresolved_orders())

            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()
            clock["t"] = svc._balance_retry_next_attempt_at

            recovered_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            call_count = {"n": 0}

            def _succeed():
                call_count["n"] += 1
                return recovered_balance

            with mock.patch.object(svc.broker, "get_account_balance", side_effect=_succeed), \
                 mock.patch.object(svc, "_run_once_with_balance", new=mock.AsyncMock()) as run_mock:
                recovered = asyncio.run(svc.handle_balance_outage_tick())

            self.assertTrue(recovered)
            self.assertEqual(call_count["n"], 1, "잔고 API는 이 tick 안에서 정확히 1회만 호출돼야 합니다.")
            run_mock.assert_awaited_once_with(recovered_balance)
            self.assertFalse(svc.is_in_balance_outage(), "복구되면 장애 상태를 벗어나야 합니다.")
            self.assertIs(svc.cached_balance, recovered_balance)

    def test_recovery_logs_balance_freshness_with_recovery_trigger_reason(self):
        """복구 시에도 캐시·관측 스냅샷·신선도 로그가 정상 성공
        경로와 동일한 헬퍼(`_record_balance_fetch_success`)로 갱신돼야
        한다 — trigger_reason으로 "장애 재시도로 복구됐다"는 사실이
        구분 가능해야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()
            clock["t"] = svc._balance_retry_next_attempt_at

            recovered_balance = AccountBalance(cash=1_000_000, total_asset=1_000_000, positions=[])
            with mock.patch.object(svc.broker, "get_account_balance", return_value=recovered_balance):
                asyncio.run(svc.handle_balance_outage_tick())

            rows = _read_csv_rows(svc.settings.storage.balance_freshness_log_file)
            self.assertEqual(rows[-1]["outcome"], "fetch_success")
            self.assertEqual(rows[-1]["trigger_reason"], "balance_outage_recovery")

    def test_recovery_resets_backoff_so_next_outage_starts_at_minimum(self):
        """GPT 필수 테스트 항목: "복구 직후 다시 실패하는 경우" —
        상한(180초)까지 늘어난 뒤 복구되면, 다음에 다시 장애가 나도
        그 상한에서 이어지지 않고 최솟값부터 다시 시작해야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]
            svc.enter_balance_outage()

            with mock.patch.object(
                svc.broker, "get_account_balance",
                side_effect=lambda: (_ for _ in ()).throw(_make_429_error()),
            ):
                for _ in range(4):  # 30→60→120→180으로 상한까지 올린다.
                    clock["t"] = svc._balance_retry_next_attempt_at
                    asyncio.run(svc.handle_balance_outage_tick())
            self.assertEqual(
                svc._balance_retry_interval_seconds,
                svc.settings.trading.balance_retry_backoff_max_seconds,
            )

            clock["t"] = svc._balance_retry_next_attempt_at
            with mock.patch.object(
                svc.broker, "get_account_balance",
                return_value=AccountBalance(cash=0, total_asset=0, positions=[]),
            ), mock.patch.object(svc, "_run_once_with_balance", new=mock.AsyncMock()):
                recovered = asyncio.run(svc.handle_balance_outage_tick())
            self.assertTrue(recovered)
            self.assertFalse(svc.is_in_balance_outage())

            svc.enter_balance_outage()
            self.assertEqual(
                svc._balance_retry_interval_seconds,
                svc.settings.trading.balance_retry_backoff_min_seconds,
                "이전 장애의 상한에서 이어지지 않고 최솟값부터 다시 시작해야 합니다.",
            )


# ─────────────────────────────────────────────────────────────────
# 4. run_once() 통합 검증 — 429 → (관측 지속) 대기 → 복구
# ─────────────────────────────────────────────────────────────────
class TestRunOnceOutageRecoveryIntegration(unittest.TestCase):
    """trading_loop()(app/main.py)의 except 분기가 실제로 하는 일을
    그대로 재현합니다: run_once()가 429로 실패 → enter_balance_
    outage() → handle_balance_outage_tick() 반복 호출(관측·재시도
    각각 독립 monotonic 타이머, `_FrozenDateTime`을 실제로 전진시킴)
    → 잔고 재시도 성공 시 그 tick 안에서 정상 처리까지 자동 진행 →
    정상 경로 복귀, 부수 효과 중복 없음.

    2026-09-15 (독립 잔고 재시도 설계, GPT 재검토 반영): 구
    `wait_out_balance_outage()`(180초 단일 블로킹, 이번에 제거)를
    호출하던 부분을 `enter_balance_outage()` + 반복
    `handle_balance_outage_tick()` 호출로 다시 짰다 — 그 결과 "복구
    직후 run_once()를 한 번 더 호출"하던 5단계도 사라졌다(잔고 재시도
    성공 자체가 곧 `_run_once_with_balance()` 호출이므로, 이제 그
    응답을 다시 조회할 필요가 없다는 것이 이번 설계의 핵심).

    2026-09-15 보완 (GPT 재검토 4번 지적 반영, 기존 테스트의 5가지
    결함을 모두 다시 짬):
    (a) sleep을 즉시 반환시켜 시간 기반 신선도를 전혀 검증 못 함 →
        `_FrozenDateTime`으로 실제 시간을 전진시킨다.
    (b) 관측 행 "개수"만 확인하고 가격이 실제로 바뀌었는지는 확인
        안 함 → 장애 도중 시세를 실제로 손절선 아래로 떨어뜨리고,
        그 변화가 관측 로그의 `current_price`/`status`에 반영되는지
        직접 비교한다.
    (c) "정상 보유" 상태를 `svc.cached_balance` 직접 대입으로
        만들어, 바로 그 대입 방식이 감췄던 결함(캐시 무효화 시
        관측 스냅샷도 함께 사라짐)을 테스트가 재현하지 못했음 →
        이번에는 실제 메서드(`_get_balance_with_cache()`/
        `_get_market_price_with_cache()`)를 호출해 스냅샷을 채운다.
    (d) 주문 추적 mock이 대기 함수 실행 전에 종료됨 → 이번에는 429
        run_once() 호출 → 대기 → 복구 run_once()까지 하나의
        `mock.patch.object(svc.broker, "place_order", ...)` 블록으로
        전체를 감싼다.
    (e) 복구 후 로그 "행 수"만 비교해 부수 효과 중복 여부를 증명하지
        못함 → 로그 행 수 비교에 더해 `cached_balance_loaded_at`이
        정확히 한 번만 갱신됐는지도 함께 확인한다.

    2026-09-15 3차 보완 (GPT 재검토 3번 지적 반영, "검증 보완" — 프로덕션
    코드는 바뀌지 않음): 아래 `test_429_then_price_drop_during_wait_
    then_recovers_cleanly`의 기존 주석/독스트링이 "복구 부수 효과의
    정확히 1회 적용을 증명했다"고 말하던 부분은 실제 검증 범위보다
    넓은 주장이었다 — 이 테스트가 실제로 확인하는 건 place_order()
    호출이 정확히 1회라는 것(= "복구 직후 SELL 제출 1회")까지다. 그
    SELL이 이후 체결 확인 과정에서 손익·수량에 어떻게 반영되는지,
    부분체결/최종체결 처리가 맞는지, 같은 체결 증거가 반복 노출돼도
    중복 반영되지 않는지, 다음 폴링에서 중복 SELL이 없는지는 이
    테스트 하나로 증명되지 않는다(MockBroker는 SELL을 place_order()
    안에서 즉시·완전 체결시키므로 "체결 확인"이라는 별도 단계 자체가
    없다 — 부분체결/재시도 큐를 쓰는 실제 브로커 경로의 부수 효과
    중복 방지는 여전히 미검증). 아래
    `test_recovery_sell_does_not_repeat_on_subsequent_polls`가 그 중
    "다음 폴링에서 중복 SELL·중복 거래 로그가 없는지"만 별도로
    검증한다 — 나머지(부분체결 처리, 반복 노출된 같은 체결 증거의
    dedup)는 여전히 이번 3차 보완의 범위 밖이며, 별도 승인 대상으로
    남는다.
    """

    def test_429_then_price_drop_during_wait_then_recovers_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = svc.targets[0]
            svc.broker._positions[symbol] = Position(symbol=symbol, quantity=10, average_price=10000)
            svc.broker._prices[symbol] = 10500  # 아직 손절선 위
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            clock = {"t": 0.0}
            svc._monotonic = lambda: clock["t"]

            place_order_calls = []
            original_place_order = svc.broker.place_order

            def _track_place_order(*args, **kwargs):
                place_order_calls.append((args, kwargs))
                return original_place_order(*args, **kwargs)

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)

                # 1) 정상 보유 — 캐시 딕셔너리를 테스트가 직접 채우지
                #    않고, 실제 운영 경로를 통해 스냅샷을 자연스럽게
                #    채운다. 이렇게 해야 _last_observed_positions가
                #    실제로 populate되는지까지 함께 검증된다.
                svc._get_balance_with_cache()
                svc._get_market_price_with_cache(symbol)
                self.assertEqual(svc._last_observed_positions[symbol].quantity, 10)

                # 2) 주문 접수 직후처럼 캐시를 비운 상태에서 잔고 API
                #    장애 발생 — _last_observed_positions는 이미
                #    채워져 있으므로 영향받지 않아야 한다.
                svc.cached_balance = None

                error = _make_429_error()
                with mock.patch.object(svc.broker, "place_order", side_effect=_track_place_order):
                    with mock.patch.object(svc.broker, "get_account_balance", side_effect=error):
                        with self.assertRaises(KiwoomHttpError):
                            asyncio.run(svc.run_once())

                    # 이 시점에는 아직 관측이 한 번도 안 일어남 —
                    # run_once() 자체는 429를 그대로 전파할 뿐(1~2단계
                    # 회귀 유지), 대기·관측은 trading_loop()가(여기서는
                    # 이 테스트가) 별도로 호출한다.
                    rows_before = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
                    self.assertEqual(rows_before, [])

                    # 2026-09-15 (독립 잔고 재시도 설계): trading_loop()의
                    # except 블록이 하는 일 — enter_balance_outage() 호출.
                    svc.enter_balance_outage()

                    # 3) 대기 중 가격 하락 — 손절선 아래로 떨어뜨린 뒤,
                    #    price_refresh_seconds(테스트 설정 60초)를 가짜
                    #    시계로 실제 통과시켜 재조회가 실제로 발동하는지
                    #    확인한다.
                    svc.broker._prices[symbol] = 9000  # 손절선 아래로 하락

                    real_get_balance = svc.broker.get_account_balance
                    balance_call_count = {"n": 0}

                    def _balance_side_effect():
                        # 첫 재시도(잔고 재시도 타이머 30초 시점)는
                        # 여전히 429 — 두 번째 재시도(백오프가 60초로
                        # 늘어난 뒤, 90초 시점)에서 실제로 복구된다.
                        # MockBroker의 실제 get_account_balance()를
                        # 그대로 위임하므로, 복구 응답은 방금 내려간
                        # 시세가 아니라 "종목을 여전히 보유 중"이라는
                        # 실제 상태를 그대로 반영한다.
                        balance_call_count["n"] += 1
                        if balance_call_count["n"] == 1:
                            raise _make_429_error()
                        return real_get_balance()

                    def _advance_clock_to(t: float) -> None:
                        delta = t - clock["t"]
                        clock["t"] = t
                        if delta > 0:
                            _FrozenDateTime.advance(delta)

                    with mock.patch.object(
                        svc.broker, "get_account_balance", side_effect=_balance_side_effect,
                    ):
                        # observe_interval=15초(기본값), retry_backoff
                        # 최솟값=30초(기본값) — 매 15초마다 tick 하나씩
                        # 흘려보내 관측과 재시도가 각자의 monotonic
                        # 타이머로 독립 실행되는지 그대로 재현한다(잔고
                        # 재시도가 우선이라 관측과 겹치는 tick에서는
                        # 관측이 한 tick 밀린다 — GPT 지적: "동시에
                        # 예정됐다면 잔고 재시도를 우선"). t=90에서
                        # 재시도가 성공해 그 tick 안에서 정상 처리까지
                        # 자동 진행된다.
                        recovered = False
                        for t in (0, 15, 30, 45, 60, 75, 90):
                            _advance_clock_to(t)
                            recovered = asyncio.run(svc.handle_balance_outage_tick())
                            if recovered:
                                break

                    self.assertTrue(recovered, "t=90에서 잔고 재시도가 성공해 정상 처리로 전환됐어야 합니다.")
                    self.assertFalse(svc.is_in_balance_outage())

                    rows_during_outage = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
                    # t=0,15(재시도 전),45,60,75(재시도가 30·90을 먼저
                    # 처리하느라 30·90 시점의 관측은 각각 45·건너뜀 —
                    # 90은 복구라 관측 없이 곧장 정상 처리로 감)에 5회.
                    self.assertEqual(len(rows_during_outage), 5, "관측과 재시도가 겹친 tick을 제외하고 5회 관측이 있어야 합니다.")
                    self.assertTrue(all(r["symbol"] == symbol for r in rows_during_outage))

                    # 재조회 주기(60초)가 지나지 않은 앞의 관측들은
                    # 예전 가격(10500)을 그대로 씁니다 — "캐시 재사용"과
                    # "새 관측 안 함"은 다른 것임을 함께 보여줍니다.
                    self.assertEqual(rows_during_outage[0]["current_price"], "10500")
                    self.assertEqual(rows_during_outage[1]["current_price"], "10500")
                    self.assertEqual(rows_during_outage[2]["current_price"], "10500")
                    # 네 번째 관측(t=60)은 주기가 지나 실제로 새 가격을
                    # 재조회해 하락을 포착해야 합니다 — 이번 재검토의
                    # 핵심 요구사항입니다.
                    self.assertEqual(rows_during_outage[3]["current_price"], "9000")
                    self.assertEqual(rows_during_outage[3]["status"], "STOP_LOSS")
                    self.assertNotEqual(
                        rows_during_outage[3]["status"], rows_during_outage[0]["status"],
                        "가격 하락이 실제로 새로운 청산 후보로 이어져야 합니다.",
                    )
                    # 다섯 번째(t=75)는 재조회 주기가 다시 지나지 않아
                    # 방금 새로 캐시된 하락 가격을 그대로 재사용합니다.
                    self.assertEqual(rows_during_outage[4]["current_price"], "9000")

                    # 4) 장애 대기 중에는 손절 조건을 만족해도 주문·체결
                    #    확정이 전혀 없어야 합니다 — t=90의 재시도 성공
                    #    직후 정상 처리에서 나가는 SELL만 있어야 합니다
                    #    (place_order mock은 이 tick까지 그대로 유지된다,
                    #    4번 지적 (d) 요구사항).
                    #
                    #    실제로 돌려보니 — 장애 중 손절선(9,850원)
                    #    아래로 내려간 채 대기했던 종목이, 잔고 재시도가
                    #    복구된 바로 그 tick 안(handle_balance_outage_
                    #    tick()이 내부적으로 호출하는 _run_once_with_
                    #    balance())에서 정상 전략 경로를 통해 "정확히
                    #    한 번" 손절 매도가 제출된다(아래 place_order_
                    #    calls 길이 1 확인). 2026-09-15 3차 보완(GPT
                    #    재검토 3번 지적): 이건 "복구 직후 SELL 제출
                    #    1회"의 증거일 뿐이다 — MockBroker는 place_
                    #    order() 안에서 즉시·완전 체결시키므로, 이
                    #    확인이 곧 "그 이후 체결 확인·손익 반영까지
                    #    중복 없음"을 뜻하지는 않는다(부분체결·재시도
                    #    큐가 있는 실제 브로커 경로는 별도 검증 대상).
                    #    매도가 접수되면서 cached_balance가 다시
                    #    None으로 비워지는 것도 기존에 승인된 정책
                    #    (체결 확정 전 강제 재조회) 그대로이므로 함께
                    #    확인한다.
                    self.assertEqual(
                        len(place_order_calls), 1,
                        "잔고 재시도가 복구된 바로 그 tick 안에서 SELL 제출이 정확히 한 번 있어야 합니다.",
                    )

                    # 6) 2026-09-15 4차 보완 (GPT 재검토 3번 지적 반영):
                    #    복구 직후 SELL이 정확히 1회 제출됐다는 확인만으로는
                    #    "다음 폴링에서 중복 제출이 없다"는 것까지 증명하지
                    #    못한다 — 기존에는 이를 별도의
                    #    test_recovery_sell_does_not_repeat_on_subsequent_polls가
                    #    검증했지만, 그 테스트는 실제 429→관측→복구 경로를
                    #    거치지 않고 손절 조건을 직접 만들어 run_once()를
                    #    반복 호출했을 뿐이었다(재현 시나리오와 무관). 이제는
                    #    바로 이 통합 테스트의 복구 직후에 후속 폴링을 붙여,
                    #    실제 429→관측→복구를 거친 뒤에도 중복 제출이 없는지
                    #    확인한다. place_order mock은 계속 유지된다.
                    #
                    #    검증 범위는 "SELL 제출·접수 로그 중복 없음"까지다 —
                    #    손익·수량이 그 SELL을 통해 정확히 한 번만 반영됐는지는
                    #    관련 상태값(예: 실현손익 누계)을 직접 확인하지
                    #    않았으므로 이 테스트로는 주장하지 않는다.
                    asyncio.run(svc.run_once())
                    asyncio.run(svc.run_once())

            rows_after_recovery = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(
                len(rows_after_recovery), len(rows_during_outage),
                "정상 복구된 run_once()는 exit_candidate_outage 로그에 아무 것도 추가하지 않아야 합니다"
                "(이 로거는 장애 관측 전용 — 정상 경로는 기존 손절/트레일링 판정을 그대로 씁니다).",
            )
            sell_calls = [
                call for call in place_order_calls
                if call[0][0].symbol == symbol and call[0][0].side == OrderSide.SELL
            ]
            self.assertEqual(
                len(sell_calls), 1,
                "장애 중 관측만 되고 미뤄졌던 손절 조건이, 복구된 정상 경로에서 SELL 제출 정확히 한 번으로 "
                "이어져야 하고, 이미 청산된 뒤의 후속 폴링(2회)에서는 같은 손절이 중복 제출되면 안 됩니다"
                "(이 확인의 범위는 'SELL 제출·접수 로그 중복 없음'까지 — 손익·수량 반영 자체는 관련 상태값을 "
                "직접 확인하지 않았으므로 이 테스트로 주장하지 않는다).",
            )

            trade_rows = _read_csv_rows(svc.settings.storage.trade_log_file)
            accepted_sell_rows = [
                r for r in trade_rows
                if r["symbol"] == symbol and r["side"] == "SELL" and r["accepted"] == "True"
            ]
            self.assertEqual(
                len(accepted_sell_rows), 1,
                "거래 로그에도 같은 청산이 중복 기록되면 안 됩니다(검증 범위: 접수 로그 중복 없음).",
            )
            self.assertNotIn(
                symbol, svc.broker._positions,
                "MockBroker 포지션에서도 완전히 청산된 채로 유지돼야 합니다(추가 SELL이 없었다는 방증).",
            )

    def test_observation_continues_when_cache_cleared_right_after_order(self):
        """2026-09-15 보완 (GPT 재검토 2번 지적 반영, 별도 시나리오):
        주문이 막 접수돼 `cached_balance`가 `None`으로 비워진 그
        직후에 잔고 API 장애가 겹쳐도, 이전에 이미 확보한
        `_last_observed_positions` 스냅샷과 로컬 상태
        (`entry_time_by_symbol`)가 있으면 관측이 0건이 되면 안
        됩니다 — 재검토가 재현한 "PSM에 보유수량 10주가 있고 진입
        시각도 남아 있는데 캐시를 비웠더니 관측 기록이 0건이었다"는
        정확히 이 케이스입니다.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = svc.targets[0]
            svc.broker._positions[symbol] = Position(symbol=symbol, quantity=10, average_price=10000)
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            # 정상 사이클에서 이미 확보된 스냅샷 + 진입 시각 기록 —
            # 실제 운영 경로를 통해 채운다(캐시 딕셔너리 직접 대입 아님).
            svc._get_balance_with_cache()
            svc._get_market_price_with_cache(symbol)
            svc.state.entry_time_by_symbol[symbol] = datetime.now().isoformat()

            # 주문 접수 직후처럼 캐시를 즉시 비운다 — 기존 정책 그대로.
            svc.cached_balance = None

            error = _make_429_error()
            with mock.patch.object(svc.broker, "get_account_balance", side_effect=error):
                with self.assertRaises(KiwoomHttpError):
                    asyncio.run(svc.run_once())

            svc.observe_exit_candidates_during_outage()
            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(
                len(rows), 1,
                "cached_balance가 order 접수 직후 비워져도 마지막 스냅샷으로 관측이 계속돼야 합니다.",
            )
            self.assertEqual(rows[0]["symbol"], symbol)
            self.assertNotEqual(rows[0]["status"], "")

    # 2026-09-15 4차 보완 (GPT 재검토 3번 지적 반영): 이 자리에 있던
    # `test_recovery_sell_does_not_repeat_on_subsequent_polls`는 실제
    # 429→관측→복구 경로를 거치지 않고 손절 조건을 직접 만들어
    # run_once()를 반복 호출했을 뿐이었다 — GPT의 명시적 지적("기존
    # 장애·복구 통합 테스트의 복구 직후에 후속 폴링을 붙이세요")에 따라
    # 그 검증은 위 `test_429_then_price_drop_during_wait_then_recovers_
    # cleanly`의 복구 직후 후속 폴링(2회)으로 옮겨 실제 429→관측→복구
    # 시나리오에 연결했고, 검증 범위도 "SELL 제출·접수 로그 중복
    # 없음"으로 명확히 제한했다(손익·리스크 상태값은 직접 확인하지
    # 않았으므로 주장하지 않음). 별도 테스트로 중복 유지하지 않는다.


if __name__ == "__main__":
    unittest.main()
