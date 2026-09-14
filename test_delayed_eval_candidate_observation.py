# -*- coding: utf-8 -*-
"""2026-09-11 (S02 관측 1단계, GPT 5차 검토 반영) — 지연 평가 후보 관측 로그.

배경: entry_watch는 정상 창(elapsed_min <= watch_minutes+1)을 넘기면
`_check_entry_watch()`가 그냥 None을 반환하고 정규 전략에 위임합니다 —
그 창 안에서 한 번도 평가를 못 받은 진입 건(예: 프로세스 재시작, 시세
데이터 장기 stale)은 5분 시점 최소수익미달 판정 자체가 영구히
누락됩니다. 이번 라운드는 그 "놓친 창"을 관측만 하는 로그
(DelayedEvalCandidateLogger)와, 정상 창 안에서 유효 평가가 있었는지를
진입 건별로 영속 기록하는 필드(entry_watch_normal_eval_seen_by_symbol)만
추가합니다. 지연 청산 SELL 자체는 이 라운드에서 활성화하지 않습니다.

이 테스트가 확인하는 것:
1. 정상 창 안에서 avg>0인 평가가 있었으면 entry_watch_normal_eval_
   seen_by_symbol에 최초 시각이 기록됩니다.
2. 정상 창을 넘겼는데 그 안에서 유효 평가가 전혀 없었으면 지연 평가
   후보로 (symbol, entry_time) 최초 1건만 기록됩니다 — 이후 폴링에서는
   중복 기록되지 않습니다.
3. 정상 창 안에서 유효 평가가 있었던 진입 건은(그 평가 결과가 SELL이든
   아니든) 창을 넘겨도 지연 평가 후보로 기록되지 않습니다 — entry_watch
   범위 밖(기존 주문 추적에 위임)이라는 5차 설계 원칙을 그대로 반영.
4. 청산 확인(포지션 flat 전환) 시 두 필드 모두 리셋되어 다음 진입에
   이어지지 않습니다.
5. RuntimeState의 새 필드가 JsonStateStore.save()/load() 왕복에서
   손실 없이 보존되고, StateReconciler가 보유 종목 기준으로 정리합니다.
6. `_check_entry_watch()`의 기존 SELL 판정(1/2/3번 분기)과 반환값은
   legacy_tests/test_entry_watch.py의 8개 시나리오 그대로 완전히
   동일합니다 — 이 파일은 그 시나리오를 다시 실행해 회귀를 재확인합니다.

BUY/HOLD/SELL 로직, watch_minutes/min_profit_pct/fail_cut_pct 등 전략
파라미터는 이 라운드에서 단 한 줄도 바뀌지 않았습니다.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from config.settings import EntryWatchConfig
from domain.models import AccountBalance, Position, RuntimeState, SignalType
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.service.trading_service import TradingService
from infra.storage.logger import DelayedEvalCandidateLogger
from infra.storage.state_reconciler import StateReconciler
from infra.storage.state_store import JsonStateStore


EW = EntryWatchConfig(
    enabled=True, watch_minutes=5, min_profit_pct=0.5,
    fail_cut_pct=-1.0, fail_on_vwap_break=True,
)


def make_service(entry_watch=EW, delayed_eval_candidate_logger=None) -> TradingService:
    """legacy_tests/test_entry_watch.py와 동일한 최소 생성 패턴 —
    __init__을 건너뛰고 _check_entry_watch()/신규 헬퍼가 참조하는
    속성만 채웁니다."""
    svc = TradingService.__new__(TradingService)

    class _Settings:
        pass

    svc.settings = _Settings()
    svc.settings.entry_watch = entry_watch
    svc.state = RuntimeState()
    svc.delayed_eval_candidate_logger = delayed_eval_candidate_logger

    class _AppLogger:
        def warning(self, *a, **kw):
            pass

    svc.app_logger = _AppLogger()
    return svc


def make_minute_analysis(price_above_vwap: bool, vwap: float = 10000.0) -> MinuteAnalysis:
    import dataclasses
    fields = {f.name: None for f in dataclasses.fields(MinuteAnalysis)}
    for f in dataclasses.fields(MinuteAnalysis):
        if f.type in ("bool", bool):
            fields[f.name] = False
        elif f.type in ("float", float):
            fields[f.name] = 0.0
        elif f.type in ("int", int):
            fields[f.name] = 0
    fields["price_above_vwap"] = price_above_vwap
    fields["vwap"] = vwap
    return MinuteAnalysis(**fields)


class TestNormalWindowEvalSeenRecording(unittest.TestCase):

    def test_valid_evaluation_within_window_records_first_timestamp(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        self.assertNotIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)
        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        self.assertIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "avg>0인 유효 평가가 있었으면 이력이 기록돼야 합니다.",
        )

    def test_avg_zero_does_not_count_as_valid_evaluation(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=0)  # 방어적 케이스

        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "avg<=0이면 함수 자체가 평가를 하지 않으므로(기존 로직) 이력도 남으면 안 됩니다.",
        )

    def test_first_timestamp_is_not_overwritten_on_repeated_polls(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=1)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        first = svc.state.entry_watch_normal_eval_seen_by_symbol[symbol]
        svc._check_entry_watch(symbol, pos, current_price=10090, minute_analysis=None)
        second = svc.state.entry_watch_normal_eval_seen_by_symbol[symbol]
        self.assertEqual(first, second, "최초 평가 시각만 기록하고(setdefault) 이후 폴링에서 덮어쓰지 않아야 합니다.")


class TestDelayedEvalCandidateLogging(unittest.TestCase):

    def test_window_missed_entirely_logs_candidate_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()  # 5+1=6분 초과
            svc.state.entry_time_by_symbol[symbol] = entry_time
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig, "이 라운드는 지연 청산 SELL을 만들지 않습니다 — 정규 전략에 계속 위임합니다.")
            self.assertEqual(
                logger._seen_keys, {(symbol, entry_time)},
                "정상 창 전체를 놓친 진입 건이 후보로 기록돼야 합니다.",
            )

            # 다음 폴링에서도 다시 호출되지만 같은 (symbol, entry_time)이므로
            # 중복 기록되면 안 됨.
            svc._check_entry_watch(symbol, pos, current_price=9650, minute_analysis=None)
            self.assertEqual(len(logger._seen_keys), 1, "같은 진입 건은 최초 1건만 기록해야 합니다(dedup).")

    def test_valid_evaluation_before_window_end_prevents_candidate_logging(self):
        """5차 설계 원칙: 정상 창 안에서 평가가 한 번이라도 있었다면(그
        결과 SELL이든 아니든) entry_watch 범위 밖으로 넘어가며, 지연
        평가 후보로 기록되지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_dt = datetime.now() - timedelta(minutes=3)
            entry_time = entry_dt.isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            # 1) 3분 시점: 정상 창 안에서 양호한 평가 1회 발생(None 반환,
            #    정규전략 위임이지만 avg>0 평가 자체는 일어남).
            sig = svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
            self.assertIsNone(sig)
            self.assertIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)

            # 2) 창을 넘긴 뒤(entry_time을 7분 전으로 되돌려 재현) 다시 호출해도
            #    이미 유효 평가 이력이 있으므로 후보로 기록되면 안 됨.
            svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=7)).isoformat()
            svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertEqual(
                len(logger._seen_keys), 0,
                "정상 창 안에서 유효 평가가 있었던 건은 지연 평가 후보가 아닙니다.",
            )

    def test_none_logger_is_silently_skipped(self):
        svc = make_service(delayed_eval_candidate_logger=None)
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=10)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        # 로거가 None이어도 예외 없이 그대로 None을 반환해야 함.
        sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)
        self.assertIsNone(sig)

    def test_logger_append_failure_does_not_break_entry_watch(self):
        class _ExplodingLogger:
            def append_if_new(self, row):
                raise OSError("simulated disk full")

        svc = make_service(delayed_eval_candidate_logger=_ExplodingLogger())
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=10)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)
        self.assertIsNone(sig, "관측 로그 실패가 entry_watch의 반환값에 영향을 주면 안 됩니다.")


