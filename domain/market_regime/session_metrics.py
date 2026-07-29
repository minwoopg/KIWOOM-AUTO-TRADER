# -*- coding: utf-8 -*-
"""세션 지표(SessionMetrics) — v1.6 1C단계, shadow 전용 (2026-07-28).

배경: v1.5 종료 시점 GPT 구조 재검토에서 확인된 핵심 문제 —
`MinuteAnalysis.day_high`/`day_low`/`vwap`이라는 이름과 달리 실제
로는 "최근 60분"(minute_bar_count=60 고정, 1분봉 기준) 값이지
"당일 전체" 값이 아님. 장 초반(예: 09:16)에는 이 60분 창이 전날
오후 봉까지 끌어와 채우는 것을 1A 단계 fixture(legacy_20260721)
로 실제 데이터로 확인한 바 있음.

이 모듈은 그 문제를 고치는 게 아니라, **먼저 "진짜 당일 값"을 별도로
계산해서 기존 "60분 롤링" 값과 나란히 관찰**하는 shadow 단계입니다.
`session_metrics_mode`가 "off"(기본값)면 이 모듈은 아예 호출되지
않고, "shadow"일 때만 계산·로그만 하며 실제 매매 판정(strategy.
generate_signal, entry_watch 등)에는 절대 영향을 주지 않습니다.

2026-07-28 (GPT 2차 코드리뷰 지적, 필터링 누락 재현 확인): 최초
구현은 API가 반환한 60개 봉을 필터링 없이 그대로 세션에 병합해서,
"세션"이라는 이름과 달리 실제로는 API 응답이 이미 포함한 전일
봉까지 그대로 누적되고 있었음(재현: 전일 43개+오늘 17개 입력 시
session_bar_count가 17이 아니라 60, session_low도 전일 저가로
오염). 이번 버전은 병합 시점에 (1) KST 대상 거래일과 같은 날짜인지
(2) 정규장 09:00~15:30 안인지를 반드시 확인해서, 두 조건을 모두
만족하는 봉만 실제로 세션에 들어가도록 함 — 나머지는 필터링
카운터로만 집계.

핵심 설계:
- VWAP 계산 기준은 domain/market_regime/minute_analyzer.py의 기존
  방식(typical price = (high+low+close)/3 * volume)을 그대로
  재사용 — 계산 공식 자체를 이번 단계에서 바꾸지 않음.
- 세션 봉 누적은 (symbol, cntr_tm)을 키로 하는 딕셔너리에 저장 —
  동일 timestamp로 다시 들어오면 기존 값을 덮어씀(누적/합산이
  아니라 "교체"). 단, 대상 거래일·정규장 시간 필터를 통과한 봉만.
- session_metrics_ready는 "봉이 하나라도 있으면 True"가 아니라
  "세션 히스토리가 장 시작부터 끊김없이 확보됐는가"를 의미 —
  가장 오래된 당일 봉이 장 시작(09:00~09:01) 구간이어야 True.
"""
from __future__ import annotations

from dataclasses import dataclass
from domain.models import MinuteBar
from utils.time_utils import MARKET_OPEN, REGULAR_MARKET_CLOSE, parse_kst_bar_timestamp


@dataclass(frozen=True)
class SessionMetrics:
    """당일 세션(정규장 09:00~15:30, 대상 거래일 한정) 기준으로 계산한 지표.

    Attributes:
        session_date: 이 지표가 대상으로 하는 거래일(YYYYMMDD).
        session_vwap: 세션 시작부터 지금까지의 VWAP(typical price
            기준, MinuteAnalyzer와 동일 공식). 필터 통과 봉만 사용.
        session_high: 필터 통과 봉 기준 세션 최고가.
        session_low: 필터 통과 봉 기준 세션 최저가.
        earliest_timestamp: 세션에 포함된 가장 오래된 봉의 cntr_tm.
        latest_timestamp: 세션에 포함된 가장 최신 봉의 cntr_tm.
        rolling_vwap_20: 세션 내 최근 20개 봉만으로 계산한 VWAP.
        rolling_20_count: rolling_vwap_20 계산에 실제로 쓰인 봉 개수.
        rolling_vwap_60: 세션 내 최근 60개 봉만으로 계산한 VWAP.
        rolling_60_count: rolling_vwap_60 계산에 실제로 쓰인 봉 개수.
        recent_high_30: 세션 내 최근 30개 봉만의 최고가.
        session_bar_count: 필터를 통과해 세션에 실제로 포함된 봉 개수.
        filtered_other_date_count: 대상 거래일이 아니라서 걸러진
            봉 개수(예: 전일 봉).
        filtered_outside_market_count: 대상 거래일은 맞지만 정규장
            시간(09:00~15:30) 밖이라 걸러진 봉 개수.
        session_metrics_ready: 세션 히스토리가 장 시작부터 확보됐는지.
        readiness_reason: ready 판정의 근거 —
            "COMPLETE_FROM_OPEN"(정상, 장 시작 구간부터 확보) |
            "PARTIAL_SESSION"(중간부터 시작해 앞부분 결측) |
            "NO_SESSION_DATA"(대상 거래일 봉이 아예 없음).
    """
    session_date: str
    session_vwap: float
    session_high: int
    session_low: int
    earliest_timestamp: str | None
    latest_timestamp: str | None
    rolling_vwap_20: float | None
    rolling_20_count: int
    rolling_vwap_60: float | None
    rolling_60_count: int
    recent_high_30: int | None
    session_bar_count: int
    filtered_other_date_count: int
    filtered_outside_market_count: int
    session_metrics_ready: bool
    readiness_reason: str


