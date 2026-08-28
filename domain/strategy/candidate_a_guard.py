# -*- coding: utf-8 -*-
"""
Candidate A guard — 상승여력 낮음 + 반등거래량 spike 없음 매수 차단
(2026-08-28, Candidate A Production Pilot v1)

배경: Profitability Sprint v1.2/v1.3.1의 forward shadow 관측(low_upside_
shadow.csv, 2026-08-27~)에서 "upside_to_recent_high_pct < 0.50% AND
rebound_volume_spike is False"(Candidate A) 조건이 8/28 true-forward
표본 기준 100% skip precision, 0% 승자 손상(민우님 5일 종합분석,
2026-08-28-sprint-synthesis-and-plan.md 참고)을 보였습니다. 이 모듈은
그 판정을 단일 진실 소스로 만들어, shadow 관측(_write_signal_log의
low_upside_shadow 블록)과 실제 enforce gate(_try_buy())가 반드시
동일한 결과를 내도록 합니다 — 두 호출부가 각자 계산하면 코드가
갈라질(drift) 위험이 있기 때문입니다(민우님 명세 v1, Section 4).

핵심 설계 원칙(민우님 명세 v1 반영):
- predicate는 이 상수/함수 밖에서 절대 다시 계산하지 않습니다. 새
  조건(threshold 조정, score/PR/VWAP/시간대/GapD 예외 등)을 추가하지
  않습니다 — 이번 pilot 범위 밖입니다.
- CANDIDATE_A_UPSIDE_THRESHOLD_PCT는 YAML로 노출하지 않고 코드에
  고정합니다 — 설정 파일 수정만으로 predicate가 조용히 바뀌는 것을
  막기 위함(민우님 명세 v1, Section 1).
- 반환값은 3진(True/False/None)입니다. None은 "판정 불가"를 뜻하며,
  호출부는 이를 절대 False로 추정하지 말고 차단하지 않음(PASS)으로
  취급해야 합니다 — Sprint v1.2 분석 도구의 "결측을 skip 대상으로
  추정하지 않는다" 원칙과 동일합니다.
"""
from __future__ import annotations

from typing import Optional


CANDIDATE_A_UPSIDE_THRESHOLD_PCT = 0.50


def evaluate_candidate_a(minute_analysis) -> Optional[bool]:
    """Candidate A predicate를 평가합니다. 순수 함수 — 어떤 상태도 바꾸지 않습니다.

    predicate: upside_to_recent_high_pct < CANDIDATE_A_UPSIDE_THRESHOLD_PCT
               AND rebound_volume_spike is False

    Args:
        minute_analysis: MinuteAnalysis 또는 None(분석 실패/미수행 시).

    Returns:
        True  — predicate 성립(Candidate A 후보로 판정, 차단 대상).
        False — predicate 불성립(상승여력이 충분하거나 반등거래량
                spike가 관측됨 — 차단 대상 아님).
        None  — 판정 불가(minute_analysis가 없거나 upside_to_recent_
                high_pct를 읽을 수 없거나 rebound_volume_spike가
                True/False가 아닌 경우). 호출부는 반드시 이를
                "차단하지 않음(PASS)"으로 취급해야 하며, False로
                섣불리 추정해서는 안 됩니다.
    """
    if minute_analysis is None:
        return None

    upside_val = getattr(minute_analysis, "upside_to_recent_high_pct", None)
    if upside_val is None:
        return None

    # rebound_volume_spike는 dataclass 필드 자체가 non-Optional bool이라
    # minute_analysis가 None이 아니면 이론상 항상 True/False지만,
    # low_upside_shadow 원 코드와 동일하게 getattr + "is True/is False"
    # 명시 비교로 방어적으로 다룹니다 — 만에 하나 True/False가 아닌
    # 값(None 등)이 들어오면 False로 추정하지 않고 None을 반환합니다.
    rebound_volume_spike_val = getattr(minute_analysis, "rebound_volume_spike", None)
    if rebound_volume_spike_val is not True and rebound_volume_spike_val is not False:
        return None

    return bool(
        upside_val < CANDIDATE_A_UPSIDE_THRESHOLD_PCT
        and rebound_volume_spike_val is False
    )