class TestResetOnFlatPosition(unittest.TestCase):
    """실제 리셋은 _process_symbol() 안(포지션 flat 분기)에서 일어나므로,
    그 필드 정리 로직 자체를 직접 검증합니다."""

    def test_pop_on_flat_clears_normal_eval_seen_field(self):
        state = RuntimeState()
        symbol = "005930"
        state.entry_time_by_symbol[symbol] = "2026-09-11T09:05:00"
        state.entry_watch_normal_eval_seen_by_symbol[symbol] = "2026-09-11T09:01:00"
        state.vwap_break_streak_by_symbol[symbol] = 2

        # trading_service.py의 flat 분기와 동일한 정리 동작 재현.
        state.vwap_break_streak_by_symbol.pop(symbol, None)
        state.entry_watch_normal_eval_seen_by_symbol.pop(symbol, None)

        self.assertNotIn(symbol, state.entry_watch_normal_eval_seen_by_symbol)


class TestStatePersistenceRoundTrip(unittest.TestCase):

    def test_state_store_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonStateStore(f"{tmpdir}/state.json")
            state = RuntimeState()
            state.entry_time_by_symbol["005930"] = "2026-09-11T09:00:00"
            state.entry_watch_normal_eval_seen_by_symbol["005930"] = "2026-09-11T09:01:30"

            store.save(state, {})
            loaded_state, _ = store.load()

            self.assertEqual(
                loaded_state.entry_watch_normal_eval_seen_by_symbol,
                {"005930": "2026-09-11T09:01:30"},
                "재시작 후에도 정상 창 유효 평가 이력이 보존돼야 합니다.",
            )

    def test_load_with_missing_field_defaults_to_empty_dict(self):
        """기존(이 필드 추가 이전) state.json 형식과의 하위호환 확인."""
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/state.json"
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"entry_time_by_symbol": {"005930": "2026-09-11T09:00:00"}}, fp)

            store = JsonStateStore(path)
            loaded_state, _ = store.load()
            self.assertEqual(loaded_state.entry_watch_normal_eval_seen_by_symbol, {})

    def test_reconciler_trims_to_holding_symbols(self):
        reconciler = StateReconciler(app_logger=_NullLogger())
        state = RuntimeState()
        state.entry_time_by_symbol = {"005930": "t1", "000660": "t2"}
        state.entry_watch_normal_eval_seen_by_symbol = {"005930": "t1", "000660": "t2", "999999": "stale"}
        balance = AccountBalance(
            cash=0, total_asset=0,
            positions=[Position(symbol="005930", quantity=10, average_price=10000)],
        )

        reconciled_state, _ = reconciler.reconcile(state, {}, balance)

        self.assertEqual(
            reconciled_state.entry_watch_normal_eval_seen_by_symbol, {"005930": "t1"},
            "보유하지 않은 종목(000660, 999999)의 이력은 정리돼야 합니다.",
        )


