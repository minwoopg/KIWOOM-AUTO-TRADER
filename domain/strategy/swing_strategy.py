from __future__ import annotations

"""스윙 전략 (수일~수주 보유).

단타 전략과의 차이:
    - 익절 기준이 넓음 (+10~15%)
    - 손절 기준도 여유 있음 (-5%)
    - 장 마감 강제청산 없음 (오버나이트 허용)
    - 주봉 기반 장세가 BULLISH일 때만 매수

매수 조건 (일봉 기반 진입 타이밍):
    단타와 동일한 4가지 조건을 사용하되
    점수 기준을 더 엄격하게 적용합니다 (3/4 이상).
    스윙은 오래 들고 가는 만큼 진입 타이밍을 더 신중하게 봅니다.

매도 조건:
    - 익절: 평균단가 대비 +take_profit_pct%
    - 손절: 평균단가 대비 -stop_loss_pct%
    - 조기 매도: RSI 과매수 + MACD 히스토그램 축소 (모멘텀 소진)
    - 장 마감 강제청산 없음
"""

from config.settings import SwingConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class SwingStrategy(Strategy):
    """주봉 상승 추세 + 일봉 진입 타이밍 기반 스윙 전략입니다."""

    def __init__(self, config: SwingConfig) -> None:
        self.config = config

    def generate_signal(self, market_price: MarketPrice, position: Position | None, minute_analysis=None, highest_price: int = 0, **kwargs) -> Signal:
        """현재 시세와 보유 상태를 보고 BUY / SELL / HOLD 신호를 생성합니다."""

        current_price = market_price.current_price

        # 지표값 추출
        rsi           = market_price.indicator_rsi
        macd          = market_price.indicator_macd
        macd_signal   = market_price.indicator_macd_signal
        macd_hist_dir = market_price.indicator_macd_hist_direction
        volume_surge  = market_price.indicator_volume_surge
        above_ma5     = market_price.indicator_price_above_ma5

        has_indicators = macd is not None and macd_signal is not None

        # ── 미보유 → 매수 판단 ───────────────────────────────────
        if position is None:

            if not has_indicators:
                return Signal(type=SignalType.HOLD, reason="지표 없음 — 스윙 진입 대기")

            # 스윙은 4가지 조건 중 3개 이상 충족해야 매수 (단타보다 엄격)
            cond_macd_cross = macd > macd_signal
            cond_macd_accel = macd_hist_dir > 0
            cond_volume     = volume_surge
            cond_above_ma5  = above_ma5

            score = sum([cond_macd_cross, cond_macd_accel, cond_volume, cond_above_ma5])

            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
            ]
            summary = " | ".join(tags)

            if score >= 3:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"[스윙] 진입 조건 {score}/4 충족 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"[스윙] 진입 타이밍 대기 {score}/4 — {summary}",
            )

        # ── 보유 중 → 매도 판단 ──────────────────────────────────
        average_price    = position.average_price
        stop_loss_price  = int(average_price * (1 - self.config.stop_loss_pct / 100))
        safety_net_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        current_pnl_pct  = (current_price - average_price) / average_price * 100

        # ① 손절 (최우선)
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[스윙] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)",
            )

        # ② 트레일링 스탑 (스윙은 더 여유있게: +3% 이상부터, -4% 하락 시)
        trailing_start_price = int(average_price * 1.03)
        if highest_price >= trailing_start_price and highest_price > 0:
            trailing_stop_price = int(highest_price * 0.96)
            from_high_pct = (current_price - highest_price) / highest_price * 100
            if current_price <= trailing_stop_price:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[스윙] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high_pct:.1f}% 하락 "
                        f"(보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ③ 추세 꺾임 (RSI 75 이상 + RSI 하락 + MACD 히스토그램 축소)
        if has_indicators and rsi is not None:
            if rsi >= 75 and macd_hist_dir < 0:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[스윙] 추세 꺾임 — RSI {rsi:.1f}↓ 과매수 + MACD 히스토그램 축소 "
                        f"(보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ④ 안전망 익절 (+12%)
        if current_price >= safety_net_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[스윙] 안전망 익절 — 평균단가 대비 +{self.config.take_profit_pct:.0f}% 도달",
            )

        # 트레일링 진행 상황
        if highest_price >= trailing_start_price and highest_price > 0:
            trailing_stop_price = int(highest_price * 0.96)
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"[스윙] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing_stop_price:,}원 / 현재 {current_pnl_pct:+.1f}%"
                ),
            )

        return Signal(
            type=SignalType.HOLD,
            reason=f"[스윙] 보유 유지 {current_pnl_pct:+.1f}% — 손절 {stop_loss_price:,}원",
        )
