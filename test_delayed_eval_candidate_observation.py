# -*- coding: utf-8 -*-
"""2026-09-11 (S02 관측 1단계, GPT 5차 검토 반영)
→ 2026-09-14 (GPT 재검토 20260914 반영) — 지연 평가 후보 관측 로그.

배경: entry_watch는 실제 최소수익 판정 시점 근방(watch_minutes<=
elapsed_min<=watch_minutes+1)을 넘기면 `_check_entry_watch()`가 그냥
None을 반환하고 정규 전략에 위임합니다 — 그 판정 시점을 놓친 진입 건
(예: 프로세스 재시작, 시세 데이터 장기 stale)은 최소수익미달 판정
자체가 영구히 누락됩니다. 이번 라운드는 그 "놓친 판정 시점"을 관측만
하는 로그(DelayedEvalCandidateLogger)와, 그 판정 시점 근방에서 유효
평가가 있었는지를 진입 건별로 영속 기록하는 필드(entry_watch_normal_
eval_seen_by_symbol)만 추가합니다. 지연 청산 SELL 자체는 이 라운드에서
활성화하지 않습니다.

2026-09-14 갱신(GPT 재검토): 최초 구현은 "정상 창(0~watch_minutes+1)
안에서 아무 때나 avg>0 평가가 있으면 봄"으로 기록해서, 초반 폴링
한 번만 있어도(거의 항상 있음) 정작 판정 시점(watch_minutes 근방)을
놓친 진입 건까지 후보에서 빠지는 결함이 재현됐습니다(2분 평가 →
5~6분 공백 → 7분 복귀해도 후보 0건). 기록 조건을 판정 시점 근방
(watch_minutes<=elapsed_min<=watch_minutes+1)으로 좁히고, 기록값에
entry_time을 함께 실어(청산 정리가 지연돼도 다른 진입 건의 이력에
가려지지 않도록) 문제를 해결했습니다.

이 테스트가 확인하는 것:
1. 판정 시점 근방(watch_minutes<=elapsed_min<=watch_minutes+1)에서
   avg>0인 평가가 있었으면 entry_watch_normal_eval_seen_by_symbol에
   최초 시각이 기록됩니다.
2. 판정 시점 이전(elapsed_min<watch_minutes)의 평가는 기록되지 않습니다
   — 이 규칙이 없으면 2번 항목의 재현 사례처럼 실제로 놓친 진입 건이
   후보에서 빠집니다.
3. 판정 시점을 넘겼는데 그 근방에서 유효 평가가 전혀 없었으면 지연
   평가 후보로 (symbol, entry_time) 최초 1건만 기록됩니다 — 이후
   폴링에서는 중복 기록되지 않습니다.
4. 판정 시점 근방에서 유효 평가가 있었던 진입 건은(그 평가 결과가
   SELL이든 아니든) 창을 넘겨도 지연 평가 후보로 기록되지 않습니다 —
   entry_watch 범위 밖(기존 주문 추적에 위임)이라는 5차 설계 원칙을
   그대로 반영하되, "평가 있었음"의 기준을 판정 시점 근방으로 정확히
   좁혔습니다.
5. 청산 확정의 단일 공식 지점(_apply_deferred_sell_side_effects())에서
   두 필드 모두 정리되어 다음 진입에 이어지지 않습니다 — 이전엔
   _process_symbol()의 무보유 분기 정리에만 의존해 정리가 늦어질
   가능성이 있었습니다.
6. RuntimeState의 새 필드가 JsonStateStore.save()/load() 왕복에서
   손실 없이 보존되고, StateReconciler가 보유 종목 기준으로 정리합니다.
7. `_check_entry_watch()`의 기존 SELL 판정(1/2/3번 분기)과 반환값은
   legacy_tests/test_entry_watch.py의 8개 시나리오 그대로 완전히
   동일합니다 — 이 파일은 그 시나리오를 다시 실행해 회귀를 재확인합니다.

BUY/HOLD/SELL 로직, watch_minutes/min_profit_pct/fail_cut_pct 등 전략
파라미터는 이 라운드에서 단 한 줄도 바뀌지 않았습니다.
"""
from __future__ import annotations