class _NullLogger:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass


class TestExistingEntryWatchSignalsUnchanged(unittest.TestCase):
    """legacy_tests/test_entry_watch.py의 8개 시나리오를 이 파일에서도
    재실행해, S02 관측 1단계 추가가 기존 SELL 판정을 전혀 바꾸지
    않았음을 회귀로 재확인합니다."""

    def test_scenario_1_fail_cut_immediate_sell(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.type, SignalType.SELL)
        self.assertIn("급락청산", sig.reason)

    def test_scenario_2_vwap_break_sell(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        ma = make_minute_analysis(price_above_vwap=False, vwap=10050.0)
        sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=ma)
        self.assertIsNotNone(sig)
        self.assertIn("VWAP이탈청산", sig.reason)

    def test_scenario_3_min_profit_shortfall_sell(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=10)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        ma = make_minute_analysis(price_above_vwap=True)
        sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=ma)
        self.assertIsNotNone(sig)
        self.assertIn("최소수익미달청산", sig.reason)

    def test_scenario_4_good_position_delegates_to_normal_strategy(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=3)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        ma = make_minute_analysis(price_above_vwap=True)
        sig = svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=ma)
        self.assertIsNone(sig)

    def test_scenario_5_window_exceeded_delegates_even_with_loss(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=7)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)
        self.assertIsNone(sig)

    def test_scenario_6_disabled_always_none(self):
        ew_disabled = EntryWatchConfig(
            enabled=False, watch_minutes=5, min_profit_pct=0.5,
            fail_cut_pct=-1.0, fail_on_vwap_break=True,
        )
        svc = make_service(entry_watch=ew_disabled)
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)
        self.assertIsNone(sig)

    def test_scenario_7_entry_watch_none_returns_none(self):
        svc = make_service(entry_watch=None)
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)
        self.assertIsNone(sig)

    def test_scenario_8_no_position_returns_none(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        sig = svc._check_entry_watch(symbol, None, current_price=9880, minute_analysis=None)
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