def _typical_price_vwap(bars):
    """MinuteAnalyzer.analyze()와 동일한 공식으로 VWAP을 계산합니다.

    2026-07-28: 계산 공식 자체는 이번 1C단계에서 바꾸지 않는다는
    원칙에 따라, domain/market_regime/minute_analyzer.py의 기존
    VWAP 계산(typical price * volume 가중평균)을 그대로 재사용.
    total_vol이 0이면(거래량 정보가 전혀 없는 극단적 경우) 마지막
    봉의 종가로 대체 — 기존 analyzer의 fallback과 동일한 방어.
    """
    if not bars:
        return 0.0
    total_pv = sum(
        ((b.high_price + b.low_price + b.close_price) / 3) * b.volume for b in bars
    )
    total_vol = sum(b.volume for b in bars)
    if total_vol > 0:
        return total_pv / total_vol
    return float(bars[-1].close_price)


def _passes_session_filter(bar, session_date):
    """봉 하나가 세션 필터(대상 거래일 + 정규장 시간)를 통과하는지 확인합니다.

    2026-07-28 (GPT 2차 코드리뷰 지적, 필터링 누락 재현 확인):
    이전 버전에는 이 필터 자체가 없어서 API가 반환한 60개 전부가
    필터링 없이 세션에 들어갔음(재현: 전일 43개+오늘 17개 입력 시
    session_bar_count가 60으로 오염).

    Returns:
        (통과 여부, 사유 코드) — 통과하면 (True, ""), 실패하면
        (False, "OTHER_DATE" | "OUTSIDE_MARKET" | "INVALID_TIMESTAMP").
    """
    dt = parse_kst_bar_timestamp(bar.cntr_tm)
    if dt is None:
        return False, "INVALID_TIMESTAMP"
    if dt.strftime("%Y%m%d") != session_date:
        return False, "OTHER_DATE"
    if not (MARKET_OPEN <= dt.time() <= REGULAR_MARKET_CLOSE):
        return False, "OUTSIDE_MARKET"
    return True, ""


@dataclass
class SessionState:
    """종목 하나의 세션 누적 상태(가변) — TradingService가 종목별로 보관합니다.

    2026-07-28 (GPT 코드리뷰 지적 3, 6번): 딕셔너리 하나(symbol ->
    session_bars)만 쓰던 이전 구조는 (a) 종목별로 "이 세션이 어느
    날짜 것인지"를 별도로 추적하지 않아 날짜가 바뀌어도 reset_
    daily_loss_counts() 호출이 누락되면 전일 세션이 그대로 남을
    위험이 있었고, (b) 매번 merge_session_bar()를 호출할 때마다
    dict 전체를 복사해 60번 호출 시 O(n^2) 복사가 발생했음. 이제
    종목별로 SessionState(session_date + bars 딕셔너리)를 함께
    들고, 날짜가 바뀐 새 봉이 들어오면 자동으로 자체 초기화됨.
    """
    session_date: str
    bars: dict
    filtered_other_date_count: int = 0
    filtered_outside_market_count: int = 0


