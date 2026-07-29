# -*- coding: utf-8 -*-
"""
세션 지표(SessionMetrics) shadow 모드 검증 (2026-07-28, 1C단계, 2차)

배경: 1차 구현에 대한 GPT 코드리뷰로 발견된 문제들을 재현·수정
후 재검증:
1. 세션 병합에 날짜/시간 필터가 전혀 없어서 API가 반환한 60개
   (전일 봉 포함) 전부가 그대로 세션에 들어갔음(재현: 전일 43개+
   오늘 17개 입력 시 session_bar_count가 17이 아니라 60으로 오염).
2. session_metrics_ready가 "봉이 하나라도 있으면 True"였는데,
   원래 합의는 "세션 히스토리를 장 시작부터 확보했는가"였음.
3. 종목별 세션이 날짜 자체를 추적하지 않아, reset_daily_loss_
   counts() 호출이 누락되면 전일 세션이 새 거래일에 섞일 위험.
4. shadow 내부에서 MinuteAnalyzer.analyze()를 다시 호출해서, off
   는 analyze() 1회인데 shadow는 2회 호출되고 있었음(MinuteAnalyzer
   가 _last_v_fail_reasons를 바꾸는 상태성 객체라 "shadow는 상태를
   안 바꾼다"는 원칙 위반).

이 테스트는 GPT가 제시한 7가지 수정사항을 전부 재현·검증합니다.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.session_metrics import (
    merge_session_bars, build_session_metrics, format_session_metrics_log_line,
    SessionState,
)
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import MinuteBar
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from utils.time_utils import now_kst


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def build_service(tmpdir: str, session_mode: str = "off") -> TradingService:
    settings = build_minimal_settings(tmpdir)
    object.__setattr__(settings.experimental, "session_metrics_mode", session_mode)
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


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: 날짜·시간 필터링 (GPT 지시 1번, 가장 중요한 재현 케이스)
# ══════════════════════════════════════════════════════════════

# ── 1) 전일 43개 + 오늘 17개 -> 정확히 오늘 17개만 세션에 포함 ────
yesterday_bars = [MinuteBar(
    cntr_tm=f"2026072714{i:02d}00", open_price=50000, high_price=50100,
    low_price=49900, close_price=50000, volume=500, acc_volume=5000,
) for i in range(43)]
today_open = datetime(2026, 7, 28, 9, 0, 0)
today_bars = [MinuteBar(
    cntr_tm=(today_open + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=60000, high_price=60100, low_price=59900, close_price=60000,
    volume=500, acc_volume=5000,
) for i in range(17)]

state = merge_session_bars(None, yesterday_bars + today_bars, "20260728")
metrics = build_session_metrics(state)
check("1) 전일43개+오늘17개 입력 -> session_bar_count=17(정확히 GPT 지시 기대값)",
      metrics.session_bar_count == 17)
check("   session_low=59900(오늘 저가, 전일 저가 49900 아님)",
      metrics.session_low == 59900)
check("   filtered_other_date_count=43(정확히 GPT 지시 기대값)",
      metrics.filtered_other_date_count == 43)

# ── 2) 정규장 시간(09:00~15:30) 밖 봉은 filtered_outside_market_count로 집계 ──
early_bars = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 28, 8, 30) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=1000, high_price=1000, low_price=1000, close_price=1000,
    volume=1, acc_volume=1,
) for i in range(5)]  # 08:30~08:34, 장전
normal_bars = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 28, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=60000, high_price=60100, low_price=59900, close_price=60000,
    volume=500, acc_volume=5000,
) for i in range(10)]

state2 = merge_session_bars(None, early_bars + normal_bars, "20260728")
metrics2 = build_session_metrics(state2)
check("2) 장전(08:30~08:34) 5개 봉 -> session_bar_count=10(장전 봉 제외)",
      metrics2.session_bar_count == 10)
check("   filtered_outside_market_count=5", metrics2.filtered_outside_market_count == 5)

# ══════════════════════════════════════════════════════════════
# 2부: session_metrics_ready 의미 수정 (GPT 지시 2번)
# ══════════════════════════════════════════════════════════════

# ── 3) 장 시작부터 확보 -> ready=True, COMPLETE_FROM_OPEN ────────
from_open_bars = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 28, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=60000, high_price=60100, low_price=59900, close_price=60000,
    volume=500, acc_volume=5000,
) for i in range(30)]
state3 = merge_session_bars(None, from_open_bars, "20260728")
metrics3 = build_session_metrics(state3)
check("3) 09:00부터 시작 -> session_metrics_ready=True", metrics3.session_metrics_ready is True)
check("   readiness_reason=COMPLETE_FROM_OPEN", metrics3.readiness_reason == "COMPLETE_FROM_OPEN")

# ── 4) 13시 시작(12:01~13:00만 보유) -> ready=False, PARTIAL_SESSION ──
partial_base = datetime(2026, 7, 28, 12, 1, 0)
partial_bars = [MinuteBar(
    cntr_tm=(partial_base + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=60000, high_price=60100, low_price=59900, close_price=60000,
    volume=500, acc_volume=5000,
) for i in range(60)]
state4 = merge_session_bars(None, partial_bars, "20260728")
metrics4 = build_session_metrics(state4)
check("4) 프로그램을 13시에 시작(12:01~13:00만 보유) -> session_metrics_ready=False"
      "(정확히 GPT 지시 기대값)", metrics4.session_metrics_ready is False)
check("   readiness_reason=PARTIAL_SESSION", metrics4.readiness_reason == "PARTIAL_SESSION")

# ── 5) 세션 데이터 자체가 없음 -> NO_SESSION_DATA ─────────────────
empty_metrics = build_session_metrics(None)
check("5) 세션 상태 자체가 None -> readiness_reason=NO_SESSION_DATA",
      empty_metrics.readiness_reason == "NO_SESSION_DATA")
check("   ready=False", empty_metrics.session_metrics_ready is False)

# ══════════════════════════════════════════════════════════════
# 3부: 종목별 session_date 자동 초기화 (GPT 지시 3번)
# ══════════════════════════════════════════════════════════════

# ── 6) 날짜가 바뀐 새 봉이 들어오면 reset 호출 없이도 자동 초기화됨 ──
state_day1 = merge_session_bars(None, today_bars, "20260728")
check("6-준비) day1 세션에 17개 봉 존재", len(state_day1.bars) == 17)

day2_bars = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 29, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=70000, high_price=70100, low_price=69900, close_price=70000,
    volume=500, acc_volume=5000,
) for i in range(5)]
# reset_daily_loss_counts()를 호출하지 않고, 다음날 봉을 바로 병합
state_day2 = merge_session_bars(state_day1, day2_bars, "20260729")
check("6) reset 호출 없이 다음날 봉을 병합해도 자동으로 새 세션이 시작됨"
      "(전일 17개가 섞이지 않음)", len(state_day2.bars) == 5)
check("   session_date가 새 날짜로 갱신됨", state_day2.session_date == "20260729")
check("   필터링 카운터도 새 세션 기준으로 리셋됨(전날 카운터 안 남음)",
      state_day2.filtered_other_date_count == 0)

# ══════════════════════════════════════════════════════════════
# 4부: shadow 내부에서 MinuteAnalyzer.analyze() 재호출 금지 (GPT 지시 4번)
# ══════════════════════════════════════════════════════════════

kst_now_fixed = now_kst()
bars_60 = [MinuteBar(
    cntr_tm=(kst_now_fixed - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
    open_price=58000, high_price=58100, low_price=57900, close_price=58000,
    volume=1000, acc_volume=50000,
) for i in range(60)]

# ── 7) off 모드 -> analyze() 정확히 1회 호출 ──────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="off")
    service.broker.get_minute_bars = lambda *a, **kw: bars_60
    call_count = {"n": 0}
    original_analyze = service._minute_analyzer.analyze

    def counting_analyze_off(*a, **kw):
        call_count["n"] += 1
        return original_analyze(*a, **kw)

    service._minute_analyzer.analyze = counting_analyze_off
    service._get_minute_analysis(symbol, 58000)
    check("7) off 모드 -> MinuteAnalyzer.analyze()가 정확히 1회만 호출됨",
          call_count["n"] == 1)

# ── 8) shadow 모드 -> analyze() 정확히 1회 호출(2회 아님) ─────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="shadow")
    service.broker.get_minute_bars = lambda *a, **kw: bars_60
    call_count = {"n": 0}
    original_analyze = service._minute_analyzer.analyze

    def counting_analyze_shadow(*a, **kw):
        call_count["n"] += 1
        return original_analyze(*a, **kw)

    service._minute_analyzer.analyze = counting_analyze_shadow
    service._get_minute_analysis(symbol, 58000)
    check("8) shadow 모드 -> MinuteAnalyzer.analyze()도 정확히 1회만 호출됨"
          "(수정 전엔 2회였던 재현된 문제)", call_count["n"] == 1)

# ── 9) off/shadow의 _last_v_fail_reasons(analyzer 내부 상태)가 완전히 동일 ──
with tempfile.TemporaryDirectory() as tmpdir_off, tempfile.TemporaryDirectory() as tmpdir_shadow:
    service_off = build_service(tmpdir_off, session_mode="off")
    service_off.broker.get_minute_bars = lambda *a, **kw: bars_60
    with patch("domain.service.trading_service.now_kst", return_value=kst_now_fixed):
        result_off = service_off._get_minute_analysis(symbol, 58000)
    v_fail_off = list(service_off._minute_analyzer._last_v_fail_reasons)

    service_shadow = build_service(tmpdir_shadow, session_mode="shadow")
    service_shadow.broker.get_minute_bars = lambda *a, **kw: bars_60
    with patch("domain.service.trading_service.now_kst", return_value=kst_now_fixed):
        result_shadow = service_shadow._get_minute_analysis(symbol, 58000)
    v_fail_shadow = list(service_shadow._minute_analyzer._last_v_fail_reasons)

    check("9) off/shadow의 MinuteDataResult가 완전히 동일함(핵심 안전 조건)",
          result_off == result_shadow)
    check("   off/shadow의 MinuteAnalyzer._last_v_fail_reasons(내부 상태)도 완전히 동일함",
          v_fail_off == v_fail_shadow)

# ══════════════════════════════════════════════════════════════
# 5부: shadow 예외 방어 (fail-open)
# ══════════════════════════════════════════════════════════════

# ── 10) shadow 계산이 강제로 예외를 던져도 반환값에 영향 없음 ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="shadow")
    service.broker.get_minute_bars = lambda *a, **kw: bars_60
    with patch.object(service, "_update_session_metrics_shadow", side_effect=RuntimeError("강제 예외")):
        result = service._get_minute_analysis(symbol, 58000)
    check("10) shadow 계산이 강제로 예외를 던져도 entry_safe는 정상 계산됨(fail-open)",
          result.entry_safe is True)
    check("    analysis도 정상 계산됨", result.analysis is not None)

# ══════════════════════════════════════════════════════════════
# 6부: off 모드 완전 비활성 확인
# ══════════════════════════════════════════════════════════════

# ── 11) off 모드에서는 세션 상태가 전혀 쌓이지 않음 ───────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="off")
    service.broker.get_minute_bars = lambda *a, **kw: bars_60
    service._get_minute_analysis(symbol, 58000)
    check("11) off 모드 -> _session_state_by_symbol이 완전히 비어있음",
          service._session_state_by_symbol == {})
    with open(service.settings.storage.app_log_file, encoding="utf-8") as f:
        log_content = f.read()
    check("    off 모드 -> SESSION_SHADOW 로그가 전혀 없음",
          "SESSION_SHADOW" not in log_content)

# ══════════════════════════════════════════════════════════════
# 7부: 필터링된 오염이 실제로 제거되는지 (구 테스트 9번, 기대값 반전)
#
# 2026-07-28 (GPT 지시 7번): 이전 버전은 "전일 영향이 session_vwap
# 에 남는 것"을 PASS로 인정했음 — 이제는 반대로 "전일 봉이 필터링
# 되어 session_vwap/session_low에 전혀 영향을 주지 않는 것"이 성공.
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="shadow")
    # 2026-07-28 (3차 GPT 코드리뷰 재검증 중 발견): 이 시나리오는
    # "장 초반부터 시간이 흐르며 오늘 봉이 늘어나는 것"을 재현하는데,
    # today_open2를 09:01로 고정한 채 now_kst()를 실제(테스트 실행)
    # 시각 그대로 두면, 실행 시각이 09:01에서 많이 벗어난 경우
    # 최신 봉의 age가 minute_bar_max_age_seconds(120초)를 초과해
    # 신선도 검증 자체에서 걸려버림(재현: 실행 시각이 09:50경이면
    # age=2043초로 STALE_MINUTE_DATA, entry_safe=False, 세션에
    # 아무것도 안 쌓임 — 이 테스트가 실행 시각에 의존하는 flaky
    # 문제였음을 발견). now_kst()를 각 단계의 "그 시점"으로 명시적
    # 고정해 실행 시각과 무관하게 항상 동일한 결과를 내도록 함.
    fixed_now = now_kst().replace(hour=11, minute=11, second=0, microsecond=0)
    today_open2 = fixed_now.replace(hour=9, minute=1, second=0, microsecond=0)

    yesterday_bars2 = [MinuteBar(
        cntr_tm=f"2026072714{i:02d}00", open_price=50000, high_price=50100,
        low_price=49900, close_price=50000, volume=500, acc_volume=5000,
    ) for i in range(44)]

    bar_counts_over_time = []
    for minute_count in [16, 70, 130]:
        today_bars2 = [MinuteBar(
            cntr_tm=(today_open2 + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
            open_price=60000, high_price=60100, low_price=59900, close_price=60000,
            volume=500, acc_volume=5000,
        ) for i in range(minute_count)]
        api_response_bars = (yesterday_bars2 + today_bars2)[-60:]  # API는 항상 최근 60개만 반환
        service.broker.get_minute_bars = lambda *a, **kw: api_response_bars
        service.cached_minute_bars_loaded_at.pop(symbol, None)
        service.cached_minute_bars_failed_at.pop(symbol, None)
        call_time = today_open2 + timedelta(minutes=minute_count)  # 매 단계를 그 시점의 "지금"으로 고정
        with patch("domain.service.trading_service.now_kst", return_value=call_time):
            service._get_minute_analysis(symbol, 60000)
        state_now = service._session_state_by_symbol.get(symbol)
        bar_counts_over_time.append(len(state_now.bars) if state_now else 0)

    final_state = service._session_state_by_symbol.get(symbol)
    final_metrics = build_session_metrics(final_state)

    check("12) 시간이 지날수록 세션 누적 개수가 증가함(오늘 봉만 누적)",
          bar_counts_over_time == sorted(bar_counts_over_time)
          and bar_counts_over_time[-1] > bar_counts_over_time[0])
    check("    전일 봉이 필터링되어 session_low가 오늘 저가(59900)로 유지됨"
          "(전일 저가 49900의 영향을 받지 않음 — 이전 버전은 이게 실패였는데 "
          "이제는 이게 성공 기준)", final_metrics.session_low == 59900)
    check("    filtered_other_date_count가 계속 44로 유지됨(전일 44개가 매번 정확히 걸러짐)",
          final_metrics.filtered_other_date_count == 44)
    check("    session_vwap이 전일 저가(49900대)의 영향을 받지 않고 오늘 가격대"
          "(60000대)로 계산됨", final_metrics.session_vwap > 55000)

# ══════════════════════════════════════════════════════════════
# 8부: 로그 필드 추가 확인 (GPT 지시 5번)
# ══════════════════════════════════════════════════════════════

sample_state = merge_session_bars(None, yesterday_bars + today_bars, "20260728")
sample_metrics = build_session_metrics(sample_state)
log_line = format_session_metrics_log_line(symbol, sample_metrics, 58000.0, 58100, 57900)

check("13) 로그에 session_date 포함됨", "date=20260728" in log_line)
check("    로그에 earliest_timestamp 포함됨", "earliest=" in log_line)
check("    로그에 latest_timestamp 포함됨", "latest=" in log_line)
check("    로그에 rolling_20_count 포함됨(괄호 안)", "봉)" in log_line)
check("    로그에 filtered_other_date_batch/unique 둘 다 포함됨(3차 코드리뷰 수정)",
      "filtered_other_date_batch=43" in log_line and "filtered_other_date_unique=43" in log_line)
check("    로그에 filtered_outside_market_batch/unique 포함됨",
      "filtered_outside_market_batch=" in log_line and "filtered_outside_market_unique=" in log_line)
check("    로그에 readiness_reason 포함됨", "reason=" in log_line)

# ══════════════════════════════════════════════════════════════
# 9부: 검증 실패 OHLC의 세션 오염 방지 (2026-07-28, 3차 GPT 코드리뷰
# 지적 1번, TradingService 통합 레벨)
#
# 배경: _get_minute_analysis()가 OHLC 구조 검증에 실패하면 entry_
# safe=False로 안전하게 차단하지만, bars=new_bars(검증 실패한 원본)
# 를 그대로 _update_session_metrics_shadow()에 넘기고 있어서 세션
# 저장소가 오염되던 것을 재현 확인(session_bar_count=41, 저장된
# high_price 전부 {0}). bars(분석용)와 session_ingest_bars(세션
# 반영용)를 분리해 수정.
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="shadow")
    kst_now2 = now_kst()
    bad_ohlc_bars = [MinuteBar(
        cntr_tm=(kst_now2 - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=0, low_price=0, close_price=58000,
        volume=1000, acc_volume=50000,
    ) for i in range(60)]
    service.broker.get_minute_bars = lambda *a, **kw: bad_ohlc_bars

    result = service._get_minute_analysis(symbol, 58000)
    check("14) invalid OHLC(high=0,low=0) 60개 응답 -> entry_safe=False",
          result.entry_safe is False)
    check("    reason=INVALID_MINUTE_OHLC", result.reason == "INVALID_MINUTE_OHLC")

    state_after_bad = service._session_state_by_symbol.get(symbol)
    check("14) invalid OHLC 응답이 세션 상태에 전혀 들어가지 않음"
          "(세션 저장소 자체가 생성 안 되거나 비어있음, 정확히 GPT 지시 필수 테스트)",
          state_after_bad is None or len(state_after_bad.bars) == 0)

# ── 15) invalid 응답이 기존 정상 세션을 오염시키지 않음 ────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, session_mode="shadow")
    kst_now3 = now_kst()
    good_bars_15 = [MinuteBar(
        cntr_tm=(kst_now3 - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=58100, low_price=57900, close_price=58000,
        volume=1000, acc_volume=50000,
    ) for i in range(60)]
    service.broker.get_minute_bars = lambda *a, **kw: good_bars_15
    service._get_minute_analysis(symbol, 58000)
    state_before_15 = service._session_state_by_symbol.get(symbol)
    bars_before_15 = dict(state_before_15.bars)

    service.cached_minute_bars_loaded_at[symbol] = kst_now3 - timedelta(seconds=999)
    bad_bars_15 = [MinuteBar(
        cntr_tm=(kst_now3 - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=0, low_price=0, close_price=58000,
        volume=1000, acc_volume=50000,
    ) for i in range(60)]
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars_15
    result_15 = service._get_minute_analysis(symbol, 58000)

    state_after_15 = service._session_state_by_symbol.get(symbol)
    check("15) invalid 응답이 기존 정상 세션을 오염시키지 않음"
          "(정확히 GPT 지시 필수 테스트) — 세션 봉이 그대로 유지됨",
          state_after_15.bars == bars_before_15)
    check("    세션에 high_price=0인 오염 데이터가 전혀 섞이지 않음",
          0 not in set(b.high_price for b in state_after_15.bars.values()))
    check("    이번 호출 자체는 entry_safe=False(안전 차단)", result_15.entry_safe is False)

# ══════════════════════════════════════════════════════════════
# 10부: filtered 카운터 unique/batch 정확성 (2026-07-28, 3차 GPT
# 코드리뷰 지적 2번, TradingService 통합 레벨)
# ══════════════════════════════════════════════════════════════

# ── 16) 동일 오염 창을 반복 병합해도 unique count는 불변 ──────────
yesterday_10 = [MinuteBar(
    cntr_tm=f"2026072714{i:02d}00", open_price=50000, high_price=50100,
    low_price=49900, close_price=50000, volume=500, acc_volume=5000,
) for i in range(10)]
today_10 = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 28, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=60000, high_price=60100, low_price=59900, close_price=60000,
    volume=500, acc_volume=5000,
) for i in range(10)]

state16 = None
unique_counts = []
batch_counts = []
for _ in range(3):
    state16 = merge_session_bars(state16, yesterday_10 + today_10, "20260728")
    metrics16 = build_session_metrics(state16)
    unique_counts.append(metrics16.filtered_other_date_count)
    batch_counts.append(metrics16.last_batch_filtered_other_date_count)

check("16) 동일 오염 창(전일10개+오늘10개)을 3회 반복 병합해도 "
      "unique count는 계속 10으로 불변(정확히 GPT 지시 필수 테스트, "
      "수정 전엔 10->20->30으로 증가했던 재현된 버그)",
      unique_counts == [10, 10, 10])
check("    반면 batch count는 매번 이번 병합에서 새로 걸러진 개수(10)를 그대로 보여줌",
      batch_counts == [10, 10, 10])
check("    session_bar_count(오늘 봉)는 정확히 10으로 유지됨(오염되지 않음)",
      metrics16.session_bar_count == 10)

# ── 17) 한 봉씩 이동하는 overlapping window에서 batch/unique count 정확 ──
yesterday_43 = [MinuteBar(
    cntr_tm=f"2026072714{i:02d}00", open_price=50000, high_price=50100,
    low_price=49900, close_price=50000, volume=500, acc_volume=5000,
) for i in range(43)]

state17 = None
for today_count in range(1, 18):
    today_n = [MinuteBar(
        cntr_tm=(datetime(2026, 7, 28, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
        open_price=60000, high_price=60100, low_price=59900, close_price=60000,
        volume=500, acc_volume=5000,
    ) for i in range(today_count)]
    window17 = (yesterday_43 + today_n)[-60:]  # API가 항상 최근 60개만 반환
    state17 = merge_session_bars(state17, window17, "20260728")

metrics17 = build_session_metrics(state17)
check("17) 한 봉씩 이동하는 overlapping window(1~17분) 끝에도 "
      "filtered_other_date_unique가 정확히 43(전일 봉 실제 개수)으로 유지됨"
      "(정확히 GPT 지시 필수 테스트)",
      metrics17.filtered_other_date_count == 43)
check("    session_bar_count가 정확히 17(오늘 실제 봉 개수)로 유지됨",
      metrics17.session_bar_count == 17)

# ══════════════════════════════════════════════════════════════
# 11부: session_metrics 모듈 자체의 OHLC 2차 방어 확인
# ══════════════════════════════════════════════════════════════

bad_ohlc_direct = [MinuteBar(
    cntr_tm=(datetime(2026, 7, 28, 9, 0) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
    open_price=58000, high_price=0, low_price=0, close_price=58000,
    volume=1000, acc_volume=50000,
) for i in range(10)]
state18 = merge_session_bars(None, bad_ohlc_direct, "20260728")
metrics18 = build_session_metrics(state18)
check("18) session_metrics 모듈 자체의 2차 OHLC 방어 — "
      "invalid OHLC 10개 -> session_bar_count=0", metrics18.session_bar_count == 0)
check("    filtered_invalid_ohlc_count=10으로 정확히 집계됨",
      metrics18.filtered_invalid_ohlc_count == 10)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
