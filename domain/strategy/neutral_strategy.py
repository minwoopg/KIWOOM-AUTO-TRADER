from __future__ import annotations

"""NEUTRAL 장세 전략 (NeutralStrategy).

추세가 애매한 구간에서 B(저점 반등)와 C(눌림목) 매수만 허용합니다.
A(상승 돌파)는 추세 불명확 구간에서 고가 추격이 위험하므로 제외합니다.

매수 조건:
    B조건 (저점 반등):  저점 대비 +2% + VWAP 위
    C조건 (눌림목):     등락률 -1%~-8% + MA5>MA20 + VWAP 위
    → B 또는 C 중 하나 충족
    → 점수제 3점 이상 (BULLISH의 2점보다 엄격)

매도:
    BULLISH와 동일 (트레일링 스탑 + 추세 꺾임 + 안전망)
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class NeutralStrategy(Strategy):
    """NEUTRAL 장세 전략 — 반등/눌림목 매수만 허용합니다."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def generate_signal(
        self,
        market_price: MarketPrice,
        position: Position | None,
        minute_analysis=None,
        highest_price: int = 0,
    ) -> Signal:

        current_price = market_price.current_price
        rsi           = market_price.indicator_rsi
        macd          = market_price.indicator_macd
        macd_signal   = market_price.indicator_macd_signal
        macd_hist_dir = market_price.indicator_macd_hist_direction
        rsi_direction = market_price.indicator_rsi_direction
        volume_surge  = market_price.indicator_volume_surge
        above_ma5     = market_price.indicator_price_above_ma5
        has_indicators = macd is not None and macd_signal is not None

        # ── 미보유 → 매수 판단 ───────────────────────────────────
        if position is None:
            if not has_indicators:
                return Signal(type=SignalType.HOLD, reason="[중립] 지표 없음 — 대기")

            if minute_analysis is None:
                return Signal(type=SignalType.HOLD, reason="[중립] 분봉 데이터 없음 — 대기")

            # 거래대금 체크
            if not minute_analysis.is_valid_trading_value:
                return Signal(
                    type=SignalType.HOLD,
                    reason=f"[중립] 거래대금 부족 — {minute_analysis.trading_value//100_000_000}억",
                )

            # B/C 조건만 허용 (A 상승 돌파 제외)
            pass_rebound  = minute_analysis.is_valid_rebound   # B: 저점 반등
            pass_pulldown = minute_analysis.is_valid_pulldown  # C: 눌림목

            if not pass_rebound and not pass_pulldown:
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"[중립] B/C 조건 미충족 — "
                        f"B(반등 {minute_analysis.rebound_pct:+.1f}% "
                        f"VWAP {'위' if minute_analysis.price_above_vwap else '아래'}) / "
                        f"C(눌림목 MA5>MA20:{'✓' if minute_analysis.ma5_above_ma20 else '✗'} "
                        f"등락 {minute_analysis.change_rate_pct:+.1f}%)"
                    ),
                )

            # C조건이면 눌림목 범위 확인
            if pass_pulldown and not pass_rebound:
                if not minute_analysis.is_valid_pullback:
                    return Signal(
                        type=SignalType.HOLD,
                        reason=(
                            f"[중립][C] 눌림목 범위 벗어남 — "
                            f"고가 대비 {minute_analysis.pullback_pct:+.1f}% "
                            f"(유효범위 -1%~-7%)"
                        ),
                    )

            # ── 점수제 (NEUTRAL은 3점 이상 엄격 적용) ────────────
            cond_macd_cross = macd > macd_signal
            cond_macd_accel = macd_hist_dir > 0
            cond_volume     = volume_surge
            cond_above_ma5  = above_ma5
            cond_above_vwap = minute_analysis.price_above_vwap
            cond_low_rising = minute_analysis.low_rising

            score = sum([
                cond_macd_cross, cond_macd_accel,
                cond_volume, cond_above_ma5,
                cond_above_vwap, cond_low_rising,
            ])

            mode = "B반등" if pass_rebound else "C눌림목"
            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
                f"VWAP {'위✓' if cond_above_vwap else '아래✗'}",
                f"저점 {'상승✓' if cond_low_rising else '하락✗'}",
            ]
            summary = " | ".join(tags)

            # NEUTRAL은 3점 이상만 허용
            if score >= 3:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"[중립][{mode}] 진입 {score}/6 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"[중립][{mode}] 점수 부족 {score}/6 (최소 3점) — {summary}",
            )

        # ── 보유 중 → 매도 판단 (BULLISH와 동일) ─────────────────
        average_price    = position.average_price
        stop_loss_price  = int(average_price * (1 - self.config.stop_loss_pct / 100))
        safety_net_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        current_pnl_pct  = (current_price - average_price) / average_price * 100

        # ① 손절
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[중립] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)",
            )

        # ② 트레일링 스탑
        trailing_start = int(average_price * (1 + self.config.trailing_start_pct / 100))
        if highest_price >= trailing_start and highest_price > 0:
            trailing_stop = int(highest_price * (1 - self.config.trailing_stop_pct / 100))
            from_high = (current_price - highest_price) / highest_price * 100
            if current_price <= trailing_stop:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[중립] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high:.1f}% "
                        f"(보유 {current_pnl_pct:+.1f}%)"
                    ),
                )
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"[중립] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing_stop:,}원 / 현재 {current_pnl_pct:+.1f}%"
                ),
            )

        # ③ 추세 꺾임
        if has_indicators and rsi is not None:
            trend_reversal = (
                rsi >= self.config.trend_reversal_rsi
                and rsi_direction < 0
                and macd_hist_dir < 0
            )
            if trend_reversal:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[중립] 추세 꺾임 — RSI {rsi:.1f}↓ + MACD 히스토그램 축소 "
                        f"(보유 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ④ 안전망
        if current_price >= safety_net_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"[중립] 안전망 익절 +{self.config.take_profit_pct:.0f}%",
            )

        return Signal(
            type=SignalType.HOLD,
            reason=(
                f"[중립] 보유 유지 {current_pnl_pct:+.1f}% — "
                f"트레일링 시작까지 +{self.config.trailing_start_pct:.0f}% 필요 / "
                f"손절 {stop_loss_price:,}원"
            ),
        )
