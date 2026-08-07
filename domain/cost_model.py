"""거래 비용 모델 — 단일 출처 (2026-08-07, 1J → 1J.1)

배경
----
분석기마다 비용을 직접 하드코딩하고 값이 서로 달랐습니다.

    infra/storage/daily_reporter.py   COST_RATE = 0.009  (0.90%)
    replay_runner.py 외 3종           0.25 + 0.10 = 0.35%

2026-07에 `COST_RATE`를 0.53% → 0.90%로 정정했을 때 백테스트
스크립트들이 따라가지 못한 결과입니다. 0.55%p 차이는 "승리"와
"적자"를 가르는 크기입니다.

설계 원칙 1 — 하나로 고정하지 않는다
-----------------------------------
실제 체결 비용이 아직 검증되지 않았으므로, 하나를 고르면 그
선택이 모든 결론에 숨은 가정으로 박힙니다. 세 시나리오를 **항상
함께** 계산해 결론이 비용 가정에 얼마나 민감한지 드러냅니다.

    Gross  0.00%  비용 전 원수익 — 신호 자체의 품질
    Base   0.35%  왕복 수수료+세금+슬리피지 추정치
    Stress 0.90%  보수적 상한

7/31 실측: 5분 후 플러스비율이 Base 83% → Stress 33%로 뒤집혔습니다.

설계 원칙 2 — fail-closed (1J.1)
--------------------------------
1J의 로더는 설정을 못 찾거나 파싱에 실패하면 **조용히 기본값으로
돌아갔습니다.** 재현 결과:

    cwd를 프로젝트 밖으로 변경 → 예외 없이 Base 0.35 반환
    YAML이 깨져 있음          → 예외 없이 Base 0.35 반환

나중에 `base_roundtrip_pct: 0.42`로 교정했는데 Windows 작업
디렉터리가 달라 설정을 못 찾으면, 51일 분석이 **아무 경고 없이
다시 0.35%로 돌아갑니다.** 그러면 1J를 한 의미가 없습니다.

그래서 기본 동작을 strict로 바꿨습니다.
  - 설정 경로는 cwd가 아니라 **이 파일 기준 프로젝트 루트**로 해석
  - 설정 파일 누락 → 예외
  - YAML 파싱 실패 → 예외
  - `cost_model` 블록 누락 → 예외
  - 숫자 변환 실패 → 예외
  - `Gross < Base < Stress` 관계 위반 → 예외
  - 음수 비용 → 예외

기본값이 정말 필요한 곳만 `allow_default=True`를 명시합니다.

설계 원칙 3 — roundtrip_pct의 기준금액 (1J.1)
--------------------------------------------
replay는 `gross_return_pct - cost_pct`로 차감하는데 daily_report는
`매도금액 × cost_pct`로 계산했습니다. 둘 다 "0.90%"라 부르지만
기준금액이 달랐습니다(매수 100 → 매도 110이면 매도금액 기준
비용은 진입원금 대비 0.99%).

    roundtrip_pct := 진입 원금(buy notional) 대비 왕복 총비용 추정률

로 정의를 고정합니다. 따라서
  replay:  net_return_pct = gross_return_pct - cost_pct
  daily :  estimated_cost = buy_notional × cost_pct / 100
가 정확히 같은 의미가 됩니다.

실제 수수료·세금·슬리피지 체결 데이터가 확보되면 이 추정 모델을
실제 비용 모델로 대체합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

# 프로젝트 루트 — 이 파일이 <root>/domain/cost_model.py 이므로 두 단계 위.
# cwd에 의존하지 않기 위해 반드시 이 기준을 씁니다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

# `allow_default=True`일 때만 쓰이는 기본값. 여기가 유일한 하드코딩 지점.
DEFAULT_GROSS_ROUNDTRIP_PCT = 0.00
DEFAULT_BASE_ROUNDTRIP_PCT = 0.35
DEFAULT_STRESS_ROUNDTRIP_PCT = 0.90

SCENARIO_ORDER = ("gross", "base", "stress")


class CostModelConfigError(RuntimeError):
    """비용 설정을 신뢰할 수 없을 때 발생합니다.

    백테스트가 잘못된 비용으로 조용히 도는 것보다, 즉시 멈추고
    원인을 드러내는 편이 안전하기 때문에 예외로 처리합니다.
    """


@dataclass(frozen=True)
class CostModel:
    """왕복 비용 시나리오 3종. 기준금액은 진입 원금(buy notional)."""

    gross_roundtrip_pct: float = DEFAULT_GROSS_ROUNDTRIP_PCT
    base_roundtrip_pct: float = DEFAULT_BASE_ROUNDTRIP_PCT
    stress_roundtrip_pct: float = DEFAULT_STRESS_ROUNDTRIP_PCT

    def validate(self) -> "CostModel":
        vals = {s: getattr(self, f"{s}_roundtrip_pct") for s in SCENARIO_ORDER}
        for name, v in vals.items():
            if v < 0:
                raise CostModelConfigError(
                    f"비용은 음수일 수 없습니다: {name}_roundtrip_pct={v}")
        if not (vals["gross"] < vals["base"] < vals["stress"]):
            raise CostModelConfigError(
                "비용 시나리오는 Gross < Base < Stress 여야 합니다: "
                f"gross={vals['gross']}, base={vals['base']}, stress={vals['stress']}")
        return self

    def cost_pct(self, scenario: str) -> float:
        key = str(scenario).lower()
        if key not in SCENARIO_ORDER:
            raise ValueError(
                f"알 수 없는 비용 시나리오: {scenario!r} "
                f"(사용 가능: {', '.join(SCENARIO_ORDER)})")
        return float(getattr(self, f"{key}_roundtrip_pct"))

    def net(self, gross_return_pct: float | None, scenario: str) -> float | None:
        """원수익률(%)에서 해당 시나리오 비용을 뺀 순수익률(%)."""
        if gross_return_pct is None:
            return None
        return float(gross_return_pct) - self.cost_pct(scenario)

    def net_all(self, gross_return_pct: float | None) -> dict[str, float | None]:
        return {s: self.net(gross_return_pct, s) for s in SCENARIO_ORDER}

    def cost_amount(self, buy_notional: float, scenario: str) -> float:
        """진입 원금 대비 비용 금액.

        1J.1에서 기준금액을 buy notional로 고정했습니다 — replay의
        `gross - cost_pct`와 정확히 같은 의미가 되도록.
        """
        return float(buy_notional) * self.cost_pct(scenario) / 100.0

    @staticmethod
    def positive_rate_key(scenario: str) -> str:
        return "gross_positive_rate" if scenario == "gross" else f"{scenario}_net_positive_rate"

    def positive_rates(self, gross_returns: Iterable[float | None]) -> dict[str, float | None]:
        """시나리오별 플러스 비율(0~1). 표본이 없으면 None."""
        vals = [v for v in gross_returns if v is not None]
        if not vals:
            return {self.positive_rate_key(s): None for s in SCENARIO_ORDER}
        out: dict[str, float | None] = {}
        for s in SCENARIO_ORDER:
            c = self.cost_pct(s)
            out[self.positive_rate_key(s)] = sum(1 for v in vals if (v - c) > 0) / len(vals)
        return out

    def describe(self) -> str:
        return (f"Gross {self.gross_roundtrip_pct:.2f}% / "
                f"Base {self.base_roundtrip_pct:.2f}% / "
                f"Stress {self.stress_roundtrip_pct:.2f}% (기준: 진입 원금)")

    def scenario_lines(self, gross_returns: Iterable[float | None],
                       indent: str = "           ") -> list[str]:
        """분석기 공용 3시나리오 출력 — 평균 + 플러스비율.

            Gross  +0.71%   플러스비율 83%
            Base   +0.36%   플러스비율 83%
            Stress -0.19%   플러스비율 33%
        """
        vals = [v for v in gross_returns if v is not None]
        if not vals:
            return [f"{indent}{s.capitalize():7s}n/a" for s in SCENARIO_ORDER]
        avg = sum(vals) / len(vals)
        rates = self.positive_rates(vals)
        lines = []
        for s in SCENARIO_ORDER:
            r = rates[self.positive_rate_key(s)]
            lines.append(f"{indent}{s.capitalize():7s}{avg - self.cost_pct(s):+.2f}%"
                         f"   플러스비율 {r * 100:.0f}%")
        return lines


# resolved path별 캐시 — 1J의 전역 단일 캐시는 서로 다른 설정을
# 순서대로 읽을 때 첫 번째 값을 계속 돌려줬습니다(재현 확인).
# 1K에서 여러 설정으로 실험할 수 있으므로 경로별로 분리합니다.
_CACHE: dict[Path, CostModel] = {}


def load_cost_model(path: str | Path | None = None, *,
                    allow_default: bool = False,
                    use_cache: bool = True) -> CostModel:
    """설정에서 비용 모델을 읽습니다 (기본 strict).

    path가 None이면 **cwd가 아니라 프로젝트 루트 기준**
    `<root>/config/settings.yaml`을 씁니다. 상대경로가 주어졌고
    cwd에 없으면 프로젝트 루트 기준으로 한 번 더 시도합니다.

    allow_default=False(기본)에서는 설정 누락·파싱 실패·블록 누락·
    숫자 변환 실패·시나리오 순서 위반·음수 비용에 모두 예외를
    던집니다. 백테스트가 잘못된 비용으로 조용히 도는 것을 막기
    위함입니다.
    """
    if path is None:
        resolved = DEFAULT_SETTINGS_PATH
    else:
        p = Path(path)
        resolved = p if p.is_absolute() else (p if p.exists() else PROJECT_ROOT / p)
    resolved = resolved.resolve() if resolved.exists() else resolved

    if use_cache and resolved in _CACHE:
        return _CACHE[resolved]

    if not resolved.exists():
        if allow_default:
            return CostModel().validate()
        raise CostModelConfigError(
            f"비용 설정 파일을 찾을 수 없습니다: {resolved}\n"
            f"(프로젝트 루트: {PROJECT_ROOT}) — 백테스트가 잘못된 비용으로 "
            f"조용히 도는 것을 막기 위해 기본값으로 대체하지 않습니다.")

    try:
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        if allow_default:
            return CostModel().validate()
        raise CostModelConfigError(f"비용 설정 YAML 파싱 실패: {resolved} — {exc}") from exc

    raw = (loaded or {}).get("cost_model") if isinstance(loaded, dict) else None
    if not isinstance(raw, dict):
        if allow_default:
            return CostModel().validate()
        raise CostModelConfigError(
            f"설정에 cost_model 블록이 없습니다: {resolved}\n"
            f"필요한 형식:\n"
            f"  cost_model:\n"
            f"    gross_roundtrip_pct: 0.00\n"
            f"    base_roundtrip_pct: 0.35\n"
            f"    stress_roundtrip_pct: 0.90")

    try:
        model = CostModel(
            gross_roundtrip_pct=float(raw["gross_roundtrip_pct"]),
            base_roundtrip_pct=float(raw["base_roundtrip_pct"]),
            stress_roundtrip_pct=float(raw["stress_roundtrip_pct"]),
        )
    except KeyError as exc:
        raise CostModelConfigError(
            f"cost_model에 필수 키가 없습니다: {exc} ({resolved})") from exc
    except (TypeError, ValueError) as exc:
        raise CostModelConfigError(
            f"cost_model 값을 숫자로 변환할 수 없습니다: {raw} ({resolved})") from exc

    model.validate()
    if use_cache:
        _CACHE[resolved] = model
    return model


def reset_cache() -> None:
    """테스트에서 설정을 바꿔가며 검증할 때 사용."""
    _CACHE.clear()