import csv
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
    svc._pending_sell_side_effects = {}

    class _AppLogger:
        def warning(self, *a, **kw):
            pass

        def info(self, *a, **kw):
            pass

    svc.app_logger = _AppLogger()

    class _Notifier:
        def send(self, *a, **kw):
            pass

    svc._notifier = _Notifier()
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
    """watch_minutes=5(EW 기준)이므로 판정 구간은 elapsed_min이
    5~6분(watch_minutes<=elapsed_min<=watch_minutes+1)입니다."""

    def test_evaluation_at_decision_window_records_first_timestamp(self):
        svc = make_service()
        symbol = "005930"
        entry_time = (datetime.now() - timedelta(minutes=5, seconds=20)).isoformat()
        svc.state.entry_time_by_symbol[symbol] = entry_time
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        self.assertNotIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)
        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        self.assertIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "판정 시점 근방(watch_minutes~+1분)에서 avg>0 평가가 있었으면 이력이 기록돼야 합니다.",
        )
        recorded_entry_time = svc.state.entry_watch_normal_eval_seen_by_symbol[symbol].partition("|")[0]
        self.assertEqual(recorded_entry_time, entry_time, "기록값에 현재 진입의 entry_time이 함께 실려야 합니다.")

    def test_evaluation_before_decision_window_does_not_record(self):
        """2026-09-14 GPT 재검토로 재현된 결함의 핵심 — 판정 시점
        이전(elapsed_min<watch_minutes)의 평가는 '판정을 실제로
        받았다'는 근거가 아니므로 기록되면 안 됩니다."""
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "판정 시점 이전 평가는 '봄'으로 기록되면 안 됩니다(놓친 판정 시점을 가리는 결함 재현 방지).",
        )

    def test_avg_zero_does_not_count_as_valid_evaluation(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=20)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=0)  # 방어적 케이스

        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "avg<=0이면 함수 자체가 평가를 하지 않으므로(기존 로직) 이력도 남으면 안 됩니다.",
        )

    def test_first_timestamp_is_not_overwritten_on_repeated_polls(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=10)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
        first = svc.state.entry_watch_normal_eval_seen_by_symbol[symbol]
        svc._check_entry_watch(symbol, pos, current_price=10090, minute_analysis=None)
        second = svc.state.entry_watch_normal_eval_seen_by_symbol[symbol]
        self.assertEqual(first, second, "같은 진입 건은 최초 평가 시각만 기록하고 이후 폴링에서 덮어쓰지 않아야 합니다.")

    def test_fail_cut_early_return_within_window_does_not_record_seen(self):
        """2026-09-14 GPT 재검토 2차 반영 — 재현된 결함: 기록 위치가
        1)/2) 분기보다 앞이면, 5~6분 구간에서 급락청산으로 먼저
        SELL을 반환해도 실제로는 아직 실행되지 않은 3번 분기(최소수익
        비교)가 "평가함"으로 잘못 기록됩니다. 기록을 3번 분기 직전으로
        옮긴 뒤에는 이 경우 기록되지 않아야 합니다."""
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=30)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)  # -1.2%

        self.assertIsNotNone(sig)
        self.assertEqual(sig.type, SignalType.SELL)
        self.assertIn("급락청산", sig.reason)
        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "급락청산으로 먼저 반환되면 최소수익 비교(3번 분기)에 도달하지 않았으므로 기록되면 안 됩니다.",
        )

    def test_vwap_break_early_return_within_window_does_not_record_seen(self):
        """위와 동일한 이유로, VWAP 이탈청산(2번 분기)으로 먼저
        반환되는 경우도 최소수익 비교 이전이므로 기록되면 안 됩니다."""
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=30)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        ma = make_minute_analysis(price_above_vwap=False, vwap=10050.0)

        sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=ma)

        self.assertIsNotNone(sig)
        self.assertIn("VWAP이탈청산", sig.reason)
        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "VWAP이탈청산으로 먼저 반환되면 최소수익 비교(3번 분기)에 도달하지 않았으므로 기록되면 안 됩니다.",
        )

    def test_min_profit_comparison_reached_records_seen_even_when_sufficient(self):
        """최소수익 비교(3번 분기)에 실제로 도달했다면, 그 비교 결과가
        '충족'(SELL 미발생)이든 '미달'(SELL 발생)이든 관계없이
        기록돼야 합니다 — 기록 대상은 '비교를 실제로 했는가'이지
        '얼마나 손해를 봤는가'가 아닙니다."""
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=30)).isoformat()
        pos = Position(symbol=symbol, quantity=10, average_price=10000)
        ma = make_minute_analysis(price_above_vwap=True)

        # +0.8% > min_profit_pct(0.5%) → 충족, SELL 없음(정규전략 위임).
        sig = svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=ma)

        self.assertIsNone(sig)
        self.assertIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "최소수익 비교에 실제로 도달했다면 충족 여부와 무관하게 기록돼야 합니다.",
        )


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

    def test_valid_evaluation_at_decision_window_prevents_candidate_logging(self):
        """5차 설계 원칙: 판정 시점 근방(watch_minutes~+1분)에서 평가가
        한 번이라도 있었다면(그 결과 SELL이든 아니든) entry_watch 범위
        밖으로 넘어가며, 지연 평가 후보로 기록되지 않아야 합니다.

        같은 진입 건(entry_time 고정)에 대해 "5분 20초 경과 시점의
        평가" → "7분 경과 시점의 재확인"을 재현하기 위해, 실제 대기
        대신 trading_service 모듈의 `datetime.now()`만 통제합니다."""
        from unittest.mock import patch
        import domain.service.trading_service as ts_module

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            entry_dt = datetime(2026, 9, 14, 9, 0, 0)
            entry_time = entry_dt.isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time

            class _FrozenDateTime(datetime):
                _now = entry_dt

                @classmethod
                def now(cls, tz=None):
                    return cls._now

            with patch.object(ts_module, "datetime", _FrozenDateTime):
                # 1) 판정 시점 근방(5분 20초 경과)에서 양호한 평가 1회
                #    발생(None 반환, 정규전략 위임이지만 avg>0 평가
                #    자체는 일어남).
                _FrozenDateTime._now = entry_dt + timedelta(minutes=5, seconds=20)
                sig = svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
                self.assertIsNone(sig)
                self.assertIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)

                # 2) 창을 넘긴 뒤(7분 경과) 같은 (symbol, entry_time)으로
                #    다시 호출해도 이미 유효 평가 이력이 있으므로 후보로
                #    기록되면 안 됨.
                _FrozenDateTime._now = entry_dt + timedelta(minutes=7)
                svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertEqual(
                len(logger._seen_keys), 0,
                "판정 시점 근방에서 유효 평가가 있었던 건은 지연 평가 후보가 아닙니다.",
            )

    def test_early_evaluation_before_window_still_logs_candidate_when_window_missed(self):
        """2026-09-14 GPT 재검토 재현 사례 그대로: 같은 진입 건에 대해
        진입 2분 시점에 평가가 한 번 있었지만(판정 시점 이전이므로
        '봄'으로 기록되지 않음), 정작 판정 시점 근방(5~6분)에는 평가가
        없었고 7분에 복귀한 경우 — 이 진입 건은 지연 평가 후보로
        기록돼야 합니다(수정 전에는 2분 평가가 잘못 '봄'으로 기록돼
        후보가 0건으로 나오는 결함이 있었습니다).

        같은 진입 건(entry_time 고정)에 대해 서로 다른 경과시간(2분 →
        7분)을 재현하기 위해, 실제 대기 대신 trading_service 모듈의
        `datetime.now()`만 통제합니다 — entry_time_by_symbol은 시작부터
        끝까지 고정된 값 하나만 씁니다.
        """
        from unittest.mock import patch
        import domain.service.trading_service as ts_module

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            entry_dt = datetime(2026, 9, 14, 9, 0, 0)
            entry_time = entry_dt.isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time

            class _FrozenDateTime(datetime):
                _now = entry_dt

                @classmethod
                def now(cls, tz=None):
                    return cls._now

            with patch.object(ts_module, "datetime", _FrozenDateTime):
                # 1) 진입 2분 시점 — 판정 시점 이전이므로 평가는 일어나도
                #    "봄"으로 기록되지 않아야 함(위 클래스에서 이미 확인).
                _FrozenDateTime._now = entry_dt + timedelta(minutes=2)
                svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=None)
                self.assertNotIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)

                # 2) 5~6분 구간 공백(시세 stale 등으로 폴링이 비었다고
                #    가정) 후 7분에 복귀 — 같은 (symbol, entry_time).
                _FrozenDateTime._now = entry_dt + timedelta(minutes=7)
                sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig, "이 라운드는 지연 청산 SELL을 만들지 않습니다.")
            self.assertEqual(
                logger._seen_keys, {(symbol, entry_time)},
                "판정 시점을 놓친 진입 건은 초반 조기 평가와 무관하게 후보로 기록돼야 합니다.",
            )

    def test_fail_cut_sell_within_window_then_order_fails_still_logs_candidate_later(self):
        """2026-09-14 GPT 재검토 2차 반영 — end-to-end 재현: 5~6분
        구간에서 급락청산 SELL이 반환됐지만(예: 그 뒤 실제 주문이
        실패해 포지션이 그대로 남았다고 가정) 최소수익 비교(3번 분기)
        자체는 실행되지 않았으므로, 창을 넘긴 뒤에도 이 진입 건은
        여전히 지연 평가 후보로 기록돼야 합니다."""
        from unittest.mock import patch
        import domain.service.trading_service as ts_module

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            entry_dt = datetime(2026, 9, 14, 9, 0, 0)
            entry_time = entry_dt.isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time

            class _FrozenDateTime(datetime):
                _now = entry_dt

                @classmethod
                def now(cls, tz=None):
                    return cls._now

            with patch.object(ts_module, "datetime", _FrozenDateTime):
                # 1) 5분 30초 시점 — 급락청산 SELL 반환(하지만 실제
                #    주문은 실패해 포지션이 남아있다고 가정).
                _FrozenDateTime._now = entry_dt + timedelta(minutes=5, seconds=30)
                sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)
                self.assertIsNotNone(sig)
                self.assertIn("급락청산", sig.reason)
                self.assertNotIn(symbol, svc.state.entry_watch_normal_eval_seen_by_symbol)

                # 2) 7분 시점 복귀 — 창을 넘겼고, 최소수익 비교는 한
                #    번도 실행되지 못했으므로 후보로 기록돼야 함.
                _FrozenDateTime._now = entry_dt + timedelta(minutes=7)
                sig2 = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig2, "이 라운드는 지연 청산 SELL을 만들지 않습니다.")
            self.assertEqual(
                logger._seen_keys, {(symbol, entry_time)},
                "급락청산 SELL 반환은 최소수익 비교를 대신하지 않으므로, 주문 실패로 포지션이 "
                "남으면 여전히 지연 평가 후보로 기록돼야 합니다.",
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
    """2026-09-14 (GPT 재검토 20260914 반영): 청산 확정의 단일 공식
    지점인 `_apply_deferred_sell_side_effects()`를 실제로 호출해
    정리를 검증합니다 — 이전엔 서비스 코드를 거치지 않고 테스트
    안에서 직접 `dict.pop()`을 호출해서, 실제 정리 누락(리뷰에서
    지적된 "_apply_deferred_sell_side_effects()가 이 필드를 지우지
    않는다"는 문제)을 잡지 못하는 테스트였습니다."""

    def test_apply_deferred_sell_side_effects_clears_normal_eval_seen_field(self):
        svc = make_service()
        symbol = "005930"
        entry_time = "2026-09-11T09:05:00"
        svc.state.entry_time_by_symbol[symbol] = entry_time
        svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = f"{entry_time}|2026-09-11T09:01:00"
        svc.state.vwap_break_streak_by_symbol[symbol] = 2
        svc._pending_sell_side_effects[symbol] = {
            "exit_reason": "entry_watch 급락청산",
            "avg_buy_price": 10000,
            "current_price": 9800,
            "quantity": 10,
        }

        svc._apply_deferred_sell_side_effects(symbol)

        self.assertNotIn(
            symbol, svc.state.entry_watch_normal_eval_seen_by_symbol,
            "청산 확정의 단일 공식 지점에서 이 필드도 함께 정리돼야 합니다.",
        )
        self.assertNotIn(symbol, svc.state.entry_time_by_symbol)

    def test_reentry_after_liquidation_starts_with_clean_eval_history(self):
        """청산 → 같은 종목 재진입 경로 — 이전 진입의 평가 이력이
        새 진입의 관측을 가리면 안 됩니다."""
        svc = make_service()
        symbol = "005930"
        old_entry_time = "2026-09-11T09:00:00"
        svc.state.entry_time_by_symbol[symbol] = old_entry_time
        svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = f"{old_entry_time}|2026-09-11T09:05:30"
        svc._pending_sell_side_effects[symbol] = {
            "exit_reason": "entry_watch 최소수익미달청산",
            "avg_buy_price": 10000,
            "current_price": 9900,
            "quantity": 10,
        }
        svc._apply_deferred_sell_side_effects(symbol)

        # 같은 종목 재진입 — 새 entry_time으로 갱신됐다고 가정.
        new_entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
        svc.state.entry_time_by_symbol[symbol] = new_entry_time
        pos = Position(symbol=symbol, quantity=10, average_price=11000)

        with tempfile.TemporaryDirectory() as tmpdir:
            svc.delayed_eval_candidate_logger = DelayedEvalCandidateLogger(
                f"{tmpdir}/delayed_eval_candidate.csv"
            )
            svc._check_entry_watch(symbol, pos, current_price=10800, minute_analysis=None)
            self.assertEqual(
                svc.delayed_eval_candidate_logger._seen_keys, {(symbol, new_entry_time)},
                "이전 진입의 이력이 정리됐으므로 새 진입은 정상적으로 지연 평가 후보로 잡혀야 합니다.",
            )

    def test_stale_seen_record_from_uncleaned_prior_entry_does_not_mask_new_entry(self):
        """이중 안전장치 검증 — 어떤 이유로든(예: 스테일 잔고 때문에
        `_process_symbol()`의 무보유 분기가 늦게 판단됨) 청산 정리가
        빠졌다고 가정해도, 이전 진입(old_entry_time)의 '봄' 기록이
        entry_time이 다른 새 진입까지 잘못 가리면 안 됩니다."""
        svc = make_service()
        symbol = "005930"
        old_entry_time = "2026-09-11T09:00:00"
        # 정리가 빠진 상태를 그대로 재현 — entry_time_by_symbol도
        # entry_watch_normal_eval_seen_by_symbol도 지우지 않음.
        svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = f"{old_entry_time}|2026-09-11T09:05:30"

        new_entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
        svc.state.entry_time_by_symbol[symbol] = new_entry_time
        pos = Position(symbol=symbol, quantity=10, average_price=11000)

        with tempfile.TemporaryDirectory() as tmpdir:
            svc.delayed_eval_candidate_logger = DelayedEvalCandidateLogger(
                f"{tmpdir}/delayed_eval_candidate.csv"
            )
            svc._check_entry_watch(symbol, pos, current_price=10800, minute_analysis=None)
            self.assertEqual(
                svc.delayed_eval_candidate_logger._seen_keys, {(symbol, new_entry_time)},
                "entry_time이 다른 이전 이력은 새 진입의 관측을 가리면 안 됩니다(1차 안전장치가 "
                "빠져도 이 검사가 2차로 막아줍니다).",
            )


class TestStatePersistenceRoundTrip(unittest.TestCase):

    def test_eval_seen_survives_save_reload_and_still_suppresses_candidate_after_window(self):
        """재현 시나리오(GPT 재검토 3차): 정상 창 안에서 평가 →
        state.json 저장 → (재시작을 흉내내) 새 서비스 인스턴스가 그
        state를 로드 → 창을 넘긴 뒤 재확인해도, 같은 진입 건은 여전히
        지연 평가 후보로 기록되지 않아야 합니다."""
        from unittest.mock import patch
        import domain.service.trading_service as ts_module

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonStateStore(f"{tmpdir}/state.json")
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")

            symbol = "005930"
            pos = Position(symbol=symbol, quantity=10, average_price=10000)
            entry_dt = datetime(2026, 9, 14, 9, 0, 0)
            entry_time = entry_dt.isoformat()

            class _FrozenDateTime(datetime):
                _now = entry_dt

                @classmethod
                def now(cls, tz=None):
                    return cls._now

            # 1) 서비스 인스턴스 1 — 판정 시점 근방(5분 30초)에서 평가.
            svc1 = make_service(delayed_eval_candidate_logger=logger)
            svc1.state.entry_time_by_symbol[symbol] = entry_time
            with patch.object(ts_module, "datetime", _FrozenDateTime):
                _FrozenDateTime._now = entry_dt + timedelta(minutes=5, seconds=30)
                svc1._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=None)
            self.assertIn(symbol, svc1.state.entry_watch_normal_eval_seen_by_symbol)

            store.save(svc1.state, {})

            # 2) "재시작" — 새 서비스 인스턴스가 저장된 state를 로드.
            loaded_state, _ = store.load()
            svc2 = make_service(delayed_eval_candidate_logger=logger)
            svc2.state = loaded_state
            svc2.state.entry_time_by_symbol[symbol] = entry_time

            # 3) 창을 넘긴 뒤(7분) 재확인 — 후보로 기록되면 안 됨.
            with patch.object(ts_module, "datetime", _FrozenDateTime):
                _FrozenDateTime._now = entry_dt + timedelta(minutes=7)
                sig = svc2._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig)
            self.assertEqual(
                logger._seen_keys, set(),
                "재시작 전에 이미 판정 시점 근방에서 평가된 진입 건은 재시작 후에도 지연 평가 "
                "후보로 기록되면 안 됩니다.",
            )

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

    def test_json_state_store_drops_non_string_values_on_load(self):
        """2026-09-14 (GPT 재검토 3차 반영) — 손상된 state.json에
        entry_watch_normal_eval_seen_by_symbol 값으로 null/숫자 등이
        들어와도 load()가 이를 걸러내고, 다른 필드는 그대로 보존해야
        합니다."""
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/state.json"
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({
                    "entry_time_by_symbol": {"005930": "2026-09-11T09:00:00"},
                    "consecutive_losses": 2,
                    "entry_watch_normal_eval_seen_by_symbol": {
                        "005930": None,
                        "000660": 12345,
                        "005380": ["not", "a", "string"],
                        "035720": "2026-09-14T09:00:00|2026-09-14T09:05:30",
                    },
                }, fp)

            store = JsonStateStore(path)
            loaded_state, _ = store.load()

            self.assertEqual(
                loaded_state.entry_watch_normal_eval_seen_by_symbol,
                {"035720": "2026-09-14T09:00:00|2026-09-14T09:05:30"},
                "문자열이 아닌 값(null/숫자/리스트)은 걸러지고, 정상 문자열 값만 남아야 합니다.",
            )
            self.assertEqual(loaded_state.consecutive_losses, 2, "다른 필드는 영향받지 않아야 합니다.")


