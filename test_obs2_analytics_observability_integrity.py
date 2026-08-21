# -*- coding: utf-8 -*-
"""1P0.8-OBS.2: Analytics/Observability Integrity 회귀 테스트.

민우님이 8/21 번들을 직접 대조해 발견한 4가지 P1 데이터 오염을
검증합니다. 전부 "기록/집계가 실제 사실과 다르게 남는다"는 관측성
버그이며, BUY/SELL 판단·점수 계산·upside 임계값·entry_watch 5분
기준·VWAP 청산 정책·PENDING/orphan 타임아웃·Broker API 호출·주문
상태 폴링 주기·D.1/D.1.1 재조정 정책·entry_quality_guard enforce
스위치·E.1 시작복구·PARTIALLY_FILLED enum 추가는 이 라운드에서
전혀 건드리지 않습니다(절대 금지 항목, 전부 무변경 검증 대상 아님).

  A. entry_watch_shadow가 실제 SELL accepted 최초 시점에만 고정되는지
     (064260 8/21 실측 재현: VWAP 10:49:43 최초 접수 → balance lag →
     최소수익미달 반복 판단 → 최종 shadow가 VWAP/10:49:43에 고정)
  B. Low Upside 통계가 upside=0.0을 결측이 아니라 유효값으로 세는지
  C. WIN/LOSS/BREAKEVEN 정의가 daily_reporter.py/analyze_trades.py
     사이에서 완전히 일치하는지 (017670 8/21 실측: pnl=0)
  D. ORPHAN_CLEARED 로그의 수량이 stale known_quantity가 아니라
     실제 관측된 terminal 수량을 담는지
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from test_run_once_integration import build_minimal_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from domain.models import Position
from domain.position.lifecycle import PositionStateMachine, PositionLifecycle
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_store import JsonStateStore

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


def _build_service(broker) -> TradingService:
    tmpdir = tempfile.mkdtemp()
    settings = build_minimal_settings(tmpdir)
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


class _CountingBroker(MockBroker):
    """place_order() 호출 횟수를 세는 브로커 더블 — "후속 판단이 다시
    주문을 내지 않는다"를 로그 문자열이 아니라 실제 호출 횟수로 검증."""

    def __init__(self) -> None:
        super().__init__()
        self.place_order_calls = 0

    def place_order(self, order):
        self.place_order_calls += 1
        return super().place_order(order)


# ══════════════════════════════════════════════════════════════
# A. entry_watch_shadow 최초 accepted 고정 (OBS.2-A)
# ══════════════════════════════════════════════════════════════
broker_a = _CountingBroker()
svc_a = _build_service(broker_a)
SYM = "064260"
AVG_BUY = 10000
broker_a._prices[SYM] = 10000
broker_a._positions[SYM] = Position(SYM, 100, AVG_BUY)

# A-1) 최초 VWAP SELL accepted → shadow가 VWAP/정확한 시각으로 기록
before_first_call = datetime.now()
svc_a._try_sell(
    SYM, 100, current_price=9700,
    exit_reason="entry_watch VWAP이탈청산 — 매수 후 1.2분, 수익률 -3.00%, VWAP 아래",
    avg_buy_price=AVG_BUY,
)
tracking = svc_a._entry_watch_shadow_tracking.get(SYM)
check("A-1) 최초 VWAP SELL accepted 직후 shadow 기록이 생성됨", tracking is not None)
check("A-1) trigger_type=VWAP이탈청산으로 기록됨",
      tracking is not None and tracking["trigger_type"] == "VWAP이탈청산")
check("A-1) trigger_price가 실제 SELL 가격(9700)으로 기록됨",
      tracking is not None and tracking["trigger_price"] == 9700)
check("A-1) entry_price가 avg_buy_price(10000)로 기록됨",
      tracking is not None and tracking["entry_price"] == AVG_BUY)
check("A-1) trigger_at이 실제 accepted 시각 근방으로 기록됨",
      tracking is not None
      and before_first_call <= tracking["trigger_at"] <= datetime.now())
check("A-1) 최초 접수는 place_order를 정확히 1회 호출함",
      broker_a.place_order_calls == 1)

