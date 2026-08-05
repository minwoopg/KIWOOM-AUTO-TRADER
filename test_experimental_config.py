# -*- coding: utf-8 -*-
"""
ExperimentalConfig 검증 (2026-07-27, 리팩터링 0단계)

1) settings.yaml의 experimental 섹션이 정확히 파싱되는지
2) 전부 "off"로 시작해서 기존 동작에 영향이 없는지
3) YAML boolean 함정(off -> False) 이 방어되는지
4) 잘못된 값(off/shadow/enforce 외)이 거부되는지
5) experimental 섹션이 아예 없어도 기본값(전부 off)으로 안전하게 로드되는지
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from config.settings import ExperimentalConfig, load_settings


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


FLAG_NAMES = [
    "session_metrics_mode", "decision_engine_mode", "position_lifecycle_mode",
    "reward_risk_guard_mode", "candidate_ranking_mode", "trailing_breakeven_mode",
    "entry_quality_guard_mode",
]

# ── 1) 실제 settings.yaml에서 정확히 파싱되는지 ────────────────────
settings = load_settings("config/settings.yaml")
check("1) settings.yaml에 experimental 섹션이 정상 파싱됨", settings.experimental is not None)

# ── 2) 각 플래그가 실제 리팩터링 진행 상황과 일치하는지 ────────────
# 2026-07-28 (1C.2단계): 이 테스트는 원래 "리팩터링 시작 전에는
# 전부 off"를 검증하는 용도였으나, 1C(세션 지표 shadow 구현)가
# GPT 코드리뷰 검증을 거쳐 완료되면서 session_metrics_mode를
# 실제로 "shadow"로 전환함(settings.yaml). 이 테스트가 "전부 off"
# 라는 고정된 기준을 계속 강제하면, 의도적인 shadow 전환 자체가
# 매번 회귀로 오인될 것 — 대신 "각 플래그가 해당 단계의 진행
# 상황에 맞는 값인지"를 검증하도록 갱신. 1C는 shadow까지 진행됨,
# 나머지 5개 단계(2~6단계)는 아직 착수 전이라 여전히 off여야 함.
# 2026-08-05 (1E.5단계): entry_quality_guard_mode 신규 추가 —
# 이 단계는 아직 shadow 관측 코드만 구현됐고 settings.yaml의
# 실제 값은 "off"로 시작(운영자가 명시적으로 shadow로 전환하기
# 전까지는 계산 자체가 스킵됨).
EXPECTED_MODES = {
    "session_metrics_mode": "shadow",      # 1단계: 1C.2 완료, shadow 관찰 중
    "decision_engine_mode": "off",         # 2단계: 착수 전
    "position_lifecycle_mode": "off",      # 3단계: 착수 전
    "reward_risk_guard_mode": "off",       # 4단계: 착수 전
    "candidate_ranking_mode": "off",       # 5단계: 착수 전
    "trailing_breakeven_mode": "off",      # 6단계: 착수 전
    "entry_quality_guard_mode": "off",     # 1E.5단계: 코드 구현 완료, shadow 전환 대기
}
modes_match = all(
    getattr(settings.experimental, f) == expected
    for f, expected in EXPECTED_MODES.items()
)
check("2) 각 플래그가 현재 리팩터링 진행 상황과 일치함"
      "(session_metrics_mode만 shadow, 나머지 5개는 여전히 off)", modes_match)
if not modes_match:
    for f, expected in EXPECTED_MODES.items():
        actual = getattr(settings.experimental, f)
        if actual != expected:
            print(f"   불일치: {f}={actual!r} (기대: {expected!r})")

# ── 3) YAML boolean 함정 방어 확인 ─────────────────────────────────
try:
    ExperimentalConfig(session_metrics_mode=False)  # YAML의 off가 무따옴표면 이렇게 됨
    check("3) YAML boolean(False) 오염 시 ValueError 발생", False)
except ValueError as e:
    check("3) YAML boolean(False) 오염 시 ValueError 발생", "문자열" in str(e) or "bool" in str(e).lower())

try:
    ExperimentalConfig(decision_engine_mode=True)  # YAML의 on이 무따옴표면 이렇게 됨
    check("   True(YAML의 on) 오염도 거부됨", False)
except ValueError:
    check("   True(YAML의 on) 오염도 거부됨", True)

# ── 4) 잘못된 문자열 값도 거부 ──────────────────────────────────────
try:
    ExperimentalConfig(reward_risk_guard_mode="enable")  # "enforce"의 오타 시뮬레이션
    check("4) 잘못된 값('enable') -> ValueError 발생", False)
except ValueError:
    check("4) 잘못된 값('enable') -> ValueError 발생", True)

# ── 5) 정상 값(off/shadow/enforce) 조합은 모두 통과 ─────────────────
try:
    ExperimentalConfig(
        session_metrics_mode="shadow",
        decision_engine_mode="enforce",
        position_lifecycle_mode="off",
    )
    check("5) 정상적인 off/shadow/enforce 조합은 예외 없이 생성됨", True)
except ValueError as e:
    check(f"5) 예상치 못한 예외: {e}", False)

# ── 5-1) entry_quality_guard_mode="enforce"는 명시적으로 거부됨 ──────
# 2026-08-05 (1E.5단계, GPT 코드리뷰 지시): 이 플래그는 아직 shadow
# 관측까지만 구현됐으므로, 설정 파일에 실수로 "enforce"가 들어가도
# 조용히 무시되지 않고 명확한 오류로 막혀야 함.
try:
    ExperimentalConfig(entry_quality_guard_mode="enforce")
    check('5-1) entry_quality_guard_mode="enforce" -> ValueError 발생'
          '(1E.5단계는 shadow까지만 지원, enforce 미구현 명시)', False)
except ValueError as e:
    check('5-1) entry_quality_guard_mode="enforce" -> ValueError 발생'
          '(1E.5단계는 shadow까지만 지원, enforce 미구현 명시)',
          "지원되지" in str(e))
check("    entry_quality_guard_mode='shadow'는 정상적으로 생성됨",
      ExperimentalConfig(entry_quality_guard_mode="shadow").entry_quality_guard_mode == "shadow")

# ── 6) experimental 섹션이 없는 YAML도 기본값(전부 off)으로 안전하게 로드 ──
with tempfile.TemporaryDirectory() as tmpdir:
    yaml_path = Path(tmpdir) / "settings_no_experimental.yaml"
    original = Path("config/settings.yaml").read_text(encoding="utf-8")
    # experimental 섹션을 제거한 버전 생성(간단히 섹션 헤더 이후 텍스트를 잘라냄)
    if "experimental:" in original:
        without_experimental = original.split("experimental:")[0]
    else:
        without_experimental = original
    yaml_path.write_text(without_experimental, encoding="utf-8")

    settings_no_exp = load_settings(str(yaml_path))
    check("6) experimental 섹션 없는 YAML도 정상 로드됨(기본값 전부 off)",
          settings_no_exp.experimental is not None
          and all(getattr(settings_no_exp.experimental, f) == "off" for f in FLAG_NAMES))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
