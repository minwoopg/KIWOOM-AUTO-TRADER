from __future__ import annotations

"""BULLISH 장세 전용 진입 타이밍 전략.

설계 원칙:
    장세 분류기(classifier)가 이미 MA + RSI + MACD를 종합 판단해서
    BULLISH 판정을 내렸습니다. 그러므로 이 전략에서 RSI를 다시 보는 건
    중복입니다. 대신 "지금 이 순간 진입해도 되는가?"에 집중합니다.

매수 조건 (BULLISH 장세에서):
    ① MACD 골든크로스        — 상승 모멘텀이 살아있는가?
    ② MACD 히스토그램 확대 중 — 모멘텀이 강해지는 중인가?
    ③ 거래량 급증             — 세력이 들어오고 있는가?
    ④ 현재가 > 5일 이동평균   — 단기 추세 위에 있는가?

점수제:
    4개 충족 → 강력 매수 (최적 타점)
    3개 충족 → 매수
    2개 충족 → 매수 (보수적)
    1개 이하 → HOLD (타이밍 대기)

매도 조건:
    - 익절: 평균단가 대비 +take_profit_pct%
    - 손절: 평균단가 대비 -stop_loss_pct%
    - 조기 매도: RSI 70 이상 + MACD 히스토그램 축소 중 (모멘텀 소진)
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class BreakoutStrategy(Strategy):
    """BULLISH 장세에서 진입 타이밍을 잡는 전략입니다."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def generate_signal(self, market_price: MarketPrice, position: Position | None) -> Signal:
        """현재 시세와 보유 상태를 보고 BUY / SELL / HOLD 신호를 생성합니다."""

        current_price = market_price.current_price

        # 지표값 추출
        rsi           = market_price.indicator_rsi
        macd          = market_price.indicator_macd
        macd_signal   = market_price.indicator_macd_signal
        macd_hist_dir = market_price.indicator_macd_hist_direction  # +1 확대, -1 축소
        volume_surge  = market_price.indicator_volume_surge
        above_ma5     = market_price.indicator_price_above_ma5

        has_indicators = macd is not None and macd_signal is not None

        # ── 미보유 → 매수 판단 ───────────────────────────────────
        if position is None:

            if not has_indicators:
                # 지표 없으면 단순 돌파 방식으로 폴백
                reference_price = market_price.reference_price
                breakout_price  = int(reference_price * (1 + self.config.breakout_threshold_pct / 100))
                if current_price >= breakout_price:
                    return Signal(
                        type=SignalType.BUY,
                        reason=f"지표 없음 — 단순 돌파 조건 충족 (기준가 {reference_price:,}원 대비 +{self.config.breakout_threshold_pct:.1f}%)",
                    )
                return Signal(type=SignalType.HOLD, reason="지표 없음 — 단순 돌파 조건 미충족")

            # ── 4가지 진입 타이밍 조건 평가 ──────────────────────
            cond_macd_cross = macd > macd_signal                    # ① MACD 골든크로스
            cond_macd_accel = macd_hist_dir > 0                     # ② 히스토그램 확대 중
            cond_volume     = volume_surge                           # ③ 거래량 급증
            cond_above_ma5  = above_ma5                             # ④ 현재가 > MA5

            score = sum([cond_macd_cross, cond_macd_accel, cond_volume, cond_above_ma5])

            # 로그 태그 생성
            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
            ]
            summary = " | ".join(tags)

            if score == 4:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"최적 타점 4/4 — {summary}",
                )
            if score == 3:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"강한 진입 신호 3/4 — {summary}",
                )
            if score == 2:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"보수적 진입 2/4 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"진입 타이밍 대기 {score}/4 — {summary}",
            )

        # ── 보유 중 → 매도 판단 ──────────────────────────────────
        average_price     = position.average_price
        take_profit_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        stop_loss_price   = int(average_price * (1 - self.config.stop_loss_pct / 100))

        # 익절
        if current_price >= take_profit_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"익절 목표 {take_profit_price:,}원 도달 (+{self.config.take_profit_pct:.1f}%)",
            )

        # 손절
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"손절 기준 {stop_loss_price:,}원 하회 (-{self.config.stop_loss_pct:.1f}%)",
            )

        # 조기 매도: RSI 과매수 + 모멘텀 소진
        if has_indicators and rsi is not None and rsi >= 70 and macd_hist_dir < 0:
            return Signal(
                type=SignalType.SELL,
                reason=f"모멘텀 소진 — RSI {rsi:.1f} 과매수 + MACD 히스토그램 축소 → 조기 차익 실현",
            )

        return Signal(
            type=SignalType.HOLD,
            reason=f"보유 유지 — 익절 {take_profit_price:,}원 / 손절 {stop_loss_price:,}원 사이",
        )
