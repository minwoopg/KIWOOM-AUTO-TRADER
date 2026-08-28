# -*- coding: utf-8 -*-
"""
Candidate A Production Pilot v1 검증 (2026-08-28, 민우님 명세 v1 + 승인 A단계)

이번 승인 범위: 코드 구현 + candidate_a_guard_mode="shadow" 배포까지.
enforce는 여기서 검증만 하고(코드 레벨 동작 확인), settings.yaml에는
절대 "enforce"로 배포하지 않는다(별도 배포 확인은 4부).

predicate: upside_to_recent_high_pct < 0.50 AND rebound_volume_spike is False
(domain/strategy/candidate_a_guard.py에 고정, 여기서 재계산하지 않음)

이 테스트가 검증하는 것:
  1부: evaluate_candidate_a() 순수 predicate 정확성 — 매치/불일치/경계값/
       결측(Unknown) 케이스. Unknown은 반드시 None(False로 추정 금지).
  2부: ExperimentalConfig.candidate_a_guard_mode — 기본값, 유효값 검증,
       YAML boolean 자동coercion 방어, 잘못된 문자열 거부.
  3부: _try_buy() 게이트 — off/shadow에서는 predicate 매치 여부와
       무관하게 주문이 그대로 나가고, enforce에서만 정확히 predicate가
       True일 때만 차단됨. 차단 시 broker.place_order/PSM/journal
       어느 쪽에도 side effect가 없음. Unknown(None)은 PASS.
  4부: 배포 설정(config/settings.yaml) 회귀 가드 — 지금 배포값이
       "shadow"인지(실수로 "enforce"가 커밋되지 않았는지) 확인.
  5부: precedence — RiskManager가 이미 차단한 경우 Candidate A gate가
       그보다 먼저 끼어들지 않고 원래 사유가 그대로 반환됨(삽입 위치가
       risk_manager.can_place_order() "이후"라는 것의 동작 증거).
  6부: logging parity — _write_signal_log()의 low_upside_shadow 기록이
       evaluate_candidate_a()와 동일한 값을 쓰는지(단일 진실 소스 증명).
  7부: order_block_reason이 bare code(SkipReason.CANDIDATE_A_GUARD)이고
       파라미터가 섞여 있지 않은지(1P0.3 컨벤션).
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from config.settings import ExperimentalConfig, load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.strategy.candidate_a_guard import evaluate_candidate_a, CANDIDATE_A_UPSIDE_THRESHOLD_PCT
from domain.models import MarketRegime, Position, Signal, SignalType
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore
from infra.storage.skip_reason import SkipReason
from utils.time_utils import KST_TZ

FIXED_MARKET_TIME = datetime(2026, 8, 28, 10, 0, 0, tzinfo=KST_TZ)

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


def read_rows(path: str):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


symbol = "475150"

# ══════════════════════════════════════════════════════════════
# 1부: evaluate_candidate_a() predicate 정확성
# ══════════════════════════════════════════════════════════════

check("1-1) threshold 상수는 명세대로 0.50", CANDIDATE_A_UPSIDE_THRESHOLD_PCT == 0.50)

check("1-2) minute_analysis=None → None(판정불가)", evaluate_candidate_a(None) is None)

ma_match = make_ma(upside_to_recent_high_pct=0.24, rebound_volume_spike=False)
check("1-3) upside=0.24(<0.50) AND spike=False → True(매치)",
      evaluate_candidate_a(ma_match) is True)

ma_high_upside = make_ma(upside_to_recent_high_pct=0.80, rebound_volume_spike=False)
check("1-4) upside=0.80(>=0.50) AND spike=False → False(불일치, 상승여력 충분)",
      evaluate_candidate_a(ma_high_upside) is False)

ma_spike = make_ma(upside_to_recent_high_pct=0.10, rebound_volume_spike=True)
check("1-5) upside=0.10(<0.50) AND spike=True → False(불일치, 반등거래량 spike 있음)",
      evaluate_candidate_a(ma_spike) is False)

ma_neither = make_ma(upside_to_recent_high_pct=2.0, rebound_volume_spike=True)
check("1-6) upside 충분 AND spike=True → False(둘 다 불일치)",
      evaluate_candidate_a(ma_neither) is False)

ma_boundary = make_ma(upside_to_recent_high_pct=0.50, rebound_volume_spike=False)
check("1-7) upside 정확히 0.50(경계값) → False(strict < 이므로 매치 아님)",
      evaluate_candidate_a(ma_boundary) is False)

ma_negative = make_ma(upside_to_recent_high_pct=-1.2, rebound_volume_spike=False)
check("1-8) upside가 음수(현재가가 이미 최근고점 위)여도 <0.50이면 정상 매치",
      evaluate_candidate_a(ma_negative) is True)

# MinuteAnalysis는 frozen이 아닌 일반 dataclass이므로 생성 후 직접
# 대입해 "이론상 나올 수 없지만 방어적으로 다뤄야 하는" 결측 상태를 흉내냄.
ma_none_spike = make_ma(upside_to_recent_high_pct=0.1)
ma_none_spike.rebound_volume_spike = None  # type: ignore[assignment]
check("1-9) rebound_volume_spike가 True/False가 아닌 값(None)이면 → None(Unknown, False로 추정 금지)",
      evaluate_candidate_a(ma_none_spike) is None)

check("1-10) 결측 시 True/False가 아니라 반드시 None(불리언 오추정 방지)",
      evaluate_candidate_a(ma_none_spike) is not False
      and evaluate_candidate_a(ma_none_spike) is not True)


# ══════════════════════════════════════════════════════════════
# 2부: ExperimentalConfig.candidate_a_guard_mode
# ══════════════════════════════════════════════════════════════

check("2-1) 기본값은 off", ExperimentalConfig().candidate_a_guard_mode == "off")

for _mode in ("off", "shadow", "enforce"):
    cfg = ExperimentalConfig(candidate_a_guard_mode=_mode)
    check(f"2-2) 유효값 '{_mode}'는 정상 생성됨", cfg.candidate_a_guard_mode == _mode)

try:
    ExperimentalConfig(candidate_a_guard_mode="bogus")
    check("2-3) 잘못된 문자열 값은 ValueError로 거부됨", False)
except ValueError:
    check("2-3) 잘못된 문자열 값은 ValueError로 거부됨", True)

try:
    ExperimentalConfig(candidate_a_guard_mode=False)  # YAML 1.1 boolean coercion 시뮬레이션
    check("2-4) YAML boolean 자동coercion(off->False)이 ValueError로 방어됨", False)
except ValueError:
    check("2-4) YAML boolean 자동coercion(off->False)이 ValueError로 방어됨", True)


# ══════════════════════════════════════════════════════════════
# 3부: _try_buy() 게이트 — off/shadow/enforce 모드별 동작
# ══════════════════════════════════════════════════════════════

# ── 3-1) off: predicate가 매치해도(차단 후보) 주문이 그대로 나감 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "off")
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # predicate 매치
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("3-1) off 모드: predicate 매치여도 차단 없음(빈 문자열 반환)", not block)
    check("   off 모드: 실제로 broker에 주문이 접수됨(포지션 생성)",
          symbol in service.broker._positions)

# ── 3-2) shadow: 마찬가지로 주문이 그대로 나감 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "shadow")
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # predicate 매치
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("3-2) shadow 모드: predicate 매치여도 차단 없음(빈 문자열 반환) — shadow는 절대 주문을 막지 않음",
          not block)
    check("   shadow 모드: 실제로 broker에 주문이 접수됨(포지션 생성)",
          symbol in service.broker._positions)

# ── 3-3) enforce + predicate 매치 → 차단, side effect 전무 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # predicate 매치
    balance = service.broker.get_account_balance()
    cash_before = service.broker._cash
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("3-3) enforce 모드 + predicate 매치 → SkipReason.CANDIDATE_A_GUARD 반환",
          block == SkipReason.CANDIDATE_A_GUARD)
    check("   차단 시 broker.place_order 미호출(포지션 생성 안 됨)",
          symbol not in service.broker._positions)
    check("   차단 시 broker 현금 변동 없음(주문 자체가 안 나감)",
          service.broker._cash == cash_before)
    # 2026-08-28: would_block_buy_detail()이 함수 맨 앞에서 이미 self.get(symbol)을
    # 호출해 _states[symbol]을 FLAT 기본값으로 auto-vivify함(Candidate A와
    # 무관한 기존 동작) — 그래서 "키 자체의 부재"가 아니라 "on_buy_requested()가
    # 호출됐을 때만 바뀌는 lifecycle이 여전히 FLAT인지"로 side effect 없음을 검증.
    from domain.position.lifecycle import PositionLifecycle
    check("   차단 시 PositionStateMachine이 BUY_PENDING으로 전이하지 않음(on_buy_requested 미호출)",
          service._position_state_machine.get(symbol).lifecycle == PositionLifecycle.FLAT)
    check("   차단 시 tracked_order_journal에 기록 없음",
          service._tracked_order_journal.get(symbol) is None)

# ── 3-4) enforce + predicate 불일치(upside 충분) → 정상 주문 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    ma = make_ma(upside_to_recent_high_pct=2.0, rebound_volume_spike=False)  # predicate 불일치
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("3-4) enforce 모드 + predicate 불일치(상승여력 충분) → 차단 없음", not block)
    check("   정상적으로 주문이 접수됨(포지션 생성)", symbol in service.broker._positions)

# ── 3-5) enforce + predicate 불일치(spike=True) → 정상 주문 ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=True)  # spike 있음 → 불일치
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("3-5) enforce 모드 + predicate 불일치(반등spike 있음) → 차단 없음", not block)
    check("   정상적으로 주문이 접수됨(포지션 생성)", symbol in service.broker._positions)

# ── 3-6) enforce + minute_analysis=None(Unknown) → PASS(차단 안 함) ──
with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=None)
    check("3-6) enforce 모드 + minute_analysis=None(판정불가) → PASS(차단 없음, False로 추정 안 함)",
          not block)
    check("   정상적으로 주문이 접수됨(포지션 생성)", symbol in service.broker._positions)


# ══════════════════════════════════════════════════════════════
# 4부: 배포 설정 회귀 가드 — 지금 실제 커밋되는 값이 shadow인지
# ══════════════════════════════════════════════════════════════

try:
    _deployed = load_settings("config/settings.yaml")
    check("4-1) config/settings.yaml의 candidate_a_guard_mode는 현재 'shadow' "
          "(이번 승인 범위는 A단계까지 — 'enforce'가 실수로 커밋되지 않았는지 확인)",
          _deployed.experimental.candidate_a_guard_mode == "shadow")
except Exception as exc:
    check(f"4-1) config/settings.yaml 로딩 실패 — {type(exc).__name__}: {exc}", False)


# ══════════════════════════════════════════════════════════════
# 5부: precedence — RiskManager가 이미 차단했으면 그 사유가 우선
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    object.__setattr__(service.settings.trading, "max_positions", 1)
    # 이미 다른 종목을 보유 중 → max_positions 초과로 risk_manager 이전
    # 단계(_try_buy 자체의 max_positions 체크)에서 먼저 막혀야 함.
    service.broker._positions["000660"] = Position(symbol="000660", quantity=10, average_price=10000)
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # Candidate A도 매치하지만
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("5-1) MAX_POSITIONS로 이미 차단된 경우 → CANDIDATE_A_GUARD가 아니라 "
          "SKIP_MAX_POSITIONS가 그대로 반환됨(Candidate A gate가 먼저 끼어들지 않음)",
          block == SkipReason.MAX_POSITIONS)

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    object.__setattr__(service.settings.risk, "min_cash_buffer", 999_999_999_999)  # RiskManager가 RISK_LIMIT으로 거부
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # Candidate A도 매치하지만
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    check("5-2) RiskManager.can_place_order()가 이미 거부한 경우(RISK_LIMIT) → "
          "Candidate A gate는 그 뒤에 있으므로 원래 사유가 그대로 반환됨 "
          "(삽입 위치가 can_place_order() '이후'라는 것의 동작 증거)",
          block == SkipReason.RISK_LIMIT)


# ══════════════════════════════════════════════════════════════
# 6부: logging parity — low_upside_shadow가 evaluate_candidate_a()와 동일 값을 씀
# ══════════════════════════════════════════════════════════════

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.1, rebound_volume_spike=False)  # Candidate A 매치
    signal = Signal(type=SignalType.BUY, reason="테스트 6/8")
    expected = evaluate_candidate_a(ma)
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260828090000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("6-1) low_upside_shadow.csv의 would_skip_low_upside_no_spike가 "
          "evaluate_candidate_a()와 정확히 같은 값을 기록함(매치 케이스)",
          expected is True and r["would_skip_low_upside_no_spike"] == "True")
    check("   rebound_volume_spike 원시값도 그대로 기록됨", r["rebound_volume_spike"] == "False")

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=2.0, rebound_volume_spike=False)  # 불일치
    signal = Signal(type=SignalType.BUY, reason="테스트 5/8")
    expected = evaluate_candidate_a(ma)
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260828091000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("6-2) 불일치 케이스도 evaluate_candidate_a()와 동일한 값을 기록함",
          expected is False and r["would_skip_low_upside_no_spike"] == "False")

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    ma = make_ma(upside_to_recent_high_pct=0.1)
    ma.rebound_volume_spike = None  # type: ignore[assignment]
    signal = Signal(type=SignalType.BUY, reason="테스트 4/8")
    service._write_signal_log(
        symbol=symbol, price=10000, regime=MarketRegime.BULLISH,
        signal=signal, minute_analysis=ma, final_decision="BUY",
        order_block_reason="", latest_bar_timestamp="20260828092000",
    )
    r = read_rows(service.settings.storage.low_upside_shadow_log_file)[0]
    check("6-3) Unknown(rebound_volume_spike=None) 케이스는 빈 문자열로 기록됨"
          "(False로 추정해 기록하지 않음)",
          r["would_skip_low_upside_no_spike"] == "" and r["rebound_volume_spike"] == "")


# ══════════════════════════════════════════════════════════════
# 7부: order_block_reason은 bare code — 파라미터가 섞여 있지 않음
# ══════════════════════════════════════════════════════════════

check("7-1) SkipReason.CANDIDATE_A_GUARD는 파라미터 없는 bare code",
      SkipReason.CANDIDATE_A_GUARD == "SKIP_CANDIDATE_A_GUARD"
      and "(" not in SkipReason.CANDIDATE_A_GUARD
      and "=" not in SkipReason.CANDIDATE_A_GUARD)

with tempfile.TemporaryDirectory() as tmpdir:
    service = build_service(tmpdir)
    object.__setattr__(service.settings.experimental, "candidate_a_guard_mode", "enforce")
    ma = make_ma(upside_to_recent_high_pct=0.05, rebound_volume_spike=False)
    balance = service.broker.get_account_balance()
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block1 = service._try_buy(symbol, 58000, balance, signal=None, regime=None, minute_analysis=ma)
    ma2 = make_ma(upside_to_recent_high_pct=0.49, rebound_volume_spike=False)
    with patch("domain.service.trading_service.now_kst", return_value=FIXED_MARKET_TIME):
        block2 = service._try_buy("000660", 58000, balance, signal=None, regime=None, minute_analysis=ma2)
    check("7-2) upside 값이 달라도(0.05 vs 0.49) 반환되는 order_block_reason은 "
          "완전히 동일한 문자열(값마다 다른 사유로 오염되지 않음, 1P0.3 컨벤션)",
          block1 == block2 == SkipReason.CANDIDATE_A_GUARD)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