# 이 시점 PSM은 SELL_PENDING(HARD block) — balance lag를 흉내냄
psm_a = svc_a._position_state_machine
check("A(설정) SELL accepted 직후 PSM이 SELL_PENDING(HARD block)",
      psm_a.get(SYM).lifecycle == PositionLifecycle.SELL_PENDING)

# A-2) balance lag 동안 후속 최소수익미달 SELL 판단 → 원본 불변
svc_a._try_sell(
    SYM, 100, current_price=9750,
    exit_reason="entry_watch 최소수익미달청산 — 매수 후 3.1분, 수익률 -2.50% 미달",
    avg_buy_price=AVG_BUY,
)
tracking2 = svc_a._entry_watch_shadow_tracking.get(SYM)
check("A-2) 후속 최소수익미달 판단 후에도 trigger_type이 VWAP이탈청산으로 그대로",
      tracking2 is not None and tracking2["trigger_type"] == "VWAP이탈청산")
check("A-2) 후속 판단 후에도 trigger_price가 9700 그대로(9750으로 덮어쓰이지 않음)",
      tracking2 is not None and tracking2["trigger_price"] == 9700)
check("A-2) 후속 HARD block 판단은 place_order를 다시 호출하지 않음(여전히 1회)",
      broker_a.place_order_calls == 1)

# A-3) 064260 8/21 실측처럼 수십 차례 반복 판단 → 여전히 1건만 존재
for i in range(40):
    svc_a._try_sell(
        SYM, 100, current_price=9600 + i,
        exit_reason="entry_watch 최소수익미달청산 — 반복 판단",
        avg_buy_price=AVG_BUY,
    )
tracking3 = svc_a._entry_watch_shadow_tracking.get(SYM)
check("A-3) 40회 반복 판단 후에도 trigger가 VWAP/9700에 고정됨",
      tracking3 is not None and tracking3["trigger_type"] == "VWAP이탈청산"
      and tracking3["trigger_price"] == 9700)
check("A-3) 40회 반복 판단 후에도 place_order 호출은 여전히 1회뿐",
      broker_a.place_order_calls == 1)
check("A-3) 이 종목의 shadow 추적 딕셔너리 항목은 정확히 1개",
      len([k for k in svc_a._entry_watch_shadow_tracking if k == SYM]) == 1)

# A-4) balance lag가 완전히 풀리고(FLAT 확정) 새로 재진입한 뒤 다시 청산
#      → 새 트리거가 정상적으로 등록돼야 함
psm_a.on_sell_result(SYM, accepted=True, broker_quantity=0)
check("A(설정) FLAT 확정 후 SELL_PENDING(HARD block)이 해제됨",
      psm_a.get(SYM).lifecycle == PositionLifecycle.FLAT)
broker_a._positions[SYM] = Position(SYM, 50, 9000)  # 재진입 포지션
svc_a._try_sell(
    SYM, 50, current_price=8500,
    exit_reason="entry_watch 급락청산 — 매수 후 0.5분, 수익률 -5.56%",
    avg_buy_price=9000,
)
tracking4 = svc_a._entry_watch_shadow_tracking.get(SYM)
check("A-4) 완전히 새로운 포지션의 청산은 새 trigger로 등록됨",
      tracking4 is not None and tracking4["trigger_type"] == "급락청산"
      and tracking4["trigger_price"] == 8500)
check("A-4) 새 접수는 place_order를 다시 호출함(2회째)",
      broker_a.place_order_calls == 2)

# A-5) 거부된(accepted=False) SELL은 절대 shadow를 새로 잠그지 않음
broker_b = _CountingBroker()
svc_b = _build_service(broker_b)
SYM_B = "017670"
# 브로커에 포지션을 두지 않아 place_order가 "no position"으로 거부하게 함
svc_b._try_sell(
    SYM_B, 100, current_price=9000,
    exit_reason="entry_watch VWAP이탈청산 — 거부될 시도",
    avg_buy_price=10000,
)
check("A-5) 거부된 SELL도 place_order 자체는 호출됨(no position으로 거부)",
      broker_b.place_order_calls == 1)
check("A-5) 거부된 SELL은 shadow 추적을 생성하지 않음",
      SYM_B not in svc_b._entry_watch_shadow_tracking)


# ══════════════════════════════════════════════════════════════
# B. Low Upside 0.0 결측 처리 (OBS.2-B)
# ══════════════════════════════════════════════════════════════
import analyze_trades as AT

