from __future__ import annotations

"""signal_log.csv에 기록되는 skip_reason 표준 상수 모음.

전략 reason 문자열을 그대로 기록하면 pandas group by 분석이 어렵습니다.
이 모듈의 상수를 사용하면 나중에 아래와 같은 분석이 가능합니다.

    df[df.skip_reason == SkipReason.BELOW_VWAP]   → VWAP 조건 탈락 종목 추적
    df.skip_reason.value_counts()                 → 탈락 사유 분포 확인
"""


class SkipReason:
    # ── 분봉 조건 미충족 ─────────────────────────────────────────
    NO_MINUTE_DATA          = "SKIP_NO_MINUTE_DATA"       # 분봉 데이터 없음
    NO_INDICATORS           = "SKIP_NO_INDICATORS"        # 일봉 지표 없음
    NO_VOLUME               = "SKIP_NO_VOLUME"            # 거래대금 부족
    NO_PATTERN              = "SKIP_NO_PATTERN"           # A/B/C/V/PR 모두 미충족
    BELOW_VWAP              = "SKIP_BELOW_VWAP"           # VWAP 아래
    SCORE_TOO_LOW           = "SKIP_SCORE_TOO_LOW"        # 점수 부족
    TOO_MUCH_REBOUND        = "SKIP_TOO_MUCH_REBOUND"     # 반등폭 상한 초과 (추격매수)
    PULLBACK_OUT_OF_RANGE   = "SKIP_PULLBACK_OUT_OF_RANGE" # 눌림목 범위 이탈

    # ── 장세 조건 미충족 ─────────────────────────────────────────
    MARKET_NOT_ALLOWED      = "SKIP_MARKET_NOT_ALLOWED"   # 장세가 매수를 허용하지 않음
    UNKNOWN_REGIME          = "SKIP_UNKNOWN_REGIME"       # 장세 판단 불가

    # ── 리스크/운영 제한 ─────────────────────────────────────────
    ALREADY_HOLDING         = "SKIP_ALREADY_HOLDING"      # 이미 보유 중
    MAX_POSITIONS           = "SKIP_MAX_POSITIONS"        # 최대 보유 종목 수 초과
    COOLDOWN                = "SKIP_COOLDOWN"             # 재진입 쿨다운 중
    RISK_LIMIT              = "SKIP_RISK_LIMIT"           # RiskManager 차단
    DAILY_LOSS_LIMIT        = "SKIP_DAILY_LOSS_LIMIT"     # 일일 손실 한도 도달
    CONSECUTIVE_LOSS_LIMIT  = "SKIP_CONSECUTIVE_LOSS"     # 연속 손절 한도 도달
    ORDER_FAILED            = "SKIP_ORDER_FAILED"         # 주문 실패 (브로커 거부)

    # ── BUY 신호이지만 체결 안 된 경우 ──────────────────────────
    BUY_SIGNAL              = "BUY"                       # 매수 체결


def classify_skip_reason(signal_reason: str, signal_type_value: str) -> str:
    """전략 reason 문자열을 표준 skip_reason 상수로 분류합니다.

    분류가 불가능하면 원본 문자열 앞 60자를 그대로 반환합니다.
    """
    if signal_type_value == "BUY":
        return SkipReason.BUY_SIGNAL

    r = signal_reason

    # 분봉 데이터 / 지표 없음
    if "분봉 데이터 없음" in r or "분봉" in r and "없음" in r:
        return SkipReason.NO_MINUTE_DATA
    if "지표 없음" in r or "일봉 데이터 대기" in r:
        return SkipReason.NO_INDICATORS

    # 거래대금 부족
    if "거래대금 부족" in r:
        return SkipReason.NO_VOLUME

    # 패턴 미충족
    if "진입 조건 미충족" in r or "B/C 조건 미충족" in r or "B/C/V/PR" in r:
        return SkipReason.NO_PATTERN

    # 점수 부족
    if "점수 부족" in r or "타이밍 대기" in r:
        return SkipReason.SCORE_TOO_LOW

    # 눌림목 범위 이탈
    if "눌림목" in r and ("범위" in r or "유효범위" in r or "너무 밀림" in r):
        return SkipReason.PULLBACK_OUT_OF_RANGE

    # 추격매수 방지
    if "추격" in r or "상승 초과" in r:
        return SkipReason.TOO_MUCH_REBOUND

    # 장세
    if "횡보장" in r or "하락장" in r or "UNKNOWN" in r:
        return SkipReason.MARKET_NOT_ALLOWED

    # fallback: 원본 앞 60자
    return r[:60]
