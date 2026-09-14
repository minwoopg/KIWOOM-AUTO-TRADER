"""exit_calc.py 추출 이후 4개 전략의 손절·트레일링 SELL/HOLD 판정이
추출 이전과 완전히 동일한지 검증합니다.

2026-09-14 (180초 감시 공백 대응 — 구현 지시서 1번, GPT 재검토 반영):
`domain/strategy/exit_calc.py`로 손절·트레일링 계산을 뽑아내면서
"정상 전략의 기존 평가 순서·임계값·반올림·반환 사유는 유지한다"는
요구를 지켰는지 확인하는 것이 이 파일의 유일한 목적입니다.

방법: 각 전략의 "보유 중" 가격 기반 판단 부분을 리팩터 이전 코드
그대로(exit_calc.py를 전혀 참조하지 않고) 이 파일 안에 독립적으로
재구현한 `_reference_*` 함수와, 실제 리팩터된 전략 객체의
`generate_signal()` 출력을 같은 입력 그리드에 대해 비교합니다.
경계값(손절가 정확히 일치, 트레일링 시작가 정확히 일치, 구간 경계
수익률 정확히 일치, 트레일링 스탑가 바로 아래/동일/바로 위)과 복수의
평균단가(정수 배 아닌 값 포함)를 포함합니다. 하나라도 다르면 이
테스트가 실패해야 합니다 — 즉 이 파일은 exit_calc.py와 무관하게
"원본 공식"을 다시 손으로 옮겨 적은 것이라, exit_calc.py 자체의
버그도 잡아낼 수 있습니다(자기 자신과 비교하는 순환 검증이 아님).

RSI/MACD/VWAP/거래대금 등에 의존하는 매수 판단, ③ 추세 꺾임, ④
안전망 익절은 이번 추출 대상이 아니므로(변경하지 않았으므로) 이
파일에서 별도로 재검증하지 않습니다 — 이 조건들이 손절·트레일링보다
먼저 SELL/HOLD을 내지 않도록, current_pnl_pct < 0.5%(추세 꺾임 비활성
구간)이고 current_price < safety_net_price(안전망 미도달)인 값만
그리드에 사용해 ①②만 순수하게 격리해서 비교합니다. 이 필터 안에서는
①②만으로 최종 SELL/HOLD가 완전히 결정되므로, `_reference_*` 함수는
"판정 불가"를 뜻하는 `(None, None)`을 반환하지 않고 항상 구체적인
타입·문구를 반환합니다 — HOLD로 끝나는 경로(트레일링 추적 중/보유
유지)도 전부 비교 대상에 포함합니다.

2026-09-14 (GPT 재검토, 완료 처리 전 수정 — 2건):
1) `run_regression_tests.py`는 pytest가 아니라 각 test_*.py를
   `subprocess`로 그대로 실행하는 방식이라, 이전 버전(pytest 스타일
   클래스·bare assert만 있고 직접 실행 진입점이 없던 버전)은 아무
   것도 실행하지 않고 조용히 종료 코드 0으로 끝나 "PASS"로 오인될 수
   있었습니다. 프로젝트 관례(unittest.TestCase + `if __name__ ==
   "__main__": unittest.main()`, 예: test_delayed_eval_candidate_
   observation.py)에 맞춰 다시 작성했습니다 — 이제 `python
   test_exit_calc_equivalence.py`만 실행해도 unittest가 실행한 테스트
   개수를 stderr에 출력하고, 실패 시 0이 아닌 종료 코드를 반환합니다.
2) 손절·트레일링 참조 함수의 HOLD 경로 일부(트레일링 추적 중/보유
   유지)가 비교 대상에서 빠져 있던 것을 채웠고, 평균단가를 여러 값
   (정수 배 아닌 값 포함)으로 확장했습니다.

2026-09-14 (2단계 — 구현 지시서 §2.5): `TestTrailingParamsSingleSource`와
`TestEvaluateExitCandidate`를 추가했습니다. 위 `Test*ExitEquivalence`가
`generate_signal()`의 최종 출력으로 트레일링 계산이 기존과 같은지
간접 검증한다면, 이 두 클래스는 (1) `trailing_params()`가 각 전략의
실제 값(또는 HoldStrategy의 None)을 정확히 반환하는지, (2)
`evaluate_exit_candidate()`가 `StrategyRouter.select(regime)`으로
얻은 실제 전략 객체를 그대로 써서 손절 우선순위·트레일링 판정·
HoldStrategy의 "트레일링 후보 절대 없음"을 지키는지를 직접
확인합니다 — 특히 REBOUND가 라우터에서 실제로는 NeutralStrategy로
연결된다는 사실을 관측 경로가 무시하고 BottomStrategy로 별도
연결하지 않는지까지 함께 검증합니다.

2026-09-14 (2단계 GPT 재검토, 완료 처리 전 수정 — 2건):
1) `TrailingParams.tiers`가 가변 리스트였을 때, 반환값을 호출자가
   수정하면 클래스 상수 자체가 바뀌어 **다른 전략 인스턴스**의
   `generate_signal()` 판정까지 달라지는 경로가 재현됐다(직접
   재현 확인 — 정상 케이스가 SELL에서 HOLD로 바뀜). `tiers`를
   튜플로 바꾼 뒤 `test_returned_tiers_are_immutable_and_shared_
   safely`로 항목 대입이 TypeError로 막히는지, 서로 다른 인스턴스가
   여전히 같은 값으로 정확히 판정하는지 고정했다.
2) `test_rebound_regime_uses_router_selected_strategy_not_bottom_
   by_name`의 기존 입력(highest_price=1.01배, current_price=
   highest_price)은 NeutralStrategy(트레일링 활성·미트리거→None)와
   잘못 연결된 BottomStrategy(트레일링 비활성→None)가 둘 다 None을
   반환해 실제 연결을 구별하지 못했다. current_price=9,950으로
   바꿔 NeutralStrategy면 TRAILING, BottomStrategy면 None으로
   갈라지도록 보강하고, 대조군으로 BottomStrategy 직접 계산도
   같은 테스트에서 확인한다.

무효 입력(현재가 0, 평균단가 0 등) 처리는 이번에도 다루지 않는다 —
`exit_calc.py` 모듈 docstring 참고. 잔고 장애 관측 경로를 실제
연결하는 다음 단계에서 관측 경계에 입력 검증을 추가하기로 했다.
"""

