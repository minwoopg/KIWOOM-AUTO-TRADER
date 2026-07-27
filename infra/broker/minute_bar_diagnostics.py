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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# 2026-07-27 (GPT 코드리뷰 지시 7번): 정규장 시간 기준을 임의로
# 새로 정하지 않고, 기존 프로젝트에 이미 있던 상수를 재사용.
# utils/time_utils.py의 MARKET_OPEN=09:00, MARKET_CLOSE=15:20을
# 그대로 가져옴 — 다만 이 값은 "신규 매수/매도 중단 시각"(15:20)
# 이지 실제 정규장 마감(통상 15:30)과는 다른 의미라는 점에 주의.
# 진단 목적(장중 정규 시간대 밖 봉이 섞였는지 관찰)에는 기존 값을
# 그대로 쓰는 게 프로젝트 전체의 시간 기준과 일관성이 있어 안전함.
from utils.time_utils import MARKET_OPEN, MARKET_CLOSE  # noqa: E402


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

    oldest_raw_timestamp: str | None
    newest_raw_timestamp: str | None
    raw_sort_direction: str              # "ASC" | "DESC" | "UNKNOWN" | "N/A"

    returned_oldest_timestamp: str | None
    returned_newest_timestamp: str | None
    returned_sort_direction: str

    continuation_available: bool         # cont-yn == "Y" (원문 미저장)
    next_key_present: bool               # next-key 비어있지 않음 (원문 미저장)

    other_date_count: int                # base_date와 다른 날짜의 봉 개수 (raw 전체 기준)
    regular_session_outside_count: int   # 정규장 시간(MARKET_OPEN~MARKET_CLOSE) 밖 봉 개수
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


def _infer_sort_direction(timestamps: list[str]) -> str:
    """timestamp 리스트(파싱 가능한 것만)의 정렬 방향을 추정합니다."""
    valid = [ts for ts in timestamps if ts and len(ts) == 14 and ts.isdigit()]
    if len(valid) < 2:
        return "N/A"
    if all(a <= b for a, b in zip(valid, valid[1:])):
        return "ASC"
    if all(a >= b for a, b in zip(valid, valid[1:])):
        return "DESC"
    return "UNKNOWN"


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

    oldest_raw = min(valid_raw_timestamps) if valid_raw_timestamps else None
    newest_raw = max(valid_raw_timestamps) if valid_raw_timestamps else None

    # raw 전체 순서 그대로의 정렬 방향(min/max가 아니라 원래 순서 기준)
    raw_order_timestamps = [ts for ts in raw_timestamps_all if ts]
    raw_sort_direction = _infer_sort_direction(raw_order_timestamps)

    returned_valid_timestamps = [ts for ts in returned_bars_timestamps if ts]
    returned_oldest = min(returned_valid_timestamps) if returned_valid_timestamps else None
    returned_newest = max(returned_valid_timestamps) if returned_valid_timestamps else None
    returned_sort_direction = _infer_sort_direction(returned_bars_timestamps)

    other_date_count = sum(
        1 for ts in valid_raw_timestamps if len(base_date) == 8 and ts[:8] != base_date
    )

    def _in_regular_session(ts: str) -> bool:
        dt = _parse_bar_timestamp(ts)
        if dt is None:
            return True  # 파싱 실패는 "정규장 밖" 통계에 포함하지 않음(별도 invalid로 집계)
        t = dt.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    regular_session_outside_count = sum(
        1 for ts in valid_raw_timestamps if not _in_regular_session(ts)
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
        oldest_raw_timestamp=oldest_raw,
        newest_raw_timestamp=newest_raw,
        raw_sort_direction=raw_sort_direction,
        returned_oldest_timestamp=returned_oldest,
        returned_newest_timestamp=returned_newest,
        returned_sort_direction=returned_sort_direction,
        continuation_available=continuation_available,
        next_key_present=next_key_present,
        other_date_count=other_date_count,
        regular_session_outside_count=regular_session_outside_count,
        duplicate_timestamp_count=duplicate_timestamp_count,
        invalid_timestamp_count=invalid_count,
        newest_raw_bar_age_seconds=newest_raw_bar_age_seconds,
        newest_raw_bar_same_minute_as_response=newest_raw_bar_same_minute_as_response,
        newest_raw_bar_is_future=newest_raw_bar_is_future,
    )


def format_diagnostics_log_line(d: MinuteBarDiagnostics) -> str:
    """진단 결과를 한 줄 로그 문자열로 포맷합니다 (원문 민감정보 미포함)."""
    age = f"{d.newest_raw_bar_age_seconds:.1f}" if d.newest_raw_bar_age_seconds is not None else "N/A"
    return (
        f"[MIN_BOOTSTRAP] symbol={d.symbol} date={d.base_date} tick_scope={d.tick_scope} "
        f"requested={d.requested_count} raw_received={d.raw_received_count} "
        f"raw_parseable={d.raw_timestamp_parseable_count} returned={d.returned_parsed_count} "
        f"oldest_raw={d.oldest_raw_timestamp} newest_raw={d.newest_raw_timestamp} "
        f"raw_order={d.raw_sort_direction} returned_order={d.returned_sort_direction} "
        f"continuation={d.continuation_available} next_key_present={d.next_key_present} "
        f"other_date={d.other_date_count} outside_session={d.regular_session_outside_count} "
        f"duplicates={d.duplicate_timestamp_count} invalid_ts={d.invalid_timestamp_count} "
        f"newest_bar_age_sec={age} "
        f"same_minute_as_response={d.newest_raw_bar_same_minute_as_response} "
        f"is_future={d.newest_raw_bar_is_future}"
    )