def merge_session_bars(state, new_bars, target_session_date):
    """새로 받은 분봉 목록을 세션 상태에 배치로 병합합니다(필터링 포함).

    2026-07-28 (GPT 코드리뷰 지적 3, 6번): 다음을 한 번에 처리하는
    배치 함수로 재설계:
    - 봉 하나하나에 대해 개별 dict 복사를 반복하지 않고, 이 함수
      안에서 한 번만 딕셔너리를 복사(호출부 관점에서는 여전히 새
      SessionState를 반환하는 순수 함수 스타일을 유지하되, 불필요한
      반복 복사를 피함 — 60개 봉을 병합해도 dict 복사는 1회).
    - 대상 거래일 필터 + 정규장 시간 필터를 통과한 봉만 실제로
      병합(동일 timestamp는 교체, 누적 아님).
    - 필터를 통과하지 못한 봉은 filtered_other_date_count/
      filtered_outside_market_count에 집계.
    - 종목의 기존 세션 상태(state)가 다른 날짜의 것이면(즉 새
      target_session_date와 다르면), reset_daily_loss_counts()
      호출 여부와 무관하게 이 함수 자체가 자동으로 빈 세션부터
      다시 시작 — "reset 누락에도 전일 혼입을 막는다"는 GPT
      지시를 이 함수 레벨에서 보장.

    Args:
        state: 종목의 기존 SessionState. None이거나 session_date가
            target_session_date와 다르면 새로 시작.
        new_bars: 이번에 API/캐시로부터 받은 분봉 목록(예: 최근
            60개 롤링 — 필터링 전 원본 그대로 전달).
        target_session_date: 지금 계산 대상인 거래일(YYYYMMDD,
            보통 now_kst()의 날짜).

    Returns:
        갱신된 SessionState(새 객체 — 호출부가 참조 동일성에
        의존하지 않도록 함).
    """
    if state is None or state.session_date != target_session_date:
        bars_dict = {}
        filtered_other = 0
        filtered_outside = 0
    else:
        bars_dict = dict(state.bars)  # 한 번만 복사
        filtered_other = state.filtered_other_date_count
        filtered_outside = state.filtered_outside_market_count

    for bar in new_bars:
        passed, reason_code = _passes_session_filter(bar, target_session_date)
        if not passed:
            if reason_code == "OTHER_DATE":
                filtered_other += 1
            elif reason_code == "OUTSIDE_MARKET":
                filtered_outside += 1
            continue
        bars_dict[bar.cntr_tm] = bar  # 동일 timestamp는 교체(누적 아님)

    return SessionState(
        session_date=target_session_date,
        bars=bars_dict,
        filtered_other_date_count=filtered_other,
        filtered_outside_market_count=filtered_outside,
    )


def build_session_metrics(state):
    """세션 상태로부터 SessionMetrics를 계산합니다.

    2026-07-28 (GPT 코드리뷰 지적 2번, readiness 의미 수정): 기존엔
    "봉이 하나라도 있으면 ready=True"였는데, 원래 합의는 "전체 세션
    히스토리 확보 여부"였음. 이제 가장 오래된 세션 봉이 장 시작
    (09:00~09:01) 구간이어야 COMPLETE_FROM_OPEN(ready=True) —
    그렇지 않으면(예: 프로그램을 13시에 시작해 12:01~13:00만 가진
    경우) PARTIAL_SESSION(ready=False).

    이 함수는 state.bars의 삽입 순서에 의존하지 않고, 매번 cntr_tm
    문자열 기준으로 정렬해서 계산 — 호출 순서가 뒤섞여 들어와도
    항상 시간순으로 올바르게 계산되도록 함.

    Returns:
        SessionMetrics. 세션 봉이 하나도 없으면 session_metrics_
        ready=False, readiness_reason="NO_SESSION_DATA"이고 나머지
        값은 0/None으로 채워진 결과 반환(예외를 던지지 않음).
    """
    if state is None or not state.bars:
        return SessionMetrics(
            session_date=state.session_date if state is not None else "",
            session_vwap=0.0, session_high=0, session_low=0,
            earliest_timestamp=None, latest_timestamp=None,
            rolling_vwap_20=None, rolling_20_count=0,
            rolling_vwap_60=None, rolling_60_count=0,
            recent_high_30=None,
            session_bar_count=0,
            filtered_other_date_count=state.filtered_other_date_count if state else 0,
            filtered_outside_market_count=state.filtered_outside_market_count if state else 0,
            session_metrics_ready=False,
            readiness_reason="NO_SESSION_DATA",
        )

    sorted_bars = [state.bars[ts] for ts in sorted(state.bars.keys())]

    session_vwap = _typical_price_vwap(sorted_bars)
    session_high = max(b.high_price for b in sorted_bars)
    session_low = min(b.low_price for b in sorted_bars)
    earliest_ts = sorted_bars[0].cntr_tm
    latest_ts = sorted_bars[-1].cntr_tm

    last_20 = sorted_bars[-20:]
    last_60 = sorted_bars[-60:]
    last_30 = sorted_bars[-30:]

    rolling_vwap_20 = _typical_price_vwap(last_20)
    rolling_vwap_60 = _typical_price_vwap(last_60)
    recent_high_30 = max(b.high_price for b in last_30)

    # 2026-07-28 (GPT 코드리뷰 지적 2번): ready 판정 — 가장 오래된
    # 세션 봉이 장 시작(09:00) 첫 1분 구간(09:00 또는 09:01, API가
    # 09:00:xx를 09:01로 집계해서 줄 수도 있으므로 09:01까지 허용)
    # 이어야 "장 시작부터 끊김없이 확보"로 인정.
    earliest_dt = parse_kst_bar_timestamp(earliest_ts)
    if earliest_dt is not None and earliest_dt.hour == 9 and earliest_dt.minute <= 1:
        ready = True
        readiness_reason = "COMPLETE_FROM_OPEN"
    else:
        ready = False
        readiness_reason = "PARTIAL_SESSION"

    return SessionMetrics(
        session_date=state.session_date,
        session_vwap=session_vwap,
        session_high=session_high,
        session_low=session_low,
        earliest_timestamp=earliest_ts,
        latest_timestamp=latest_ts,
        rolling_vwap_20=rolling_vwap_20,
        rolling_20_count=len(last_20),
        rolling_vwap_60=rolling_vwap_60,
        rolling_60_count=len(last_60),
        recent_high_30=recent_high_30,
        session_bar_count=len(sorted_bars),
        filtered_other_date_count=state.filtered_other_date_count,
        filtered_outside_market_count=state.filtered_outside_market_count,
        session_metrics_ready=ready,
        readiness_reason=readiness_reason,
    )