import unittest
from datetime import datetime

from config.settings import StrategyConfig
from domain.models import MarketPrice, MarketRegime, Position, SignalType
from domain.strategy.bottom_strategy import BottomStrategy
from domain.strategy.breakout_strategy import BreakoutStrategy
from domain.strategy.exit_calc import evaluate_exit_candidate
from domain.strategy.hold_strategy import HoldStrategy
from domain.strategy.neutral_strategy import NeutralStrategy
from domain.strategy.strategy_router import StrategyRouter


def _market_price(symbol: str, current_price) -> MarketPrice:
    # ③ 추세 꺾임이 끼어들지 않도록 지표를 전부 "SELL 쪽으로 기울지
    # 않는" 값으로 고정합니다(RSI None → has_indicators 관련 분기와
    # 무관하게 ①②만 격리).
    return MarketPrice(
        symbol=symbol,
        current_price=current_price,
        reference_price=current_price,
        previous_close=current_price,
        timestamp=datetime.now(),
        indicator_rsi=None,
        indicator_macd=None,
        indicator_macd_signal=None,
    )


def _market_price_for_bottom(symbol: str, current_price) -> MarketPrice:
    """BottomStrategy 전용 — 이 리팩터와 무관한 별개 발견 사항에 대한 우회.

    2026-09-14: `BottomStrategy.generate_signal()`은 position 분기보다
    앞에서 `market_price.indicator_rsi_signal_cross` /
    `indicator_volume_exhaustion` / `indicator_volume_buying`을 무조건
    읽는데, 이 세 필드는 `domain/models.py`의 `MarketPrice`
    dataclass에 더 이상 존재하지 않는다(2026-05-21 "NEUTRAL 장세 추가"
    커밋 8093acd에서 관련 없는 docstring 정리 중 실수로 삭제된 것으로
    보임 — git blame으로 확인). `BottomStrategy`를 직접 호출하면 이
    필드 부재로 즉시 `AttributeError`가 난다 — 다만 실제
    `StrategyRouter.select()`는 REBOUND일 때 이 전략이 아니라
    `NeutralStrategy`를 반환하도록 이미 우회되어 있어(주석 "BottomStrategy는
    MarketPrice 필드 미완성 — 임시로 NeutralStrategy 사용"), 현재
    운영 경로에서는 이 크래시가 발생하지 않는다 — 즉 "비활성 상태인
    BottomStrategy를 직접 호출할 때만" 재현되는 결함이다. 이 테스트가
    검증하려는 것은 손절·트레일링 추출의 동등성이지 이 별개 결함의
    수정이 아니므로, 여기서는 frozen dataclass에 `object.__setattr__`로
    해당 필드를 임시로 얹어 우회만 한다 — 실제 models.py는 건드리지
    않는다.
    """
    mp = _market_price(symbol, current_price)
    object.__setattr__(mp, "indicator_rsi_signal_cross", 0)
    object.__setattr__(mp, "indicator_volume_exhaustion", False)
    object.__setattr__(mp, "indicator_volume_buying", False)
    return mp


# ── 원본 공식을 exit_calc.py와 무관하게 손으로 재구현(리팩터 이전 코드 그대로) ──
# 아래 함수들은 ①②만으로 최종 판정이 끝나는 필터링된 그리드 안에서만
# 호출되므로, 원본 코드가 실제로 도달하는 모든 return(HOLD 포함)을
# 빠짐없이 재현합니다.

