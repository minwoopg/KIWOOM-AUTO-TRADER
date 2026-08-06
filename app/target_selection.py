from __future__ import annotations

"""조건검색 편입 상태 → 최종 감시 종목 목록 계산 (순수 함수).

2026-08-06 (1E.9단계): 이 모듈은 원래 `app/main.py`의
`on_symbols_changed()` 콜백 안에 인라인으로 들어있던 로직을
그대로 떼어낸 것입니다. 분리한 이유는 두 가지입니다.

1. **1E.8에서 드러난 회귀 스위트 사각지대 해소**
   1E.7이 `ConditionWatcher._symbols_by_seq`를 `_confirmed_
   symbols_by_seq`로 개명했을 때, `app/main.py`의 콜백 3곳이
   따라가지 못해 `AttributeError` 재연결 루프에 빠졌는데도
   회귀 스위트가 이를 전혀 잡지 못했습니다. 콜백이 `main()`
   안의 클로저라 어떤 테스트도 실행할 수 없는 구조였기
   때문입니다. 이 모듈은 watcher/settings 객체가 아니라
   순수 원시값만 받으므로 테스트에서 직접 호출할 수 있습니다.

2. **1E.9에서 고친 실시간 편입 누락 결함의 재발 방지**
   아래 `compute_day_targets()` 주석 참고.

2026-08-06 (1F단계): 스윙 전략 전량 폐기에 따라 `swing_seqs`
매개변수와 그에 딸린 분기를 제거했습니다. 구독하는 조건검색식이
전부 단타용이므로, 출처 미확정 종목의 소속이 애매할 여지가
원천적으로 없어졌습니다.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DayTargetSelection:
    """`compute_day_targets()`의 계산 결과 묶음.

    final_targets  : trading_service.update_targets()에 넘길 최종 목록
    day_symbols    : 제외/상한 적용 전, 단타 대상으로 인정된 전체 종목
    blocked        : 자동 제외 종목이라 재편입이 차단된 종목들
    unresolved_used: 출처 미확정 상태로 day_symbols에 포함된 종목들
    """

    final_targets: list[str]
    day_symbols: list[str]
    blocked: set[str] = field(default_factory=set)
    unresolved_used: list[str] = field(default_factory=list)


def compute_day_targets(
    *,
    confirmed_symbols_by_seq: dict[str, set[str]],
    realtime_unresolved: set[str],
    day_seqs,
    manual_symbols,
    excluded_symbols: set[str],
    max_symbols: int,
) -> DayTargetSelection:
    """조건검색 상태로부터 단타 감시 종목 목록을 계산합니다.

    ── 2026-08-06 (1E.9, 실서버 로그로 재현 확인된 P0 결함 수정) ──

    **증상**: 1E.7 적용 후 첫 거래일(8/6) 실서버 로그에서
    `[COND] [출처불명] 편입: 215790` 직후의 `[COND_STATUS]`가
    `final=5종목: ['010170','006260','005930','080220','069540']`
    으로, 방금 편입된 215790을 포함하지 않았습니다.

    **원인**: 기존 콜백은 `_confirmed_symbols_by_seq`(= CNSRREQ
    초기 조회로 확정된 종목)만 순회했는데,
      - CNSRREQ는 `_on_login()`에서 연결당 1회만 발송되고
        주기적 재조회가 없으며,
      - 장전 08:40 시점의 초기 조회 결과는 실측상 항상 0종목
        (8/5·8/6 로그 확인)
    이므로, 장중 편입은 100% REAL 실시간 이벤트로 들어옵니다.
    그런데 1E.6~1E.7의 재설계로 REAL 편입 종목은
    `_realtime_unresolved`에만 들어가게 되어, 확정 버킷은
    하루 종일 비어 있고 결과적으로 **수동 targets 4종목만
    감시**하는 상태가 됐습니다. 8/5(구 코드)에는 실시간 편입이
    3,318건 발생해 seq 버킷이 0/0/0 → 52/3/23까지 성장했으므로,
    신규 코드에서 이 종목들이 전량 누락된 것입니다.

    **수정**: 출처 미확정 종목도 단타 대상에 포함합니다. 다만
    `symbol_condition_source_reliable`은 여전히 False로 유지되므로,
    VWAP shadow의 condition-source 기반 판단(조건식명에 "눌림목"
    포함 여부)은 이 종목들을 정확히 배제한 채로 관측을 이어갑니다.
    즉 "매매 대상 포함"과 "조건식 출처 신뢰"를 분리해서, 1E.6이
    바로잡으려던 신뢰도 의미론은 그대로 두고 targets 산출만
    8/5 이전 동작으로 복구합니다.

    ── 2026-08-06 (1F, 스윙 폐기) ──
    1E.9 시점에는 스윙 검색식이 같은 WebSocket으로 동시 구독될 수
    있어, 출처 미확정 종목이 스윙 소속일 가능성 때문에 별도 분기가
    있었습니다. 스윙 전략을 전량 폐기하면서 구독 대상이 전부 단타
    조건식이 되었으므로 그 분기를 제거했습니다. `day_seqs` 필터링
    자체는 남겨둡니다 — 설정에 등록되지 않은 seq의 결과가 흘러들어와
    조용히 targets에 섞이는 일을 막는 방어선이기 때문입니다.
    """
    day_seqs_set = {str(s) for s in day_seqs}

    day_symbols: list[str] = []
    for seq, syms in confirmed_symbols_by_seq.items():
        if seq in day_seqs_set:
            for s in sorted(syms):
                if s not in day_symbols:
                    day_symbols.append(s)

    unresolved_used: list[str] = []
    for s in sorted(realtime_unresolved):
        if s not in day_symbols:
            day_symbols.append(s)
            unresolved_used.append(s)

    blocked = excluded_symbols & set(day_symbols)
    filtered = [s for s in day_symbols if s not in excluded_symbols]
    combined = list(dict.fromkeys(list(manual_symbols) + filtered))
    limited = combined[:max_symbols]

    return DayTargetSelection(
        final_targets=limited,
        day_symbols=day_symbols,
        blocked=blocked,
        unresolved_used=unresolved_used,
    )
