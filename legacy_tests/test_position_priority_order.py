# -*- coding: utf-8 -*-
"""
보유 포지션 우선 처리(순서 재정렬) 단위테스트 (2026-07-20)

run_once()에서 targets를 보유/미보유로 나눠 보유 종목을 먼저 처리하도록
바꾼 정렬 로직이 다음을 만족하는지 확인합니다:
  1) 보유 종목이 항상 미보유 종목보다 앞에 온다
  2) 같은 그룹 내에서는 원래 targets 순서가 그대로 유지된다 (안정 정렬)
  3) targets에 없는 심볼이 balance.positions에 있어도 에러 없이 동작한다
  4) 보유 종목이 하나도 없으면 원래 순서와 완전히 동일하다
  5) 전체 종목이 다 보유 중이어도 원래 순서와 완전히 동일하다
"""
from __future__ import annotations


def reorder(targets: list[str], held_symbols: set[str]) -> list[str]:
    """run_once()에 적용한 것과 동일한 정렬 로직."""
    ordered = sorted(
        enumerate(targets),
        key=lambda pair: 0 if pair[1] in held_symbols else 1,
    )
    return [sym for _, sym in ordered]


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


# 1) 보유 종목이 항상 앞에 옴
targets = ["A", "B", "C", "D", "E"]
held = {"D"}
result = reorder(targets, held)
check("1) 보유종목(D)이 맨 앞으로", result[0] == "D")
check("   전체 결과 = ['D','A','B','C','E']", result == ["D", "A", "B", "C", "E"])

# 2) 같은 그룹 내 원래 순서 유지 (안정 정렬)
targets = ["A", "B", "C", "D", "E", "F"]
held = {"B", "E"}
result = reorder(targets, held)
check("2) 보유그룹 내 순서 보존 [B,E,...]", result[:2] == ["B", "E"])
check("   미보유그룹 내 순서 보존 [...,A,C,D,F]", result[2:] == ["A", "C", "D", "F"])

# 3) targets에 없는 심볼이 held에 있어도 무관 (실제로는 balance.positions
#    에만 있고 targets(조건검색 편입)엔 없는 경우 — 정렬 자체엔 영향 없음)
targets = ["A", "B", "C"]
held = {"B", "ZZZZ"}
result = reorder(targets, held)
check("3) targets에 없는 심볼 포함돼도 정상 동작", result == ["B", "A", "C"])
check("   길이는 targets와 동일(늘거나 줄지 않음)", len(result) == len(targets))

# 4) 보유 종목 없음 → 원래 순서와 완전히 동일
targets = ["A", "B", "C", "D"]
held = set()
result = reorder(targets, held)
check("4) 보유종목 0개 → 원래 순서 그대로", result == targets)

# 5) 전체가 다 보유 중 → 원래 순서와 완전히 동일
targets = ["A", "B", "C", "D"]
held = {"A", "B", "C", "D"}
result = reorder(targets, held)
check("5) 전체 보유 중 → 원래 순서 그대로", result == targets)

# 6) targets가 비어있어도 에러 없음
result = reorder([], {"X"})
check("6) targets 빈 리스트 → 빈 결과", result == [])

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
import sys
if failed:
    sys.exit(1)