def _reference_breakout(average_price, current_price, highest_price: int, cfg: StrategyConfig):
    stop_loss_price = int(average_price * (1 - cfg.stop_loss_pct / 100))
    current_pnl_pct = (current_price - average_price) / average_price * 100
    if current_price <= stop_loss_price:
        return SignalType.SELL, f"손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)"

    trailing_start_price = int(average_price * 1.012)
    if highest_price >= trailing_start_price and highest_price > 0:
        high_pnl_pct = (highest_price - average_price) / average_price * 100
        if high_pnl_pct >= 5.0:
            trail_pct = 2.8
        elif high_pnl_pct >= 3.5:
            trail_pct = 2.2
        elif high_pnl_pct >= 2.0:
            trail_pct = 1.8
        else:
            trail_pct = 1.5
        trailing_stop_price = int(highest_price * (1 - trail_pct / 100))
        from_high_pct = (current_price - highest_price) / highest_price * 100
        if current_price <= trailing_stop_price:
            return SignalType.SELL, (
                f"트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high_pct:.1f}% 하락 "
                f"(트레일링 폭 -{trail_pct:.1f}% / 보유 수익 {current_pnl_pct:+.1f}%)"
            )
        # ③④는 필터링으로 이번 그리드에서 발동하지 않으므로, 원본은
        # 함수 맨 끝의 "트레일링 스탑 진행 상황 표시" 블록에 도달한다
        # (같은 trailing_start_price/tier를 재계산 — 원본과 동일 수치).
        return SignalType.HOLD, (
            f"트레일링 추적 중 — 최고가 {highest_price:,}원 / "
            f"스탑 {trailing_stop_price:,}원 (폭 -{trail_pct:.1f}%) / 현재 {current_pnl_pct:+.1f}%"
        )

    return SignalType.HOLD, (
        f"보유 유지 {current_pnl_pct:+.1f}% — "
        f"트레일링 시작까지 +{cfg.trailing_start_pct:.0f}% 필요 / "
        f"손절 {stop_loss_price:,}원"
    )


def _reference_bottom(average_price, current_price, highest_price: int, cfg: StrategyConfig):
    stop_loss_price = int(average_price * (1 - cfg.stop_loss_pct / 100))
    current_pnl_pct = (current_price - average_price) / average_price * 100
    if current_price <= stop_loss_price:
        return SignalType.SELL, f"[바닥] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)"

    trailing_start = int(average_price * 1.03)
    if highest_price >= trailing_start and highest_price > 0:
        trailing_stop = int(highest_price * (1 - cfg.trailing_stop_pct / 100))
        from_high = (current_price - highest_price) / highest_price * 100
        if current_price <= trailing_stop:
            return SignalType.SELL, (
                f"[바닥] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high:.1f}% "
                f"(보유 {current_pnl_pct:+.1f}%)"
            )
        return SignalType.HOLD, (
            f"[바닥] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
            f"스탑 {trailing_stop:,}원 / 현재 {current_pnl_pct:+.1f}%"
        )

    return SignalType.HOLD, (
        f"[바닥] 보유 유지 {current_pnl_pct:+.1f}% — 트레일링 시작까지 +3% 필요 / 손절 {stop_loss_price:,}원"
    )


def _reference_neutral(average_price, current_price, highest_price: int, cfg: StrategyConfig):
    stop_loss_price = int(average_price * (1 - cfg.stop_loss_pct / 100))
    current_pnl_pct = (current_price - average_price) / average_price * 100
    if current_price <= stop_loss_price:
        return SignalType.SELL, f"[중립] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)"

    trailing_start_price = int(average_price * 1.005)
    if highest_price >= trailing_start_price and highest_price > 0:
        high_pnl_pct = (highest_price - average_price) / average_price * 100
        if high_pnl_pct >= 3.0:
            trail_pct = 2.0
        elif high_pnl_pct >= 2.0:
            trail_pct = 1.5
        elif high_pnl_pct >= 1.0:
            trail_pct = 1.2
        else:
            trail_pct = 0.8
        trailing_stop = int(highest_price * (1 - trail_pct / 100))
        from_high_pct = (current_price - highest_price) / highest_price * 100
        if current_price <= trailing_stop:
            return SignalType.SELL, (
                f"[중립] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high_pct:.1f}% 하락 "
                f"(트레일링 폭 -{trail_pct:.1f}% / 보유 수익 {current_pnl_pct:+.1f}%)"
            )
        return SignalType.HOLD, (
            f"[중립] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
            f"스탑 {trailing_stop:,}원 (폭 -{trail_pct:.1f}%) / 현재 {current_pnl_pct:+.1f}%"
        )

    return SignalType.HOLD, (
        f"[중립] 보유 유지 {current_pnl_pct:+.1f}% — "
        f"트레일링 시작까지 +{cfg.trailing_start_pct:.0f}% 필요 / "
        f"손절 {stop_loss_price:,}원"
    )


