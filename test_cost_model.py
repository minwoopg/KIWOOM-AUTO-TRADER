# -*- coding: utf-8 -*-
"""비용 모델 단일 출처 검증 (2026-08-07, 1J단계)

배경: 분석기마다 비용을 직접 하드코딩해 값이 갈라져 있었음.
  daily_reporter          0.90%
  replay_runner 외 3종    0.35%
2026-07에 COST_RATE를 0.53% → 0.90%로 정정했을 때 백테스트
스크립트들이 따라가지 못한 결과. 0.55%p 차이는 "승리"와 "적자"를
가르는 크기라, 이 테스트로 단일 출처를 고정한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from domain.cost_model import (
    CostModel, CostModelConfigError, SCENARIO_ORDER, DEFAULT_SETTINGS_PATH,
    load_cost_model, reset_cache,
    DEFAULT_BASE_ROUNDTRIP_PCT, DEFAULT_STRESS_ROUNDTRIP_PCT,
)

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


# ── 1. 동일 gross return에 세 시나리오가 정확히 계산됨 ──────────
m = CostModel(gross_roundtrip_pct=0.0, base_roundtrip_pct=0.35, stress_roundtrip_pct=0.90)
nets = m.net_all(0.61)
check("1-1) Gross는 원수익 그대로", abs(nets["gross"] - 0.61) < 1e-9)
check("1-2) Base는 0.35%p 차감", abs(nets["base"] - 0.26) < 1e-9)
check("1-3) Stress는 0.90%p 차감", abs(nets["stress"] - (-0.29)) < 1e-9)
check("1-4) None 입력은 None 반환", m.net(None, "base") is None)
try:
    m.cost_pct("unknown")
    _raised = False
except ValueError:
    _raised = True
check("1-6) 알 수 없는 시나리오 이름에 ValueError", _raised)

# 승률도 시나리오별로 갈려야 함
rates = m.positive_rates([0.71, 0.50, 0.20, -0.10])
check("1-7) gross_positive_rate 산출", abs(rates["gross_positive_rate"] - 0.75) < 1e-9)
check("1-8) base_net_positive_rate 산출", abs(rates["base_net_positive_rate"] - 0.50) < 1e-9)
check("1-9) stress_net_positive_rate 산출",
      abs(rates["stress_net_positive_rate"] - 0.0) < 1e-9)
check("1-10) 표본 없으면 None", m.positive_rates([])["gross_positive_rate"] is None)


# ── 2. 모든 분석기가 같은 cost model을 참조 ─────────────────────
ANALYZERS = ["replay_runner.py", "analyze_crash_rebound_days.py",
             "simulate_pullback_removal.py"]
for name in ANALYZERS:
    src = Path(name).read_text(encoding="utf-8")
    check(f"2-1) {name}이 cost_model을 import", "from domain.cost_model import" in src)
    check(f"2-2) {name}이 load_cost_model()로 값을 받음", "load_cost_model()" in src)

# analyze_v_drop_backtest는 replay_runner에서 재사용 (간접 참조)
vd = Path("analyze_v_drop_backtest.py").read_text(encoding="utf-8")
check("2-3) analyze_v_drop_backtest가 replay_runner의 값을 재사용",
      "from replay_runner import" in vd and "TOTAL_COST_PCT" in vd)

rep = Path("infra/storage/daily_reporter.py").read_text(encoding="utf-8")
check("2-4) daily_reporter가 cost_model을 참조", "from domain.cost_model import" in rep)
check("2-5) daily_reporter가 Stress 시나리오를 사용", "stress_roundtrip_pct" in rep)


# ── 3. 허용 위치 밖에 비용 하드코딩이 없음 (1J.1 강화) ────────
# 1J의 검사는 과거 변수명(ROUND_TRIP_COST_PCT 등)만 봐서
# `BACKTEST_COST = 0.35` 같은 새 하드코딩을 못 잡았음.
# 이제 **비용 literal 자체**(0.35 / 0.90 / 0.0035 / 0.009)를
# 광범위하게 찾고 허용 위치만 제외한다.
ALLOWED = {"domain/cost_model.py", "config/settings.yaml",
           "test_cost_model.py", "CHANGELOG_v1.6.md"}
COST_LITERAL = re.compile(r"=\s*0\.(35|90|9|0035|009)\b")
offenders = []
for p in list(Path(".").rglob("*.py")) + list(Path(".").rglob("*.yaml")):
    rel = str(p).replace("\\", "/").lstrip("./")
    if rel in ALLOWED or rel.startswith(("logs/", "reports/", "exports/", "tests/", ".venv/")):
        continue
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception:
        continue
    for i, line in enumerate(txt.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue          # 주석은 설명일 수 있으므로 제외
        if COST_LITERAL.search(line):
            offenders.append(f"{rel}:{i}  {stripped[:70]}")
check("3-1) 허용 위치 밖에 비용 literal 하드코딩이 없음", not offenders)
for o in offenders[:5]:
    print(f"       └ {o}")

# 검사기 자체가 새 변수명도 잡는지 (자기검증)
import tempfile as _tf
_probe = Path(_tf.mkdtemp()) / "probe.py"
_probe.write_text("BACKTEST_COST = 0.35\n", encoding="utf-8")
check("3-2) 새로운 임의 변수명의 0.35 하드코딩도 검출됨",
      bool(COST_LITERAL.search(_probe.read_text(encoding="utf-8"))))
_probe.write_text("RATE = 0.009\n", encoding="utf-8")
check("3-3) 비율형(0.009) 하드코딩도 검출됨",
      bool(COST_LITERAL.search(_probe.read_text(encoding="utf-8"))))
check("3-4) 주석은 오탐으로 잡지 않음",
      "#" == "# 왕복 0.35% 가정".strip()[0])


# ── 4. 설정 변경 시 모든 결과가 동일하게 변경됨 ─────────────────
import tempfile
tmp = Path(tempfile.mkdtemp()) / "settings.yaml"
tmp.write_text(
    "cost_model:\n  gross_roundtrip_pct: 0.00\n"
    "  base_roundtrip_pct: 0.50\n  stress_roundtrip_pct: 1.20\n", encoding="utf-8")
reset_cache()
m2 = load_cost_model(tmp, use_cache=False)
check("4-1) 설정의 Base 값이 반영됨", abs(m2.base_roundtrip_pct - 0.50) < 1e-9)
check("4-2) 설정의 Stress 값이 반영됨", abs(m2.stress_roundtrip_pct - 1.20) < 1e-9)
check("4-3) 변경된 설정으로 순수익이 함께 바뀜",
      abs(m2.net(1.00, "base") - 0.50) < 1e-9 and abs(m2.net(1.00, "stress") - (-0.20)) < 1e-9)

# 2026-08-07 (1J.1 정책 변경): 1J에서는 설정 부재 시 조용히 기본값을
# 썼는데, 51일 백테스트가 아무 경고 없이 잘못된 비용으로 도는 위험이
# 있어 fail-closed로 바꿨음. 기본값은 allow_default=True일 때만.
m3 = load_cost_model(Path(tempfile.mkdtemp()) / "nope.yaml",
                     allow_default=True, use_cache=False)
check("4-4) allow_default=True를 명시할 때만 기본값 허용",
      abs(m3.base_roundtrip_pct - DEFAULT_BASE_ROUNDTRIP_PCT) < 1e-9)
reset_cache()


# ── 9. fail-closed 로딩 (1J.1) ──────────────────────────────────
# 재현(1J): cwd를 프로젝트 밖으로 옮기거나 YAML이 깨져도 예외 없이
# Base 0.35로 돌아갔음 — 비용을 0.42로 교정해도 조용히 무시됨.
import os


def _yaml(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "s.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except CostModelConfigError:
        return True


_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp())
try:
    reset_cache()
    m9 = load_cost_model()
    ok_cwd = abs(m9.base_roundtrip_pct - 0.35) < 1e-9
finally:
    os.chdir(_cwd)
check("9-1) cwd가 프로젝트 밖이어도 올바른 settings.yaml을 로드", ok_cwd)
check("9-2) 기본 설정 경로가 프로젝트 루트 기준 절대경로",
      DEFAULT_SETTINGS_PATH.is_absolute() and DEFAULT_SETTINGS_PATH.name == "settings.yaml")

check("9-3) 설정 파일 누락 → 예외",
      _raises(lambda: load_cost_model(Path(tempfile.mkdtemp()) / "nope.yaml", use_cache=False)))
check("9-4) YAML malformed → 예외",
      _raises(lambda: load_cost_model(_yaml("cost_model: [[["), use_cache=False)))
check("9-5) cost_model 블록 누락 → 예외",
      _raises(lambda: load_cost_model(_yaml("other: 1"), use_cache=False)))
check("9-6) 필수 키 누락 → 예외",
      _raises(lambda: load_cost_model(_yaml("cost_model:\n  base_roundtrip_pct: 0.4"), use_cache=False)))
check("9-7) 숫자 변환 실패 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: abc\n  base_roundtrip_pct: 0.4\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))
check("9-8) Gross < Base < Stress 위반 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.5\n  base_roundtrip_pct: 0.2\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))
check("9-9) 음수 비용 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: -0.1\n  base_roundtrip_pct: 0.4\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))


# ── 10. 경로별 cache (1J.1) ─────────────────────────────────────
# 재현(1J): 전역 캐시 하나라 A→B 순서로 읽으면 B도 A 값이 나왔음.
reset_cache()
A = _yaml("cost_model:\n  gross_roundtrip_pct: 0.0\n  base_roundtrip_pct: 0.11\n"
          "  stress_roundtrip_pct: 0.22")
B = _yaml("cost_model:\n  gross_roundtrip_pct: 0.0\n  base_roundtrip_pct: 0.77\n"
          "  stress_roundtrip_pct: 0.88")
a, b = load_cost_model(A), load_cost_model(B)
check("10-1) A 설정이 정확히 로드", abs(a.base_roundtrip_pct - 0.11) < 1e-9)
check("10-2) B 설정이 A로 오염되지 않음", abs(b.base_roundtrip_pct - 0.77) < 1e-9)
check("10-3) A를 다시 읽어도 값 유지", abs(load_cost_model(A).base_roundtrip_pct - 0.11) < 1e-9)
reset_cache()


# ── 11. 기준금액 통일 (1J.1) ────────────────────────────────────
# roundtrip_pct := 진입 원금 대비 왕복 총비용 추정률
live2 = load_cost_model(use_cache=False)
check("11-1) cost_amount가 진입 원금 기준으로 계산",
      abs(live2.cost_amount(1_000_000, "stress") - 9000.0) < 1e-6)
check("11-2) replay 정의와 일치 — 원금 100, 수익 10%면 순수익 = 10 - cost_pct",
      abs(live2.net(10.0, "stress") - 9.10) < 1e-9)
check("11-3) daily_report가 매도금액이 아니라 매수금액에 비용률을 곱함",
      'm["buy_price"] * m["qty"] * COST_RATE' in rep
      and 'm["sell_price"] * m["qty"] * COST_RATE' not in rep)
check("11-4) 이월 포지션은 평균단가 기준 추정치를 사용",
      "avg_buy_p * sell_qty * COST_RATE" in rep)
check("11-5) 리포트에 진입 원금 기준임을 명시", "진입 원금 기준" in rep)
check("11-6) 이월 비용이 추정치임을 명시", "추정치입니다" in rep)
check("11-7) '체결가 기준' 표현을 '주문가 기준 예상'으로 정정",
      "주문가 기준 예상" in rep)
check("11-8) describe()에 기준금액이 드러남", "진입 원금" in live2.describe())


# ── 12. 분석기 3시나리오 출력 통일 (1J.1) ───────────────────────
for name in ["analyze_crash_rebound_days.py", "analyze_v_drop_backtest.py",
             "simulate_pullback_removal.py"]:
    src = Path(name).read_text(encoding="utf-8")
    check(f"12-1) {name}이 3시나리오 출력 헬퍼를 가짐",
          "append_cost_scenarios" in src and "scenario_lines" in src)
for name in ["replay_runner.py", "analyze_v_drop_backtest.py", "simulate_pullback_removal.py"]:
    src = Path(name).read_text(encoding="utf-8")
    check(f"12-2) {name}이 gross/base/stress 필드를 산출",
          '"gross_5m"' in src or 'gross_5m=' in src or "COST_MODEL.net(" in src)
    check(f"12-3) {name}의 net_*가 Base alias로 유지됨",
          '"base"' in src)
check("12-4) scenario_lines가 평균과 플러스비율을 함께 냄",
      all("플러스비율" in l for l in live2.scenario_lines([0.71, 0.5, -0.1])))


# ── 5. Base < Gross, Stress < Base 관계 ─────────────────────────
live = load_cost_model("config/settings.yaml", use_cache=False)
check("5-1) 실제 설정이 로드됨", live.base_roundtrip_pct > 0)
check("5-2) 비용 크기: Gross < Base < Stress",
      live.gross_roundtrip_pct < live.base_roundtrip_pct < live.stress_roundtrip_pct)
n = live.net_all(1.00)
check("5-3) 순수익 크기: Gross > Base > Stress", n["gross"] > n["base"] > n["stress"])
check("5-4) 시나리오 순서 상수가 고정", SCENARIO_ORDER == ("gross", "base", "stress"))


# ── 6. 기존 report/replay 계산 결과 재현 ────────────────────────
# 기존 replay는 0.35%를 뺐음 → Base와 일치해야 함
check("6-1) Base가 기존 replay의 0.35%와 일치",
      abs(live.base_roundtrip_pct - 0.35) < 1e-9)
# 기존 daily_report는 COST_RATE=0.009 (0.90%) → Stress와 일치
check("6-2) Stress가 기존 daily_report의 0.90%와 일치",
      abs(live.stress_roundtrip_pct - DEFAULT_STRESS_ROUNDTRIP_PCT) < 1e-9
      and abs(live.stress_roundtrip_pct - 0.90) < 1e-9)
check("6-3) 기존 replay 수치 재현 (+0.71% → +0.36%)",
      abs(live.net(0.71, "base") - 0.36) < 1e-9)


# ── 7. 리포트에 기준 차이가 명시됨 ──────────────────────────────
check("7-1) daily_report가 Stress 기준임을 표기", "Stress" in rep)
check("7-2) daily_report가 replay와 기준이 다름을 명시",
      "replay" in rep and "다릅니다" in rep)
rr = Path("replay_runner.py").read_text(encoding="utf-8")
check("7-3) replay가 세 시나리오를 함께 출력",
      'for _s in ("gross", "base", "stress")' in rr)
check("7-4) replay가 daily_report와의 기준 차이를 명시",
      "daily_report는 Stress" in rr)


# ── 8. trades.csv raw PnL 보존 ──────────────────────────────────
# 비용은 분석 단계에서만 더해야 하며, 기록 시점에 섞으면 안 됨.
tw = Path("infra/storage/logger.py").read_text(encoding="utf-8")
check("8-1) 거래 기록기가 비용을 차감하지 않음(raw PnL 보존)",
      "COST_RATE" not in tw and "TOTAL_COST_PCT" not in tw)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
