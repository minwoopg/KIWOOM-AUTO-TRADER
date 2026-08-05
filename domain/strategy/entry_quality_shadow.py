# -*- coding: utf-8 -*-
"""
진입 품질 shadow 평가 — VWAP 거리 기반 가상 게이트 (2026-08-05, 1E.5단계)

배경: 매매 성과 분석(7/30~8/4)에서 VWAP 대비 +2% 초과 진입 3건이
전부 손실 방향이었음을 확인했으나, "PR 조건이 VWAP 기준"이라는
초기 분석은 부정확했음 — 실제로 domain/market_regime/minute_
analyzer.py를 읽어보면:

- is_pulldown_recovery(PR): 저점 우상향 + 거래량 팽창 조건이고
  VWAP 거리와 무관.
- is_valid_pulldown(C): 등락률 범위 + MA5>MA20 + "VWAP 위에
  있는지"(단순 위/아래, 몇 % 위인지는 무관)만 확인.

즉 "VWAP +2% 초과 차단"은 기존 PR/C 계산식을 조정하는 게 아니라
완전히 새로운 진입 품질 게이트다. 이 모듈은 그 게이트가 실제로
발동했다면 어떤 BUY 후보를 막았을지를 순수하게 계산만 하고,
Signal이나 주문 결과에는 절대 개입하지 않는 shadow 평가기다.

핵심 설계 원칙(GPT 코드리뷰 지시):
- rolling VWAP(MinuteAnalyzer가 최근 60분으로 계산)과 session
  VWAP(1C단계, 당일 정규장 전체 기준)을 각각 독립적으로 관측 —
  두 기준의 성과를 직접 비교할 수 있어야 실제로 어느 기준이 더
  안정적인지 데이터로 판단할 수 있음.
- session_metrics_ready=False(장 시작 구간을 못 봐서 세션 값이
  아직 불완전한 상태)이면 session_vwap_distance_pct는 "관찰용"
  으로 기록하되, session 기반 would_block은 반드시 빈 값으로
  남김 — 불완전한 세션 값을 완전한 당일 VWAP처럼 오인해 판단에
  쓰면 안 됨.
- 네 가지 범위(PR-only / C-or-PR / condition-source / PR-or-
  condition-source)를 rolling·session 각각 독립적으로 관측 —
  실제 데이터가 쌓이기 전까지는 어느 범위가 가장 안정적인
  개선인지 알 수 없으므로, 하나로 미리 확정하지 않음.
"""
from __future__ import annotations

from dataclasses import dataclass


VWAP_DISTANCE_THRESHOLD_PCT = 2.0