def _reference_hold(average_price, current_price, cfg: StrategyConfig, regime_label: str):
    take_profit_price = int(average_price * (1 + cfg.take_profit_pct / 100))
    stop_loss_price = int(average_price * (1 - cfg.stop_loss_pct / 100))
    if current_price >= take_profit_price:
        return SignalType.SELL, f"{regime_label}이지만 익절 목표 {take_profit_price:,}원 도달 — 매도합니다"
    if current_price <= stop_loss_price:
        return SignalType.SELL, f"{regime_label} + 손절 기준 {stop_loss_price:,}원 하회 — 손절합니다"
    return SignalType.HOLD, (
        f"{regime_label} — 익절 {take_profit_price:,}원 / 손절 {stop_loss_price:,}원 사이에서 유지합니다"
    )


def _price_grid(average_price) -> list[int]:
    """손절가·트레일링 경계 근방을 촘촘히 포함하는 현재가 그리드."""
    sl = int(average_price * 0.985)  # stop_loss_pct=1.5% 기준 경계 부근
    grid = set()
    for base in (average_price, sl):
        for delta in (-3, -2, -1, 0, 1, 2, 3, 10, 50):
            grid.add(int(base) + delta)
    grid.add(int(average_price * 1.20))  # 안전망(take_profit 20%) 근처 넘지 않는 값도 포함
    return sorted(p for p in grid if p > 0)


def _highest_price_grid(average_price) -> list[int]:
    """트레일링 시작가·구간 경계(수익률 0.5/1.0/1.2/2.0/3.0/3.5/5.0%) 근방."""
    pcts = [0.0, 0.5, 1.0, 1.2, 1.3, 2.0, 3.0, 3.5, 5.0, 6.0]
    grid = set()
    for pct in pcts:
        base = int(average_price * (1 + pct / 100))
        for delta in (-1, 0, 1):
            grid.add(base + delta)
    grid.add(0)
    return sorted(p for p in grid if p >= 0)


# 평균단가를 여러 값(정수 배 아닌 값 포함)으로 확장 — 특정 라운드
# 넘버에서만 우연히 일치하는 것을 방지.
AVERAGE_PRICES = [10_000, 13_337]


class TestBreakoutExitEquivalence(unittest.TestCase):
    def _cfg(self) -> StrategyConfig:
        return StrategyConfig(
            name="breakout", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.0, trend_reversal_rsi=70.0,
        )

    def test_grid_matches_reference_exactly(self) -> None:
        cfg = self._cfg()
        strategy = BreakoutStrategy(cfg)
        checked = 0
        for average_price in AVERAGE_PRICES:
            position = Position(symbol="005930", quantity=10, average_price=average_price)
            safety_net_price = int(average_price * (1 + cfg.take_profit_pct / 100))
            for current_price in _price_grid(average_price):
                if current_price >= safety_net_price:
                    continue  # ④ 안전망 구간은 이번 추출 대상이 아니므로 격리
                current_pnl_pct = (current_price - average_price) / average_price * 100
                if current_pnl_pct >= 0.5:
                    continue  # ③ 추세 꺾임이 활성화될 수 있는 구간은 격리
                for highest_price in _highest_price_grid(average_price):
                    expected_type, expected_reason = _reference_breakout(
                        average_price, current_price, highest_price, cfg
                    )
                    market_price = _market_price("005930", current_price)
                    signal = strategy.generate_signal(market_price, position, None, highest_price)
                    self.assertEqual(
                        signal.type, expected_type,
                        f"type mismatch avg={average_price} cur={current_price} high={highest_price}: "
                        f"got {signal.type} expected {expected_type} (reason={signal.reason!r})",
                    )
                    self.assertEqual(
                        signal.reason, expected_reason,
                        f"reason mismatch avg={average_price} cur={current_price} high={highest_price}",
                    )
                    checked += 1
        self.assertGreater(checked, 40, "격리 조건이 과해 실제로 비교된 케이스가 거의 없음")

    def test_trailing_stop_price_exact_boundary(self) -> None:
        """트레일링 스탑가 바로 아래/동일/바로 위에서 SELL/HOLD 경계가 정확한지."""
        cfg = self._cfg()
        strategy = BreakoutStrategy(cfg)
        average_price = 10_000
        position = Position(symbol="005930", quantity=10, average_price=average_price)
        highest_price = int(average_price * 1.06)  # high_pnl_pct=6.0% → trail_pct=2.8%
        trailing_stop_price = int(highest_price * (1 - 2.8 / 100))

        for current_price, expect_sell in (
            (trailing_stop_price - 1, True),
            (trailing_stop_price, True),
            (trailing_stop_price + 1, False),
        ):
            market_price = _market_price("005930", current_price)
            signal = strategy.generate_signal(market_price, position, None, highest_price)
            if expect_sell:
                self.assertEqual(signal.type, SignalType.SELL, f"cur={current_price}")
            else:
                self.assertEqual(signal.type, SignalType.HOLD, f"cur={current_price}")


