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
    # A 조건 세분화
    NO_PAT_A_RATE           = "NO_PAT_A_RATE"             # A: 등락률 범위 벗어남
    # B 조건 세분화 (비활성화 중)
    NO_PAT_B_REBOUND_SMALL  = "NO_PAT_B_REBOUND_SMALL"    # B: 반등폭 부족
    NO_PAT_B_BELOW_VWAP     = "NO_PAT_B_BELOW_VWAP"       # B: VWAP 아래
    # C 조건 세분화
    NO_PAT_C_MA_FAIL        = "NO_PAT_C_MA_FAIL"          # C: MA5>MA20 미충족
    NO_PAT_C_RATE_FAIL      = "NO_PAT_C_RATE_FAIL"        # C: 등락률 범위 벗어남
    NO_PAT_C_VWAP_FAIL      = "NO_PAT_C_VWAP_FAIL"        # C: VWAP 아래
    # PR 조건 세분화
    NO_PAT_PR_MA_FAIL       = "NO_PAT_PR_MA_FAIL"         # PR: MA5>MA20 미충족
    NO_PAT_PR_LOW_FAIL      = "NO_PAT_PR_LOW_FAIL"        # PR: 저점 우상향 미충족
    NO_PAT_PR_VOL_FAIL      = "NO_PAT_PR_VOL_FAIL"        # PR: 거래량 팽창 미충족
    # V 조건 세분화
    NO_PAT_V_FAIL           = "NO_PAT_V_FAIL"             # V: 실패 (세부사유는 V_FAIL 로그)
    NEUTRAL_C_BLOCKED       = "NEUTRAL_C_BLOCKED"          # NEUTRAL: C/B단독 — PR/V 필수 미충족
    SKIP_EXCLUDED_SYMBOL    = "SKIP_EXCLUDED_SYMBOL"       # excluded_symbols 차단
    BELOW_VWAP              = "SKIP_BELOW_VWAP"           # VWAP 아래
    SCORE_TOO_LOW           = "SKIP_SCORE_TOO_LOW"        # 점수 부족
    TOO_MUCH_REBOUND        = "SKIP_TOO_MUCH_REBOUND"     # 반등폭 상한 초과 (추격매수)
    PULLBACK_OUT_OF_RANGE   = "SKIP_PULLBACK_OUT_OF_RANGE" # 눌림목 범위 이탈
    NO_CONFIRMATION_SCORE5  = "SKIP_NO_CONFIRMATION_SCORE5" # 5점이나 확인지표(거래량/V/PR/spike) 없음

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

    # ── 보유 중 포지션 관리 ──────────────────────────────────────
    HOLDING_TRAILING        = "HOLD_TRAILING"             # 트레일링 스탑 추적 중
    HOLDING_POSITION        = "HOLD_POSITION"             # 보유 유지 (손절/익절 대기)
    HOLDING_SELL            = "SELL"                      # 매도 신호

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

    # 패턴 미충족 — 세분화된 사유 먼저 확인
    if "NO_PAT_BELOW_VWAP" in r:
        return "NO_PAT_BELOW_VWAP"
    if "NO_PAT_A_RATE" in r:
        # 등락률 값 포함 (예: NO_PAT_A_RATE(+1.3%))
        import re
        m = re.search(r'NO_PAT_A_RATE\(([^)]+)\)', r)
        return f"NO_PAT_A_RATE({m.group(1)})" if m else "NO_PAT_A_RATE"
    if "NO_PAT_B_REBOUND_SMALL" in r:
        import re
        m = re.search(r'NO_PAT_B_REBOUND_SMALL\(([^)]+)\)', r)
        return f"NO_PAT_B_REBOUND_SMALL({m.group(1)})" if m else "NO_PAT_B_REBOUND_SMALL"
    if "NO_PAT_C_MA_FAIL" in r:
        return "NO_PAT_C_MA_FAIL"
    if "NO_PAT_C_PULLBACK" in r:
        import re
        m = re.search(r'NO_PAT_C_PULLBACK\(([^)]+)\)', r)
        return f"NO_PAT_C_PULLBACK({m.group(1)})" if m else "NO_PAT_C_PULLBACK"
    if "NO_PAT_PR_LOW_FAIL" in r:
        return "NO_PAT_PR_LOW_FAIL"
    if "NO_PAT_PR_VOL_WEAK" in r:
        return "NO_PAT_PR_VOL_WEAK"
    if "NO_PAT_V_FAIL" in r:
        return "NO_PAT_V_FAIL"
    # 기존 패턴 미충족 (세분화 전 호환)
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

    # 상승여력 부족 (2026-07-09)
    if "상승여력부족 차단" in r:
        return "SKIP_LOW_UPSIDE_REQUIRE5"

    # 확인지표 부족 (2026-07-14)
    if "확인지표부족 차단" in r:
        return "SKIP_NO_CONFIRMATION_SCORE5"

    # 장세
    if "횡보장" in r or "하락장" in r or "UNKNOWN" in r:
        return SkipReason.MARKET_NOT_ALLOWED

    # 보유 중 — 트레일링 추적
    if "트레일링 추적 중" in r or "트레일링 스탑" in r:
        return SkipReason.HOLDING_TRAILING

    # 보유 중 — 보유 유지
    if "보유 유지" in r or "손절" in r or "익절" in r or "추세 꺾임" in r:
        return SkipReason.HOLDING_POSITION

    # 눌림목 고가 이탈 (A조건)
    if "고가에서 너무 밀림" in r:
        return SkipReason.PULLBACK_OUT_OF_RANGE

    # fallback: 원본 앞 60자
    return r[:60]
