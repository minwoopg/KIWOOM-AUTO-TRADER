from __future__ import annotations

"""기술적 지표 기반 매매 전략 (RSI + MACD + 거래량 3중 필터).

엑셀 자료(기술치수에 의해 매매시기 조사)의 황금시간 조건을 코드로 구현합니다.

매수 조건 (3가지 동시 충족 시):
    1. RSI 30 이하 (과매도 구간 — 바닥권)
    2. MACD 골든크로스 (MACD > Signal — 상승 모멘텀 시작)
    3. 거래량 급증 (평균 대비 1.5배 이상 — 세력 유입 확인)
    → 바닥권 V자 반등 타점. 3가지 모두 충족해야 매수합니다.

조건 미충족 시 단계별 완화:
    - 3가지 중 2가지만 충족 → 매수 가능 (보수적 진입)
    - 1가지만 충족 → HOLD

매도 조건 (익절/손절은 항상 적용):
    - 익절: 평균단가 대비 +take_profit_pct%
    - 손절: 평균단가 대비 -stop_loss_pct%
    + 추가 매도 신호: RSI 70 이상 + MACD 데드크로스 → 고점 반전 감지 시 조기 매도
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy


class BreakoutStrategy(Strategy):
    """RSI + MACD + 거래량 3중 필터 기반 매매 전략입니다."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def generate_signal(self, market_price: MarketPrice, position: Position | None) -> Signal:
        """현재 시세와 보유 상태를 보고 BUY / SELL / HOLD 신호를 생성합니다."""

        current_price = market_price.current_price

        # 지표값 추출 (없으면 기존 단순 돌파 전략으로 폴백)
        rsi           = market_price.indicator_rsi
        rsi_direction = market_price.indicator_rsi_direction  # +1 상승, -1 하락, 0 보합
        macd          = market_price.indicator_macd
        macd_signal   = market_price.indicator_macd_signal
        volume_surge  = market_price.indicator_volume_surge

        has_indicators = rsi is not None and macd is not None and macd_signal is not None

        # ── 미보유 → 매수 판단 ────────────────────────────────────
        if position is None:

            if not has_indicators:
                # 지표 없으면 기존 단순 돌파 방식 유지 (안전망)
                reference_price = market_price.reference_price
                breakout_price = int(reference_price * (1 + self.config.breakout_threshold_pct / 100))
                if current_price >= breakout_price:
                    return Signal(
                        type=SignalType.BUY,
                        reason=f"지표 없음 — 단순 돌파 조건 충족 (기준가 {reference_price:,}원 대비 {self.config.breakout_threshold_pct:.1f}% 상승)",
                    )
                return Signal(type=SignalType.HOLD, reason="지표 없음 — 단순 돌파 조건 미충족")

            # 3중 필터 조건 평가
            cond_rsi    = rsi <= 30 and rsi_direction >= 0   # RSI 과매도 + 하락 중 아님
            cond_macd   = macd > macd_signal                  # MACD 골든크로스
            cond_volume = volume_surge                         # 거래량 급증
            score = sum([cond_rsi, cond_macd, cond_volume])

            rsi_dir_tag = "↑" if rsi_direction > 0 else ("↓" if rsi_direction < 0 else "→")
            rsi_tag    = f"RSI {rsi:.1f}{rsi_dir_tag}{'✓' if cond_rsi else '✗'}"
            macd_tag   = f"MACD {'골든크로스✓' if cond_macd else '데드크로스✗'}"
            volume_tag = f"거래량 {'급증✓' if cond_volume else '보통✗'}"
            summary    = f"{rsi_tag} | {macd_tag} | {volume_tag}"

            # RSI가 30 이하이지만 아직 하락 중이면 별도 안내
            if rsi <= 30 and rsi_direction < 0:
                return Signal(
                    type=SignalType.HOLD,
                    reason=f"RSI {rsi:.1f}↓ 과매도 구간이나 아직 하락 중 — 바닥 확인 후 진입 (매수 대기)",
                )

            if score == 3:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"황금시간 3중 조건 모두 충족 — {summary}",
                )

            if score == 2:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"3중 조건 중 2개 충족 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"매수 조건 {score}/3 충족 — {summary}",
            )

        # ── 보유 중 → 매도 판단 ──────────────────────────────────
        average_price     = position.average_price
        take_profit_price = int(average_price * (1 + self.config.take_profit_pct / 100))
        stop_loss_price   = int(average_price * (1 - self.config.stop_loss_pct / 100))

        # 익절
        if current_price >= take_profit_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"익절 목표 {take_profit_price:,}원 도달 (평균단가 {average_price:,}원 대비 +{self.config.take_profit_pct:.1f}%)",
            )

        # 손절
        if current_price <= stop_loss_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"손절 기준 {stop_loss_price:,}원 하회 (평균단가 {average_price:,}원 대비 -{self.config.stop_loss_pct:.1f}%)",
            )

        # 고점 반전 감지 (RSI 과매수 + MACD 데드크로스) → 조기 매도
        if has_indicators and rsi >= 70 and macd < macd_signal:
            return Signal(
                type=SignalType.SELL,
                reason=f"고점 반전 감지 — RSI {rsi:.1f} 과매수 + MACD 데드크로스 → 조기 차익 실현",
            )

        return Signal(
            type=SignalType.HOLD,
            reason=f"보유 유지 — 익절 {take_profit_price:,}원 / 손절 {stop_loss_price:,}원 사이",
        )