class TestBottomExitEquivalence(unittest.TestCase):
    def _cfg(self) -> StrategyConfig:
        return StrategyConfig(
            name="bottom", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.0, trend_reversal_rsi=70.0,
        )

    def test_grid_matches_reference_exactly(self) -> None:
        cfg = self._cfg()
        strategy = BottomStrategy(cfg)
        checked = 0
        for average_price in AVERAGE_PRICES:
            position = Position(symbol="005930", quantity=10, average_price=average_price)
            safety_net_price = int(average_price * (1 + cfg.take_profit_pct / 100))
            for current_price in _price_grid(average_price):
                if current_price >= safety_net_price:
                    continue
                for highest_price in _highest_price_grid(average_price):
                    expected_type, expected_reason = _reference_bottom(
                        average_price, current_price, highest_price, cfg
                    )
                    market_price = _market_price_for_bottom("005930", current_price)
                    signal = strategy.generate_signal(market_price, position, None, highest_price)
                    self.assertEqual(signal.type, expected_type)
                    self.assertEqual(signal.reason, expected_reason)
                    checked += 1
        self.assertGreater(checked, 40)


class TestNeutralExitEquivalence(unittest.TestCase):
    def _cfg(self) -> StrategyConfig:
        return StrategyConfig(
            name="neutral", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.0, trend_reversal_rsi=70.0,
        )

    def test_grid_matches_reference_exactly(self) -> None:
        cfg = self._cfg()
        strategy = NeutralStrategy(cfg)
        checked = 0
        for average_price in AVERAGE_PRICES:
            position = Position(symbol="005930", quantity=10, average_price=average_price)
            safety_net_price = int(average_price * (1 + cfg.take_profit_pct / 100))
            for current_price in _price_grid(average_price):
                if current_price >= safety_net_price:
                    continue
                current_pnl_pct = (current_price - average_price) / average_price * 100
                if current_pnl_pct >= 0.5:
                    continue
                for highest_price in _highest_price_grid(average_price):
                    expected_type, expected_reason = _reference_neutral(
                        average_price, current_price, highest_price, cfg
                    )
                    market_price = _market_price("005930", current_price)
                    signal = strategy.generate_signal(market_price, position, None, highest_price)
                    self.assertEqual(signal.type, expected_type)
                    self.assertEqual(signal.reason, expected_reason)
                    checked += 1
        self.assertGreater(checked, 40)


class TestHoldExitEquivalence(unittest.TestCase):
    def _cfg(self) -> StrategyConfig:
        return StrategyConfig(
            name="hold", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.0, trend_reversal_rsi=70.0,
        )

    def test_grid_matches_reference_exactly(self) -> None:
        cfg = self._cfg()
        regime_label = "횡보/하락장"
        strategy = HoldStrategy(cfg, regime_label=regime_label)
        checked = 0
        for average_price in AVERAGE_PRICES:
            position = Position(symbol="005930", quantity=10, average_price=average_price)
            for current_price in _price_grid(average_price):
                expected_type, expected_reason = _reference_hold(average_price, current_price, cfg, regime_label)
                market_price = _market_price("005930", current_price)
                signal = strategy.generate_signal(market_price, position, None, 0)
                self.assertEqual(signal.type, expected_type)
                self.assertEqual(signal.reason, expected_reason)
                checked += 1
        self.assertGreater(checked, 10)

    def test_zero_average_price_does_not_raise(self) -> None:
        """2026-09-14 (GPT 재검토 발견, 회귀 수정 확인): calc_stop_loss()가
        수익률까지 계산하던 이전 버전에서는 average_price=0인 HoldStrategy
        입력에 원본에 없던 ZeroDivisionError가 새로 생겼다. 원본은 이
        입력에서 크래시 없이 SELL(익절)을 반환했으므로, 수정 후에도 그
        동작이 그대로인지 고정한다."""
        cfg = self._cfg()
        strategy = HoldStrategy(cfg, regime_label="횡보장")
        position = Position(symbol="005930", quantity=1, average_price=0)
        market_price = _market_price("005930", 10_000)
        signal = strategy.generate_signal(market_price, position, None, 0)
        self.assertEqual(signal.type, SignalType.SELL)
        self.assertEqual(signal.reason, "횡보장이지만 익절 목표 0원 도달 — 매도합니다")


