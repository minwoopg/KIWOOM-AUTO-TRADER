from __future__ import annotations

"""전략별 손절·트레일링 계산을 공유하는 순수 함수 모음.

2026-09-14 (180초 감시 공백 대응 — 구현 지시서 1번, GPT 재검토 반영):
잔고 장애 중 관측 전용 경로가 "고정 손절"과 "트레일링"만 평가하려면,
정상 전략의 `Strategy.generate_signal()`을 통째로 호출할 수 없다 —
그 함수는 VWAP 이탈·추세 꺾임(③)·안전망 익절(④)까지 함께 계산해서
반환하므로, 반환된 SELL을 그대로 기록하면 "손절·트레일링만 본다"는
범위를 넘어선다. `reason` 문자열에 "손절"/"트레일링"이 들어있는지
검사해 걸러내는 방식도 문구 변경에 취약하고 검사 누락 위험이 있어
채택하지 않는다.

대신 각 전략 파일(`breakout_strategy.py`/`bottom_strategy.py`/
`neutral_strategy.py`)이 실제로 쓰는 손절·트레일링 계산 자체를 이
모듈의 순수 함수로 추출해서, 정상 전략과 장애 관측 경로가 **동일한
함수**를 호출하게 한다. 각 전략의 기존 평가 순서·임계값·반올림·
분기 조건은 정확히 그대로 옮겼다 — 트레일링 구간(tier) 경계값,
`>=`/`>` 비교 방향, `int()` 반올림 위치까지 원본과 동일해야 한다.

`reason` 문자열 포맷은 전략마다 다르므로(예: "[바닥] 손절 — ..." vs
"손절 — ...") 이 모듈은 문자열을 만들지 않는다 — 판정에 필요한
수치만 반환하고, 실제 `Signal.reason` 조립은 각 전략 파일이 기존과
동일하게 담당한다. `HoldStrategy`는 트레일링 자체가 없으므로(항상
`highest_price`를 쓰지 않음) `calc_trailing_stop()`을 호출하지 않는다.

2026-09-14 (GPT 재검토 반영, 완료 처리 전 수정): `calc_stop_loss()`가
처음에는 `current_pnl_pct = (current_price - average_price) /
average_price * 100`까지 함께 계산해 반환했는데, 이건 손절 판정에
필요한 값이 아니라 reason 문자열 조립에만 쓰이는 값이다. `HoldStrategy`
원본은 이 수익률을 아예 계산하지 않는데(익절/손절 가격 비교만 함),
공용 함수가 무조건 나눗셈을 하게 만들어 버리면 `average_price=0`
같은 입력에서 HoldStrategy에 원래 없던 `ZeroDivisionError`가 새로
생긴다 — 순수 리팩터가 새 예외를 추가하면 안 되므로 제거했다.
`Breakout`/`Bottom`/`Neutral`은 원본도 이 수익률을 손절 검사 전에
무조건 계산했으므로(기존에도 `average_price=0`이면 크래시), 이제
각 파일이 그 계산을 직접 하도록 되돌렸다 — 이건 새 회귀가 아니라
원본 그대로다. 무효 `average_price`(0 이하 등)를 어떻게 처리할지는
이번 순수 추출과 분리된 별도 정책 문제로 남겨둔다.

2026-09-14 (2단계 — 구현 지시서 §2.5, GPT 재검토 반영): 1단계
완료 승인과 함께 "다음 단계에서는 전략별 트레일링 설정을 관측
코드에 다시 복사하지 말라"는 지적을 받았다. `TrailingParams`와
`evaluate_exit_candidate()`를 추가해 대응한다 — 각 전략은 자신의
트레일링 시작 배율·구간표를 `trailing_params()` 메서드 하나로만
노출하고(`domain/strategy/base.py`의 `Strategy.trailing_params()`
기본값은 `None`, 트레일링이 있는 전략만 오버라이드), 그 전략의
`generate_signal()`도 같은 메서드를 호출해 값을 얻는다 — 즉 숫자가
두 곳에 따로 존재하지 않고 한 곳(전략 자신)에만 있다. 관측 경로는
`StrategyRouter.select(regime)`으로 정상 경로와 동일하게 전략
객체를 얻은 뒤 `evaluate_exit_candidate(strategy=...)`를 호출한다 —
`generate_signal()` 전체를 부르지 않으므로 VWAP·추세 꺾임·안전망은
평가되지 않는다(이번 관측 범위 밖). 이 함수는 순수 계산만 하고
"청산 필요 후보"를 반환할 뿐 — 주문 제출이나 `highest_price`
갱신에는 관여하지 않는다(둘 다 별도 승인 대상, 아직 비활성).

2026-09-14 (2단계 GPT 재검토, 완료 처리 전 수정 — 2건):
1) `trailing_params()`가 반환하는 `tiers`가 가변 리스트였던 문제를
   `TrailingParams` docstring에 적은 대로 튜플로 바꿔 고쳤다 — 반환값을
   호출자가 수정하면 클래스 상수(다른 인스턴스가 참조하는 값)까지
   바뀌는 경로가 실제로 재현됐었다.
2) `evaluate_exit_candidate()`가 `strategy.trailing_params()`로 얻은
   전략이 실제로 사용됐는지 구분 못 하는 테스트 케이스(REBOUND →
   Neutral과 잘못 연결한 Bottom이 같은 입력에서 둘 다 `None`을
   반환)가 지적됐다 — 두 전략의 트레일링 파라미터가 다른 결과를
   내는 입력으로 테스트를 보강했다(`test_exit_calc_equivalence.py`
   참고).

무효 입력(현재가 0, 평균단가 0인데 최고가는 양수, NaN 등) 처리는
이번에도 다루지 않는다 — 아래 전략들이 원래부터 이런 입력에
대한 검증이 없었고(신선한 시세만 들어온다고 가정), 순수 추출 원칙상
동작을 바꾸지 않았다. 잔고 장애 관측 경로를 실제로 연결하는 다음
단계에서 관측 경계(이 함수를 부르기 전)에 현재가·평균단가 양수·
유한값 검증과 시세 신선도 검증을 추가해, 무효 입력은 "평가 보류
+ 사유"로 별도 기록하고 `None`은 "유효한 입력을 평가했지만 후보
없음"에만 쓰도록 구분해야 한다 — 기존 정상 전략들의 무효 입력
처리 정책 자체는 이번 범위에서도, 다음 단계에서도 바꾸지 않는다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StopLossResult:
    """손절 판정 결과. 모든 전략이 공통으로 쓰는 단일 공식입니다.

    수익률(`current_pnl_pct`)은 여기서 계산하지 않습니다 — 손절
    판정 자체(`stop_loss_price`/`triggered`)에는 필요 없고, reason
    문자열 조립에만 쓰이는 값이라 필요한 전략이 직접 계산합니다
    (`HoldStrategy`처럼 애초에 쓰지 않는 곳까지 나눗셈을 강제하지
    않기 위함 — 위 모듈 docstring 참고).
    """

    stop_loss_price: int
    triggered: bool


def calc_stop_loss(average_price: int, current_price: int, stop_loss_pct: float) -> StopLossResult:
    """평균단가 대비 `stop_loss_pct`만큼 하락하면 손절 트리거.

    기존 4개 전략 파일에서 공통으로 쓰던 계산(`int(average_price *
    (1 - stop_loss_pct / 100))`, `current_price <= stop_loss_price`)을
    그대로 옮긴 것입니다 — 새 공식이 아닙니다.
    """
    stop_loss_price = int(average_price * (1 - stop_loss_pct / 100))
    return StopLossResult(
        stop_loss_price=stop_loss_price,
        triggered=current_price <= stop_loss_price,
    )


@dataclass(frozen=True)
class TrailingResult:
    """트레일링 스탑 판정 결과.

    `active=False`면 아직 트레일링 시작 조건(`highest_price >=
    trailing_start_price and highest_price > 0`)을 만족하지 못한
    상태입니다 — 이때 `trail_pct`/`trailing_stop_price`/`high_pnl_pct`/
    `from_high_pct`는 전부 `None`입니다.
    """

    active: bool
    trailing_start_price: int
    trail_pct: float | None
    trailing_stop_price: int | None
    triggered: bool
    high_pnl_pct: float | None
    from_high_pct: float | None


def _pick_trail_pct(high_pnl_pct: float, tiers: tuple[tuple[float, float], ...]) -> float:
    """`tiers`(내림차순 (임계 high_pnl_pct, trail_pct) 목록)에서 첫 매치를 고릅니다.

    각 전략의 기존 `if high_pnl_pct >= X: ... elif >= Y: ... else: Z`
    사슬을 그대로 표현한 것 — 마지막 tier의 임계값은 `float("-inf")`로
    둬서 항상 매치되도록 합니다(기존 `else` 분기에 대응).
    """
    for threshold, trail_pct in tiers:
        if high_pnl_pct >= threshold:
            return trail_pct
    raise ValueError("tiers must cover the full range (마지막 항목은 -inf여야 함)")


def calc_trailing_stop(
    *,
    average_price: int,
    current_price: int,
    highest_price: int,
    trailing_start_multiplier: float,
    tiers: tuple[tuple[float, float], ...],
) -> TrailingResult:
    """구간형(또는 단일 폭) 트레일링 스탑 계산.

    `trailing_start_multiplier`: 트레일링 시작 기준 배율
      (BreakoutStrategy 1.012 / BottomStrategy 1.03 / NeutralStrategy 1.005).
    `tiers`: high_pnl_pct 구간별 트레일링 폭(%) 목록, 내림차순 정렬,
      마지막 항목은 `(float("-inf"), 폭)`로 전 구간을 커버해야 함.
      BottomStrategy처럼 구간이 없고 항상 같은 폭이면
      `((float("-inf"), config.trailing_stop_pct),)` 하나만 전달합니다.
      튜플(불변)만 받습니다 — 리스트를 넘기지 마십시오(아래
      `TrailingParams.tiers` 및 모듈 docstring의 2단계 GPT 재검토
      참고: 가변 리스트를 공유하면 한 곳에서의 변경이 다른 전략
      인스턴스의 실제 매매 판정에 영향을 줄 수 있었습니다).
    """
    trailing_start_price = int(average_price * trailing_start_multiplier)
    active = highest_price >= trailing_start_price and highest_price > 0
    if not active:
        return TrailingResult(
            active=False,
            trailing_start_price=trailing_start_price,
            trail_pct=None,
            trailing_stop_price=None,
            triggered=False,
            high_pnl_pct=None,
            from_high_pct=None,
        )

    high_pnl_pct = (highest_price - average_price) / average_price * 100
    trail_pct = _pick_trail_pct(high_pnl_pct, tiers)
    trailing_stop_price = int(highest_price * (1 - trail_pct / 100))
    from_high_pct = (current_price - highest_price) / highest_price * 100
    return TrailingResult(
        active=True,
        trailing_start_price=trailing_start_price,
        trail_pct=trail_pct,
        trailing_stop_price=trailing_stop_price,
        triggered=current_price <= trailing_stop_price,
        high_pnl_pct=high_pnl_pct,
        from_high_pct=from_high_pct,
    )


@dataclass(frozen=True)
class TrailingParams:
    """전략이 실제로 쓰는 트레일링 시작 배율·구간표.

    `Strategy.trailing_params()`가 반환하는 값입니다 — 정상 전략의
    `generate_signal()`과 잔고 장애 관측 경로(`evaluate_exit_candidate()`)가
    이 값 하나를 공유해서 씁니다. `tiers`는 `calc_trailing_stop()`이
    받는 형식과 동일(내림차순, 마지막 항목은 `(float("-inf"), 폭)`).

    2026-09-14 (GPT 재검토, 2단계 완료 처리 전 수정): `tiers`를
    `list[tuple[...]]`로 뒀을 때, `Breakout`/`NeutralStrategy`가
    클래스 상수 리스트를 그대로 반환해 호출자가 반환받은 리스트를
    수정하면(예: `params.tiers[0] = (...)`) **클래스 상수 자체가
    바뀌어 다른 인스턴스의 `generate_signal()` 결과까지 달라지는**
    경로가 재현됐다(`frozen=True`는 필드 재대입만 막을 뿐, 필드가
    가리키는 리스트의 내용 변경은 막지 못함). 관측 경로는 이 값을
    읽기만 해야 하므로, `tuple[tuple[float, float], ...]`(불변)로
    바꿔 애초에 항목 대입이 불가능하게 했다 — 클래스 상수도 전부
    튜플 리터럴로 바꿨다(각 전략 파일 참고).
    """

    start_multiplier: float
    tiers: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ExitCandidate:
    """잔고 장애 관측 중 계산된 "청산 필요 후보".

    §2.5: 이 결과는 로그·기록용일 뿐 주문 제출에 직접 연결되지
    않습니다 — 실제 제출 여부는 별도 게이트(§1)와 정책 승인이
    필요합니다. `kind`는 `"STOP_LOSS"` 또는 `"TRAILING"`.
    """

    kind: str
    stop_loss: StopLossResult
    trailing: TrailingResult | None


def evaluate_exit_candidate(
    *,
    strategy,
    average_price: int,
    current_price: int,
    highest_price: int,
    stop_loss_pct: float,
) -> ExitCandidate | None:
    """잔고 장애 관측 중 손절·트레일링만 평가해 청산 후보를 계산합니다.

    `strategy`는 `StrategyRouter.select(regime)`으로 정상 경로와
    동일하게 골라야 합니다(REBOUND를 이름만 보고 BottomStrategy로
    임의 연결하는 등 관측 경로만의 별도 라우팅을 만들지 않습니다).
    이 함수는 `strategy.generate_signal()`을 호출하지 않습니다 —
    호출하면 VWAP 이탈·추세 꺾임·안전망 익절까지 함께 평가되어 범위를
    넘어서기 때문입니다(모듈 docstring 참고). 대신 `strategy.
    trailing_params()`로 그 전략이 실제로 쓰는 시작 배율·구간표만
    꺼내 `calc_trailing_stop()`에 그대로 넘깁니다 — 관측 코드에 숫자를
    다시 옮겨 적지 않습니다.

    손절이 트레일링보다 우선(모든 전략의 원본 평가 순서와 동일)합니다.
    `trailing_params()`가 `None`이면(예: `HoldStrategy`) 트레일링은
    평가하지 않습니다. 아무 조건도 만족하지 않으면 `None`을 반환합니다
    — "청산 후보 아님"이지 "평가 실패"가 아닙니다(호출자가 regime을
    확정하지 못해 이 함수 자체를 부르지 못하는 경우가 "평가 보류"입니다).
    """
    stop_loss = calc_stop_loss(average_price, current_price, stop_loss_pct)
    if stop_loss.triggered:
        return ExitCandidate(kind="STOP_LOSS", stop_loss=stop_loss, trailing=None)

    trailing_params = strategy.trailing_params()
    if trailing_params is not None:
        trailing = calc_trailing_stop(
            average_price=average_price,
            current_price=current_price,
            highest_price=highest_price,
            trailing_start_multiplier=trailing_params.start_multiplier,
            tiers=trailing_params.tiers,
        )
        if trailing.triggered:
            return ExitCandidate(kind="TRAILING", stop_loss=stop_loss, trailing=trailing)

    return None
