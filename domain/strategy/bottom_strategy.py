from __future__ import annotations

"""바닥권 안전 매수 전략 (BottomStrategy).

REBOUND 장세에서 작동합니다.

매수 조건 (필수 3가지 모두 충족):
    ① RSI(14) < 35 + RSI Signal(9) 골든크로스
    ② MACD 히스토그램 반전 (음수 구간에서 증가 시작)
    ③ 거래량 시나리오 A 또는 B
       A: 매물 고갈 — 거래량 < 20일 평균의 70%
       B: 세력 유입 — 거래량 > 20일 평균의 130%

추가 필터 (분봉 있을 때):
    ④ 분봉 저점 상승 또는 VWAP 근접(0.5% 이내)

매도 조건:
    ① 손절: -1.5% (설정값 사용)
    ② 트레일링: +3% 이상부터 시작, 최고가 대비 -2%
    ③ 안전망: +15%
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class BottomStrategy(Strategy):
    """바닥권 안전 매수 전략입니다."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def generate_signal(
        self,
        market_price: MarketPrice,
        position: Position | None,
        minute_analysis=None,
        highest_price: int = 0,
        **kwargs,  # bb_percent_b 등 다른 전략용 추가 인자를 안전하게 무시
    ) -> Signal:

        current_price    = market_price.current_price
        rsi              = market_price.indicator_rsi
        rsi_signal_cross = market_price.indicator_rsi_signal_cross
        macd             = market_price.indicator_macd
        macd_signal      = market_price.indicator_macd_signal
        macd_hist_dir    = market_price.indicator_macd_hist_direction
        vol_exhaustion   = market_price.indicator_volume_exhaustion
        vol_buying       = market_price.indicator_volume_buying
        has_indicators   = rsi is not None and macd is not None

        # ── 미보유 → 매수 판단 ───────────────────────────────────
        if position is None:
            if not has_indicators:
                return Signal(type=SignalType.HOLD, reason="[바닥] 지표 없음 — 대기")

            # 필수 3가지 조건
            cond_rsi    = rsi is not None and rsi < 35 and rsi_signal_cross == 1
            cond_hist   = macd_hist_dir > 0 and (macd is not None and macd_signal is not None and macd < macd_signal)
            cond_volume = vol_exhaustion or vol_buying

            # 분봉 조건 (선택)
            cond_minute = False
            minute_tag  = "분봉없음"
            if minute_analysis is not None:
                cond_minute = (
                    minute_analysis.low_rising
                    or (
                        minute_analysis.vwap > 0
                        and abs(minute_analysis.vwap - current_price) / current_price < 0.005
                    )
                )
                minute_tag = f"분봉{'✓' if cond_minute else '✗'}"

            vol_tag = (
                "매물고갈✓" if vol_exhaustion else
                "세력유입✓" if vol_buying else "거래량시나리오✗"
            )

            tags = [
                f"RSI {rsi:.1f} Signal{'골든✓' if rsi_signal_cross == 1 else '✗'}",
                f"히스토그램{'반전✓' if cond_hist else '✗'}",
                vol_tag,
                minute_tag,
            ]
            summary = " | ".join(tags)

            if cond_rsi and cond_hist and cond_volume:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"[바닥] 안전 매수 조건 충족 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"[바닥] 조건 미충족 — {summary}",
            )

        # ── 보유 중 → 매도 판단 ──────────────────────────────────
        average_price    = position.average_price
        stop_loss_price  = int(average_price * (1 - self.config.stop_loss_pct / 100))
        safety_net_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        current_pnl_pct  = (current_price - average_price) / average_price * 100

        # ① 손절
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[바닥] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)",
            )

        # ② 트레일링 스탑 (+3% 이상부터 작동)
        trailing_start = int(average_price * 1.03)
        if highest_price >= trailing_start and highest_price > 0:
            trailing_stop = int(highest_price * (1 - self.config.trailing_stop_pct / 100))
            from_high = (current_price - highest_price) / highest_price * 100
            if current_price <= trailing_stop:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[바닥] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high:.1f}% "
                        f"(보유 {current_pnl_pct:+.1f}%)"
                    ),
                )
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"[바닥] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing_stop:,}원 / 현재 {current_pnl_pct:+.1f}%"
                ),
            )

        # ③ 안전망
        if current_price >= safety_net_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[바닥] 안전망 익절 +{self.config.take_profit_pct:.0f}%",
            )

        return Signal(
            type=SignalType.HOLD,
            reason=f"[바닥] 보유 유지 {current_pnl_pct:+.1f}% — 트레일링 시작까지 +3% 필요 / 손절 {stop_loss_price:,}원",
        )