class TestExitCalcUnitBoundaries(unittest.TestCase):
    """exit_calc.py 순수 함수 자체의 경계값 동작(>= 비교 방향, tier 선택)."""

    def test_stop_loss_boundary_exactly_at_threshold_triggers(self) -> None:
        from domain.strategy.exit_calc import calc_stop_loss

        result = calc_stop_loss(average_price=10_000, current_price=9_850, stop_loss_pct=1.5)
        self.assertEqual(result.stop_loss_price, 9_850)
        self.assertTrue(result.triggered)  # current_price <= stop_loss_price, 경계 포함

        result2 = calc_stop_loss(average_price=10_000, current_price=9_851, stop_loss_pct=1.5)
        self.assertFalse(result2.triggered)

    def test_stop_loss_zero_average_price_does_not_raise(self) -> None:
        from domain.strategy.exit_calc import calc_stop_loss

        result = calc_stop_loss(average_price=0, current_price=10_000, stop_loss_pct=1.5)
        self.assertEqual(result.stop_loss_price, 0)
        self.assertFalse(result.triggered)

    def test_trailing_tier_boundary_uses_higher_tier_when_equal(self) -> None:
        from domain.strategy.exit_calc import calc_trailing_stop

        average_price = 10_000
        # high_pnl_pct 정확히 5.0%인 지점 — breakout 기준 trail_pct=2.8이어야 함(>=)
        highest_price = int(average_price * 1.05)
        result = calc_trailing_stop(
            average_price=average_price, current_price=highest_price, highest_price=highest_price,
            trailing_start_multiplier=1.012,
            tiers=((5.0, 2.8), (3.5, 2.2), (2.0, 1.8), (float("-inf"), 1.5)),
        )
        self.assertAlmostEqual(result.high_pnl_pct, 5.0)
        self.assertEqual(result.trail_pct, 2.8)

    def test_trailing_inactive_before_start_price(self) -> None:
        from domain.strategy.exit_calc import calc_trailing_stop

        average_price = 10_000
        result = calc_trailing_stop(
            average_price=average_price, current_price=average_price, highest_price=average_price,
            trailing_start_multiplier=1.012,
            tiers=((float("-inf"), 1.5),),
        )
        self.assertFalse(result.active)
        self.assertIsNone(result.trail_pct)
        self.assertFalse(result.triggered)


class TestTrailingParamsSingleSource(unittest.TestCase):
    """2단계(구현 지시서 §2.5): 전략별 트레일링 시작 배율·구간표가
    `trailing_params()` 한 곳에만 있고, `generate_signal()`도 같은
    값을 읽는지 확인합니다. 값 자체가 기존과 동일한지는 위
    `Test*ExitEquivalence`가 이미 `generate_signal()` 출력으로
    간접 검증하므로, 여기서는 `trailing_params()`가 그 값과 정확히
    일치하는 객체를 반환하는지를 직접 확인합니다."""

    def _cfg(self, trailing_stop_pct: float = 2.0) -> StrategyConfig:
        return StrategyConfig(
            name="x", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=trailing_stop_pct, trailing_start_pct=1.0,
            trend_reversal_rsi=70.0,
        )

    def test_breakout_trailing_params(self) -> None:
        params = BreakoutStrategy(self._cfg()).trailing_params()
        self.assertIsNotNone(params)
        self.assertAlmostEqual(params.start_multiplier, 1.012)
        self.assertEqual(params.tiers, ((5.0, 2.8), (3.5, 2.2), (2.0, 1.8), (float("-inf"), 1.5)))

    def test_neutral_trailing_params(self) -> None:
        params = NeutralStrategy(self._cfg()).trailing_params()
        self.assertIsNotNone(params)
        self.assertAlmostEqual(params.start_multiplier, 1.005)
        self.assertEqual(params.tiers, ((3.0, 2.0), (2.0, 1.5), (1.0, 1.2), (float("-inf"), 0.8)))

    def test_bottom_trailing_params_reads_config_each_call(self) -> None:
        """BottomStrategy는 트레일링 폭이 config.trailing_stop_pct에
        달려 있어 고정 상수로 둘 수 없다 — 서로 다른 config로 만든
        두 인스턴스가 서로 다른 tiers를 반환하는지 확인합니다."""
        params_a = BottomStrategy(self._cfg(trailing_stop_pct=2.0)).trailing_params()
        params_b = BottomStrategy(self._cfg(trailing_stop_pct=4.5)).trailing_params()
        self.assertAlmostEqual(params_a.start_multiplier, 1.03)
        self.assertAlmostEqual(params_b.start_multiplier, 1.03)
        self.assertEqual(params_a.tiers, ((float("-inf"), 2.0),))
        self.assertEqual(params_b.tiers, ((float("-inf"), 4.5),))

    def test_hold_trailing_params_is_none(self) -> None:
        """HoldStrategy는 트레일링 자체가 없다 — highest_price를 쓰지
        않으므로 trailing_params()는 명시적으로 None이어야 한다."""
        params = HoldStrategy(self._cfg()).trailing_params()
        self.assertIsNone(params)

    def test_returned_tiers_are_immutable_and_shared_safely(self) -> None:
        """2026-09-14 (GPT 재검토, 2단계 완료 처리 전 수정): 이전에는
        `TrailingParams.tiers`가 가변 리스트였고, Breakout/Neutral의
        `trailing_params()`가 클래스 상수 리스트를 그대로 반환했다 —
        호출자가 반환받은 리스트를 수정하면(`params.tiers[0] = ...`)
        클래스 상수 자체가 바뀌어, 같은 클래스의 **다른 인스턴스**의
        `generate_signal()` 판정까지 달라지는 것이 실제로 재현됐다.
        지금은 `tiers`가 튜플(불변)이라 항목 대입 자체가 TypeError로
        막히고, 두 인스턴스가 같은 튜플 객체를 공유해도(동일 클래스
        상수 참조) 문제가 없음을 확인한다."""
        s1 = BreakoutStrategy(self._cfg())
        s2 = BreakoutStrategy(self._cfg())
        params = s1.trailing_params()

        with self.assertRaises(TypeError):
            params.tiers[0] = (5.0, 99.9)  # 튜플은 항목 대입 자체가 불가능해야 함

        # 다른 인스턴스가 같은 트레일링 파라미터를 여전히 그대로 보는지 확인
        # (참조를 공유해도 무해함 — 애초에 아무도 내용을 바꿀 수 없으므로).
        self.assertEqual(s2.trailing_params().tiers, params.tiers)

        average_price = 10_000
        position = Position(symbol="005930", quantity=10, average_price=average_price)
        highest_price = int(average_price * 1.06)  # high_pnl_pct=6.0% → trail_pct=2.8%
        trailing_stop_price = int(highest_price * (1 - 2.8 / 100))
        market_price = _market_price("005930", trailing_stop_price)

        # 트레일링 파라미터를 "읽기만" 한 뒤에도, 서로 다른 인스턴스가
        # 여전히 같은(변경되지 않은) 값으로 정확히 SELL을 반환하는지 확인.
        signal1 = s1.generate_signal(market_price, position, None, highest_price)
        signal2 = s2.generate_signal(market_price, position, None, highest_price)
        self.assertEqual(signal1.type, SignalType.SELL)
        self.assertEqual(signal2.type, SignalType.SELL)
        self.assertEqual(signal1.reason, signal2.reason)


