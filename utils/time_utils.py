from __future__ import annotations

"""장 시간 관련 유틸리티 모음.

첫 버전은 매우 단순하게 만들었습니다.
실전 단계에서는 공휴일/반장/특수 시장 시간까지 정교화해야 합니다.
"""

from datetime import datetime, time, timedelta, timezone


# 국장 기본 장중 시간(단순 버전)
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 20)  # 15:20 이후 단일가 — 신규 매수/매도 중단

# 2026-07-28 (1C단계, GPT 코드리뷰 지적): 세션 지표(SessionMetrics)
# 가 "당일 정규장 09:00~15:30" 봉만 세션에 포함시켜야 하는데, 위
# MARKET_CLOSE(15:20)는 "프로그램이 신규 매수/매도를 중단하는 전략
# 거래창 종료 시각"이지 실제 한국거래소 정규장 마감(15:30)이 아님
# — 애초에 infra/broker/minute_bar_diagnostics.py(1B단계 진단
# 목적)에 같은 상수가 있었는데, domain 레이어(session_metrics.py)
# 가 infra 레이어를 import하면 계층 방향이 역전되므로 공통 위치인
# 여기로 옮김. 한국거래소(KRX) 정규장은 09:00~15:30이 공식 운영
# 시간(동시호가 마감 포함)이라는 일반적으로 알려진 기준 — 매매
# 로직에는 쓰이지 않고 오직 진단/세션 필터링 목적으로만 사용됨.
REGULAR_MARKET_CLOSE = time(15, 30)

# 2026-07-28 (GPT 코드리뷰 지적, stale 분봉 안전장치 3단계): 키움
# API의 분봉 timestamp(cntr_tm)는 KST 기준인데, 기존 코드는
# datetime.now()(타임존 미지정, 서버의 시스템 로컬시각)와 naive하게
# 비교하고 있었음 — UTC 서버 환경에서 실제로 재현하면 KST 09:20
# 봉이 UTC 00:20 시각과 비교되어 age가 -32400초(-9시간)로 계산되는데
# 도 entry_safe=True로 판정되는 심각한 버그가 있었음. 한국 표준시는
# 서머타임이 없는 고정 UTC+9라 IANA 시간대 데이터베이스(zoneinfo/
# tzdata) 없이도 정확히 계산 가능 — infra/broker/minute_bar_
# diagnostics.py에서 이미 같은 방식으로 tzdata 의존성 문제를 해결한
# 바 있음(1B.2절), 그 방식을 그대로 재사용.
#
# 기존 now_local()/MARKET_OPEN/MARKET_CLOSE(naive datetime 기반)는
# 이번 라운드에서 건드리지 않음 — 이 값들을 쓰는 다른 코드 경로
# (14:50 시간 게이트 등)까지 한 번에 바꾸면 검증 범위가 지나치게
# 커짐. 이번엔 분봉 신선도 판정 전용으로 KST_TZ와 now_kst()만 신설.
KST_TZ = timezone(timedelta(hours=9), name="Asia/Seoul")


def now_kst() -> datetime:
    """timezone-aware KST 현재 시각을 반환합니다.

    시스템 로컬시각이 UTC든 KST든 관계없이 항상 정확한 KST 시각을
    반환합니다 — datetime.now(KST_TZ)는 시스템이 어떤 타임존으로
    설정되어 있어도 내부적으로 UTC 기준시를 KST로 변환하므로 안전.
    """
    return datetime.now(KST_TZ)


def parse_kst_bar_timestamp(cntr_tm: str | None) -> datetime | None:
    """분봉의 cntr_tm('YYYYMMDDHHMMSS', 키움 API는 KST 기준)을
    timezone-aware KST datetime으로 파싱합니다.

    실패하면 None을 반환합니다(예외를 던지지 않음).
    """
    if not cntr_tm or len(cntr_tm) != 14 or not cntr_tm.isdigit():
        return None
    try:
        return datetime.strptime(cntr_tm, "%Y%m%d%H%M%S").replace(tzinfo=KST_TZ)
    except ValueError:
        return None


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