check("B-1) upside=None은 결측으로 처리", AT.safe_float_zero_valid(None) is None)
check("B-2) upside=''(빈 문자열)는 결측으로 처리", AT.safe_float_zero_valid("") is None)
check("B-3) upside=0.0은 유효값(0.0)으로 처리됨(결측 아님)",
      AT.safe_float_zero_valid("0.0") == 0.0 and AT.safe_float_zero_valid("0.0") is not None)
check("B-4) upside=0.18은 그대로 0.18로 유효 처리", AT.safe_float_zero_valid("0.18") == 0.18)
check("B-5) 파싱 불가 문자열은 결측으로 처리", AT.safe_float_zero_valid("N/A") is None)
check("B-6) 기존 safe_float()는 그대로 0.0을 None으로 접음(v_drop_pct 등 무변경 확인)",
      AT.safe_float("0.0") is None)


def _trade_row(side, symbol, price, ts, **extra):
    base = {
        "timestamp": ts, "symbol": symbol, "side": side, "accepted": "True",
        "price": str(price), "quantity": "10", "exit_reason": "",
        "hold_minutes": "5",
    }
    base.update(extra)
    return base


# 8/21 017670 재현: upside=0.00(경계값) + 그 외 3건 <1% 구간 + 1건 그 이상
rows_b = [
    _trade_row("BUY", "017670", 100300, "2026-08-21T10:00:00", upside_to_recent_high_pct="0.00"),
    _trade_row("SELL", "017670", 100300, "2026-08-21T10:05:00"),
    _trade_row("BUY", "005930", 71000, "2026-08-21T10:10:00", upside_to_recent_high_pct="0.30"),
    _trade_row("SELL", "005930", 71500, "2026-08-21T10:15:00"),
    _trade_row("BUY", "000660", 185000, "2026-08-21T10:20:00", upside_to_recent_high_pct="0.90"),
    _trade_row("SELL", "000660", 184000, "2026-08-21T10:25:00"),
    _trade_row("BUY", "047040", 5000, "2026-08-21T10:30:00", upside_to_recent_high_pct="0.55"),
    _trade_row("SELL", "047040", 5050, "2026-08-21T10:35:00"),
    _trade_row("BUY", "319400", 30000, "2026-08-21T10:40:00", upside_to_recent_high_pct="2.50"),
    _trade_row("SELL", "319400", 29500, "2026-08-21T10:45:00"),
]
pairs_b = AT.pair_trades(rows_b)
under_1pct = [p for p in pairs_b if p["upside"] is not None and p["upside"] < 1.0]
check("B-7) 8/21 재현 픽스처에서 매매 쌍 5건이 모두 매칭됨", len(pairs_b) == 5)
check("B-8) upside<1% 구간이 0.0 포함 4건으로 정확히 집계됨(0.0 누락 시 3건으로 오집계)",
      len(under_1pct) == 4)
check("B-9) upside=0.00인 017670이 결측으로 빠지지 않고 포함됨",
      any(p["symbol"] == "017670" and p["upside"] == 0.0 for p in pairs_b))


# ══════════════════════════════════════════════════════════════
# C. WIN/LOSS/BREAKEVEN 정의 일치 (OBS.2-C)
# ══════════════════════════════════════════════════════════════
from utils.trade_outcome import classify_outcome, format_win_rate, WIN, LOSS, BREAKEVEN

check("C-1) pnl=+1은 WIN으로 분류", classify_outcome(1) == WIN)
check("C-2) pnl=0은 BREAKEVEN으로 분류", classify_outcome(0) == BREAKEVEN)
check("C-3) pnl=-1은 LOSS로 분류", classify_outcome(-1) == LOSS)
check("C-4) format_win_rate 분모는 breakeven을 제외함(승률=1/2=50%)",
      format_win_rate(1, 1, 1) == "1승 1무 1패 (50%)")
check("C-5) breakeven=0이면 기존과 동일한 'N승 K패' 형식만 출력",
      format_win_rate(2, 1, 0) == "2승 1패 (67%)")