@dataclass(frozen=True)
class VwapShadowAssessment:
    """VWAP shadow 평가 결과 — 순수 관측치, 실제 판단에는 쓰이지 않음.

    Attributes:
        is_pr: minute_analysis.is_pulldown_recovery 그대로.
        is_c: minute_analysis.is_valid_pulldown 그대로.
        condition_names: 이 종목이 편입된 모든 조건식 이름(튜플).
        is_pullback_condition: condition_names 중 하나라도 "눌림목"을
            포함하는지.
        is_pr_or_pullback_condition: is_pr 또는 is_pullback_condition.

        rolling_vwap: MinuteAnalyzer가 계산한 최근 60분 기준 VWAP.
        rolling_vwap_distance_pct: (현재가-rolling_vwap)/rolling_vwap*100.
        rolling_over_threshold: rolling 거리가 임계값(2.0%)을
            초과하는지 — legacy_buy_candidate와 무관하게 계산되는
            순수 상태값.

        session_vwap: 1C단계 SessionMetrics의 당일 세션 VWAP.
            session_metrics_mode="off"이거나 세션 데이터가 아직
            없으면 None.
        session_vwap_distance_pct: (현재가-session_vwap)/session_vwap*100.
            session_metrics_ready=False여도 관찰용으로는 계산해
            기록 — 다만 아래 session 기반 would_block에는 안 쓰임.
        session_metrics_ready: 세션 값이 장 시작 구간부터 확보돼
            신뢰할 만한 상태인지(1C단계 readiness 기준 그대로).
        session_readiness_reason: 위 판단의 근거 문자열.
        session_gate_eligible: session_vwap이 존재하고 session_
            metrics_ready=True일 때만 True — 이게 True일 때만
            session 기반 would_block_*이 실제로 계산됨.

        would_block_pr_only_rolling_vwap 등 8개 필드: legacy_buy_
            candidate=True(전략이 실제로 BUY를 반환한 경우)일 때만
            계산 — 그 외에는 None(빈 값).

        condition_source_reliable: 이 종목의 condition_names가
            CNSRREQ(정확한 seq 포함)로 확정된 것인지(True), REAL
            (실시간, 출처 불명)로만 알려진 것인지(False). False면
            is_pullback_condition과 그로부터 파생되는 4개 would_
            block_*(*_pullback_condition_*, *_pr_or_pullback_
            condition_*)가 전부 None — PR-only/C-or-PR 4개는
            이 값과 무관하게 항상 정상 계산됨.
    """
    is_pr: bool
    is_c: bool
    condition_names: tuple[str, ...]
    condition_source_reliable: bool
    is_pullback_condition: bool | None
    is_pr_or_pullback_condition: bool

    rolling_vwap: float | None
    rolling_vwap_distance_pct: float | None
    rolling_over_threshold: bool | None

    session_vwap: float | None
    session_vwap_distance_pct: float | None
    session_metrics_ready: bool
    session_readiness_reason: str
    session_gate_eligible: bool

    would_block_pr_only_rolling_vwap: bool | None
    would_block_c_or_pr_rolling_vwap: bool | None
    would_block_pullback_condition_rolling_vwap: bool | None
    would_block_pr_or_pullback_condition_rolling_vwap: bool | None

    would_block_pr_only_session_vwap: bool | None
    would_block_c_or_pr_session_vwap: bool | None
    would_block_pullback_condition_session_vwap: bool | None
    would_block_pr_or_pullback_condition_session_vwap: bool | None


