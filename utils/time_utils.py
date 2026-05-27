from __future__ import annotations

"""장 시간 관련 유틸리티 모음.

첫 버전은 매우 단순하게 만들었습니다.
실전 단계에서는 공휴일/반장/특수 시장 시간까지 정교화해야 합니다.
"""

from datetime import datetime, time, timedelta


# 국장 기본 장중 시간(단순 버전)
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 20)  # 15:20 이후 단일가 — 신규 매수/매도 중단


def now_local() -> datetime:
    """현재 로컬 시간을 반환합니다."""

    return datetime.now()


def is_market_open() -> bool:
    """현재 시간이 장중인지 아주 단순하게 판단합니다."""

    current = now_local().time()
    return MARKET_OPEN <= current <= MARKET_CLOSE


def seconds_until_market_open() -> float:
    """장 시작(09:00)까지 남은 초를 반환합니다.

    장이 이미 열렸거나 지났으면 0을 반환합니다.
    """
    current = now_local()
    open_dt = current.replace(
        hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
        second=0, microsecond=0
    )
    remaining = (open_dt - current).total_seconds()
    return max(remaining, 0.0)


def is_near_market_close(minutes_before_close: int) -> bool:
    """장 마감 직전 강제 청산 시점을 판단합니다."""

    current = now_local()
    close_dt = current.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)
    threshold = close_dt - timedelta(minutes=minutes_before_close)
    return threshold <= current <= close_dt