class TestMalformedObservationStateDoesNotBlockSell(unittest.TestCase):
    """2026-09-14 (GPT 재검토 3차 반영) — 재현된 결함: entry_watch_
    normal_eval_seen_by_symbol에 예상치 못한 값이 들어오면
    `_check_entry_watch()`가 AttributeError로 죽어서, 그 폴링의 실제
    SELL 판정(급락/VWAP/최소수익)까지 함께 막혔습니다. 관측 실패가
    매매 판단을 막으면 절대 안 된다는 설계 원칙을 지키는지 확인합니다.
    """

    def test_none_value_does_not_block_min_profit_sell(self):
        svc = make_service()
        symbol = "005930"
        svc.state.entry_time_by_symbol[symbol] = (
            datetime.now() - timedelta(minutes=5, seconds=30)
        ).isoformat()
        svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = None
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=None)

        self.assertIsNotNone(sig, "관측값이 손상돼도 최소수익미달 SELL 판정은 그대로 나와야 합니다.")
        self.assertEqual(sig.type, SignalType.SELL)
        self.assertIn("최소수익미달청산", sig.reason)

    def test_non_string_value_does_not_block_delayed_candidate_logging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = 12345  # 손상된 값
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig, "이 라운드는 지연 청산 SELL을 만들지 않습니다.")
            self.assertEqual(
                logger._seen_keys, {(symbol, entry_time)},
                "관측값이 손상돼도 지연 평가 후보 기록 자체는 정상적으로 동작해야 합니다.",
            )

    def test_malformed_state_loaded_from_real_json_file_does_not_block_sell(self):
        """실제 JsonStateStore.load() 경로로 손상된 state.json을 읽어도
        (state_store.py의 1차 방어를 우회한다고 가정해도, 즉
        trading_service.py의 2차 방어만으로도) SELL 판정이 막히지
        않는지 end-to-end로 확인합니다."""
        state = RuntimeState()
        symbol = "005930"
        entry_time = (datetime.now() - timedelta(minutes=5, seconds=30)).isoformat()
        state.entry_time_by_symbol[symbol] = entry_time
        # state_store.py의 로딩 시 필터를 거치지 않고 직접 손상된 값을
        # 주입 — trading_service.py 쪽 방어(2차 안전장치)만으로도
        # 안전한지 확인하기 위함.
        state.entry_watch_normal_eval_seen_by_symbol[symbol] = None

        svc = make_service()
        svc.state = state
        pos = Position(symbol=symbol, quantity=10, average_price=10000)

        sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=None)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.type, SignalType.SELL)