# 017670 8/21 실측 재현: 5건 중 1건이 정확히 동률(pnl=0)
# daily_reporter.py는 과거 "3승 2패", analyze_trades.py는 "2/5(=2승 3패)"로
# 서로 다르게 냈던 바로 그 조합 — 두 리포트에 동일한 fixture를 먹여
# 완전히 같은 승/무/패 숫자가 나오는지 검증.
rows_c = [
    _trade_row("BUY", "017670", 100300, "2026-08-21T09:00:00"),
    _trade_row("SELL", "017670", 100300, "2026-08-21T09:05:00"),   # 동률(무)
    _trade_row("BUY", "005930", 71000, "2026-08-21T09:10:00"),
    _trade_row("SELL", "005930", 72000, "2026-08-21T09:15:00"),    # 승
    _trade_row("BUY", "000660", 185000, "2026-08-21T09:20:00"),
    _trade_row("SELL", "000660", 186000, "2026-08-21T09:25:00"),   # 승
    _trade_row("BUY", "047040", 5000, "2026-08-21T09:30:00"),
    _trade_row("SELL", "047040", 4900, "2026-08-21T09:35:00"),     # 패
    _trade_row("BUY", "319400", 30000, "2026-08-21T09:40:00"),
    _trade_row("SELL", "319400", 29000, "2026-08-21T09:45:00"),    # 패
]
pairs_c = AT.pair_trades(rows_c)
at_wins = [p for p in pairs_c if p["outcome"] == WIN]
at_losses = [p for p in pairs_c if p["outcome"] == LOSS]
at_breakevens = [p for p in pairs_c if p["outcome"] == BREAKEVEN]
check("C-6) analyze_trades.py 기준 5건이 2승 1무 2패로 분류됨",
      len(at_wins) == 2 and len(at_losses) == 2 and len(at_breakevens) == 1)

import infra.storage.daily_reporter as DR
reporter = DR.DailyReporter(
    trade_log_file=str(__import__("pathlib").Path(tempfile.mkdtemp()) / "trades.csv"),
    report_dir=tempfile.mkdtemp(),
)
report_c = reporter._build_report(
    __import__("datetime").date(2026, 8, 21), rows_c, {},
)
check("C-7) daily_reporter.py가 '2승 1무 2패'를 정확히 출력함(동률 배제 승률=50%)",
      "2승 1무 2패 (50%)" in report_c)
check("C-8) analyze_trades.py 승률 문자열도 동일한 승/무/패 숫자로 표시됨",
      format_win_rate(len(at_wins), len(at_losses), len(at_breakevens))
      == "2승 1무 2패 (50%)")
check("C-9) 두 리포트가 동일 fixture에 대해 완전히 동일한 승/무/패 숫자를 냄",
      (len(at_wins), len(at_losses), len(at_breakevens))
      == (2, 2, 1))


# ── OBS.2-C closure: 세부 breakdown 승률도 breakeven 제외 분모로 통일 ──
# headline은 outcome 3분류로 통일됐지만,
# exit_reason/전략/점수/V-PR/volume_spike/Low Upside/당일 등락률/
# 종목별/추세꺾임/트레일링/조건검색식별 등 세부 구간은 여전히
# "wins/len(grp)"(=breakeven이 분모에 섞임) 형태였음. WIN1/
# BREAKEVEN1/LOSS2 픽스처면 정확한 승률은 1/(1+2)=33.3%인데,
# 버그가 있으면 1/4=25.0%가 나옴.
_wlb_grp = [
    {"outcome": WIN}, {"outcome": BREAKEVEN}, {"outcome": LOSS}, {"outcome": LOSS},
]
check("C-10) win_rate_pct()가 WIN1/BREAKEVEN1/LOSS2에서 33.3%를 반환"
      "(잘못된 계산이면 25.0%)",
      AT.win_rate_pct(_wlb_grp) == "33.3%")
check("C-11) win_rate_frac()도 동일 픽스처에서 '1/3 (33%)'를 반환",
      AT.win_rate_frac(_wlb_grp) == "1/3 (33%)")
check("C-12) win_rate_pct(ndigits=0)는 정수 %로 반올림",
      AT.win_rate_pct(_wlb_grp, ndigits=0) == "33%")
