# -*- coding: utf-8 -*-
"""merge_excluded_symbols() 검증 (2026-10-21, 감시 대상 정리 1순위)

이 테스트가 존재하는 이유:

민우님의 10/20~21 번들 분석에서, 설정상 영구 차단 종목
(`config/settings.yaml`의 `excluded_symbols`, 예: 114800·252670)이
`compute_day_targets()`의 `excluded_symbols` 인자(당일 동적 제외
집합)에 전혀 반영되지 않아, 매수가 아예 불가능한 종목이 매 폴링
마다 전부 분석되는 낭비가 있었음이 드러났습니다
(10/20 37.0%, 10/21 58.8%의 판단 횟수).

`app/main.py`의 `on_symbols_changed()` 콜백은 클로저라 직접
테스트할 수 없으므로(1E.9 주석 참고), 그 안에서 호출하는
병합 로직만 `merge_excluded_symbols()`로 분리해 여기서 검증하고,
`compute_day_targets()`와 결합했을 때 실제로 영구 차단 종목이
day_symbols/final_targets에서 빠지는지도 함께 확인합니다.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.target_selection import compute_day_targets, merge_excluded_symbols


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


# ── 1) 동적 제외 집합만 있을 때 (영구 제외 없음/None) ──────────────
check(
    "1) permanent_excluded_symbols=None이면 동적 집합만 반환",
    merge_excluded_symbols({"005930"}, None) == {"005930"},
)
check(
    "   permanent_excluded_symbols=[]여도 동적 집합만 반환",
    merge_excluded_symbols({"005930"}, []) == {"005930"},
)

# ── 2) 영구 제외 목록이 합쳐지는지 ────────────────────────────────
merged = merge_excluded_symbols({"005930"}, ["114800", "252670"])
check(
    "2) 동적 집합 + 영구 제외 목록이 합집합으로 합쳐짐",
    merged == {"005930", "114800", "252670"},
)

# ── 3) 중복(동적 집합에도 이미 있는 종목)은 한 번만 ────────────────
merged_overlap = merge_excluded_symbols({"114800"}, ["114800", "252670"])
check(
    "3) 동적 집합과 영구 제외 목록이 겹쳐도 중복 없이 합쳐짐",
    merged_overlap == {"114800", "252670"},
)

# ── 4) 동적 집합이 비어 있어도 영구 제외 목록만으로 동작 ──────────
check(
    "4) 동적 집합이 빈 set이어도 영구 제외 목록이 반영됨",
    merge_excluded_symbols(set(), ["114800", "252670"]) == {"114800", "252670"},
)

# ── 5) 입력 집합을 변형(mutate)하지 않음 ──────────────────────────
dynamic_src = {"005930"}
merge_excluded_symbols(dynamic_src, ["114800"])
check(
    "5) 호출 후에도 전달한 동적 집합 원본은 변하지 않음",
    dynamic_src == {"005930"},
)

# ── 6) tuple/list 등 리스트가 아닌 시퀀스도 안전하게 처리 ──────────
check(
    "6) permanent_excluded_symbols가 tuple이어도 정상 동작",
    merge_excluded_symbols(set(), ("114800",)) == {"114800"},
)

# ── 7) compute_day_targets()와 결합 — 영구 차단 종목이 실제로
#      day_symbols/final_targets에서 빠지는지 종단 확인 ───────────
# 10/20~21 상황 재현: 조건검색으로 114800(영구 차단)과 014950이
# 함께 편입됐다고 가정. 동적 제외 집합은 비어 있음(UNKNOWN/거래
# 대금 부족 트리거가 아직 발동하지 않은 상태) — merge 없이 그냥
# trading_service.get_excluded_symbols()만 넘기면 114800이
# 그대로 day_symbols에 남아 매 폴링마다 분석됐을 상황.
dynamic_excluded = set()  # trading_service.get_excluded_symbols() 흉내
permanent_excluded = ["114800", "252670"]  # settings.trading.excluded_symbols 흉내
effective_excluded = merge_excluded_symbols(dynamic_excluded, permanent_excluded)

selection = compute_day_targets(
    confirmed_symbols_by_seq={"900001": {"114800", "014950"}},
    realtime_unresolved=set(),
    day_seqs=["900001"],
    manual_symbols=[],
    excluded_symbols=effective_excluded,
    max_symbols=10,
)
check(
    "7) 영구 차단 종목(114800)이 final_targets에서 제외됨",
    "114800" not in selection.final_targets,
)
check(
    "   정상 종목(014950)은 final_targets에 그대로 남음",
    "014950" in selection.final_targets,
)
check(
    "   영구 차단 종목이 selection.blocked(재편입 차단 로그용)에 기록됨",
    "114800" in selection.blocked,
)

# ── 8) merge 없이(기존 동작) 호출하면 114800이 그대로 남는다는
#      것도 대조군으로 확인 — 회귀 발생 시 7)·8)이 같아져 버림 ──────
selection_without_merge = compute_day_targets(
    confirmed_symbols_by_seq={"900001": {"114800", "014950"}},
    realtime_unresolved=set(),
    day_seqs=["900001"],
    manual_symbols=[],
    excluded_symbols=dynamic_excluded,  # merge 적용 전 — 영구 제외 미반영
    max_symbols=10,
)
check(
    "8) (대조군) merge 없이 넘기면 영구 차단 종목이 그대로 남아있었음"
    "(수정 전 증상 재현 — 이 테스트가 깨지면 7)의 의미가 없어짐)",
    "114800" in selection_without_merge.final_targets,
)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