class TestDelayedCandidatePriceValidity(unittest.TestCase):
    """2026-09-14 (GPT 재검토 3차 반영) — 재현된 결함: 지연 후보 기록이
    current_price<=0(시세 글리치 등)이어도 그대로 (symbol, entry_time)
    dedup 슬롯을 소모해서, 이후 정상 가격이 들어와도 다시 기록되지
    않았습니다."""

    def test_zero_price_does_not_consume_dedup_slot_valid_price_still_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            # 1) 무효 가격(0) — 기록은 되지만 dedup 슬롯을 소모하면 안 됨.
            svc._check_entry_watch(symbol, pos, current_price=0, minute_analysis=None)
            self.assertEqual(
                logger._seen_keys, set(),
                "무효 가격 관측은 dedup 슬롯을 소모하면 안 됩니다.",
            )

            # 2) 유효 가격 — 이제 정상적으로 기록돼야 함.
            svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)
            self.assertEqual(
                logger._seen_keys, {(symbol, entry_time)},
                "무효 가격 관측 다음의 유효 가격 관측은 정상적으로 기록돼야 합니다.",
            )

            rows = list(csv.DictReader(open(f"{tmpdir}/delayed_eval_candidate.csv", encoding="utf-8")))
            self.assertEqual(len(rows), 2, "무효 관측 1건 + 유효 관측 1건, 총 2행이 남아야 합니다.")
            self.assertEqual(rows[0]["price_valid"], "False")
            self.assertEqual(rows[1]["price_valid"], "True")

    def test_negative_avg_price_is_treated_as_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            pos = Position(symbol=symbol, quantity=10, average_price=-1)

            svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertEqual(logger._seen_keys, set())
            rows = list(csv.DictReader(open(f"{tmpdir}/delayed_eval_candidate.csv", encoding="utf-8")))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["price_valid"], "False")


