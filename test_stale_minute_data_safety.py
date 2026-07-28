# -*- coding: utf-8 -*-
"""
분봉 데이터 신선도 기반 신규매수 차단 검증 (2026-07-28, 1B.7 안전 핫픽스 2단계)

배경: 1B.6에서 TradingService._get_minute_analysis()가 (analysis,
is_fresh, reason) 튜플을 반환하도록 고쳐 "API 예외 발생 시 오래된
캐시" 문제는 막았으나, GPT 코드리뷰로 두 가지 우회 경로가 추가로
발견됨:
  (a) API가 예외 없이 빈 리스트를 "정상 반환" — is_fresh=True로
      유지되고, 기존 정상 캐시까지 빈 리스트로 덮어써짐
  (b) API가 예외 없이 과거(전거래일) 분봉을 "정상 반환" — 예외가
      없다는 이유만으로 is_fresh=True

이번 라운드에서 _get_minute_analysis()를 MinuteDataResult(명시적
결과 객체)를 반환하도록 전면 재설계 — entry_safe/source/reason/
latest_bar_timestamp/age_seconds 필드로 두 우회 경로를 모두 차단.

이 테스트는 GPT가 제시한 12개 필수 테스트 중 이번 라운드 범위
(빈 응답/과거 봉/신선한 데이터 케이스)를 검증합니다. SELL 세분화,
카운터 상태 오염 방지, KST Clock 의존성은 범위가 커서 별도
라운드로 분리 — CHANGELOG에 명시.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import AccountBalance, MarketRegime, MinuteBar, Position, Signal, SignalType
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


def build_service(tmpdir: str) -> TradingService:
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


def make_bars(n: int, base: datetime) -> list[MinuteBar]:
    return [
        MinuteBar(
            cntr_tm=(base + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
        for i in range(n)
    ]


def make_minimal_minute_analysis() -> MinuteAnalysis:
    return MinuteAnalysis(
        vwap=58000.0, price_above_vwap=True, low_rising=False,
        pullback_pct=0.0, is_valid_pullback=False,
        change_rate_pct=0.0, is_valid_change_rate=False,
        rebound_pct=0.0, is_valid_rebound=False,
        trading_value=1_000_000_000, is_valid_trading_value=True,
        day_high=58200, day_low=57800, is_valid_pulldown=False,
        ma5_above_ma20=True,
        is_v_rebound=False, v_fail_reason="", v_bottom_k=0,
        v_drop_pct=0.0, v_rise_pct=0.0, v_volume_ratio=0.0,
        v_bottom_spike=False, v_ma5_rising=False,
        rebound_volume_spike=False, rebound_volume_ratio=0.0,
        upside_to_recent_high_pct=5.0,
        is_pulldown_recovery=False, pr_low_turning=False, pr_volume_expanding=False,
        is_slow_v_rebound=False, slow_v_drop_pct=0.0, slow_v_rise_pct=0.0, slow_v_bottom_k=0,
    )


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: _get_minute_analysis() 반환값(MinuteDataResult) 검증
# ══════════════════════════════════════════════════════════════

# ── 1) 정상 조회 성공(오늘 신선한 봉) -> entry_safe=True ────────────
# 2026-07-28: MockBroker.get_minute_bars()는 datetime.now()(naive,
# 시스템 로컬시각)로 봉을 생성하는데, 이 테스트 실행 환경은 UTC를
# 로컬시각으로 쓰므로 그 결과가 실제 KST 관점에서는 "9시간 전" 봉이
# 되어버림(재현 확인) — 이건 MockBroker의 한계이지 운영 코드
# 버그가 아니므로, 여기서는 KST 기준으로 명시적으로 신선한 봉을
# 구성해서 주입.
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now_1 = now_kst()
    explicit_fresh_bars = [
        MinuteBar(
            cntr_tm=(kst_now_1 - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
        for i in range(60)
    ]
    service.broker.get_minute_bars = lambda *a, **kw: explicit_fresh_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("1) 정상 조회 성공(오늘 신선한 봉) -> entry_safe=True", result.entry_safe is True)
    check("   source=LIVE", result.source == "LIVE")
    check("   reason은 빈 문자열", result.reason == "")

# ── 2) 조회 실패(예외) + 캐시 있음 -> entry_safe=False, STALE ───────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    bars = make_bars(60, datetime(2026, 7, 21, 9, 0, 0))
    service.cached_minute_bars[symbol] = bars
    service.cached_minute_bars_loaded_at[symbol] = now_kst() - timedelta(days=7)
    service.broker.get_minute_bars = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("API 실패"))

    result = service._get_minute_analysis(symbol, 58000)
    check("2) 조회 실패(예외) + 캐시 있음 -> entry_safe=False", result.entry_safe is False)
    check("   reason=STALE_MINUTE_DATA", result.reason == "STALE_MINUTE_DATA")
    check("   analysis는 캐시 기반으로 여전히 계산됨(보유종목 손절판단 유지)",
          result.analysis is not None)

# ── 3) 조회 실패(예외) + 캐시도 없음 -> entry_safe=False, UNAVAILABLE ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.broker.get_minute_bars = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("API 실패"))
    result = service._get_minute_analysis("999999", 58000)
    check("3) 조회 실패(예외) + 캐시도 없음 -> entry_safe=False", result.entry_safe is False)
    check("   reason=MINUTE_DATA_UNAVAILABLE", result.reason == "MINUTE_DATA_UNAVAILABLE")
    check("   analysis는 None", result.analysis is None)

# ══════════════════════════════════════════════════════════════
# 2부: 빈 응답 우회 차단 (GPT 3차 지적 1번, 재현 확인된 버그)
# ══════════════════════════════════════════════════════════════

# ── 4) API가 예외 없이 빈 리스트 반환 + 기존 캐시 있음 ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    old_bars = make_bars(60, datetime(2026, 7, 21, 9, 0, 0))
    old_loaded_at = now_kst() - timedelta(days=7)
    service.cached_minute_bars[symbol] = old_bars
    service.cached_minute_bars_loaded_at[symbol] = old_loaded_at
    service.broker.get_minute_bars = lambda *a, **kw: []  # 예외 없이 빈 리스트

    result = service._get_minute_analysis(symbol, 58000)
    check("4) 빈 응답(예외 아님) + 기존 캐시 있음 -> entry_safe=False"
          "(수정 전엔 True였던 재현된 버그)", result.entry_safe is False)
    check("   reason=STALE_MINUTE_DATA(캐시로 대체)", result.reason == "STALE_MINUTE_DATA")
    check("   기존 정상 캐시가 빈 리스트로 덮어써지지 않고 보존됨",
          service.cached_minute_bars[symbol] == old_bars)
    check("   loaded_at이 갱신되지 않음(빈 응답을 신선한 조회로 취급하지 않음)",
          service.cached_minute_bars_loaded_at[symbol] == old_loaded_at)

# ── 5) API가 예외 없이 빈 리스트 반환 + 캐시도 없음 ─────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    service.broker.get_minute_bars = lambda *a, **kw: []
    result = service._get_minute_analysis(symbol, 58000)
    check("5) 빈 응답 + 캐시도 없음 -> entry_safe=False, MINUTE_DATA_UNAVAILABLE",
          result.entry_safe is False and result.reason == "MINUTE_DATA_UNAVAILABLE")
    check("   analysis는 None", result.analysis is None)

# ══════════════════════════════════════════════════════════════
# 3부: 과거 봉 우회 차단 (GPT 3차 지적 2번, 재현 확인된 버그)
# ══════════════════════════════════════════════════════════════

# ── 6) API가 예외 없이 과거(전거래일) 봉을 정상 반환 ────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    old_bars = make_bars(60, datetime(2026, 7, 21, 9, 0, 0))  # 전거래일
    service.broker.get_minute_bars = lambda *a, **kw: old_bars

    result = service._get_minute_analysis(symbol, 58000)
    check("6) 예외 없이 과거(전거래일) 봉 반환 -> entry_safe=False"
          "(수정 전엔 True였던 재현된 버그)", result.entry_safe is False)
    check("   source=LIVE_OLD_BAR", result.source == "LIVE_OLD_BAR")
    check("   reason=STALE_MINUTE_DATA", result.reason == "STALE_MINUTE_DATA")
    check("   latest_bar_timestamp가 실제 값으로 채워짐(진단용)",
          result.latest_bar_timestamp == old_bars[-1].cntr_tm)

# ── 7) 최신 봉 timestamp 파싱 실패 -> entry_safe=False ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    bad_bars = [
        MinuteBar(cntr_tm="INVALID_TS", open_price=58000, high_price=58100,
                  low_price=57900, close_price=58000, volume=1000, acc_volume=50000)
    ]
    service.broker.get_minute_bars = lambda *a, **kw: bad_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("7) 최신 봉 timestamp 파싱 실패 -> entry_safe=False", result.entry_safe is False)
    check("   reason=STALE_MINUTE_DATA", result.reason == "STALE_MINUTE_DATA")

# ── 8) 최신 봉의 age가 minute_bar_max_age_seconds를 초과 ───────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    now = datetime.now()
    old_but_today = [
        MinuteBar(
            cntr_tm=(now - timedelta(seconds=300)).strftime("%Y%m%d%H%M%S"),  # 5분 전(120초 초과)
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
    ]
    service.broker.get_minute_bars = lambda *a, **kw: old_but_today
    result = service._get_minute_analysis(symbol, 58000)
    check("8) 최신 봉이 오늘 날짜지만 age가 minute_bar_max_age_seconds(120초) 초과 "
          "-> entry_safe=False", result.entry_safe is False)
    check("   age_seconds가 120보다 크게 계산됨",
          result.age_seconds is not None and result.age_seconds > 120)

# ══════════════════════════════════════════════════════════════
# 4부: _process_symbol() 통합 흐름에서 실제 BUY 차단 검증
# ══════════════════════════════════════════════════════════════

# ── 9) 미보유 종목 + 빈 응답(우회 시나리오) + BUY 신호 -> HOLD로 강제 전환 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis = make_minimal_minute_analysis()
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(
        service, "_get_minute_analysis",
    ) as mock_get_analysis:
        from domain.market_regime.minute_analyzer import MinuteDataResult
        mock_get_analysis.return_value = MinuteDataResult(
            analysis=fake_analysis, entry_safe=False, source="EMPTY",
            reason="MINUTE_DATA_UNAVAILABLE", latest_bar_timestamp=None, age_seconds=None,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="테스트용 강제 BUY"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="테스트용 강제 BUY"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance))

    import csv
    with open(service.settings.storage.signal_log_file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    matching = [r for r in rows if r["symbol"] == symbol]
    check("9) 미보유+EMPTY응답+BUY신호 상황에서 signal_log에 기록이 남음", len(matching) >= 1)
    if matching:
        last_row = matching[-1]
        check("   final_decision이 BUY가 아니라 HOLD로 강제 전환됨",
              last_row["final_decision"] != "BUY")

# ── 10) 보유 종목 + stale + SELL 신호 -> 차단 없이 그대로 SELL 진행 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis2 = make_minimal_minute_analysis()
    balance_with_position = AccountBalance(
        cash=100_000_000, total_asset=100_000_000,
        positions=[Position(symbol=symbol, quantity=10, average_price=60000)],
    )
    sell_calls = []

    def fake_try_sell(sym, qty, price, **kwargs):
        sell_calls.append((sym, qty, price))

    with patch.object(
        service, "_get_minute_analysis",
    ) as mock_get_analysis:
        from domain.market_regime.minute_analyzer import MinuteDataResult
        mock_get_analysis.return_value = MinuteDataResult(
            analysis=fake_analysis2, entry_safe=False, source="CACHE",
            reason="STALE_MINUTE_DATA", latest_bar_timestamp="20260721091600", age_seconds=99999.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 테스트"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.SELL, reason="손절 테스트"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_sell", side_effect=fake_try_sell,
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance_with_position))

    check("10) 보유종목 + stale + SELL 신호 -> _try_sell이 실제로 호출됨"
          "(stale이어도 위험축소 SELL은 차단되지 않음 — 이번 라운드에서는 세분화하지 않음, "
          "SELL 세분화는 다음 라운드로 분리)",
          len(sell_calls) == 1)

# ── 11) (회귀) 미보유+fresh데이터+BUY신호 -> 정상적으로 BUY 진행 ────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    fake_analysis3 = make_minimal_minute_analysis()
    balance_no_position = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])
    buy_calls = []

    def fake_try_buy(sym, price, bal, **kwargs):
        buy_calls.append(sym)
        return ""

    with patch.object(
        service, "_get_minute_analysis",
    ) as mock_get_analysis:
        from domain.market_regime.minute_analyzer import MinuteDataResult
        mock_get_analysis.return_value = MinuteDataResult(
            analysis=fake_analysis3, entry_safe=True, source="LIVE",
            reason="", latest_bar_timestamp="20260728000000", age_seconds=5.0,
        )
        with patch(
            "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="신선한 데이터 BUY"),
        ), patch(
            "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
            return_value=Signal(type=SignalType.BUY, reason="신선한 데이터 BUY"),
        ), patch.object(
            service, "_check_entry_watch", return_value=None,
        ), patch.object(
            service.regime_classifier, "classify",
            return_value=(MarketRegime.BULLISH, "테스트"),
        ), patch.object(
            service, "_try_buy", side_effect=fake_try_buy,
        ):
            import asyncio
            asyncio.run(service._process_symbol(symbol, balance_no_position))

    check("11) (회귀) 미보유+fresh데이터+BUY신호 -> _try_buy가 정상적으로 호출됨",
          len(buy_calls) == 1)

# ══════════════════════════════════════════════════════════════
# 5부: 캐시 재사용 경로 우회 차단 (2026-07-28, 1B.8, GPT 4차 지적)
#
# 배경: 1회차에서 과거 봉으로 entry_safe=False가 나와도, 60초 캐시
# 구간 안의 2회차 호출이 신선도 재검증 없이 source가 초기값 "LIVE"
# 로 남아 entry_safe=True가 되던 치명적 버그를 재현·수정.
# ══════════════════════════════════════════════════════════════

# ── 12) 과거 봉 첫 호출 false + 즉시 두 번째 호출도 false (핵심 버그 수정 검증) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    old_bars = make_bars(60, datetime(2026, 7, 21, 9, 0, 0))
    service.broker.get_minute_bars = lambda *a, **kw: old_bars

    result1 = service._get_minute_analysis(symbol, 58000)
    result2 = service._get_minute_analysis(symbol, 58000)
    check("12) 과거 봉 1회차 -> entry_safe=False", result1.entry_safe is False)
    check("    과거 봉 즉시 2회차(캐시구간)도 -> entry_safe=False"
          "(수정 전엔 True였던 재현된 치명적 버그)", result2.entry_safe is False)
    # 2026-07-28: 1회차에서 신선도 검증 실패로 캐시가 갱신되지 않고
    # (GPT 지적 2번 반영), 대신 failed_at이 기록되어 2회차는 백오프
    # 구간 안이라 재조회 없이 캐시 재사용 경로로 감 — 캐시가 비어
    # 있으니 latest_bar_timestamp가 None이 되는 게 정확한 동작(백오프
    # 중에는 API를 다시 안 부르므로 새로운 봉 정보 자체가 없음).
    # 이 자체가 "2회차도 안전하게 차단된다"는 걸 보여주는 것이지,
    # 두 timestamp가 같아야 한다는 요구사항은 아니었음(최초 테스트
    # 작성 시의 잘못된 기대값이었음 - 실제 백오프 동작을 반영해 수정).
    check("    2회차는 백오프로 재조회하지 않아 latest_bar_timestamp=None"
          "(entry_safe=False는 여전히 보장됨)",
          result2.latest_bar_timestamp is None and result2.source == "UNAVAILABLE")

# ── 12-1) 백오프 만료 후 재조회하면 동일하게 과거 봉으로 다시 차단됨 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    old_bars2 = make_bars(60, datetime(2026, 7, 21, 9, 0, 0))
    service.broker.get_minute_bars = lambda *a, **kw: old_bars2

    result1 = service._get_minute_analysis(symbol, 58000)
    kst_now_12 = now_kst()
    with patch(
        "domain.service.trading_service.now_kst",
        return_value=kst_now_12 + timedelta(seconds=25),  # backoff_sec(20) 경과
    ):
        result3 = service._get_minute_analysis(symbol, 58000)
    check("12-1) 백오프(20초) 만료 후 재조회하면 다시 API를 불러 "
          "과거 봉임을 재확인하고 entry_safe=False 유지",
          result3.entry_safe is False and result3.source == "LIVE_OLD_BAR")
    check("     이번엔 latest_bar_timestamp가 실제 값으로 채워짐(재조회했으므로)",
          result3.latest_bar_timestamp == old_bars2[-1].cntr_tm)

# ── 13) fresh 응답 후 cache hit은 true(회귀 확인) ──────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    fresh_bars = make_bars(60, kst_now - timedelta(minutes=59))
    # make_bars는 base부터 오름차순 생성 — 마지막 봉이 kst_now 근접하도록 재구성
    fresh_bars = [
        MinuteBar(
            cntr_tm=(kst_now - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
        for i in range(60)
    ]
    service.broker.get_minute_bars = lambda *a, **kw: fresh_bars

    result1 = service._get_minute_analysis(symbol, 58000)
    result2 = service._get_minute_analysis(symbol, 58000)
    check("13) fresh 응답(1회차) -> entry_safe=True", result1.entry_safe is True)
    check("    직후 캐시 재사용(2회차) -> entry_safe=True, source=CACHE_FRESH",
          result2.entry_safe is True and result2.source == "CACHE_FRESH")

# ── 14) 캐시가 max_age를 넘으면 refresh 구간 안이어도 false ─────────
with tempfile.TemporaryDirectory() as tmpdir:
    settings_local = build_minimal_settings(tmpdir)
    # max_age(30초)를 refresh_sec(60초)보다 짧게 설정해, "봉 자체가
    # 너무 오래됐지만 API 재호출은 아직 안 하는" 구간을 재현
    object.__setattr__(settings_local.market_regime, "minute_bar_max_age_seconds", 30)
    broker_local = MockBroker()
    app_logger_local = build_app_logger(settings_local.storage.app_log_file, settings_local.app.log_level)
    service = TradingService(
        settings=settings_local, broker=broker_local,
        strategy_router=StrategyRouter(settings_local.strategy),
        regime_classifier=MarketRegimeClassifier(settings_local.market_regime),
        risk_manager=RiskManager(settings_local.trading, settings_local.risk, settings_local.storage.trade_log_file),
        app_logger=app_logger_local,
        trade_logger=TradeCsvLogger(settings_local.storage.trade_log_file),
        signal_logger=SignalCsvLogger(settings_local.storage.signal_log_file),
        state_store=JsonStateStore(settings_local.storage.state_file),
    )
    kst_now = now_kst()
    fresh_bars = [
        MinuteBar(
            cntr_tm=(kst_now - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
        for i in range(60)
    ]
    service.broker.get_minute_bars = lambda *a, **kw: fresh_bars
    service._get_minute_analysis(symbol, 58000)

    with patch(
        "domain.service.trading_service.now_kst",
        return_value=kst_now + timedelta(seconds=45),  # refresh_sec(60) 안, max_age(30) 밖
    ):
        result = service._get_minute_analysis(symbol, 58000)
    check("14) refresh 구간(60초) 안이지만 max_age(30초)를 넘으면 -> entry_safe=False",
          result.entry_safe is False)
    check("    source=CACHE_STALE(재조회 안 하고 캐시 재검증으로 걸림)",
          result.source == "CACHE_STALE")

# ── 15) stale 응답이 최신 캐시를 덮어쓰지 않음 ──────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    newer_bars = [
        MinuteBar(
            cntr_tm=(kst_now - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
            open_price=58000, high_price=58100, low_price=57900,
            close_price=58000, volume=1000, acc_volume=50000,
        )
        for i in range(60)
    ]
    service.broker.get_minute_bars = lambda *a, **kw: newer_bars
    result1 = service._get_minute_analysis(symbol, 58000)
    check("15-준비) 최초 신선한 캐시 확보", result1.entry_safe is True)
    cached_before = list(service.cached_minute_bars[symbol])

    # refresh 구간을 강제로 다시 열되(직접 loaded_at을 과거로), 이번엔
    # 더 오래된(과거) 봉을 반환하도록 만듦
    service.cached_minute_bars_loaded_at[symbol] = kst_now - timedelta(seconds=999)
    older_bars = make_bars(60, datetime(2026, 7, 20, 9, 0, 0))
    service.broker.get_minute_bars = lambda *a, **kw: older_bars

    result2 = service._get_minute_analysis(symbol, 58000)
    check("15) 새 응답(더 오래된 봉)이 기존(더 최신인) 캐시를 덮어쓰지 않음",
          service.cached_minute_bars[symbol] == cached_before)
    check("    entry_safe=False(더 오래된 응답이므로 안전하게 차단)",
          result2.entry_safe is False)

# ══════════════════════════════════════════════════════════════
# 6부: KST timezone-aware 계산 검증 (2026-07-28, GPT 4차 지적)
# ══════════════════════════════════════════════════════════════

# ── 16) UTC 서버 환경에서 KST 봉 age가 정확히 계산됨(재현했던 -32400초 버그) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    # 서버 시스템 로컬시각이 UTC 00:20이어도, now_kst()는 항상 정확한
    # KST(09:20)를 반환해야 함 — parse_kst_bar_timestamp도 동일 기준.
    kst_bar_time = now_kst().replace(hour=9, minute=20, second=0, microsecond=0)
    bars_at_0920 = [MinuteBar(
        cntr_tm=kst_bar_time.strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=58100, low_price=57900,
        close_price=58000, volume=1000, acc_volume=50000,
    )]
    service.broker.get_minute_bars = lambda *a, **kw: bars_at_0920

    with patch(
        "domain.service.trading_service.now_kst",
        return_value=kst_bar_time,  # 봉 시각과 정확히 동일한 "현재" 시각
    ):
        result = service._get_minute_analysis(symbol, 58000)
    check("16) 봉 시각과 현재 시각이 동일할 때 age_seconds가 0에 가까움"
          "(UTC/KST 혼동 없이 정확히 계산됨)",
          result.age_seconds is not None and abs(result.age_seconds) < 1.0)
    check("    entry_safe=True(신선함)", result.entry_safe is True)

# ── 17) 미래 timestamp(age < -5초) -> entry_safe=False ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    future_bars = [MinuteBar(
        cntr_tm=(kst_now + timedelta(minutes=10)).strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=58100, low_price=57900,
        close_price=58000, volume=1000, acc_volume=50000,
    )]
    service.broker.get_minute_bars = lambda *a, **kw: future_bars
    result = service._get_minute_analysis(symbol, 58000)
    check("17) 미래 timestamp(age<-5초) -> entry_safe=False", result.entry_safe is False)
    check("    age_seconds가 명확히 음수로 계산됨",
          result.age_seconds is not None and result.age_seconds < -5)

# ══════════════════════════════════════════════════════════════
# 7부: 실패/빈 응답 백오프 검증 (2026-07-28, GPT 4차 지적 5번)
# ══════════════════════════════════════════════════════════════

# ── 18) 빈 응답 반복 시 백오프 적용(연속 호출해도 API가 매번 불리지 않음) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    call_count = {"n": 0}

    def counting_empty(*a, **kw):
        call_count["n"] += 1
        return []

    service.broker.get_minute_bars = counting_empty
    for _ in range(3):
        service._get_minute_analysis(symbol, 58000)

    check("18) 빈 응답이 연속 반복돼도 백오프 구간 안에서는 API가 재호출되지 않음"
          "(3회 연속 호출, 실제 API 호출은 1회만)", call_count["n"] == 1)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