def format_session_metrics_log_line(symbol, metrics, legacy_vwap, legacy_day_high, legacy_day_low):
    """세션 지표를 기존(60분 롤링) 값과 나란히 보여주는 관찰 로그 한 줄을 만듭니다.

    2026-07-28: shadow 단계의 목적 자체가 "기존 값과 세션 값이
    실제로 얼마나 다른지 눈으로 확인하는 것"이므로, legacy_*
    (MinuteAnalysis에서 그대로 가져온 기존 60분 롤링 값)를 함께
    출력. 이 값은 이제 호출부가 이미 계산해둔 analysis에서 전달
    받음 — 이 함수/모듈 내부에서 MinuteAnalyzer를 다시 호출하지
    않음(GPT 코드리뷰 지적 4번, "shadow가 상태성 객체를 재호출해
    상태를 바꾸면 안 됨").
    """
    legacy_vwap_str = f"{legacy_vwap:.2f}" if legacy_vwap is not None else "N/A"
    legacy_high_str = str(legacy_day_high) if legacy_day_high is not None else "N/A"
    legacy_low_str = str(legacy_day_low) if legacy_day_low is not None else "N/A"
    rolling_20_str = (
        f"{metrics.rolling_vwap_20:.2f}" if metrics.rolling_vwap_20 is not None else "N/A"
    )
    rolling_60_str = (
        f"{metrics.rolling_vwap_60:.2f}" if metrics.rolling_vwap_60 is not None else "N/A"
    )
    recent_high_30_str = (
        str(metrics.recent_high_30) if metrics.recent_high_30 is not None else "N/A"
    )
    vwap_diff = (
        f"{metrics.session_vwap - legacy_vwap:+.2f}"
        if legacy_vwap is not None and metrics.session_metrics_ready
        else "N/A"
    )
    return (
        f"[SESSION_SHADOW] {symbol} | date={metrics.session_date} "
        f"ready={metrics.session_metrics_ready} reason={metrics.readiness_reason} "
        f"bar_count={metrics.session_bar_count} "
        f"filtered_other_date={metrics.filtered_other_date_count} "
        f"filtered_outside_market={metrics.filtered_outside_market_count} | "
        f"earliest={metrics.earliest_timestamp} latest={metrics.latest_timestamp} | "
        f"session_vwap={metrics.session_vwap:.2f} legacy_vwap(60min)={legacy_vwap_str} "
        f"diff={vwap_diff} | "
        f"session_high={metrics.session_high} legacy_day_high(60min)={legacy_high_str} | "
        f"session_low={metrics.session_low} legacy_day_low(60min)={legacy_low_str} | "
        f"rolling_vwap_20={rolling_20_str}({metrics.rolling_20_count}봉) "
        f"rolling_vwap_60={rolling_60_str}({metrics.rolling_60_count}봉) "
        f"recent_high_30={recent_high_30_str}"
    )
