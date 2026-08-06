"""entry_quality_shadow 행의 assessment signature (공용 순수 함수).

2026-08-06 (1I.1, GPT 코드리뷰 지적 4번): 이 로직은 원래
`infra/storage/logger.py`의 `_entry_quality_shadow_key()`에만
있었고, 분석 쪽(`analyze_shadow.py`)은 `(symbol,
latest_bar_timestamp)`만으로 중복을 판정했음. 그 결과 **같은
종목·같은 분봉이라도 게이트 상태가 바뀐 정상적인 별도 행**
(예: `would_block_* False→True`, `order_accepted False→True`,
`final_decision BLOCKED→BUY`)까지 "재시작 중복 가능" 경고로
잡혔음.

로거와 분석기가 **같은 함수**를 쓰도록 여기로 추출함 — 복제
구현은 향후 필드가 추가될 때 다시 어긋날 위험이 있음.

의존성 없는 순수 모듈이므로 스크립트·인프라 어느 쪽에서든
import할 수 있음.
"""
from __future__ import annotations

from typing import Any

# 중복 방지 키에 포함될 "게이트 상태" 필드 — 이 값들 중 하나라도
# 이전 기록과 다르면 새 행을 남김. MACD 2개 + VWAP 8개(rolling 4 +
# session 4) + final_decision + order_block_reason = 12개.
ASSESSMENT_SIGNATURE_FIELDS: list[str] = [
    "would_block_macd_dead_min_score5",
    "would_block_macd_above_signal_required",
    "would_block_pr_only_rolling_vwap",
    "would_block_c_or_pr_rolling_vwap",
    "would_block_pullback_condition_rolling_vwap",
    "would_block_pr_or_pullback_condition_rolling_vwap",
    "would_block_pr_only_session_vwap",
    "would_block_c_or_pr_session_vwap",
    "would_block_pullback_condition_session_vwap",
    "would_block_pr_or_pullback_condition_session_vwap",
    "final_decision",
    "order_block_reason",
]

# 2026-08-06 (1I.2, GPT 코드리뷰 P0-1, 재현 확인): 상태 전이 필드.
# 1I.1에서는 이 필드들을 분석기 전용(ANALYSIS_EXTRA_FIELDS)으로만
# 뒀는데, **로거가 먼저 행을 버리기 때문에** 분석기가 아무리 정확한
# signature를 써도 원본 CSV에 데이터 자체가 없었음.
#
# 재현 (수정 전):
#   1) order_accepted=False 기록 → True
#   2) order_accepted=True  기록 → False (중복으로 오판)
#   3) reliable=False       기록 → False (중복으로 오판)
#   최종 CSV 행 수: 1행
#
# 브로커 거부 후 수락(reject→accept)이나 조건식 출처 신뢰도 변화는
# **분석에 반드시 필요한 상태 전이**이므로 로거 키에도 포함한다.
#
# order_id는 일부러 넣지 않는다 — 같은 판단에서 주문을 재시도할
# 때마다 별도 행이 생겨 표본이 부풀려지기 때문.
STATE_TRANSITION_FIELDS: list[str] = [
    "order_attempted",
    "order_accepted",
    "condition_source_reliable",
]

_BASE_FIELDS: list[str] = [
    "symbol",
    "latest_bar_timestamp",
    "detected_patterns",
    "score",
]


def _norm(row: dict[str, Any], field: str) -> str:
    """값을 문자열로 정규화합니다.

    2026-08-05 (재현 확인): 새로 들어오는 row는 Python 값
    (bool True/False 등)이지만, 재시작 시 CSV에서 복원하는 row는
    전부 문자열("True"/"False")임 — 그대로 튜플 키로 쓰면 같은
    논리적 값인데 타입이 달라 키가 일치하지 않는 문제가 재현됨.
    모든 값을 str()로 정규화해 타입 불일치를 원천 차단.
    """
    return str(row.get(field, ""))


def assessment_signature(row: dict[str, Any]) -> tuple:
    """게이트 상태·최종 결정만으로 구성된 signature."""
    return tuple(_norm(row, f) for f in ASSESSMENT_SIGNATURE_FIELDS)


def entry_quality_shadow_key(row: dict[str, Any]) -> tuple:
    """중복 방지 키 — 로거와 분석기가 **동일하게** 사용합니다.

    (base) + (assessment signature) + (state transition)

    2026-08-06 (1I.2): 로거와 분석기가 서로 다른 키를 쓰면 로거가
    버린 행을 분석기가 볼 방법이 없으므로, 두 키를 하나로 통일함.
    """
    return (
        tuple(_norm(row, f) for f in _BASE_FIELDS)
        + (assessment_signature(row),)
        + tuple(_norm(row, f) for f in STATE_TRANSITION_FIELDS)
    )


def analysis_dedup_key(row: dict[str, Any]) -> tuple:
    """분석기의 중복 판정 키 — 로거 키와 동일합니다.

    이 키까지 전부 같으면 진짜 중복(또는 재시작 중복)입니다.
    """
    return entry_quality_shadow_key(row)
