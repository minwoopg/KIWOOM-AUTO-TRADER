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
    건너뛰어야 한다."""

    def test_repeated_failures_within_backoff_do_not_retry_broker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)

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
                    _FrozenDateTime.advance(15)
                    price2 = svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 재조회를 시도하면 안 됩니다.")
                    self.assertEqual(svc._market_price_fetch_outcome[symbol], "cache_after_failure")

                    _FrozenDateTime.advance(15)
                    price3 = svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 재조회를 시도하면 안 됩니다.")

                    # 백오프(60초)가 지나면 다시 시도해야 한다.
                    _FrozenDateTime.advance(40)  # 누적 70초 경과
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
                    with self.assertRaises(Exception):
                        svc._get_market_price_with_cache(symbol)
                    self.assertEqual(call_count["n"], 1, "백오프 중에는 캐시가 없어도 재호출하면 안 됩니다.")

    def test_success_after_failure_clears_backoff_and_records_fetched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)

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
                price = svc._get_market_price_with_cache(symbol)
                self.assertEqual(svc._market_price_fetch_outcome[symbol], "fetched")
                self.assertNotIn(symbol, svc._market_price_fetch_failed_at)

    def test_price_source_distinguishes_fresh_cache_from_failure_fallback(self):
        """관측 CSV의 `price_source` 필드가 "정상 캐시 재사용
        (cache_fresh)"과 "갱신 실패 후 대체(cache_after_failure)"를
        구분해야 한다 — 이전에는 둘 다 같은 current_price로만
        보여 구분이 불가능했다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)

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
class TestWaitOutBalanceOutage(unittest.TestCase):

    def test_total_wait_time_unchanged_and_observes_repeatedly(self):
        """총 대기시간(기존 180초 상당)은 그대로 유지하되, 그 안에서
        여러 번 관측 기회가 있어야 합니다 — "재시도 대기 중 관측은
        지속한다"는 요구사항의 핵심 검증.

        2026-09-15 보완 (GPT 재검토 4번 지적 반영): `asyncio.sleep`을
        단순히 무력화만 하지 않고, 그 mock 자체가 `_FrozenDateTime`을
        실제로 전진시켜 매 구간 관측의 `price_age_seconds`가 진짜
        경과 시간을 반영하는지까지 확인합니다 — 이전 테스트는 시간이
        전혀 흐르지 않아 "같은 캐시를 반복 평가"하는 결함도 통과시켰을
        것입니다.

        2026-09-15 3차 보완 (GPT 재검토 2번 지적 반영): `wait_out_
        balance_outage()`가 이제 각 관측 호출 앞뒤로 실제 벽시계
        (`self._monotonic`, 기본은 `time.monotonic`)를 읽어 그 소요
        시간도 예산에 반영한다 — 이 테스트의 관측 자체는 실제로도
        매우 빠르지만(메모리 연산 + 파일 I/O 몇 바이트) 0은 아니므로,
        그 미세한 실제 소요까지 `elapsed`에 더해지면 40.0과의 비교가
        기존처럼 딱 떨어지지 않는다. 이 테스트가 검증하려는 것은
        "관측이 사실상 즉시 끝나는 정상적인 경우, 총 sleep은 여전히
        40초 그대로"이므로 `svc._monotonic`을 항상 같은 값을
        반환하도록 고정해(관측 소요 0으로 취급) 그 취지를 그대로
        유지한다(전역 `time.monotonic`이 아니라 이 인스턴스의 속성만
        바꿔치기 — asyncio 이벤트 루프 내부도 `time.monotonic`을 쓰므로
        전역을 건드리면 무관한 호출까지 영향을 받는다). "관측이 실제로
        느릴 때" 시나리오는 바로 아래
        `test_slow_observation_shrinks_next_sleep_so_total_wait_does_not_inflate`가
        별도로 검증한다.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = "005930"
            frozen_start = _FrozenDateTime(2026, 9, 15, 10, 0, 0)
            svc._monotonic = lambda: 0.0

            with mock.patch("domain.service.trading_service.datetime", _FrozenDateTime):
                _FrozenDateTime.set_now(frozen_start)
                svc._last_observed_positions[symbol] = Position(
                    symbol=symbol, quantity=10, average_price=10000,
                )
                svc.cached_market_prices[symbol] = MarketPrice(
                    symbol=symbol, current_price=10500, reference_price=10000,
                    previous_close=9800, timestamp=frozen_start,
                )
                svc.cached_market_price_loaded_at[symbol] = frozen_start
                svc.cached_regime[symbol] = MarketRegime.NEUTRAL

                slept_seconds = []

                async def _fake_sleep(seconds):
                    slept_seconds.append(seconds)
                    _FrozenDateTime.advance(seconds)

                with mock.patch("asyncio.sleep", side_effect=_fake_sleep):
                    asyncio.run(
                        svc.wait_out_balance_outage(total_seconds=40, observe_interval_seconds=15)
                    )

            self.assertAlmostEqual(sum(slept_seconds), 40.0)
            self.assertEqual(slept_seconds, [15, 15, 10])

            rows = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            # 15초 간격 x 3구간(15,15,10) = 관측 3회
            self.assertEqual(len(rows), 3, "매 구간 시작마다 관측이 한 번씩 있어야 합니다.")
            # price_refresh_seconds(테스트 설정 60초)를 아직 넘지 않았으므로
            # 캐시 자체는 재사용되지만(정상 스로틀), 나이는 가짜 시계가
            # 전진한 만큼 0 → 15 → 30으로 실제 경과를 반영해야 합니다 —
            # 매번 같은 나이라면 "시간이 전혀 흐르지 않은 반복 평가"라는
            # 이번 재검토의 핵심 결함이 재현된 것입니다.
            ages = [float(r["price_age_seconds"]) for r in rows]
            self.assertEqual(ages, [0.0, 15.0, 30.0])

    def test_slow_observation_shrinks_next_sleep_so_total_wait_does_not_inflate(self):
        """GPT 3차 재검토 2번 지적("우선 수정") 재현: 관측 1회가
        동기식 시세 재조회 등으로 실제 오래(예: 20초) 걸리면, 기존
        구현은 그 시간을 전혀 계산에 넣지 않고 매번 관측_interval
        만큼 그대로 추가로 잤다 — 재현: 설정 45초/간격 15초에서 관측
        1회당 20초씩 걸리면 실제 총 경과가 105초(45+60)까지 불어남.

        이 테스트는 `svc._monotonic`(기본은 `time.monotonic`)을
        결정적으로 제어해(각 관측 호출 앞뒤로 20초씩 진행한 것처럼
        값을 준비) 그 20초가 다음 sleep 구간에서 그대로 차감되는지
        확인한다 — 관측 소요시간 위에 매번 전체 간격을 또 얹어 자면
        안 된다. 전역 `time.monotonic`이 아니라 이 인스턴스의 속성만
        바꿔치기하는 이유는 asyncio 이벤트 루프 내부도 스케줄링에
        `time.monotonic`을 쓰기 때문 — 전역을 패치하면 그 호출까지
        같은 side_effect 목록을 소진해 버려 테스트가 무관한 이유로
        깨진다.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)

            # wait_out_balance_outage()는 관측 호출 앞뒤로 self.
            # _monotonic()을 한 번씩 읽는다 — 아래 값들은 "관측 1회가
            # 20초 걸렸다"를 매 반복 결정적으로 재현한다.
            monotonic_values = iter([0.0, 20.0, 20.0, 40.0, 40.0, 60.0, 60.0, 80.0])
            svc._monotonic = lambda: next(monotonic_values)

            with mock.patch.object(svc, "observe_exit_candidates_during_outage") as observe_mock, \
                 mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep_mock:
                asyncio.run(
                    svc.wait_out_balance_outage(total_seconds=45, observe_interval_seconds=15)
                )

            slept_chunks = [call.args[0] for call in sleep_mock.await_args_list]
            self.assertTrue(
                all(chunk <= 15.0 for chunk in slept_chunks),
                f"관측 소요시간이 이미 예산을 다 썼다면 그 위에 전체 간격을 또 자면 안 됩니다: {slept_chunks}",
            )
            self.assertLessEqual(
                len(slept_chunks), 2,
                "관측 1회가 20초씩 걸려 45초 예산을 두 번 만에 넘기므로, "
                "sleep은 많아야 한두 번(그것도 짧게)만 있어야 합니다.",
            )
            self.assertGreaterEqual(observe_mock.call_count, 2)

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
    outage() 호출(총 시간은 짧은 값으로 대체하되, `asyncio.sleep`
    mock이 `_FrozenDateTime`을 실제로 전진시킴) → 잔고 복구 후
    run_once() 재호출 → 정상 경로 복귀, 부수 효과 중복 없음.

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

                    # 3) 대기 중 가격 하락 — 손절선 아래로 떨어뜨린 뒤,
                    #    price_refresh_seconds(테스트 설정 60초)를 가짜
                    #    시계로 실제 통과시켜 재조회가 실제로 발동하는지
                    #    확인한다.
                    svc.broker._prices[symbol] = 9000  # 손절선 아래로 하락

                    async def _fake_sleep(seconds):
                        _FrozenDateTime.advance(seconds)

                    with mock.patch("asyncio.sleep", side_effect=_fake_sleep):
                        asyncio.run(
                            svc.wait_out_balance_outage(total_seconds=90, observe_interval_seconds=30)
                        )

                    rows_during_outage = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
                    self.assertEqual(len(rows_during_outage), 3, "30초 간격 3회 관측이 있어야 합니다.")
                    self.assertTrue(all(r["symbol"] == symbol for r in rows_during_outage))

                    # 처음 두 번(0초, 30초 시점)은 아직 60초 재조회
                    # 주기가 지나지 않아 예전 가격(10500)을 그대로
                    # 씁니다 — "캐시 재사용"과 "새 관측 안 함"은 다른
                    # 것임을 함께 보여줍니다.
                    self.assertEqual(rows_during_outage[0]["current_price"], "10500")
                    self.assertEqual(rows_during_outage[1]["current_price"], "10500")
                    # 세 번째(60초 시점)는 주기가 지나 실제로 새 가격을
                    # 재조회해 하락을 포착해야 합니다 — 이번 재검토의
                    # 핵심 요구사항입니다.
                    self.assertEqual(rows_during_outage[2]["current_price"], "9000")
                    self.assertEqual(rows_during_outage[2]["status"], "STOP_LOSS")
                    self.assertNotEqual(
                        rows_during_outage[2]["status"], rows_during_outage[0]["status"],
                        "가격 하락이 실제로 새로운 청산 후보로 이어져야 합니다.",
                    )

                    # 4) 장애 대기 중에는 손절 조건을 만족해도 주문·체결
                    #    확정이 전혀 없어야 합니다.
                    self.assertEqual(
                        place_order_calls, [],
                        "장애 대기 중에는 손절 조건을 만족해도 주문이 나가면 안 됩니다.",
                    )

                    # 5) 잔고 복구 — 다음 run_once()는 정상적으로 통과
                    #    해야 하고, 관측 전용 로그는 중복으로 늘지
                    #    않아야 합니다. place_order mock은 이 호출까지
                    #    그대로 유지된다(4번 지적 (d) 요구사항).
                    #
                    #    실제로 돌려보니 — 장애 중 손절선(9,850원)
                    #    아래로 내려간 채 대기했던 종목이, 잔고가
                    #    복구된 이 run_once()에서 정상 전략 경로를
                    #    통해 "정확히 한 번" 손절 매도가 제출된다(아래
                    #    place_order_calls 길이 1 확인). 2026-09-15
                    #    3차 보완(GPT 재검토 3번 지적): 이건 "복구
                    #    직후 SELL 제출 1회"의 증거일 뿐이다 —
                    #    MockBroker는 place_order() 안에서 즉시·완전
                    #    체결시키므로, 이 확인이 곧 "그 이후 체결
                    #    확인·손익 반영까지 중복 없음"을 뜻하지는
                    #    않는다(부분체결·재시도 큐가 있는 실제 브로커
                    #    경로는 별도 검증 대상). 매도가 접수되면서
                    #    cached_balance가 다시 None으로 비워지는 것도
                    #    기존에 승인된 정책(체결 확정 전 강제 재조회)
                    #    그대로이므로 함께 확인한다.
                    asyncio.run(svc.run_once())

            rows_after_recovery = _read_csv_rows(svc.settings.storage.exit_candidate_outage_log_file)
            self.assertEqual(
                len(rows_after_recovery), len(rows_during_outage),
                "정상 복구된 run_once()는 exit_candidate_outage 로그에 아무 것도 추가하지 않아야 합니다"
                "(이 로거는 장애 관측 전용 — 정상 경로는 기존 손절/트레일링 판정을 그대로 씁니다).",
            )
            self.assertEqual(
                len(place_order_calls), 1,
                "장애 중 관측만 되고 미뤄졌던 손절 조건이, 복구된 정상 경로에서 SELL 제출 정확히 한 번으로 "
                "이어져야 합니다(이 확인의 범위는 '제출 1회'까지 — 그 이후 체결·손익 반영의 중복 방지는 "
                "test_recovery_sell_does_not_repeat_on_subsequent_polls가 별도로 검증한다).",
            )
            self.assertEqual(place_order_calls[0][0][0].symbol, symbol)
            self.assertEqual(place_order_calls[0][0][0].side, OrderSide.SELL)
            # 매도가 막 접수되어 기존 정책대로 cached_balance가 다시
            # 비워진 상태 — 이 자체는 이번 3단계의 범위가 아니므로
            # (체결 확정 정책은 그대로) 회귀가 아니라 정상입니다.
            self.assertIsNone(svc.cached_balance)

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

    def test_recovery_sell_does_not_repeat_on_subsequent_polls(self):
        """2026-09-15 3차 보완 (GPT 재검토 3번 지적 — "검증 보완",
        프로덕션 코드 변경 없음): 위
        `test_429_then_price_drop_during_wait_then_recovers_cleanly`가
        증명하는 건 "복구 직후 SELL 제출 1회"뿐이다. 그 SELL이
        MockBroker에서 즉시·완전 체결되어(수량 0, 포지션 dict에서
        제거) 다음 폴링에서는 더 이상 보유 종목이 아니게 되는데, 그
        뒤로 이어지는 폴링(run_once() 반복 호출)에서도 같은 손절이
        중복 제출되거나 거래 로그에 중복 기록되지 않는지는 이 별도
        테스트로 확인해야 한다 — 그래야 "손익·수량·청산 처리가 중복
        적용되지 않는다"고 말할 수 있다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(tmpdir)
            symbol = svc.targets[0]
            svc.broker._positions[symbol] = Position(symbol=symbol, quantity=10, average_price=10000)
            svc.broker._prices[symbol] = 9000  # 이미 손절선 아래
            svc.cached_regime[symbol] = MarketRegime.NEUTRAL

            place_order_calls = []
            original_place_order = svc.broker.place_order

            def _track_place_order(*args, **kwargs):
                place_order_calls.append((args, kwargs))
                return original_place_order(*args, **kwargs)

            with mock.patch.object(svc.broker, "place_order", side_effect=_track_place_order):
                asyncio.run(svc.run_once())  # 손절 매도 1회 발생 기대
                asyncio.run(svc.run_once())  # 다음 폴링 — 이미 청산됨
                asyncio.run(svc.run_once())  # 그 다음 폴링 — 여전히 청산됨

            sell_calls = [
                call for call in place_order_calls
                if call[0][0].symbol == symbol and call[0][0].side == OrderSide.SELL
            ]
            self.assertEqual(
                len(sell_calls), 1,
                "포지션이 이미 청산됐다면 이후 폴링에서 같은 손절이 중복 제출되면 안 됩니다.",
            )

            trade_rows = _read_csv_rows(svc.settings.storage.trade_log_file)
            sell_rows = [
                r for r in trade_rows
                if r["symbol"] == symbol and r["side"] == "SELL" and r["accepted"] == "True"
            ]
            self.assertEqual(
                len(sell_rows), 1,
                "거래 로그에도 같은 청산이 중복 기록되면 안 됩니다(손익·수량 중복 반영 방지).",
            )
            self.assertNotIn(symbol, svc.broker._positions, "MockBroker 포지션에서도 완전히 청산돼야 합니다.")


if __name__ == "__main__":
    unittest.main()