check("C-13) 승패가 전혀 없으면(전부 BREAKEVEN) '해당없음'을 반환"
      "(wins/(wins+losses)=0/0은 승률 0%가 아니라 정의 불가 —"
      " headline의 format_win_rate()와 동일한 semantics, 1P0.8-OBS.2 최종 리뷰)",
      AT.win_rate_pct([{"outcome": BREAKEVEN}, {"outcome": BREAKEVEN}]) == "해당없음"
      and AT.win_rate_frac([{"outcome": BREAKEVEN}]) == "0/0 (해당없음)")

# 실제 analyze_trades.py의 "2. exit_reason별 손익" 세부 구간에서도
# 같은 4건 조합(같은 exit_reason으로 묶어 1개 그룹 생성)이 33.3%로
# 나오는지 — 리포트 텍스트를 직접 검사(문자열 매칭이 아니라 실제
# analyze() 실행 결과로 검증).
rows_wlb = [
    _trade_row("BUY", "AAA111", 10000, "2026-08-21T09:00:00", exit_reason=""),
    _trade_row("SELL", "AAA111", 10100, "2026-08-21T09:05:00", exit_reason="테스트청산"),  # WIN
    _trade_row("BUY", "BBB222", 10000, "2026-08-21T09:10:00", exit_reason=""),
    _trade_row("SELL", "BBB222", 10000, "2026-08-21T09:15:00", exit_reason="테스트청산"),  # BREAKEVEN
    _trade_row("BUY", "CCC333", 10000, "2026-08-21T09:20:00", exit_reason=""),
    _trade_row("SELL", "CCC333", 9900, "2026-08-21T09:25:00", exit_reason="테스트청산"),   # LOSS
    _trade_row("BUY", "DDD444", 10000, "2026-08-21T09:30:00", exit_reason=""),
    _trade_row("SELL", "DDD444", 9800, "2026-08-21T09:35:00", exit_reason="테스트청산"),   # LOSS
]
report_wlb = AT.analyze(rows_wlb, __import__("datetime").date(2026, 8, 21),
                        __import__("datetime").date(2026, 8, 21))
check("C-14) analyze_trades.py의 exit_reason별 breakdown이 33.3%로 정확히 집계"
      "(수정 전이면 25.0%로 오집계됐을 4건 그룹)",
      "4건  승률 33.3%" in report_wlb)
check("C-15) 잘못된 25.0%(breakeven 포함 분모)는 나오지 않음",
      "4건  승률 25.0%" not in report_wlb)

# daily_reporter.py의 _build_trade_analysis() exit_reason별도 동일하게 검증
import csv as _csv
_dr_csv = __import__("pathlib").Path(tempfile.mkdtemp()) / "trades.csv"
with _dr_csv.open("w", newline="", encoding="utf-8") as f:
    _w = _csv.DictWriter(f, fieldnames=list(rows_wlb[0].keys()))
    _w.writeheader()
    for _r in rows_wlb:
        _w.writerow(_r)
reporter_wlb = DR.DailyReporter(trade_log_file=str(_dr_csv), report_dir=tempfile.mkdtemp())
trade_analysis_wlb = reporter_wlb._build_trade_analysis(__import__("datetime").date(2026, 8, 21))
check("C-16) daily_reporter.py의 exit_reason별 breakdown도 33.3%로 정확히 집계",
      "4건  승률 33.3%" in trade_analysis_wlb)
check("C-17) daily_reporter.py도 잘못된 25.0%는 나오지 않음",
      "4건  승률 25.0%" not in trade_analysis_wlb)

# [손실 거래] 목록에 BREAKEVEN이 섞이지 않는지 (당일 등락률 섹션)
rows_loss_list = [
    _trade_row("BUY", "EEE555", 10000, "2026-08-21T09:00:00", change_rate_pct="4.0"),
    _trade_row("SELL", "EEE555", 10000, "2026-08-21T09:05:00"),   # BREAKEVEN, change_rate 4.0%
    _trade_row("BUY", "FFF666", 10000, "2026-08-21T09:10:00", change_rate_pct="6.0"),
    _trade_row("SELL", "FFF666", 9900, "2026-08-21T09:15:00"),    # LOSS, change_rate 6.0%
]
report_loss_list = AT.analyze(rows_loss_list, __import__("datetime").date(2026, 8, 21),
                              __import__("datetime").date(2026, 8, 21))
