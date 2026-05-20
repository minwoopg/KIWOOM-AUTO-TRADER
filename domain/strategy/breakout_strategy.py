from __future__ import annotations

"""BULLISH 장세 + 분봉 2차 필터 기반 단타 전략.

매수 판단 흐름:
    [1단계] 분봉 2차 필터 (등락률, 거래대금, 눌림목)
        → 조건 미충족 시 즉시 HOLD

    [2단계] 일봉 기반 타이밍 점수 (4가지)
        ① MACD 골든크로스
        ② MACD 모멘텀 가속
        ③ 거래량 급증
        ④ 현재가 > MA5

    [3단계] 분봉 기반 타이밍 점수 (2가지)
        ⑤ VWAP 위에 있는가
        ⑥ 분봉 저점 상승 중인가

    총 6점 중 3점 이상 → BUY
    2점 → BUY (보수적)
    1점 이하 → HOLD

매도:
    - 익절/손절 기준
    - 조기 매도: RSI 과매수 + MACD 히스토그램 축소
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class BreakoutStrategy(Strategy):
    """분봉 2차 필터 + 일봉 타이밍 기반 단타 전략입니다."""

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
                reference_price = market_price.reference_price
                breakout_price  = int(reference_price * (1 + self.config.breakout_threshold_pct / 100))
                if current_price >= breakout_price:
                    return Signal(
                        type=SignalType.BUY,
                        reason=f"지표 없음 — 단순 돌파 충족 (+{self.config.breakout_threshold_pct:.1f}%)",
                    )
                return Signal(type=SignalType.HOLD, reason="지표 없음 — 단순 돌파 미충족")

            # ── [1단계] 분봉 2차 필터 ─────────────────────────────
            if minute_analysis is not None:
                # 거래대금 체크
                if not minute_analysis.is_valid_trading_value:
                    return Signal(
                        type=SignalType.HOLD,
                        reason=f"거래대금 부족 — {minute_analysis.trading_value//100_000_000}억",
                    )

                # A조건(상승 중) 또는 B조건(저점 반등) 중 하나라도 충족해야 함
                pass_change  = minute_analysis.is_valid_change_rate
                pass_rebound = minute_analysis.is_valid_rebound

                if not pass_change and not pass_rebound:
                    return Signal(
                        type=SignalType.HOLD,
                        reason=(
                            f"진입 조건 미충족 — "
                            f"A(등락 {minute_analysis.change_rate_pct:+.1f}%) / "
                            f"B(반등 {minute_analysis.rebound_pct:+.1f}% VWAP {'위' if minute_analysis.price_above_vwap else '아래'})"
                        ),
                    )

                if not minute_analysis.is_valid_pullback:
                    return Signal(
                        type=SignalType.HOLD,
                        reason=f"눌림목 구간 아님 — 고가 대비 {minute_analysis.pullback_pct:+.1f}% (유효범위 -1~-7%)",
                    )

            # ── [2단계] 일봉 타이밍 점수 (4가지) ─────────────────
            cond_macd_cross = macd > macd_signal
            cond_macd_accel = macd_hist_dir > 0
            cond_volume     = volume_surge
            cond_above_ma5  = above_ma5

            # ── [3단계] 분봉 타이밍 점수 (2가지) ─────────────────
            cond_above_vwap = minute_analysis.price_above_vwap if minute_analysis else False
            cond_low_rising = minute_analysis.low_rising       if minute_analysis else False

            score = sum([
                cond_macd_cross, cond_macd_accel,
                cond_volume, cond_above_ma5,
                cond_above_vwap, cond_low_rising,
            ])

            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
                f"VWAP {'위✓' if cond_above_vwap else '아래✗'}",
                f"저점 {'상승✓' if cond_low_rising else '하락✗'}",
            ]
            summary = " | ".join(tags)

            if score >= 4:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"최적 타점 {score}/6 — {summary}",
                )
            if score == 3:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"강한 진입 {score}/6 — {summary}",
                )
            if score == 2:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"보수적 진입 {score}/6 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"타이밍 대기 {score}/6 — {summary}",
            )

        # ── 보유 중 → 매도 판단 ──────────────────────────────────
        average_price = position.average_price
        stop_loss_price = int(average_price * (1 - self.config.stop_loss_pct / 100))
        safety_net_price = int(average_price * (1 + self.config.take_profit_pct / 100))

        current_pnl_pct = (current_price - average_price) / average_price * 100

        # ① 손절 (최우선)
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss_price:,}원 하회)",
            )

        # ② 트레일링 스탑
        # 최소 수익률(trailing_start_pct) 이상 올랐을 때부터 작동
        trailing_start_price = int(average_price * (1 + self.config.trailing_start_pct / 100))
        if highest_price >= trailing_start_price and highest_price > 0:
            trailing_stop_price = int(highest_price * (1 - self.config.trailing_stop_pct / 100))
            from_high_pct = (current_price - highest_price) / highest_price * 100
            if current_price <= trailing_stop_price:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high_pct:.1f}% 하락 "
                        f"(보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ③ 추세 꺾임 감지 (RSI 과매수 + RSI 하락 전환 + MACD 히스토그램 축소)
        if has_indicators and rsi is not None:
            trend_reversal = (
                rsi >= self.config.trend_reversal_rsi   # RSI 과매수 구간
                and rsi_direction < 0                   # RSI 하락 전환
                and macd_hist_dir < 0                   # MACD 모멘텀 약화
            )
            if trend_reversal:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"추세 꺾임 감지 — RSI {rsi:.1f}↓ 과매수 + MACD 히스토그램 축소 "
                        f"(보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ④ 안전망 익절 (급등 시 +15%)
        if current_price >= safety_net_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"안전망 익절 — 평균단가 대비 +{self.config.take_profit_pct:.0f}% 도달",
            )

        # 트레일링 스탑 진행 상황 표시
        if highest_price >= trailing_start_price and highest_price > 0:
            trailing_stop_price = int(highest_price * (1 - self.config.trailing_stop_pct / 100))
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing_stop_price:,}원 / 현재 {current_pnl_pct:+.1f}%"
                ),
            )

        return Signal(
            type=SignalType.HOLD,
            reason=(
                f"보유 유지 {current_pnl_pct:+.1f}% — "
                f"트레일링 시작까지 +{self.config.trailing_start_pct:.0f}% 필요 / "
                f"손절 {stop_loss_price:,}원"
            ),
        )