class TestEvaluateExitCandidate(unittest.TestCase):
    """잔고 장애 관측 경로가 쓸 `evaluate_exit_candidate()`.

    §2.5 요구사항 검증: (1) `StrategyRouter.select(regime)`으로 얻은
    실제 전략 객체를 그대로 넘겨서 계산하고(관측 경로만의 별도
    라우팅을 만들지 않음 — REBOUND를 이름만 보고 BottomStrategy로
    연결하지 않는지도 함께 확인), (2) 이 함수는 `generate_signal()`을
    호출하지 않으므로 손절·트레일링 트리거 여부만으로 결과가
    결정되는지, (3) HoldStrategy가 선택된 경우 트레일링 후보
    (`kind="TRAILING"`)는 절대 나오지 않는지."""

    def _cfg(self) -> StrategyConfig:
        return StrategyConfig(
            name="router", breakout_threshold_pct=1.0, take_profit_pct=15.0,
            stop_loss_pct=1.5, reference_price_type="previous_close",
            trailing_stop_pct=2.0, trailing_start_pct=1.0, trend_reversal_rsi=70.0,
        )

    def test_rebound_regime_uses_router_selected_strategy_not_bottom_by_name(self) -> None:
        """StrategyRouter.select(REBOUND)는 현재 NeutralStrategy를
        반환한다(BottomStrategy의 MarketPrice 필드 미완성 우회). 관측
        경로가 이 사실을 무시하고 REBOUND를 이름만 보고 BottomStrategy에
        연결하면, 트레일링 시작 배율이 1.03(Bottom)이 아니라
        1.005(Neutral)여야 하는 이 테스트가 실패한다.

        2026-09-14 (GPT 재검토, 2단계 완료 처리 전 수정): 이전 버전은
        highest_price=1.01배·current_price=highest_price를 썼는데,
        이 입력에서는 NeutralStrategy(트레일링 활성, 미트리거 →
        None)와 만약 잘못 연결된 BottomStrategy(트레일링 아예 비활성
        → None)가 **둘 다 None을 반환**해 실제로 어느 전략 파라미터가
        쓰였는지 구별하지 못했다. average_price=10,000·
        highest_price=10,100(1.01배)·current_price=9,950으로 바꾸면:
        NeutralStrategy는 시작가(10,050) 초과로 활성화되고 high_pnl_pct
        1.0%→trail_pct 1.2%→트레일링 스탑가 9,978원이라 9,950원이
        트리거(TRAILING)되는 반면, BottomStrategy는 시작가(10,300)에
        못 미쳐 항상 비활성(None)이다 — 두 결과가 달라야 이 테스트가
        "실제 연결된 전략"을 검증하는 의미가 있다.
        """
        router = StrategyRouter(self._cfg())
        strategy = router.select(MarketRegime.REBOUND)
        self.assertIsInstance(strategy, NeutralStrategy)

        average_price = 10_000
        highest_price = int(average_price * 1.01)  # 10,100원
        current_price = 9_950  # NeutralStrategy 트레일링 스탑가(9,978원) 이하

        candidate = evaluate_exit_candidate(
            strategy=strategy, average_price=average_price, current_price=current_price,
            highest_price=highest_price, stop_loss_pct=1.5,
        )
        # NeutralStrategy 파라미터(시작 1.005배)로는 활성화·트리거된다.
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "TRAILING")

        # 대조: BottomStrategy 파라미터(시작 1.03배)로 같은 입력을
        # 계산하면 아직 시작가에도 못 미쳐 후보가 없다 — 위 결과가
        # "이름만 보고 Bottom에 연결"한 것이 아니라 실제
        # NeutralStrategy 파라미터를 썼기 때문임을 대조로 확인한다.
        bottom_strategy = BottomStrategy(self._cfg())
        bottom_candidate = evaluate_exit_candidate(
            strategy=bottom_strategy, average_price=average_price, current_price=current_price,
            highest_price=highest_price, stop_loss_pct=1.5,
        )
        self.assertIsNone(bottom_candidate)

    def test_stop_loss_candidate_takes_priority_over_trailing(self) -> None:
        """손절이 트레일링보다 우선한다(모든 전략의 원본 평가 순서와 동일)."""
        router = StrategyRouter(self._cfg())
        strategy = router.select(MarketRegime.BULLISH)
        average_price = 10_000
        stop_loss_price = int(average_price * (1 - 1.5 / 100))
        # 손절도 트리거되고(현재가가 손절가 이하) 최고가도 트레일링
        # 시작가를 넘는 극단적 케이스 — 손절이 우선해야 한다.
        highest_price = int(average_price * 1.10)
        candidate = evaluate_exit_candidate(
            strategy=strategy, average_price=average_price, current_price=stop_loss_price,
            highest_price=highest_price, stop_loss_pct=1.5,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "STOP_LOSS")
        self.assertIsNone(candidate.trailing)

    def test_trailing_candidate_when_stop_loss_not_triggered(self) -> None:
        router = StrategyRouter(self._cfg())
        strategy = router.select(MarketRegime.BULLISH)  # BreakoutStrategy
        average_price = 10_000
        highest_price = int(average_price * 1.06)  # high_pnl_pct=6.0% → trail_pct=2.8%
        trailing_stop_price = int(highest_price * (1 - 2.8 / 100))
        candidate = evaluate_exit_candidate(
            strategy=strategy, average_price=average_price, current_price=trailing_stop_price,
            highest_price=highest_price, stop_loss_pct=1.5,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "TRAILING")
        self.assertIsNotNone(candidate.trailing)
        self.assertAlmostEqual(candidate.trailing.trail_pct, 2.8)

    def test_none_when_nothing_triggers(self) -> None:
        router = StrategyRouter(self._cfg())
        strategy = router.select(MarketRegime.BULLISH)
        average_price = 10_000
        candidate = evaluate_exit_candidate(
            strategy=strategy, average_price=average_price, current_price=average_price,
            highest_price=average_price, stop_loss_pct=1.5,
        )
        self.assertIsNone(candidate)

    def test_hold_strategy_never_returns_trailing_candidate(self) -> None:
        """HoldStrategy는 trailing_params()가 None이므로, highest_price를
        아무리 극단적으로 줘도 TRAILING 후보는 절대 나오지 않는다 —
        나올 수 있는 것은 STOP_LOSS 또는 None뿐이다."""
        router = StrategyRouter(self._cfg())
        for regime in (MarketRegime.SIDEWAYS, MarketRegime.BEARISH, MarketRegime.UNKNOWN):
            strategy = router.select(regime)
            self.assertIsInstance(strategy, HoldStrategy)
            average_price = 10_000
            # highest_price를 극단적으로 크게 줘도(트레일링이 있었다면
            # 확실히 시작+트리거될 값) TRAILING은 나올 수 없어야 한다.
            candidate = evaluate_exit_candidate(
                strategy=strategy, average_price=average_price, current_price=average_price,
                highest_price=int(average_price * 2.0), stop_loss_pct=1.5,
            )
            if candidate is not None:
                self.assertEqual(candidate.kind, "STOP_LOSS")
                self.assertIsNone(candidate.trailing)


if __name__ == "__main__":
    unittest.main()