_loss_marker = "[ 손실 거래의 당일 등락률"
_after_marker = report_loss_list.split(_loss_marker, 1)[-1]
# 이 블록만 잘라냄 — 다음 sub-section 헤더("── ")가 시작되기 전까지만.
# (그 뒤로는 섹션 8/9처럼 모든 종목을 다시 나열하는 구간이 있어
# EEE555가 정당하게 재등장하므로, 손실 목록 블록만 좁혀야 함.)
_loss_block = _after_marker.split("── ", 1)[0]
check("C-18) [손실 거래] 목록에 BREAKEVEN 종목(EEE555)이 들어가지 않음",
      "EEE555" not in _loss_block)
check("C-19) [손실 거래] 목록에는 실제 LOSS 종목(FFF666)만 들어감",
      "FFF666" in _loss_block)

# all-BREAKEVEN exit_reason 그룹 — daily_reporter.py의 exit_reason별
# breakdown도 분모가 0(승도 패도 없음)일 때 "0.0%"가 아니라
# "해당없음"을 표시해야 함(1P0.8-OBS.2 최종 리뷰).
rows_all_be = [
    _trade_row("BUY", "GGG777", 10000, "2026-08-21T09:00:00", exit_reason=""),
    _trade_row("SELL", "GGG777", 10000, "2026-08-21T09:05:00", exit_reason="동률청산"),  # BREAKEVEN
    _trade_row("BUY", "HHH888", 10000, "2026-08-21T09:10:00", exit_reason=""),
    _trade_row("SELL", "HHH888", 10000, "2026-08-21T09:15:00", exit_reason="동률청산"),  # BREAKEVEN
]
_dr_csv_be = __import__("pathlib").Path(tempfile.mkdtemp()) / "trades.csv"
with _dr_csv_be.open("w", newline="", encoding="utf-8") as f:
    _w = _csv.DictWriter(f, fieldnames=list(rows_all_be[0].keys()))
    _w.writeheader()
    for _r in rows_all_be:
        _w.writerow(_r)
reporter_all_be = DR.DailyReporter(trade_log_file=str(_dr_csv_be), report_dir=tempfile.mkdtemp())
trade_analysis_all_be = reporter_all_be._build_trade_analysis(__import__("datetime").date(2026, 8, 21))
check("C-25) daily_reporter.py: all-BREAKEVEN exit_reason 그룹은 '승률 해당없음'",
      "2건  승률 해당없음" in trade_analysis_all_be)
check("C-26) daily_reporter.py: all-BREAKEVEN 그룹에서 '승률 0.0%'는 나오지 않음",
      "2건  승률 0.0%" not in trade_analysis_all_be)


# ── OBS.2-C closure: safe_float_zero_valid()의 non-finite 방어 ──
import math as _math
check("C-20) upside=nan은 결측으로 처리(math.isfinite 방어)",
      AT.safe_float_zero_valid("nan") is None)
check("C-21) upside=inf는 결측으로 처리",
      AT.safe_float_zero_valid("inf") is None)
check("C-22) upside=-inf는 결측으로 처리",
      AT.safe_float_zero_valid("-inf") is None)
check("C-23) upside=1e309(오버플로 → inf)는 결측으로 처리",
      AT.safe_float_zero_valid("1e309") is None
      and not _math.isfinite(float("1e309")))
check("C-24) 정상적인 유한값(0.0/0.18/-0.5)은 여전히 유효하게 통과",
      AT.safe_float_zero_valid("0.0") == 0.0
      and AT.safe_float_zero_valid("0.18") == 0.18
      and AT.safe_float_zero_valid("-0.5") == -0.5)