class TestLegacyFormatObservationState(unittest.TestCase):
    """2026-09-14 (GPT 재검토 3차 반영) — 배포 경계: 2026-09-14 이전
    구형식(entry_time 결합 이전, bare timestamp)으로 저장된 관측값이
    새 코드에서 크래시하거나 SELL 판정에 영향을 주면 안 되고, 지연
    후보로 기록될 때는 "legacy"로 구분 표시돼야 합니다."""

    def test_legacy_format_does_not_crash_and_is_tagged_in_candidate_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            # 구형식 — entry_time 없이 평가 시각만 저장된 상태.
            svc.state.entry_watch_normal_eval_seen_by_symbol[symbol] = "2026-09-11T09:01:30"
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            self.assertIsNone(sig, "이 라운드는 지연 청산 SELL을 만들지 않습니다.")
            self.assertEqual(logger._seen_keys, {(symbol, entry_time)})
            rows = list(csv.DictReader(open(f"{tmpdir}/delayed_eval_candidate.csv", encoding="utf-8")))
            self.assertEqual(rows[0]["prior_seen_format"], "legacy")

    def test_no_prior_record_is_tagged_none_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = DelayedEvalCandidateLogger(f"{tmpdir}/delayed_eval_candidate.csv")
            svc = make_service(delayed_eval_candidate_logger=logger)
            symbol = "005930"
            entry_time = (datetime.now() - timedelta(minutes=10)).isoformat()
            svc.state.entry_time_by_symbol[symbol] = entry_time
            pos = Position(symbol=symbol, quantity=10, average_price=10000)

            svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)

            rows = list(csv.DictReader(open(f"{tmpdir}/delayed_eval_candidate.csv", encoding="utf-8")))
            self.assertEqual(rows[0]["prior_seen_format"], "none")


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
