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
    CostModel, SCENARIO_ORDER, load_cost_model, reset_cache,
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
check("1-10) 표본 없으면 None", load_cost_model().positive_rates([])["gross_positive_rate"] is None)


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


# ── 3. 허용 위치 밖에 비용 하드코딩이 없음 ──────────────────────
# 허용: domain/cost_model.py(기본값 정의), config/settings.yaml(설정),
#       이 테스트 파일, CHANGELOG
ALLOWED = {"domain/cost_model.py", "config/settings.yaml",
           "test_cost_model.py", "CHANGELOG_v1.6.md"}
PATTERN = re.compile(r"(ROUND_TRIP_COST_PCT|SLIPPAGE_PCT)\s*=\s*0\.(25|10)\b|COST_RATE\s*=\s*0\.009")
offenders = []
for p in Path(".").rglob("*.py"):
    rel = str(p).replace("\\", "/").lstrip("./")
    if rel in ALLOWED or rel.startswith(("logs/", "reports/", "exports/", "tests/")):
        continue
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception:
        continue
    if PATTERN.search(txt):
        offenders.append(rel)
check("3-1) 허용 위치 밖에 비용 하드코딩이 없음", not offenders)
if offenders:
    print(f"       └ 발견: {offenders}")


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

# 설정 파일이 없으면 기본값 (분석이 통째로 죽지 않도록)
m3 = load_cost_model(Path(tempfile.mkdtemp()) / "nope.yaml", use_cache=False)
check("4-4) 설정 파일이 없으면 기본값으로 동작",
      abs(m3.base_roundtrip_pct - DEFAULT_BASE_ROUNDTRIP_PCT) < 1e-9)
reset_cache()


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
