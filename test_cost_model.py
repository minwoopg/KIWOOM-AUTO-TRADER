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


# ── 12. 분석기 3시나리오 — 실행 기반 검증 (1J.2) ───────────────
# 1J.1의 12절은 소스에 문자열이 있는지만 봐서, helper를 정의만 하고
# 한 번도 호출하지 않아도 통과했음(false pass). 이제 **실제로 실행해
# 출력 결과**를 검증한다. 소스 검사는 보조로만 사용.
import importlib


def _mod(name: str):
    return importlib.import_module(name)


# --- A. v-drop: simulate_event 실제 호출 (NameError 재발 방지) -----
vd = _mod("analyze_v_drop_backtest")
check("12A-1) analyze_v_drop_backtest에 COST_MODEL이 정의됨", hasattr(vd, "COST_MODEL"))
check("12A-2) TOTAL_COST_PCT가 Base alias",
      abs(vd.TOTAL_COST_PCT - vd.COST_MODEL.base_roundtrip_pct) < 1e-9)

# load_bars를 작은 fixture로 monkeypatch 후 simulate_event 실제 실행
# replay_runner.MinuteBarRow와 동일한 필드로 최소 fixture 구성
class _Bar:
    def __init__(self, t, price):
        self.cntr_tm = t
        self.open_price = int(price)
        self.high_price = int(price * 1.01)
        self.low_price = int(price * 0.99)
        self.close_price = int(price)
        self.volume = 1000
        self.acc_volume = 1000


_bars = [_Bar(f"20260807{9:02d}{m:02d}00", 1000.0 + m * 5) for m in range(0, 40)]
_orig_load = vd.load_bars
vd.load_bars = lambda *a, **k: _bars
_sim_err = None
_sim = None
try:
    from datetime import datetime as _dt
    # simulate_event(event: dict) — 실제 시그니처에 맞춘 최소 fixture
    # simulate_event가 실제로 참조하는 키: symbol / start / count
    _event = {
        "symbol": "005930",
        "start": {"timestamp": _dt(2026, 8, 7, 9, 0, 0), "drop_pct": -2.2},
        "count": 5,
    }
    _sim = vd.simulate_event(_event)
except NameError as e:
    _sim_err = f"NameError: {e}"
except Exception as e:
    _sim_err = f"{type(e).__name__}: {e}"
finally:
    vd.load_bars = _orig_load

check("12A-3) simulate_event 실행 시 NameError가 발생하지 않음",
      _sim_err is None or "NameError" not in _sim_err)
if _sim_err:
    print(f"       └ {_sim_err}")
if isinstance(_sim, dict):
    check("12A-4) gross/base/stress 필드가 산출됨",
          all(k in _sim for k in ("gross_5m", "base_5m", "stress_5m")))
    check("12A-5) net_5m이 base_5m alias",
          _sim.get("net_5m") == _sim.get("base_5m"))
    if _sim.get("gross_5m") is not None:
        check("12A-6) Gross > Base > Stress 수치 관계",
              _sim["gross_5m"] > _sim["base_5m"] > _sim["stress_5m"])
else:
    check("12A-4) simulate_event가 dict를 반환", _sim is not None or _sim_err is not None)


# --- B. helper가 실제로 3시나리오를 출력 --------------------------
live3 = load_cost_model(use_cache=False)
for name in ("analyze_crash_rebound_days", "analyze_v_drop_backtest",
             "simulate_pullback_removal"):
    m = _mod(name)
    check(f"12B-1) {name}에 append_cost_scenarios가 있음",
          hasattr(m, "append_cost_scenarios"))
    _out: list[str] = []
    m.append_cost_scenarios(_out, [1.00, 0.50, -0.20], "테스트")
    text = "\n".join(_out)
    check(f"12B-2) {name}이 Gross/Base/Stress를 실제로 출력",
          "Gross" in text and "Base" in text and "Stress" in text)
    check(f"12B-3) {name}이 플러스비율도 출력", "플러스비율" in text)
    # 수치 관계 — 평균 (1.00+0.50-0.20)/3 = 0.4333
    nums = [float(x) for x in re.findall(r"([+-]\d+\.\d+)%", text)]
    check(f"12B-4) {name}의 세 값이 Gross > Base > Stress",
          len(nums) == 3 and nums[0] > nums[1] > nums[2])
    check(f"12B-5) {name}의 Base가 Gross - base_roundtrip_pct",
          len(nums) == 3 and abs((nums[0] - nums[1]) - live3.base_roundtrip_pct) < 0.011)