def evaluate_vwap_shadow(
    *,
    legacy_buy_candidate: bool,
    current_price: float,
    minute_analysis,
    condition_names: tuple[str, ...],
    condition_source_reliable: bool,
    session_metrics,
    threshold_pct: float = VWAP_DISTANCE_THRESHOLD_PCT,
) -> VwapShadowAssessment:
    """VWAP shadow 평가를 계산합니다. 순수 함수 — 어떤 상태도 바꾸지 않습니다.

    Args:
        legacy_buy_candidate: 전략이 이번 폴링에서 실제로 BUY를
            반환했는지(entry_watch/stale 데이터 차단 등을 이미
            거친 최종 signal 기준).
        current_price: 현재가.
        minute_analysis: MinuteAnalysis 또는 None(분석 실패/미수행
            시). None이면 is_pr/is_c/rolling_vwap 전부 기본값
            (False/None)으로 반환.
        condition_names: 이 종목이 편입된 모든 조건식 이름(튜플,
            빈 튜플 가능).
        condition_source_reliable: 이 종목의 condition_names가
            CNSRREQ(조건식별 초기 조회, 정확한 seq 포함)로 확정된
            것인지(True), 아니면 REAL(실시간 이벤트, 어느 조건식
            인지 알 수 없음)로만 알려진 것인지(False). False면
            condition-source 기반 판정(is_pullback_condition 및
            그로부터 파생되는 4개 would_block_*)은 계산하지 않고
            None으로 남김 — PR-only/C-or-PR은 이 값과 무관하게
            항상 정상 계산됨(PR/C는 분봉 자체 분석값이라 조건식
            출처 문제와 무관).
        session_metrics: SessionMetrics 또는 None(session_metrics_
            mode="off"이거나 해당 종목 세션 데이터가 아직 없는 경우).
        threshold_pct: VWAP 거리 임계값(%) — 기본 2.0, 정확히
            이 값은 통과, 초과분부터 차단 후보(> 비교, >= 아님).

    Returns:
        VwapShadowAssessment.
    """
    is_pr = bool(minute_analysis is not None and minute_analysis.is_pulldown_recovery)
    is_c = bool(minute_analysis is not None and minute_analysis.is_valid_pulldown)
    # 2026-08-05 (2차 GPT 코드리뷰 지적 1번, 재현 확인): 키움 REAL
    # 메시지에는 어느 조건식에서 편입/편출됐는지 정보가 없어서,
    # 실시간 이벤트로만 알려진 종목의 조건식 소속은 확정할 수 없음
    # (재현: seq1=돌파형A, seq2=눌림목_PR 상태에서 실시간 편입 이벤트
    # 가 오면 기존 코드는 항상 seq1 결과로만 기록하고 있었음).
    # condition_source_reliable=False면 이 종목의 condition_names가
    # 실제로 정확한지 확신할 수 없으므로, condition-source 기반
    # 판정(is_pullback_condition 및 그로부터 파생되는 4개 would_
    # block_*)은 계산하지 않고 None으로 남김 — PR/C(분봉 자체
    # 분석값이라 조건식 출처 문제와 무관)는 신뢰도와 무관하게 항상
    # 정상 계산.
    is_pullback_condition: bool | None
    if condition_source_reliable:
        is_pullback_condition = any("눌림목" in name for name in condition_names)
    else:
        is_pullback_condition = None

    # 2026-08-05: is_pr_or_pullback_condition은 PR(항상 신뢰 가능)
    # 과 is_pullback_condition(신뢰 불가일 수 있음)의 OR — PR이
    # 이미 True면 조건식 출처를 몰라도 최종 결과는 확정적으로
    # True(OR 연산의 단락 평가와 동일한 원리). PR이 False인데
    # is_pullback_condition을 모르면(None) 최종 결과도 알 수 없으
    # 므로 None — bool(None)=False로 암묵 변환하면 "조건식 소속이
    # 아니다"로 잘못 단정하게 되므로 반드시 3진으로 처리.
    is_pr_or_pullback_condition: bool | None
    if is_pr:
        is_pr_or_pullback_condition = True
    elif is_pullback_condition is None:
        is_pr_or_pullback_condition = None
    else:
        is_pr_or_pullback_condition = is_pullback_condition

    rolling_vwap = None
    rolling_vwap_distance_pct = None
    rolling_over_threshold = None
    if minute_analysis is not None and minute_analysis.vwap > 0:
        rolling_vwap = minute_analysis.vwap
        rolling_vwap_distance_pct = (current_price - rolling_vwap) / rolling_vwap * 100
        rolling_over_threshold = rolling_vwap_distance_pct > threshold_pct

    session_vwap = None
    session_vwap_distance_pct = None
    session_metrics_ready = False
    session_readiness_reason = ""
    session_gate_eligible = False
    if session_metrics is not None and session_metrics.session_vwap > 0:
        session_vwap = session_metrics.session_vwap
        session_vwap_distance_pct = (current_price - session_vwap) / session_vwap * 100
        session_metrics_ready = session_metrics.session_metrics_ready
        session_readiness_reason = session_metrics.readiness_reason
        # 2026-08-05 (GPT 코드리뷰 지적): PARTIAL_SESSION이어도
        # session_vwap_distance_pct 값 자체는 위에서 이미 계산해
        # 관찰용으로 기록 — 하지만 이 게이트 적격 플래그는 ready
        # 일 때만 True. 아래 would_block_*_session_vwap 전부 이
        # 플래그를 거쳐야 계산됨(불완전 세션값을 판단에 쓰지 않기
        # 위함).
        session_gate_eligible = session_metrics_ready

    session_over_threshold = None
    if session_gate_eligible and session_vwap_distance_pct is not None:
        session_over_threshold = session_vwap_distance_pct > threshold_pct

    would_block_pr_only_rolling_vwap = None
    would_block_c_or_pr_rolling_vwap = None
    would_block_pullback_condition_rolling_vwap = None
    would_block_pr_or_pullback_condition_rolling_vwap = None
    would_block_pr_only_session_vwap = None
    would_block_c_or_pr_session_vwap = None
    would_block_pullback_condition_session_vwap = None
    would_block_pr_or_pullback_condition_session_vwap = None

    if legacy_buy_candidate:
        if rolling_over_threshold is not None:
            # PR-only/C-or-PR은 조건식 출처와 무관(PR/C는 분봉 자체
            # 분석값) — 신뢰도와 관계없이 항상 정상 계산.
            would_block_pr_only_rolling_vwap = is_pr and rolling_over_threshold
            would_block_c_or_pr_rolling_vwap = (is_pr or is_c) and rolling_over_threshold
            # 2026-08-05: 이 두 필드는 각각 자신이 의존하는 상위값
            # (is_pullback_condition / is_pr_or_pullback_condition)
            # 이 None인지를 독립적으로 확인 — is_pr_or_pullback_
            # condition은 is_pr=True이면 is_pullback_condition이
            # None(신뢰 불가)이어도 확정적으로 True가 되므로, 이
            # 경우 would_block_pr_or_pullback_condition_*는 정상
            # 계산 가능해야 함(반면 would_block_pullback_
            # condition_*는 is_pullback_condition 자체가 None이면
            # 항상 None).
            if is_pullback_condition is not None:
                would_block_pullback_condition_rolling_vwap = (
                    is_pullback_condition and rolling_over_threshold
                )
            if is_pr_or_pullback_condition is not None:
                would_block_pr_or_pullback_condition_rolling_vwap = (
                    is_pr_or_pullback_condition and rolling_over_threshold
                )

        if session_gate_eligible and session_over_threshold is not None:
            would_block_pr_only_session_vwap = is_pr and session_over_threshold
            would_block_c_or_pr_session_vwap = (is_pr or is_c) and session_over_threshold
            if is_pullback_condition is not None:
                would_block_pullback_condition_session_vwap = (
                    is_pullback_condition and session_over_threshold
                )
            if is_pr_or_pullback_condition is not None:
                would_block_pr_or_pullback_condition_session_vwap = (
                    is_pr_or_pullback_condition and session_over_threshold
                )

    return VwapShadowAssessment(
        is_pr=is_pr,
        is_c=is_c,
        condition_names=condition_names,
        condition_source_reliable=condition_source_reliable,
        is_pullback_condition=is_pullback_condition,
        is_pr_or_pullback_condition=is_pr_or_pullback_condition,
        rolling_vwap=rolling_vwap,
        rolling_vwap_distance_pct=rolling_vwap_distance_pct,
        rolling_over_threshold=rolling_over_threshold,
        session_vwap=session_vwap,
        session_vwap_distance_pct=session_vwap_distance_pct,
        session_metrics_ready=session_metrics_ready,
        session_readiness_reason=session_readiness_reason,
        session_gate_eligible=session_gate_eligible,
        would_block_pr_only_rolling_vwap=would_block_pr_only_rolling_vwap,
        would_block_c_or_pr_rolling_vwap=would_block_c_or_pr_rolling_vwap,
        would_block_pullback_condition_rolling_vwap=would_block_pullback_condition_rolling_vwap,
        would_block_pr_or_pullback_condition_rolling_vwap=(
            would_block_pr_or_pullback_condition_rolling_vwap
        ),
        would_block_pr_only_session_vwap=would_block_pr_only_session_vwap,
        would_block_c_or_pr_session_vwap=would_block_c_or_pr_session_vwap,
        would_block_pullback_condition_session_vwap=would_block_pullback_condition_session_vwap,
        would_block_pr_or_pullback_condition_session_vwap=(
            would_block_pr_or_pullback_condition_session_vwap
        ),
    )