# ══════════════════════════════════════════════════════════════
# D. ORPHAN_CLEARED 수량 정확성 (OBS.2-D)
# ══════════════════════════════════════════════════════════════
class _EventRecorder:
    """PositionLifecycleLogger 대역 — append()로 들어온 행을 그대로 저장."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(row)


def _orphan_cleared_rows(recorder: "_EventRecorder", symbol: str) -> list[dict]:
    return [r for r in recorder.rows if r["symbol"] == symbol and r["event"] == "ORPHAN_CLEARED"]


# D-1) SELL orphan — 잔고 0 도달로 해소. known_quantity는 아직 stale(1051)인데
#      실제 해소를 발생시킨 관측값(0)이 로그에 남아야 함(064260 8/21 재현).
rec_d = _EventRecorder()
psm_d = PositionStateMachine(logger=rec_d)
state_d = psm_d.get("064260")
state_d.lifecycle = PositionLifecycle.SELL_PENDING
state_d.orphan_order_id = "0098765"
state_d.orphan_since = datetime.now() - timedelta(seconds=150)
state_d.orphan_expected_delta = -1051   # SELL orphan
state_d.known_quantity = 1051           # 아직 갱신 전 stale 값
note_d1 = psm_d.observe_for_orphan("064260", 0)   # 실제 관측: 잔고 0 도달
cleared_d1 = _orphan_cleared_rows(rec_d, "064260")
check("D-1) SELL orphan 해소가 실제로 감지됨", note_d1 is not None)
check("D-1) ORPHAN_CLEARED 행이 정확히 1건 기록됨", len(cleared_d1) == 1)
check("D-1) ORPHAN_CLEARED의 broker_quantity가 실제 관측 terminal(0)로 기록됨"
      "(stale known_quantity=1051이 아님)",
      len(cleared_d1) == 1 and cleared_d1[0]["broker_quantity"] == 0)
check("D-1) state.known_quantity 자체는 이 메서드가 건드리지 않음(여전히 1051)",
      state_d.known_quantity == 1051)

# D-2) BUY orphan — 목표수량 도달로 해소. known_quantity는 아직 부분값(150)인데
#      실제 목표수량(200)이 로그에 남아야 함(017670 8/21 재현과 동형 케이스).
rec_d2 = _EventRecorder()
psm_d2 = PositionStateMachine(logger=rec_d2)
state_d2 = psm_d2.get("017670")
state_d2.lifecycle = PositionLifecycle.BUY_PENDING
state_d2.orphan_order_id = "0011223"
state_d2.orphan_since = datetime.now() - timedelta(seconds=90)
state_d2.orphan_expected_delta = 50     # BUY orphan
state_d2.expected_final_quantity = 200
state_d2.known_quantity = 150           # 아직 갱신 전 stale 값
note_d2 = psm_d2.observe_for_orphan("017670", 200)   # 실제 관측: 목표수량 도달
cleared_d2 = _orphan_cleared_rows(rec_d2, "017670")
check("D-2) BUY orphan 해소가 실제로 감지됨", note_d2 is not None)
check("D-2) ORPHAN_CLEARED의 broker_quantity가 목표수량(200)으로 기록됨"
      "(stale known_quantity=150이 아님)",
      len(cleared_d2) == 1 and cleared_d2[0]["broker_quantity"] == 200)

# D-3) 목표에 아직 미달이면 해소되지 않고 orphan 유지(회귀 없음 확인)
rec_d3 = _EventRecorder()
psm_d3 = PositionStateMachine(logger=rec_d3)
state_d3 = psm_d3.get("999999")
state_d3.lifecycle = PositionLifecycle.BUY_PENDING
state_d3.orphan_order_id = "0033445"
state_d3.orphan_since = datetime.now()
state_d3.orphan_expected_delta = 50
state_d3.expected_final_quantity = 200
state_d3.known_quantity = 150
note_d3 = psm_d3.observe_for_orphan("999999", 180)   # 아직 목표 미달
check("D-3) 목표수량 미달이면 orphan이 해소되지 않음(회귀 없음)", note_d3 is None)
check("D-3) orphan_order_id가 그대로 유지됨", state_d3.orphan_order_id == "0033445")

# D-4) acknowledge_orphan()(사람이 수동 확인)은 terminal_quantity를 명시하지
#      않는 기존 경로 — known_quantity를 그대로 쓰는 기존 동작이 무변경이어야 함
rec_d4 = _EventRecorder()
psm_d4 = PositionStateMachine(logger=rec_d4)
state_d4 = psm_d4.get("111111")
state_d4.orphan_order_id = "0055667"
state_d4.orphan_since = datetime.now()
state_d4.known_quantity = 77
psm_d4.acknowledge_orphan("111111", "브로커에서 직접 확인함")
cleared_d4 = _orphan_cleared_rows(rec_d4, "111111")
check("D-4) acknowledge_orphan()은 여전히 known_quantity(77)를 그대로 기록함"
      "(terminal_quantity 미지정 시 기존 동작 무변경)",
      len(cleared_d4) == 1 and cleared_d4[0]["broker_quantity"] == 77)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
