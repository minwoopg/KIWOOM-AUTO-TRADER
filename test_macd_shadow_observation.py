# -*- coding: utf-8 -*-
"""
MACD 상태 shadow 관측 필드 검증 (2026-08-04, 3차 개정)

배경: 1차 구현(macd_golden/macd_dead/would_be_blocked_if_macd_
dead_required)에 대한 GPT 코드리뷰로 두 가지 문제가 발견됨:

1. "MACD 데드면 최소 5점 요구"(min-score-5, breakout_strategy.py
   의 chasing_overheated 확장판)와 "MACD가 Signal 이하이면 점수와
   무관하게 완전 차단"(hard gate, 원래 검증하려던 대상)을
   would_be_blocked_if_macd_dead_required 하나로 뭉뚱그렸음.
2. legacy signal이 BUY가 아닌(HOLD 등) 행에서도 차단 여부를
   계산하고 있었음.

2차 개정(hard/min5 분리, legacy_buy_candidate 조건화)에 대한 GPT
재검토로 추가 3가지 문제가 발견됨:

3. chasing_overheated_val을 legacy_buy_candidate_val(신호가 BUY인
   경우)일 때만 계산했었는데, 실제 BreakoutStrategy의 chasing_
   overheated 게이트가 진짜로 차단한 사례는 이미 signal=HOLD로
   나옴 — 그 HOLD 행에서 legacy_buy_candidate_val=False가 되어
   chasing_overheated가 빈 값으로 남아, "기존 게이트가 실제로 몇
   건을 막았는가"를 이 필드로 전혀 집계할 수 없었음(재현 확인).
4. MinuteBarSaver.save()가 bars 전체를 now_kst().date() 하나의
   폴더에만 저장 — 키움 최근 60봉 응답이 장 초반 전일+오늘로
   섞여 오면(재현: 전일43+오늘17), 오늘 리플레이 CSV 파일 하나에
   두 날짜가 뒤섞여 저장되고 있었음.
5. _write_signal_log()의 timestamp가 datetime.now()(시스템 로컬
   시각) — UTC 컨테이너 등에서는 latest_bar_timestamp(KST 기준)와
   최대 9시간까지 어긋남(재현 확인).

이 테스트가 검증하는 것 — GPT 3차 코드리뷰 지시대로:
1. would_block_macd_dead_min_score5(min5)와 would_block_macd_
   above_signal_required(hard gate)가 정확히 분리되어 계산되는지,
   legacy_buy_candidate=True일 때만 계산되는지
2. chasing_overheated_condition/would_block_existing_chasing_gate
   가 BUY/HOLD와 무관하게(applies=True일 때는 항상) 계산되어,
   기존 게이트가 실제로 차단한 HOLD 사례도 정확히 집계 가능한지
3. macd/macd_signal 원시값, latest_bar_timestamp가 정확히
   기록되는지
4. 이 로깅 추가가 신호 판단 로직 자체에는 절대 영향을 주지 않는지
5. 53MB급 기존 CSV 마이그레이션이 백업 생성 + 원자적 교체(os.
   replace, flush+fsync)로 안전하게 이뤄지는지
6. MinuteBarSaver가 cntr_tm 기준 날짜별로 봉을 정확히 분리
   저장하는지(전일 봉이 오늘 폴더에 섞이지 않는지)
7. signal_log의 timestamp가 KST 기준으로 정확히 기록되는지
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import AccountBalance, MarketPrice, MarketRegime, MinuteBar, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger, SIGNAL_FIELDS
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


def make_market_price(macd=None, macd_signal=None, hist_dir=0):
    return MarketPrice(
        symbol="005930", current_price=100000, reference_price=98000,
        previous_close=98000, timestamp=datetime.now(),
        indicator_macd=macd, indicator_macd_signal=macd_signal,
        indicator_macd_hist_direction=hist_dir,
    )


def read_last_row(service):
    """마지막 로그 행을 csv.DictReader로 파싱해서 반환합니다.

    2026-08-04 (GPT 코드리뷰 지시): 이전엔 split(",")으로 직접
    나눴는데, skip_reason 등 필드값에 쉼표가 들어가면(예: reason
    문자열 안에 콤마가 섞인 경우) CSV quoting을 무시하고 잘못된
    열로 정렬될 위험이 있었음 — 실제 코드(SignalCsvLogger)가
    csv.DictWriter로 쓰는 것과 대칭으로, 읽을 때도 csv.DictReader
    를 써서 quoting을 정확히 처리하도록 통일.
    """
    with open(service.settings.storage.signal_log_file, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1]


symbol = "005930"

# ══════════════════════════════════════════════════════════════
# 1부: SIGNAL_FIELDS 필드명 확인 — macd_golden 폐기, 신규 필드 존재
# ══════════════════════════════════════════════════════════════

check("1) macd_golden(1차 폐기된 이름)이 더 이상 존재하지 않음",
      "macd_golden" not in SIGNAL_FIELDS)
check("   macd_dead(1차 폐기된 이름)도 더 이상 존재하지 않음",
      "macd_dead" not in SIGNAL_FIELDS)
check("   macd_above_signal 존재함(신규 명칭)", "macd_above_signal" in SIGNAL_FIELDS)
check("   macd/macd_signal 원시값 필드 존재함",
      "macd" in SIGNAL_FIELDS and "macd_signal" in SIGNAL_FIELDS)
check("   macd_hist_direction 존재함", "macd_hist_direction" in SIGNAL_FIELDS)
check("   legacy_buy_candidate 존재함", "legacy_buy_candidate" in SIGNAL_FIELDS)
check("   latest_bar_timestamp 존재함", "latest_bar_timestamp" in SIGNAL_FIELDS)
check("   chasing_overheated_applies 존재함", "chasing_overheated_applies" in SIGNAL_FIELDS)
check("   would_block_macd_dead_min_score5(min5) 존재함",
      "would_block_macd_dead_min_score5" in SIGNAL_FIELDS)
check("   would_block_macd_above_signal_required(hard gate) 존재함",
      "would_block_macd_above_signal_required" in SIGNAL_FIELDS)

# ══════════════════════════════════════════════════════════════
# 2부: GPT 지시 필수 4가지 케이스 — hard gate vs min5 정확한 분리
# ══════════════════════════════════════════════════════════════

# ── 2) dead + BUY + 6점 -> hard=True, min5=False ────────────────
# (1차 구현에서 잘못됐던 정확히 그 케이스 — hard gate 기준으로는
# "MACD가 Signal 이하면 점수 무관 차단"이므로 6점이어도 True여야
# 하는데, 1차는 min5 기준(6점이면 이미 통과)으로 False가 나왔음.)
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("2) dead + BUY + 6점 -> hard gate(would_block_macd_above_signal_required)=True"
          "(정확히 GPT가 지적한 케이스 — 점수와 무관하게 차단됨)",
          row["would_block_macd_above_signal_required"] == "True")
    check("   min5(would_block_macd_dead_min_score5)=False"
          "(6점은 이미 min-score-5 게이트를 통과할 자격이 있었음)",
          row["would_block_macd_dead_min_score5"] == "False")

# ── 3) dead + BUY + 4점 -> hard=True, min5=True ─────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="강한 진입 4/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("3) dead + BUY + 4점 -> hard gate=True", row["would_block_macd_above_signal_required"] == "True")
    check("   min5=True(4점은 5점 미만이라 min-score-5 게이트도 차단)",
          row["would_block_macd_dead_min_score5"] == "True")

# ── 4) dead + HOLD + 4점 -> MACD 상태만 기록, would_block 둘 다 빈 값 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.HOLD, reason="추격매수 차단 4/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("4) dead + HOLD + 4점 -> MACD 상태(macd_above_signal)는 정상 기록됨",
          row["macd_above_signal"] == "False")
    check("   legacy_buy_candidate=False(전략이 BUY를 반환하지 않았음)",
          row["legacy_buy_candidate"] == "False")
    check("   hard gate는 빈 값(HOLD였던 판단을 '차단'이라 부르지 않음, "
          "정확히 GPT 지시 필수 테스트)",
          row["would_block_macd_above_signal_required"] == "")
    check("   min5도 빈 값", row["would_block_macd_dead_min_score5"] == "")

# ── 5) golden + BUY -> hard=False ────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=1.5, macd_signal=1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="최적 타점 6/8 — 테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("5) golden + BUY -> hard gate=False(MACD가 Signal 위이므로 차단 아님)",
          row["would_block_macd_above_signal_required"] == "False")
    check("   macd_above_signal=True", row["macd_above_signal"] == "True")

# ══════════════════════════════════════════════════════════════
# 3부: 원시값·타임스탬프 기록 확인
# ══════════════════════════════════════════════════════════════

# ── 6) 원시 macd/macd_signal 기록 ────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-2.345, macd_signal=-1.111, hist_dir=-1)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("6) macd 원시값이 정확히 기록됨", float(row["macd"]) == -2.345)
    check("   macd_signal 원시값이 정확히 기록됨", float(row["macd_signal"]) == -1.111)
    check("   macd_hist_direction이 정확히 기록됨", row["macd_hist_direction"] == "-1")

# ── 7) latest_bar_timestamp 기록 (통합 흐름) ─────────────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    kst_now = now_kst()
    good_bars = [MinuteBar(
        cntr_tm=(kst_now - timedelta(minutes=59 - i)).strftime("%Y%m%d%H%M%S"),
        open_price=58000, high_price=58100, low_price=57900, close_price=58000,
        volume=1000, acc_volume=50000,
    ) for i in range(60)]
    service.broker.get_minute_bars = lambda *a, **kw: good_bars
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])

    with patch.object(service.regime_classifier, "classify", return_value=(MarketRegime.BULLISH, "테스트")):
        import asyncio
        asyncio.run(service._process_symbol(symbol, balance))

    row = read_last_row(service)
    check("7) latest_bar_timestamp이 실제 최신 분봉 timestamp와 정확히 일치함"
          "(통합 흐름 검증)", row["latest_bar_timestamp"] == good_bars[-1].cntr_tm)

# ── 8) BEARISH 경로(minute_result가 정의 안 되는 분기)에서도 예외 없음 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])
    with patch.object(service.regime_classifier, "classify", return_value=(MarketRegime.BEARISH, "테스트")):
        import asyncio
        try:
            asyncio.run(service._process_symbol(symbol, balance))
            check("8) BEARISH 경로(minute_result 미정의 분기)에서도 예외 없이 완료됨"
                  "(latest_bar_timestamp 참조로 인한 NameError 방어 확인)", True)
            row = read_last_row(service)
            check("   이 경로에서 latest_bar_timestamp는 안전하게 빈 값",
                  row["latest_bar_timestamp"] == "")
        except Exception as exc:
            check(f"8) BEARISH 경로에서도 예외 없이 완료됨 - 실패: {exc}", False)
            check("   이 경로에서 latest_bar_timestamp는 안전하게 빈 값", False)

# ══════════════════════════════════════════════════════════════
# 4부: chasing_overheated_applies — 장세별 게이트 존재 여부 구분
# ══════════════════════════════════════════════════════════════

# ── 9) NEUTRAL에서는 chasing_overheated_applies=False ────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.NEUTRAL,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("9) NEUTRAL(NeutralStrategy에는 chasing_overheated 로직 자체가 없음) -> "
          "chasing_overheated_applies=False(정확히 GPT 지시 필수 테스트)",
          row["chasing_overheated_applies"] == "False")
    check("   NEUTRAL에서 chasing_overheated_condition 값 자체는 빈 값"
          "(적용도 안 되는 게이트가 발동했다는 거짓 신호를 안 냄)",
          row["chasing_overheated_condition"] == "")

# ── 10) BULLISH에서는 chasing_overheated_applies=True ─────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=1)
    signal = Signal(type=SignalType.BUY, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("10) BULLISH(BreakoutStrategy에 chasing_overheated 로직 존재) -> "
          "chasing_overheated_applies=True", row["chasing_overheated_applies"] == "True")

# ══════════════════════════════════════════════════════════════
# 5부: BULLISH 계산이 breakout_strategy.py의 실제 조건과 정확히 일치
# ══════════════════════════════════════════════════════════════

# ── 11) 당일등락 4%(>=3%) + MACD 데드 -> chasing_overheated=True(실제 게이트 조건 재사용) ──
from domain.market_regime.minute_analyzer import MinuteAnalysis


def make_minute_analysis(**overrides) -> MinuteAnalysis:
    defaults = dict(
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
    defaults.update(overrides)
    return MinuteAnalysis(**defaults)


with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=-1)
    ma = make_minute_analysis(change_rate_pct=4.0, is_valid_change_rate=True)
    signal = Signal(type=SignalType.BUY, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("11) BULLISH + 당일등락4%(>=3%) + MACD데드 -> chasing_overheated_condition=True"
          "(breakout_strategy.py의 실제 chasing_overheated 조건과 계산식 일치)",
          row["chasing_overheated_condition"] == "True")
    check("    조건충족+score 파싱안됨(reason에 8점체계 없음) -> "
          "would_block_existing_chasing_gate는 빈 값(판단불가를 False로 단정 안 함)",
          row["would_block_existing_chasing_gate"] == "")

# ── 12) 당일등락 2%(<3%) -> chasing_overheated=False ──────────────
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=-1)
    ma = make_minute_analysis(change_rate_pct=2.0, is_valid_change_rate=True)
    signal = Signal(type=SignalType.BUY, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("12) BULLISH + 당일등락2%(<3%) + MACD데드 -> chasing_overheated_condition=False"
          "(등락률 조건 미충족 — 002990 4점 케이스처럼 실제 게이트가 발동 안 하는 상황 재현)",
          row["chasing_overheated_condition"] == "False")
    check("    조건 자체가 거짓이면 would_block_existing_chasing_gate=False로 명시"
          "(발동 안 함이 명확한 사실이므로 빈 값이 아님)",
          row["would_block_existing_chasing_gate"] == "False")

# ══════════════════════════════════════════════════════════════
# 6부: 지표 없음 / 하위 호환
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=None, macd_signal=None)
    signal = Signal(type=SignalType.HOLD, reason="지표 없음")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("13) 지표 없음(macd=None) -> macd/macd_signal/macd_above_signal 전부 빈 값",
          row["macd"] == "" and row["macd_signal"] == "" and row["macd_above_signal"] == "")

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    try:
        service._write_signal_log(
            symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
            signal=signal, minute_analysis=None, final_decision="HOLD",
            order_block_reason="",
            # market_price, latest_bar_timestamp 둘 다 생략
        )
        check("14) market_price/latest_bar_timestamp 생략해도 예외 없이 정상 기록됨"
              "(하위 호환)", True)
    except Exception as exc:
        check(f"14) market_price/latest_bar_timestamp 생략해도 예외 없이 정상 기록됨"
              f" - 실패: {exc}", False)

# ══════════════════════════════════════════════════════════════
# 7부: 핵심 안전 조건 — 신호 판단 자체에 영향 없음
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    balance = AccountBalance(cash=100_000_000, total_asset=100_000_000, positions=[])
    original_signal = Signal(type=SignalType.HOLD, reason="관측용 그대로 유지되어야 함")
    with patch(
        "domain.strategy.breakout_strategy.BreakoutStrategy.generate_signal",
        return_value=original_signal,
    ), patch(
        "domain.strategy.neutral_strategy.NeutralStrategy.generate_signal",
        return_value=original_signal,
    ), patch.object(
        service, "_check_entry_watch", return_value=None,
    ), patch.object(
        service.regime_classifier, "classify",
        return_value=(MarketRegime.BULLISH, "테스트"),
    ):
        import asyncio
        asyncio.run(service._process_symbol(symbol, balance))

    row = read_last_row(service)
    check("15) _process_symbol() 통합 흐름에서 signal_log의 skip_reason이 "
          "strategy.generate_signal()이 반환한 원래 reason과 정확히 일치함"
          "(관측 필드 계산 로직이 신호 자체를 바꾸지 않음)",
          row["skip_reason"] == original_signal.reason)

# ══════════════════════════════════════════════════════════════
# 8부: 53MB급 CSV 마이그레이션 안전성 — 백업 + 원자적 교체
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    old_header = (
        "timestamp,symbol,price,regime,score,signal,final_decision,order_block_reason,"
        "condition_name,skip_reason,detected_patterns,is_v_rebound,is_pulldown_recovery,"
        "v_drop_pct,v_rise_pct,v_low_age,current_vs_vwap_pct,volume_ratio,bar_amount,"
        "rebound_volume_spike,rebound_volume_ratio,change_rate_pct,v_bottom_spike,"
        "upside_to_recent_high_pct,ma5_above_ma20,v_fail_reason,atr_14,atr_14_pct,"
        "bb_percent_b,bb_bandwidth_pct,bb_position\n"
    )
    path = f"{tmpdir}/signal_log.csv"
    row_count = 500
    with open(path, "w", encoding="utf-8") as f:
        f.write(old_header)
        for i in range(row_count):
            f.write(f"2026-08-01T09:00:00,005930,70000,BULLISH,3,HOLD,HOLD,,,test{i},"
                     "-,,,,,,,,,,,,,,,,,,,,\n")
    original_content = open(path, encoding="utf-8").read()

    logger = SignalCsvLogger(path)

    check("16) 마이그레이션 전 .bak 백업 파일이 생성됨", os.path.exists(path + ".bak"))
    backup_content = open(path + ".bak", encoding="utf-8").read()
    check("    백업 내용이 원본과 완전히 동일함", backup_content == original_content)
    check("    마이그레이션 후 .tmp 임시 파일이 정리되어 남아있지 않음(원자적 교체 확인)",
          not os.path.exists(path + ".tmp"))

    with open(path, encoding="utf-8") as f:
        new_lines = f.readlines()
    check(f"    행 수가 정확히 보존됨({row_count}행)", len(new_lines) - 1 == row_count)
    check("    새 헤더에 macd_above_signal 반영됨", "macd_above_signal" in new_lines[0])

# ══════════════════════════════════════════════════════════════
# 9부: chasing_overheated_condition — HOLD 행에서도 기존 게이트 집계 가능
#
# 2026-08-04 (3차 GPT 코드리뷰 지적, 재현 확인): 기존 chasing_
# overheated_val이 legacy_buy_candidate_val=True일 때만 계산됐는데,
# 실제 BreakoutStrategy의 chasing_overheated 게이트가 진짜로
# 차단한 사례("등락4%+MACD데드+4점")는 전략 자체가 이미 HOLD를
# 반환하므로 legacy_buy_candidate_val=False가 되어 관측 자체가
# 안 됐음(재현 확인). chasing_overheated_condition/would_block_
# existing_chasing_gate는 BUY/HOLD와 무관하게 계산되어야 함.
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=-1.5, macd_signal=-1.0, hist_dir=-1)
    ma = make_minute_analysis(change_rate_pct=4.0, is_valid_change_rate=True)
    # 실제 기존 게이트가 이미 차단해서 HOLD를 반환한 상황
    # (등락4%+MACD데드+4점 -> min_score=5 요구인데 4점이라 미달)
    signal = Signal(type=SignalType.HOLD, reason="추격매수 차단 4/8 — 당일 +4.0% MACD데드")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("17) HOLD 행(기존 게이트가 실제로 차단한 사례)에서도 "
          "chasing_overheated_condition=True로 정확히 집계됨"
          "(legacy_buy_candidate=False인 HOLD인데도 관측됨, 정확히 GPT 지시 필수 테스트)",
          row["legacy_buy_candidate"] == "False" and row["chasing_overheated_condition"] == "True")
    check("    would_block_existing_chasing_gate=True(4점이 5점 미만이라 "
          "기존 게이트가 실제로 차단)", row["would_block_existing_chasing_gate"] == "True")
    check("    신규 가상 게이트 필드(hard/min5)는 legacy_buy_candidate=False라 여전히 빈 값"
          "(정책 유지 확인)",
          row["would_block_macd_dead_min_score5"] == ""
          and row["would_block_macd_above_signal_required"] == "")

# ── 18) chasing_overheated_condition=False일 때 would_block도 False로 명시 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    mp = make_market_price(macd=1.5, macd_signal=1.0, hist_dir=1)  # MACD 골든
    ma = make_minute_analysis(change_rate_pct=4.0, is_valid_change_rate=True)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="HOLD",
        order_block_reason="", market_price=mp,
    )
    row = read_last_row(service)
    check("18) MACD 골든(조건 미충족) -> chasing_overheated_condition=False",
          row["chasing_overheated_condition"] == "False")
    check("    조건이 거짓이면 would_block_existing_chasing_gate=False로 명시"
          "(빈 값이 아님 — 발동 안 함이 명확한 사실)",
          row["would_block_existing_chasing_gate"] == "False")

# ══════════════════════════════════════════════════════════════
# 10부: MinuteBarSaver 날짜별 분할 저장 (GPT 필수 테스트)
# ══════════════════════════════════════════════════════════════

from infra.storage.minute_bar_saver import MinuteBarSaver
from domain.models import MinuteBar

with tempfile.TemporaryDirectory() as tmpdir:
    saver = MinuteBarSaver(base_dir=tmpdir)

    yesterday_bars = [MinuteBar(
        cntr_tm=f"2026080414{i:02d}00", open_price=50000, high_price=50100,
        low_price=49900, close_price=50000, volume=500, acc_volume=5000,
    ) for i in range(43)]
    today_open = datetime(2026, 8, 5, 9, 0, 0)
    today_bars = [MinuteBar(
        cntr_tm=(today_open + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S"),
        open_price=60000, high_price=60100, low_price=59900, close_price=60000,
        volume=500, acc_volume=5000,
    ) for i in range(17)]

    saver.save("005930", yesterday_bars + today_bars)

    yesterday_path = os.path.join(tmpdir, "20260804", "005930.csv")
    today_path = os.path.join(tmpdir, "20260805", "005930.csv")

    check("19) 전일43+오늘17 입력 -> 전일 폴더(20260804)에 정확히 43개 저장됨"
          "(정확히 GPT 지시 필수 테스트)", os.path.exists(yesterday_path))
    check("20) 오늘 폴더(20260805)에 정확히 17개 저장됨", os.path.exists(today_path))

    if os.path.exists(yesterday_path):
        with open(yesterday_path, encoding="utf-8") as f:
            y_rows = list(csv.DictReader(f))
        check("    전일 파일 행 수가 정확히 43개", len(y_rows) == 43)
        check("    전일 파일에 다른 날짜(20260805) timestamp가 0개",
              all(not r["cntr_tm"].startswith("20260805") for r in y_rows))

    if os.path.exists(today_path):
        with open(today_path, encoding="utf-8") as f:
            t_rows = list(csv.DictReader(f))
        check("    오늘 파일 행 수가 정확히 17개", len(t_rows) == 17)
        check("    오늘 파일에 다른 날짜(20260804) timestamp가 0개",
              all(not r["cntr_tm"].startswith("20260804") for r in t_rows))

# ── 21) 파싱 불가능한 timestamp는 조용히 건너뜀(저장 자체는 막지 않음) ──
with tempfile.TemporaryDirectory() as tmpdir:
    saver = MinuteBarSaver(base_dir=tmpdir)
    bad_bar = MinuteBar(
        cntr_tm="invalid_timestamp", open_price=50000, high_price=50100,
        low_price=49900, close_price=50000, volume=500, acc_volume=5000,
    )
    good_bar = MinuteBar(
        cntr_tm="20260805090000", open_price=60000, high_price=60100,
        low_price=59900, close_price=60000, volume=500, acc_volume=5000,
    )
    try:
        saver.save("005930", [bad_bar, good_bar])
        check("21) 파싱 불가능한 timestamp가 섞여도 예외 없이 정상 봉은 저장됨", True)
        good_path = os.path.join(tmpdir, "20260805", "005930.csv")
        with open(good_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        check("    정상 봉 1개만 저장되고 잘못된 timestamp는 건너뜀", len(rows) == 1)
    except Exception as exc:
        check(f"21) 파싱 불가능한 timestamp가 섞여도 예외 없이 정상 봉은 저장됨 - 실패: {exc}", False)

# ══════════════════════════════════════════════════════════════
# 11부: signal_log timestamp KST 통일 (GPT 지시)
# ══════════════════════════════════════════════════════════════

from utils.time_utils import now_kst

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    signal = Signal(type=SignalType.HOLD, reason="테스트")
    service._write_signal_log(
        symbol=symbol, price=100000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=None, final_decision="HOLD",
        order_block_reason="",
    )
    row = read_last_row(service)
    logged_hour = int(row["timestamp"][11:13])
    kst_hour = now_kst().hour
    check("22) signal_log의 timestamp가 KST 기준으로 기록됨"
          "(시스템 로컬 시각과 KST가 다른 환경에서도 정확, 정확히 GPT 지시 필수 테스트)",
          abs(logged_hour - kst_hour) <= 1)  # 자정 경계 오차만 허용

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
