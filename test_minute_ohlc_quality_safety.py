# -*- coding: utf-8 -*-
"""
분봉 OHLC 구조 품질 검증 및 analyzer 예외 방어 (2026-07-28, "1B Safety Closure")

배경: 1B.9까지 신선도(age)·개수·정렬·중복·analysis 존재 여부는
검증했지만, 분봉의 OHLC 구조 자체(high/low가 0이거나 논리적으로
모순된 값)는 전혀 검증하지 않았음. GPT 코드리뷰로 재현 확인:

  open=58000, high=0, low=0, close=58000, volume=1000, acc_volume=50000
  으로 60개 분봉을 구성하면:
    - _evaluate_bar_freshness() -> fresh_ok=True (통과)
    - MinuteAnalyzer.analyze() -> ZeroDivisionError
      (day_high=0으로 pullback_pct 나눗셈)

더 심각하게는 분석 전에 cached_minute_bars/loaded_at을 이미
갱신하고 있어서, 잘못된 응답이 성공 캐시를 오염시킨 뒤 다음
호출에서도 반복 실패할 수 있었음.

이 파일은 다음을 검증합니다:
1. OHLC 구조 검증(high=0/low=0/low>high/close 범위밖) — 예외 없이
   entry_safe=False로 안전하게 차단
2. MinuteAnalyzer.analyze()가 예외를 던져도 종목 처리 전체가
   중단되지 않고 이 종목만 MINUTE_ANALYSIS_ERROR로 안전하게 차단
3. 검증 실패 응답이 성공 캐시(cached_minute_bars/loaded_at)를
   오염시키지 않음
4. 거부된 응답이 정상 minute_bars CSV에 섞이지 않고 별도
   rejected/ 경로에 사유와 함께 저장됨
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
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


def build_service(tmpdir: str, save_minute_bars: bool = False) -> TradingService:
    settings = build_minimal_settings(tmpdir)
    if save_minute_bars:
        object.__setattr__(settings.storage, "save_minute_bars", True)
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


def make_kst_bars(n: int, kst_now, **overrides) -> list[MinuteBar]:
    """정상 OHLC로 n개 봉을 만들되, overrides로 특정 필드를 덮어씁니다."""
    defaults = dict(open_price=58000, high_price=58100, low_price=57900,
                     close_price=58000, volume=1000, acc_volume=50000)
    defaults.update(overrides)
    return [
        MinuteBar(
            cntr_tm=(kst_now - timedelta(minutes=n - 1 - i)).strftime("%Y%m%d%H%M%S"),
            **defaults,
        )
        for i in range(n)
    ]


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: OHLC 구조 검증 — 예외 없이 차단
# ══════════════════════════════════════════════════════════════

# ── 1) high=0, low=0 (GPT가 재현한 정확한 케이스) -> 예외 없이 entry_safe=False ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, high_price=0, low_price=0)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars

    try:
        result = service._get_minute_analysis(symbol, 58000)
        check("1) high=0,low=0 -> 예외 없이 완료됨(ZeroDivisionError 전파 안 됨)", True)
        check("   entry_safe=False", result.entry_safe is False)
        check("   reason=INVALID_MINUTE_OHLC", result.reason == "INVALID_MINUTE_OHLC")
    except Exception as exc:
        check(f"1) high=0,low=0 -> 예외 없이 완료됨(실제로는 {type(exc).__name__} 발생)", False)
        check("   entry_safe=False", False)
        check("   reason=INVALID_MINUTE_OHLC", False)

# ── 2) low > high -> entry_safe=False ────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, high_price=57000, low_price=59000)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("2) low > high -> entry_safe=False", result.entry_safe is False)
    check("   reason=INVALID_MINUTE_OHLC", result.reason == "INVALID_MINUTE_OHLC")

# ── 3) close가 low~high 범위 밖 -> entry_safe=False ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, close_price=100000)  # high=58100보다 훨씬 큼
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("3) close가 low~high 범위 밖 -> entry_safe=False", result.entry_safe is False)
    check("   reason=INVALID_MINUTE_OHLC", result.reason == "INVALID_MINUTE_OHLC")

# ── 4) open이 low~high 범위 밖 -> entry_safe=False ──────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, open_price=1)  # low=57900보다 훨씬 작음
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("4) open이 low~high 범위 밖 -> entry_safe=False", result.entry_safe is False)

# ── 5) volume 음수 -> entry_safe=False ──────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, volume=-100)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("5) volume 음수 -> entry_safe=False", result.entry_safe is False)

# ── 6) 정상 OHLC -> 여전히 entry_safe=True(회귀 확인) ────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("6) 정상 OHLC -> entry_safe=True(회귀 없음)", result.entry_safe is True)
    check("   analysis도 정상 계산됨", result.analysis is not None)

# ══════════════════════════════════════════════════════════════
# 2부: MinuteAnalyzer.analyze() 예외 방어
# ══════════════════════════════════════════════════════════════

# ── 7) analyzer가 강제로 예외를 던져도 -> BUY 차단, 루프는 계속 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars

    with patch.object(service._minute_analyzer, "analyze", side_effect=RuntimeError("강제 예외")):
        result = service._get_minute_analysis(symbol, 58000)
    check("7) analyzer 강제 예외 -> 예외가 밖으로 전파되지 않음(호출 자체가 성공)", True)
    check("   entry_safe=False", result.entry_safe is False)
    check("   reason=MINUTE_ANALYSIS_ERROR", result.reason == "MINUTE_ANALYSIS_ERROR")
    check("   analysis는 None", result.analysis is None)

# ── 8) analyzer 예외 발생 후에도 캐시 정책이 명확함(오염 없음) ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars

    with patch.object(service._minute_analyzer, "analyze", side_effect=RuntimeError("강제 예외")):
        service._get_minute_analysis(symbol, 58000)
    # analyze() 예외는 구조검증(OHLC 등) 통과 *이후* 단계에서 발생하므로,
    # 이 시점엔 이미 정상 응답으로 캐시가 갱신되어 있는 게 맞는 동작
    # (구조 자체는 정상이었고, analyzer 로직만 예외를 던진 상황이므로).
    check("8) analyzer 예외는 구조검증 *이후* 단계라 캐시는 정상 갱신됨"
          "(구조 자체는 정상이었으므로 다음 폴링에서 stale로 오판되지 않음)",
          service.cached_minute_bars.get(symbol) == good_bars)

# ── 9) 다음 종목 처리는 계속됨(_process_symbol 레벨) ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars

    from domain.models import AccountBalance, MarketRegime
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(service._minute_analyzer, "analyze", side_effect=RuntimeError("강제 예외")):
        with patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ):
            import asyncio
            try:
                asyncio.run(service._process_symbol(symbol, balance))
                check("9) analyzer 예외가 나도 _process_symbol()이 예외 없이 완료됨"
                      "(다음 종목 처리를 막지 않음)", True)
            except Exception:
                check("9) analyzer 예외가 나도 _process_symbol()이 예외 없이 완료됨"
                      "(다음 종목 처리를 막지 않음)", False)

# ══════════════════════════════════════════════════════════════
# 3부: 캐시/저장 오염 방지
# ══════════════════════════════════════════════════════════════

# ── 10) invalid 응답이 기존 정상 캐시와 loaded_at을 오염시키지 않음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars
    r1 = service._get_minute_analysis(symbol, 58000)
    cached_before = list(service.cached_minute_bars[symbol])

    # refresh를 강제로 유도하기 위해 loaded_at을 의도적으로 과거로
    # 되돌림 — 이 조작된 값 자체가 "invalid 응답 처리 이후에도 그대로
    # 유지되는지"(즉, invalid 응답이 이 시각을 다시 갱신하지
    # 않는지)가 검증 대상.
    forced_old_loaded_at = kst_now - timedelta(seconds=999)
    service.cached_minute_bars_loaded_at[symbol] = forced_old_loaded_at
    bad_bars = make_kst_bars(60, kst_now, high_price=0, low_price=0)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    r2 = service._get_minute_analysis(symbol, 58000)

    check("10) invalid 응답(OHLC 이상) -> 기존 정상 캐시가 그대로 보존됨",
          service.cached_minute_bars[symbol] == cached_before)
    check("    loaded_at도 갱신되지 않음(강제로 되돌린 과거 값이 그대로 유지됨)",
          service.cached_minute_bars_loaded_at[symbol] == forced_old_loaded_at)
    check("    이번 호출 자체는 entry_safe=False(안전 차단)", r2.entry_safe is False)

# ── 11) 캐시가 아예 없는 상태에서 invalid 응답 -> 캐시 계속 비어있음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    bad_bars = make_kst_bars(60, kst_now, high_price=0, low_price=0)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    service._get_minute_analysis(symbol, 58000)
    check("11) 캐시가 없는 상태에서 invalid 응답 -> cached_minute_bars가 채워지지 않음",
          service.cached_minute_bars.get(symbol) is None)

# ── 12) rejected 응답이 정상 리플레이 CSV에 저장되지 않음(별도 경로) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir, save_minute_bars=True)
    kst_now = now_kst()
    good_bars = make_kst_bars(60, kst_now)
    service.broker.get_minute_bars = lambda *a, **kw: good_bars
    service._get_minute_analysis(symbol, 58000)

    minute_bars_dir = service.settings.storage.minute_bars_dir
    csv_path = os.path.join(minute_bars_dir, now_kst().strftime("%Y%m%d"), f"{symbol}.csv")
    check("12-준비) 정상 응답은 minute_bars CSV에 저장됨", os.path.exists(csv_path))

    with open(csv_path, encoding="utf-8-sig") as f:
        rows_before = list(csv.reader(f))

    service.cached_minute_bars_loaded_at[symbol] = kst_now - timedelta(seconds=999)
    bad_bars = make_kst_bars(60, kst_now, high_price=0, low_price=0)
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    service._get_minute_analysis(symbol, 58000)

    with open(csv_path, encoding="utf-8-sig") as f:
        rows_after = list(csv.reader(f))

    check("12) invalid(OHLC 이상) 응답 이후에도 정상 CSV의 행 수가 그대로임"
          "(거부 데이터가 섞이지 않음)", rows_before == rows_after)

    rejected_dir = os.path.join(minute_bars_dir, "rejected")
    check("    거부 응답은 별도 rejected/ 경로에 저장됨", os.path.isdir(rejected_dir))
    rejected_files = os.listdir(rejected_dir) if os.path.isdir(rejected_dir) else []
    check("    rejected 경로에 파일이 실제로 생성됨", len(rejected_files) >= 1)
    if rejected_files:
        with open(os.path.join(rejected_dir, rejected_files[0]), encoding="utf-8") as f:
            rejected_content = f.read()
        check("    rejected 파일에 사유(reason)가 기록됨",
              "INVALID_MINUTE_OHLC" in rejected_content)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
