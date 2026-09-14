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
from domain.strategy.exit_calc import TrailingParams, calc_stop_loss, calc_trailing_stop


class BottomStrategy(Strategy):
    """바닥권 안전 매수 전략입니다."""

    # 2026-09-14 (구현 지시서 §2.5): 트레일링 시작 배율은 여기 한 곳에만
    # 적어두고, generate_signal()과 trailing_params()(잔고 장애 관측
    # 경로가 읽음)가 둘 다 이 상수를 참조합니다. 트레일링 폭은
    # config.trailing_stop_pct에 달려 있어 클래스 상수로 고정할 수
    # 없으므로 trailing_params()가 self.config에서 매번 읽어 만듭니다.
    _TRAILING_START_MULTIPLIER = 1.03  # +3% 이상 시 시작

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def trailing_params(self) -> TrailingParams:
        # 2026-09-14 (GPT 재검토, 2단계 완료 처리 전 수정): 여기는 매
        # 호출마다 새로 만들어 반환하므로 Breakout/Neutral 같은 클래스
        # 상수 공유 문제는 없었지만, TrailingParams.tiers의 타입이
        # 튜플(불변)로 바뀌었으므로 여기서도 일관되게 튜플로 만든다.
        return TrailingParams(
            start_multiplier=self._TRAILING_START_MULTIPLIER,
            tiers=((float("-inf"), self.config.trailing_stop_pct),),
        )

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
        safety_net_price = int(average_price * (1 + self.config.take_profit_pct / 100))

        # 2026-09-14 (180초 감시 공백 대응 — 구현 지시서 1번): 손절·
        # 트레일링 계산을 exit_calc.py의 순수 함수로 추출 — 기존
        # 임계값·반올림·분기 조건 동일, reason 문자열만 이 파일이 유지.
        stop_loss = calc_stop_loss(average_price, current_price, self.config.stop_loss_pct)
        # 2026-09-14 (GPT 재검토, 완료 처리 전 수정): 원본처럼 이 파일이
        # 직접 계산 — 이유는 exit_calc.py 모듈 docstring 참고.
        current_pnl_pct = (current_price - average_price) / average_price * 100

        # ① 손절
        if stop_loss.triggered:
            return Signal(
                type=SignalType.SELL,
                reason=f"[바닥] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss.stop_loss_price:,}원 하회)",
            )

        # ② 트레일링 스탑 (+3% 이상부터 작동, 폭은 항상 config.trailing_stop_pct 단일값)
        trailing_params = self.trailing_params()
        trailing = calc_trailing_stop(
            average_price=average_price,
            current_price=current_price,
            highest_price=highest_price,
            trailing_start_multiplier=trailing_params.start_multiplier,
            tiers=trailing_params.tiers,
        )
        if trailing.active:
            if trailing.triggered:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[바닥] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {trailing.from_high_pct:.1f}% "
                        f"(보유 {current_pnl_pct:+.1f}%)"
                    ),
                )
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"[바닥] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing.trailing_stop_price:,}원 / 현재 {current_pnl_pct:+.1f}%"
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
            reason=f"[바닥] 보유 유지 {current_pnl_pct:+.1f}% — 트레일링 시작까지 +3% 필요 / 손절 {stop_loss.stop_loss_price:,}원",
        )
