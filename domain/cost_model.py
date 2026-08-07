"""거래 비용 모델 — 단일 출처 (2026-08-07, 1J단계)

배경
----
분석기마다 비용을 직접 하드코딩하고 있었고, 값이 서로 달랐습니다.

    infra/storage/daily_reporter.py   COST_RATE = 0.009  (0.90%)
    replay_runner.py                  0.25 + 0.10 = 0.35%
    analyze_crash_rebound_days.py     0.35%
    analyze_v_drop_backtest.py        0.35%  (replay에서 import)
    simulate_pullback_removal.py      0.35%

2026-07에 `COST_RATE`를 0.53% → 0.90%로 정정했을 때 백테스트
스크립트 4개가 따라가지 못한 결과입니다. 0.55%p 차이는 작지
않습니다 — 8/7 승리 거래 평균이 +0.28%였으므로, 0.35% 기준으로
"승리"였던 건이 0.90% 기준에서는 전부 적자가 됩니다.

설계 원칙
--------
**하나의 값으로 고정하지 않습니다.** 실제 체결 비용이 얼마인지
아직 검증되지 않았으므로, 하나를 고르면 그 선택이 모든 결론에
숨은 가정으로 박힙니다. 대신 세 시나리오를 **항상 함께** 계산해
결론이 비용 가정에 얼마나 민감한지 드러냅니다.

    Gross  0.00%  비용 전 원수익 — 신호 자체의 품질
    Base   0.35%  왕복 수수료+세금+슬리피지 추정치
    Stress 0.90%  보수적 상한 (현행 daily_report 기준)

Base와 Stress가 같은 방향이면 결론이 견고한 것이고, 부호가
갈리면 비용 가정에 의존하는 취약한 결론이라는 뜻입니다.

**실제 `trades.csv` 손익은 비용 전 raw PnL로 보존합니다.**
비용은 분석 단계에서만 더합니다 — 원본에 섞으면 나중에 Base를
교정할 때 되돌릴 수 없습니다.

향후 실제 체결 데이터(수수료·세금 필드)가 쌓이면
`config/settings.yaml`의 `cost_model.base_roundtrip_pct`만
고치면 모든 분석기에 동시에 반영됩니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

# 설정 파일에 cost_model이 없을 때 쓰는 기본값.
# 여기가 **유일한** 하드코딩 지점입니다.
DEFAULT_GROSS_ROUNDTRIP_PCT = 0.00
DEFAULT_BASE_ROUNDTRIP_PCT = 0.35
DEFAULT_STRESS_ROUNDTRIP_PCT = 0.90

SCENARIO_ORDER = ("gross", "base", "stress")


@dataclass(frozen=True)
class CostModel:
    """왕복 비용 시나리오 3종."""

    gross_roundtrip_pct: float = DEFAULT_GROSS_ROUNDTRIP_PCT
    base_roundtrip_pct: float = DEFAULT_BASE_ROUNDTRIP_PCT
    stress_roundtrip_pct: float = DEFAULT_STRESS_ROUNDTRIP_PCT

    def cost_pct(self, scenario: str) -> float:
        key = scenario.lower()
        if key not in SCENARIO_ORDER:
            raise ValueError(
                f"알 수 없는 비용 시나리오: {scenario!r} "
                f"(사용 가능: {', '.join(SCENARIO_ORDER)})"
            )
        return float(getattr(self, f"{key}_roundtrip_pct"))

    def net(self, gross_return_pct: float | None, scenario: str) -> float | None:
        """원수익률(%)에서 해당 시나리오 비용을 뺀 순수익률(%)."""
        if gross_return_pct is None:
            return None
        return float(gross_return_pct) - self.cost_pct(scenario)

    def net_all(self, gross_return_pct: float | None) -> dict[str, float | None]:
        """세 시나리오 순수익률을 한 번에."""
        return {s: self.net(gross_return_pct, s) for s in SCENARIO_ORDER}

    def positive_rates(self, gross_returns: Iterable[float | None]) -> dict[str, float | None]:
        """시나리오별 플러스 비율.

        반환 키: gross_positive_rate / base_net_positive_rate /
        stress_net_positive_rate (0~1). 표본이 없으면 None.
        """
        vals = [v for v in gross_returns if v is not None]
        if not vals:
            return {f"{k}_positive_rate" if k == "gross" else f"{k}_net_positive_rate": None
                    for k in SCENARIO_ORDER}
        out: dict[str, float | None] = {}
        for s in SCENARIO_ORDER:
            c = self.cost_pct(s)
            hits = sum(1 for v in vals if (v - c) > 0)
            key = "gross_positive_rate" if s == "gross" else f"{s}_net_positive_rate"
            out[key] = hits / len(vals)
        return out

    def describe(self) -> str:
        return (f"Gross {self.gross_roundtrip_pct:.2f}% / "
                f"Base {self.base_roundtrip_pct:.2f}% / "
                f"Stress {self.stress_roundtrip_pct:.2f}%")

    def format_scenarios(self, gross_return_pct: float | None, indent: str = "  ") -> list[str]:
        """리포트용 3줄 출력.

            Gross  +0.61%
            Base   +0.26%
            Stress -0.29%
        """
        if gross_return_pct is None:
            return [f"{indent}{'Gross':7s}n/a", f"{indent}{'Base':7s}n/a",
                    f"{indent}{'Stress':7s}n/a"]
        nets = self.net_all(gross_return_pct)
        return [f"{indent}{s.capitalize():7s}{nets[s]:+.2f}%" for s in SCENARIO_ORDER]


_CACHED: CostModel | None = None


def load_cost_model(path: str | Path = "config/settings.yaml",
                    *, use_cache: bool = True) -> CostModel:
    """설정에서 비용 모델을 읽습니다. 없으면 기본값.

    설정 예:
        cost_model:
          gross_roundtrip_pct: 0.00
          base_roundtrip_pct: 0.35
          stress_roundtrip_pct: 0.90

    분석 스크립트는 프로젝트 루트가 아닌 곳에서 실행될 수 있으므로,
    파일이 없으면 예외 대신 기본값을 씁니다(분석이 통째로 죽는 것보다
    낫고, 기본값은 이 모듈 상단에 명시돼 있음).
    """
    global _CACHED
    if use_cache and _CACHED is not None:
        return _CACHED

    raw: dict = {}
    p = Path(path)
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw = loaded.get("cost_model") or {}
        except Exception:
            raw = {}

    model = CostModel(
        gross_roundtrip_pct=float(raw.get("gross_roundtrip_pct", DEFAULT_GROSS_ROUNDTRIP_PCT)),
        base_roundtrip_pct=float(raw.get("base_roundtrip_pct", DEFAULT_BASE_ROUNDTRIP_PCT)),
        stress_roundtrip_pct=float(raw.get("stress_roundtrip_pct", DEFAULT_STRESS_ROUNDTRIP_PCT)),
    )
    if use_cache:
        _CACHED = model
    return model


def reset_cache() -> None:
    """테스트에서 설정을 바꿔가며 검증할 때 사용."""
    global _CACHED
    _CACHED = None
