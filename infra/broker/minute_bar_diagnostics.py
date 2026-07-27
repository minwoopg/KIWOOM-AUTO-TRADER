# -*- coding: utf-8 -*-
"""분봉 API(get_minute_bars) raw 응답 진단 (2026-07-27, 리팩터링 1B단계).

배경: 1A 단계에서 fixture(tests/fixtures/legacy_20260721/)를 만들며,
저장된 분봉을 사후 복원한 결과 장 초반 케이스에서 전일 봉이 최근
60봉 안에 섞여 있었을 가능성이 강하게 시사됨. 하지만 이건 "저장된
CSV로 사후 복원한 정황 증거"일 뿐, 실제 API가 매 요청마다 정확히
몇 개의 봉을 반환하는지, 정렬 순서가 어떤지, 최신 봉이 완성봉인지
진행 중 봉인지는 아직 한 번도 직접 관측한 적이 없음.

이 모듈은 그 관측 데이터를 만드는 순수 함수만 담습니다. 실제
브로커 코드(infra/broker/kiwoom_broker.py)의 반환값(raw_bars[:count]
슬라이싱, MinuteBar 파싱, bars.reverse())은 이 모듈이 존재하기
전과 완전히 동일하게 유지됩니다 — 이 모듈은 그 과정을 "관찰"만
하고, 계산 결과에 관여하지 않습니다.

GPT 코드리뷰 설계 원칙:
- 진단 계산(순수 함수, 테스트하기 쉬움)과 로그 출력(부수효과)을
  분리 — 이 모듈에는 로그 출력이 전혀 없고, 오직 계산만 있음.
- 진단 실패가 분봉 조회 자체를 실패시키면 안 됨(fail-open) — 이건
  호출부(kiwoom_broker.py)의 책임이지만, 이 모듈 자체도 예외를
  최대한 던지지 않고 None/기본값으로 방어적으로 계산.
- cont-yn/next-key 원문은 저장하지 않고 bool로만 변환.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

# 2026-07-27 (실운영 크래시로 발견, 긴급 수정): 원래 zoneinfo.
# ZoneInfo("Asia/Seoul")을 썼는데, Windows에는 OS 차원의 IANA
# 시간대 데이터베이스가 없어서 tzdata 패키지가 설치되어 있지 않으면
# ZoneInfoNotFoundError가 남 — 게다가 이 줄이 모듈 최상단(import
# 시점에 즉시 평가)에 있어서, 진단 로직을 감싸던 fail-open
# try/except의 보호 범위 밖에서 모듈 import 자체가 실패했음(실제
# 사용자 환경에서 재현: `python test_minute_bar_diagnostics.py`
# 실행 시 ModuleNotFoundError: No module named 'tzdata'로 즉시
# 크래시). 이건 1B 설계 원칙(진단 실패가 절대 분봉 조회를 막으면
# 안 됨)을 이 지점에서 어긴 것.
#
# 한국 표준시(KST)는 서머타임이 없는 고정 UTC+9이므로, IANA
# 시간대 데이터베이스(zoneinfo/tzdata)가 굳이 없어도
# datetime.timezone(timedelta(hours=9))로 정확히 동일한 결과를
# 얻을 수 있음 — 외부 데이터 의존성 자체를 제거.
KST = timezone(timedelta(hours=9), name="Asia/Seoul")

# 2026-07-27 (GPT 코드리뷰 지시 7번): 정규장 시간 기준을 임의로
# 새로 정하지 않고, 기존 프로젝트에 이미 있던 상수를 재사용.
# utils/time_utils.py의 MARKET_OPEN=09:00, MARKET_CLOSE=15:20을
# 그대로 가져옴 — 다만 이 값은 "신규 매수/매도 중단 시각"(15:20)
# 이지 실제 정규장 마감(통상 15:30)과는 다른 의미라는 점에 주의.
from utils.time_utils import MARKET_OPEN, MARKET_CLOSE  # noqa: E402

# 2026-07-27 (2차 GPT 코드리뷰 지적): 위 MARKET_CLOSE(15:20)는
# "프로그램이 신규 매수/매도를 중단하는 전략 거래창 종료 시각"이지
# 실제 한국거래소 정규장 마감 시각(15:30)이 아님 — 두 의미를
# outside_session_count 하나로 뭉뚱그리면, "봉이 전략 거래창 밖"인
# 것과 "봉이 아예 정규장 밖(거래소 자체가 안 열린 시간)"인 것을
# 구분할 수 없었음. 프로젝트에 15:30을 나타내는 기존 상수가 없어서
# (utils/time_utils.py, config/settings.yaml 전체 검색 결과 없음
# 확인) 진단 전용 상수로 새로 정의 — 한국거래소(KRX) 정규장은
# 09:00~15:30이 공식 운영시간(동시호가 마감 포함)이라는 일반적으로
# 알려진 기준을 근거로 함. 이 상수는 매매 로직에 전혀 쓰이지 않고
# 오직 진단 목적으로만 사용됨.
REGULAR_MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class MinuteBarDiagnostics:
    """get_minute_bars() 한 번 호출에 대한 raw 응답 진단 결과."""

    symbol: str
    base_date: str          # YYYYMMDD, API 요청에 실제로 사용한 값
    tick_scope: str
    requested_count: int

    request_started_at: datetime | None   # KST, timezone-aware
    response_received_at: datetime | None  # KST, timezone-aware

    raw_received_count: int
    raw_timestamp_parseable_count: int   # raw 전체 중 cntr_tm 파싱 가능한 개수
    returned_parsed_count: int           # count 제한 + 기존 파싱 규칙 통과한 실제 반환 개수

    # 2026-07-27 (실운영 첫 로그 검토로 추가, GPT 코드리뷰): 기존엔
    # raw_received_count와 requested_count를 나란히 로그에 찍기만
    # 하고 둘을 비교하는 필드가 없어서, 실제로 raw_received=63,
    # requested=60처럼 API가 요청보다 많이 준 상황을 로그를 눈으로
    # 계산해야만 알 수 있었음. 이 비교를 명시적인 필드로 승격.
    raw_excess_count: int                # raw_received_count - requested_count (음수면 부족)
    raw_received_exceeds_requested: bool  # raw_excess_count > 0

    oldest_raw_timestamp: str | None
    newest_raw_timestamp: str | None
    raw_sort_direction: str              # "ASC" | "DESC" | "UNKNOWN" | "N/A"
    # 2026-07-27 (실운영 첫 로그 검토로 추가, GPT 코드리뷰): raw_sort_
    # direction이 UNKNOWN일 때 "정확히 어디서 몇 번 순서가 깨졌는지"를
    # 사람이 원본 API를 다시 조회하지 않고도 알 수 있도록 하는 필드들.
    # 인접 쌍 검사에서 "증가/감소가 아닌" 경우의 개수와, 원본 순서
    # 그대로의 앞/뒤 일부 timestamp를 남김(원문 전체를 남기면 로그가
    # 너무 커지므로 앞뒤 5개씩만).
    raw_order_violation_count: int       # 인접 쌍 중 정렬 방향에 안 맞는 전이 개수
    raw_order_head_sample: list[str]     # raw 순서 그대로 앞 5개 timestamp
    raw_order_tail_sample: list[str]     # raw 순서 그대로 뒤 5개 timestamp

    returned_oldest_timestamp: str | None
    returned_newest_timestamp: str | None
    returned_sort_direction: str

    continuation_available: bool         # cont-yn == "Y" (원문 미저장)
    next_key_present: bool               # next-key 비어있지 않음 (원문 미저장)

    other_date_count: int                # base_date와 다른 날짜의 봉 개수 (raw 전체 기준)
    # 2026-07-27 (2차 GPT 코드리뷰 지적): 기존 regular_session_
    # outside_count 하나였던 걸, "전략 거래창(09:00~15:20) 밖"과
    # "실제 정규장(09:00~15:30) 밖"으로 명확히 분리. 전자는 프로그램
    # 관점(신규매수/매도 중단 시각), 후자는 거래소 관점(정규장 자체).
    outside_strategy_window_count: int   # MARKET_OPEN~MARKET_CLOSE(15:20) 밖 봉 개수
    outside_regular_market_count: int    # MARKET_OPEN~REGULAR_MARKET_CLOSE(15:30) 밖 봉 개수
    duplicate_timestamp_count: int       # raw 전체에서 중복된 timestamp 개수
    invalid_timestamp_count: int         # cntr_tm 파싱 실패 개수 (raw_received_count - raw_timestamp_parseable_count와 동일할 수 있음)

    newest_raw_bar_age_seconds: float | None       # response_received_at 기준
    newest_raw_bar_same_minute_as_response: bool | None
    newest_raw_bar_is_future: bool | None          # age_seconds < 0


def _parse_bar_timestamp(cntr_tm: str) -> datetime | None:
    """'YYYYMMDDHHMMSS' 형식 문자열을 KST timezone-aware datetime으로 파싱합니다.

    실패하면 None을 반환합니다(예외를 던지지 않음 — 이 함수는 진단
    목적이라 하나의 잘못된 timestamp 때문에 전체 진단이 죽으면 안 됨).
    """
    if not cntr_tm or len(cntr_tm) != 14 or not cntr_tm.isdigit():
        return None
    try:
        return datetime.strptime(cntr_tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


def _infer_sort_direction(parsed_pairs: list[tuple[str, datetime | None]]) -> str:
    """(원본 timestamp, 파싱된 datetime) 쌍 리스트에서 정렬 방향을 추정합니다.

    2026-07-27 (2차 GPT 코드리뷰 지적, 재현 확인): 기존엔 이 함수와
    _count_order_violations()가 각각 독립적으로 "14자리 숫자인가"
    라는 얕은 형식 검사만 하고, _parse_bar_timestamp()가 실제로
    파싱에 성공하는지(예: "20260230120000"처럼 2월 30일 같은 존재
    하지 않는 날짜)는 확인하지 않았음 — 형식은 14자리 숫자로
    맞지만 실제로는 유효하지 않은 날짜가 문자열 비교에 그대로
    섞여 들어가 잘못된 정렬 판정을 내리는 것을 재현 확인. 이제
    _parse_bar_timestamp()가 실제로 성공한(None이 아닌) 원소만,
    원래 순서 그대로 필터링해서 사용 — 파싱 자체를 이 함수가
    다시 하지 않고 호출부가 이미 계산해둔 결과를 재사용해 두 함수
    간 판단 기준이 어긋나지 않도록 함.
    """
    valid = [ts for ts, dt in parsed_pairs if dt is not None]
    if len(valid) < 2:
        return "N/A"
    if all(a <= b for a, b in zip(valid, valid[1:])):
        return "ASC"
    if all(a >= b for a, b in zip(valid, valid[1:])):
        return "DESC"
    return "UNKNOWN"


def _count_order_violations(parsed_pairs: list[tuple[str, datetime | None]], direction: str) -> int:
    """주어진 방향(ASC/DESC) 기준으로 어긋나는 인접 쌍의 개수를 셉니다.

    2026-07-27 (실운영 첫 로그 검토로 추가): raw_sort_direction=
    UNKNOWN이 나왔을 때 "얼마나 심하게" 어긋났는지 정량화하기 위함
    — 위반이 1건뿐이면 API 응답의 사소한 흔들림일 수 있고, 위반이
    수십 건이면 애초에 정렬 자체가 안 되어 있다는 뜻이라 원인이
    다름. 기준 방향은 전체 다수결(더 많이 만족하는 쪽)로 정함.

    2026-07-27 (2차 GPT 코드리뷰 지적): _infer_sort_direction()과
    동일하게, 파싱 성공한(dt is not None) 원소만 사용하도록 수정.
    """
    valid = [ts for ts, dt in parsed_pairs if dt is not None]
    if len(valid) < 2:
        return 0
    if direction not in ("ASC", "DESC"):
        # UNKNOWN이면 다수결로 기준 방향을 정해 위반 개수를 계산
        asc_ok = sum(1 for a, b in zip(valid, valid[1:]) if a <= b)
        desc_ok = sum(1 for a, b in zip(valid, valid[1:]) if a >= b)
        direction = "ASC" if asc_ok >= desc_ok else "DESC"
    if direction == "ASC":
        return sum(1 for a, b in zip(valid, valid[1:]) if a > b)
    return sum(1 for a, b in zip(valid, valid[1:]) if a < b)


def build_minute_bar_diagnostics(
    *,
    symbol: str,
    base_date: str,
    tick_scope: str,
    requested_count: int,
    raw_bars: list[dict[str, Any]],
    returned_bars_timestamps: list[str],
    headers: dict[str, str],
    request_started_at: datetime | None,
    response_received_at: datetime | None,
) -> MinuteBarDiagnostics:
    """raw API 응답과 실제 반환된 분봉으로부터 진단 결과를 계산합니다.

    순수 함수 — 로그를 남기지 않고, 예외를 최대한 던지지 않습니다.
    (호출부에서 이 함수 자체를 try/except로 감싸 fail-open 처리하는
    것과 별개로, 이 함수 내부도 개별 항목 계산 실패가 전체를 막지
    않도록 방어적으로 작성됨.)

    Args:
        raw_bars: API가 실제로 반환한 원본 배열(파싱 전, count로
            자르기 전 — kiwoom_broker.py의 raw_bars 그대로).
        returned_bars_timestamps: 기존 로직(raw_bars[:count] 파싱 +
            reverse())을 거쳐 실제로 반환되는 MinuteBar 리스트의
            cntr_tm만 뽑은 것 — 이 함수가 그 파싱을 다시 하지 않고
            호출부가 이미 만든 결과를 전달받음(byte-for-byte 동일성
            보장을 위해 이 함수가 별도로 파싱 로직을 재구현하지 않음).
    """
    raw_timestamps_all = [str(item.get("cntr_tm", "")) for item in raw_bars]
    parsed_dt_pairs = [(ts, _parse_bar_timestamp(ts)) for ts in raw_timestamps_all]
    valid_raw_timestamps = [ts for ts, dt in parsed_dt_pairs if dt is not None]
    invalid_count = len(raw_timestamps_all) - len(valid_raw_timestamps)

    raw_excess_count = len(raw_bars) - requested_count

    oldest_raw = min(valid_raw_timestamps) if valid_raw_timestamps else None
    newest_raw = max(valid_raw_timestamps) if valid_raw_timestamps else None

    # 2026-07-27 (2차 GPT 코드리뷰 지적): raw 전체 순서 그대로의
    # 정렬 방향을 판단할 때, 이미 위에서 계산해둔 parsed_dt_pairs
    # (실제 파싱 성공 여부까지 반영된 결과)를 그대로 재사용 —
    # _infer_sort_direction()/_count_order_violations()가 별도로
    # "14자리 숫자인가"라는 얕은 검사를 다시 하지 않도록 함(중복
    # 파싱을 피하고, 두 곳의 유효성 판단 기준이 어긋나지 않도록).
    raw_sort_direction = _infer_sort_direction(parsed_dt_pairs)
    raw_order_violation_count = _count_order_violations(parsed_dt_pairs, raw_sort_direction)
    # head/tail 샘플도 파싱 성공한 timestamp만, 원본 순서 그대로 사용
    valid_raw_timestamps_in_order = [ts for ts, dt in parsed_dt_pairs if dt is not None]
    raw_order_head_sample = valid_raw_timestamps_in_order[:5]
    raw_order_tail_sample = valid_raw_timestamps_in_order[-5:] if valid_raw_timestamps_in_order else []

    returned_parsed_pairs = [(ts, _parse_bar_timestamp(ts)) for ts in returned_bars_timestamps]
    returned_valid_timestamps = [ts for ts, dt in returned_parsed_pairs if dt is not None]
    returned_oldest = min(returned_valid_timestamps) if returned_valid_timestamps else None
    returned_newest = max(returned_valid_timestamps) if returned_valid_timestamps else None
    returned_sort_direction = _infer_sort_direction(returned_parsed_pairs)

    other_date_count = sum(
        1 for ts in valid_raw_timestamps if len(base_date) == 8 and ts[:8] != base_date
    )

    def _outside_window(ts: str, window_close: time) -> bool:
        dt = _parse_bar_timestamp(ts)
        if dt is None:
            return False  # 파싱 실패는 "밖" 통계에 포함하지 않음(별도 invalid로 집계)
        t = dt.time()
        return not (MARKET_OPEN <= t <= window_close)

    outside_strategy_window_count = sum(
        1 for ts in valid_raw_timestamps if _outside_window(ts, MARKET_CLOSE)
    )
    outside_regular_market_count = sum(
        1 for ts in valid_raw_timestamps if _outside_window(ts, REGULAR_MARKET_CLOSE)
    )

    duplicate_timestamp_count = len(valid_raw_timestamps) - len(set(valid_raw_timestamps))

    continuation_available = headers.get("cont-yn", "").strip().upper() == "Y"
    next_key_present = bool(headers.get("next-key", "").strip())

    newest_raw_dt = _parse_bar_timestamp(newest_raw) if newest_raw else None
    newest_raw_bar_age_seconds: float | None = None
    newest_raw_bar_same_minute_as_response: bool | None = None
    newest_raw_bar_is_future: bool | None = None
    if newest_raw_dt is not None and response_received_at is not None:
        try:
            delta = (response_received_at - newest_raw_dt).total_seconds()
            newest_raw_bar_age_seconds = delta
            newest_raw_bar_is_future = delta < 0
            newest_raw_bar_same_minute_as_response = (
                newest_raw_dt.replace(second=0, microsecond=0)
                == response_received_at.replace(second=0, microsecond=0)
            )
        except (TypeError, ValueError):
            pass  # 시간대 불일치 등 예외 상황 — 진단값은 None으로 남김

    return MinuteBarDiagnostics(
        symbol=symbol,
        base_date=base_date,
        tick_scope=tick_scope,
        requested_count=requested_count,
        request_started_at=request_started_at,
        response_received_at=response_received_at,
        raw_received_count=len(raw_bars),
        raw_timestamp_parseable_count=len(valid_raw_timestamps),
        returned_parsed_count=len(returned_bars_timestamps),
        raw_excess_count=raw_excess_count,
        raw_received_exceeds_requested=raw_excess_count > 0,
        oldest_raw_timestamp=oldest_raw,
        newest_raw_timestamp=newest_raw,
        raw_sort_direction=raw_sort_direction,
        raw_order_violation_count=raw_order_violation_count,
        raw_order_head_sample=raw_order_head_sample,
        raw_order_tail_sample=raw_order_tail_sample,
        returned_oldest_timestamp=returned_oldest,
        returned_newest_timestamp=returned_newest,
        returned_sort_direction=returned_sort_direction,
        continuation_available=continuation_available,
        next_key_present=next_key_present,
        other_date_count=other_date_count,
        outside_strategy_window_count=outside_strategy_window_count,
        outside_regular_market_count=outside_regular_market_count,
        duplicate_timestamp_count=duplicate_timestamp_count,
        invalid_timestamp_count=invalid_count,
        newest_raw_bar_age_seconds=newest_raw_bar_age_seconds,
        newest_raw_bar_same_minute_as_response=newest_raw_bar_same_minute_as_response,
        newest_raw_bar_is_future=newest_raw_bar_is_future,
    )


def _format_iso_or_na(dt: datetime | None) -> str:
    """datetime을 ISO 8601(+09:00 형태)로 포맷하거나, 없으면 'N/A'를 반환합니다."""
    if dt is None:
        return "N/A"
    try:
        return dt.isoformat()
    except (TypeError, ValueError):
        return "N/A"


def format_diagnostics_log_line(d: MinuteBarDiagnostics) -> str:
    """진단 결과를 한 줄 로그 문자열로 포맷합니다 (원문 민감정보 미포함)."""
    age = f"{d.newest_raw_bar_age_seconds:.1f}" if d.newest_raw_bar_age_seconds is not None else "N/A"

    # 2026-07-27 (2차 GPT 코드리뷰 지적): 요청/응답 시각, 소요시간,
    # 반환 분봉의 최고/최신 timestamp가 로그에 전혀 없었음 — 값이
    # 있으면 ISO 8601(+09:00) 형태로, 없으면 'N/A'로 명시.
    request_str = _format_iso_or_na(d.request_started_at)
    response_str = _format_iso_or_na(d.response_received_at)
    duration_ms = "N/A"
    if d.request_started_at is not None and d.response_received_at is not None:
        try:
            duration_ms = f"{(d.response_received_at - d.request_started_at).total_seconds() * 1000:.1f}"
        except (TypeError, ValueError):
            pass

    return (
        f"[MIN_BOOTSTRAP] symbol={d.symbol} date={d.base_date} tick_scope={d.tick_scope} "
        f"requested={d.requested_count} raw_received={d.raw_received_count} "
        f"raw_excess={d.raw_excess_count} raw_exceeds_requested={d.raw_received_exceeds_requested} "
        f"raw_parseable={d.raw_timestamp_parseable_count} returned={d.returned_parsed_count} "
        f"request_started_at={request_str} response_received_at={response_str} "
        f"request_duration_ms={duration_ms} "
        f"oldest_raw={d.oldest_raw_timestamp} newest_raw={d.newest_raw_timestamp} "
        f"returned_oldest={d.returned_oldest_timestamp} returned_newest={d.returned_newest_timestamp} "
        f"raw_order={d.raw_sort_direction} raw_order_violations={d.raw_order_violation_count} "
        f"returned_order={d.returned_sort_direction} "
        f"continuation={d.continuation_available} next_key_present={d.next_key_present} "
        f"other_date={d.other_date_count} "
        f"outside_strategy_window={d.outside_strategy_window_count} "
        f"outside_regular_market={d.outside_regular_market_count} "
        f"duplicates={d.duplicate_timestamp_count} invalid_ts={d.invalid_timestamp_count} "
        f"newest_bar_age_sec={age} "
        f"same_minute_as_response={d.newest_raw_bar_same_minute_as_response} "
        f"is_future={d.newest_raw_bar_is_future}"
    )


def format_order_detail_log_line(d: MinuteBarDiagnostics) -> str | None:
    """raw_sort_direction이 UNKNOWN(비단조)일 때만 순서 상세를 보여주는 2차 로그.

    2026-07-27 (실운영 첫 로그 검토로 추가, GPT 코드리뷰): 주 로그
    한 줄에 원본 순서 전체를 넣으면 매번 로그가 너무 길어지므로,
    "정렬 방향이 확실할 때"(ASC/DESC)는 이 로그를 생략하고
    UNKNOWN일 때만 앞/뒤 일부 timestamp를 보여줘 원인 조사를 돕는다.
    정상(ASC/DESC/N/A)이면 None을 반환 — 호출부가 None이면 로그를
    남기지 않는다.
    """
    if d.raw_sort_direction != "UNKNOWN":
        return None
    return (
        f"[MIN_BOOTSTRAP_ORDER_DETAIL] symbol={d.symbol} date={d.base_date} "
        f"violations={d.raw_order_violation_count}/{max(d.raw_received_count - 1, 0)} "
        f"head={d.raw_order_head_sample} tail={d.raw_order_tail_sample}"
    )