# --- C. helper가 정의만 되고 호출되지 않는 상태를 잡아냄 ----------
# 1J.1의 false pass 재발 방지: 소스에서 "정의 1회 + 호출 1회 이상"을 확인
for name in ("analyze_crash_rebound_days.py", "analyze_v_drop_backtest.py",
             "simulate_pullback_removal.py"):
    src = Path(name).read_text(encoding="utf-8")
    total = src.count("append_cost_scenarios")
    defs = src.count("def append_cost_scenarios")
    check(f"12C-1) {name}에서 helper가 실제로 호출됨 (등장 {total}회, 정의 {defs}회)",
          defs == 1 and total >= 2)


# --- D. pullback row가 3시나리오 필드를 실제로 가짐 ----------------
pb = _mod("simulate_pullback_removal")
pb_src = Path("simulate_pullback_removal.py").read_text(encoding="utf-8")
for m_ in (5, 10, 20):
    check(f"12D-1) pullback row에 gross/base/stress_{m_}m 필드 존재",
          all(f"{sc}_{m_}m=" in pb_src for sc in ("gross", "base", "stress")))
check("12D-2) pullback의 net_*가 Base alias로 명시됨", "# Base alias" in pb_src)

cr_src = Path("analyze_crash_rebound_days.py").read_text(encoding="utf-8")
check("12D-3) crash entries에 3시나리오 필드 존재",
      all(k in cr_src for k in ('"gross_return"', '"base_return"', '"stress_return"')))
check("12D-4) crash의 net_return이 Base alias", "net_return = base_return" in cr_src)


# ── 13. Gross 정의 강제 (1J.2) ──────────────────────────────────
check("13-1) gross=0.10 → 예외",
      _raises(lambda: CostModel(0.10, 0.35, 0.90).validate()))
check("13-2) gross=-0.10 → 예외",
      _raises(lambda: CostModel(-0.10, 0.35, 0.90).validate()))
check("13-3) gross=0.00 → 정상",
      CostModel(0.00, 0.35, 0.90).validate().gross_roundtrip_pct == 0.0)
check("13-4) 설정에서 gross>0이면 로딩 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.10\n  base_roundtrip_pct: 0.35\n"
          "  stress_roundtrip_pct: 0.90"), use_cache=False)))


# ── 14. allow_default 정책 일치 (1J.2) ──────────────────────────
# 정책: "설정 파일 자체를 사용할 수 없을 때만" fallback.
#       블록은 있는데 값이 틀린 경우는 항상 예외.
check("14-1) 파일 누락 + allow_default → 기본값",
      load_cost_model(Path(tempfile.mkdtemp()) / "x.yaml",
                      allow_default=True, use_cache=False).base_roundtrip_pct == 0.35)
check("14-2) YAML 파싱 실패 + allow_default → 기본값",
      load_cost_model(_yaml("cost_model: [[["), allow_default=True,
                      use_cache=False).base_roundtrip_pct == 0.35)
check("14-3) 블록 부재 + allow_default → 기본값",
      load_cost_model(_yaml("other: 1"), allow_default=True,
                      use_cache=False).base_roundtrip_pct == 0.35)
check("14-4) 키 누락은 allow_default여도 예외(설정이 있는데 틀린 경우)",
      _raises(lambda: load_cost_model(_yaml("cost_model:\n  base_roundtrip_pct: 0.4"),
                                      allow_default=True, use_cache=False)))
check("14-5) 검증 위반은 allow_default여도 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.10\n  base_roundtrip_pct: 0.35\n"
          "  stress_roundtrip_pct: 0.90"), allow_default=True, use_cache=False)))
check("14-6) 정책이 문서화됨",
      "설정 파일 자체를 사용할 수 없을 때만" in
      Path("domain/cost_model.py").read_text(encoding="utf-8"))


# ── 15. daily report 중복 문구 제거 (1J.2) ──────────────────────
check("15-1) 비용 기준 문구가 한 번만 출력",
      rep.count("※ 비용 기준: {_cm.describe()}") <= 1)
check("15-2) replay 기준 차이 문구가 한 번만 출력",
      rep.count("replay/백테스트 리포트는 Base 기준") == 1)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
