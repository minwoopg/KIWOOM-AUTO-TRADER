from __future__ import annotations

"""NEUTRAL 장세 전략 (NeutralStrategy).

추세가 애매한 구간에서 B(저점 반등)와 C(눌림목) 매수만 허용합니다.
A(상승 돌파)는 추세 불명확 구간에서 고가 추격이 위험하므로 제외합니다.

매수 조건:
    B조건 (저점 반등):  저점 대비 +2% + VWAP 위 (현재 단독 비활성 — 아래 참고)
    C조건 (눌림목):     등락률 -1%~-8% + MA5>MA20 + VWAP 위
    → PR(눌림목재상승) 또는 V(V자반등)이 반드시 포함되어야 함 (C/B 단독 불허)
    → 점수제 5점 이상 (2026-07-16: docstring이 "3점 이상"으로 되어있던 게
      실제 코드(score >= 5)와 안 맞아 정정 — 실제 동작은 항상 5점 기준이었음)

매도:
    BULLISH와 동일 (트레일링 스탑 + 추세 꺾임 + 안전망)
"""

from config.settings import StrategyConfig
from domain.models import MarketPrice, Position, Signal, SignalType
from domain.strategy.base import Strategy
from domain.strategy.exit_calc import TrailingParams, calc_stop_loss, calc_trailing_stop


class NeutralStrategy(Strategy):
    """NEUTRAL 장세 전략 — 반등/눌림목 매수만 허용합니다."""

    # 2026-09-14 (구현 지시서 §2.5): 트레일링 시작 배율·구간표를 여기
    # 한 곳에만 적어두고, generate_signal()과 trailing_params()(잔고
    # 장애 관측 경로가 읽음)가 둘 다 이 상수를 참조합니다 — 숫자를
    # 두 곳에 복사하지 않기 위함.
    # 2026-09-14 (GPT 재검토, 2단계 완료 처리 전 수정): 튜플(불변)로
    # 바꿨다 — 리스트였을 때는 trailing_params()가 이 객체를 그대로
    # 반환해서, 호출자가 반환받은 리스트를 수정하면 이 클래스 상수
    # 자체가 바뀌어 다른 NeutralStrategy 인스턴스의 실제 SELL/HOLD
    # 판정까지 달라지는 경로가 재현됐다(exit_calc.py의 TrailingParams
    # docstring 참고).
    _TRAILING_START_MULTIPLIER = 1.005  # +0.5% 이상 시 시작
    _TRAILING_TIERS: tuple[tuple[float, float], ...] = (
        (3.0, 2.0), (2.0, 1.5), (1.0, 1.2), (float("-inf"), 0.8),
    )

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def trailing_params(self) -> TrailingParams:
        return TrailingParams(start_multiplier=self._TRAILING_START_MULTIPLIER, tiers=self._TRAILING_TIERS)

    def generate_signal(
        self,
        market_price: MarketPrice,
        position: Position | None,
        minute_analysis=None,
        highest_price: int = 0,
        **kwargs,  # bb_percent_b 등 다른 전략용 추가 인자를 안전하게 무시
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

            # NEUTRAL 허용 패턴: PR/B, B/C, V, PR만 허용
            # C 단독, B 단독, A/B 차단 — 반복 손실 방지
            pass_rebound  = False  # B 단독 비활성
            pass_pulldown = minute_analysis.is_valid_pulldown       # C: 눌림목
            pass_v        = minute_analysis.is_v_rebound            # V: V자 반등
            pass_pr       = minute_analysis.is_pulldown_recovery    # PR: 눌림목 재상승

            # PR 또는 V가 반드시 포함돼야 진입 허용
            # (C 단독은 허용 안 함 — 오늘처럼 하락장에서 손실 방지)
            has_pr_or_v = pass_v or pass_pr
            has_b_or_c  = pass_rebound or pass_pulldown
            # 허용: PR 단독, V 단독, PR/B, PR/C, B/C(C 포함 시 PR 필요)
            neutral_pattern_ok = has_pr_or_v or (has_b_or_c and has_pr_or_v)

            if not any([pass_rebound, pass_pulldown, pass_v, pass_pr]):
                ma = minute_analysis
                # ── 세분화 실패 사유 ──────────────────────────
                if not ma.price_above_vwap:
                    detail = "NO_PAT_BELOW_VWAP"
                elif ma.rebound_pct < 2.0:
                    detail = f"NO_PAT_B_REBOUND_SMALL({ma.rebound_pct:+.1f}%)"
                elif not ma.ma5_above_ma20:
                    detail = "NO_PAT_C_MA_FAIL"
                elif not ma.is_valid_pullback:
                    detail = f"NO_PAT_C_PULLBACK({ma.pullback_pct:+.1f}%)"
                elif not ma.pr_low_turning:
                    detail = "NO_PAT_PR_LOW_FAIL"
                elif not ma.pr_volume_expanding:
                    detail = "NO_PAT_PR_VOL_WEAK"
                else:
                    detail = "NO_PAT_V_FAIL"
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"[중립] {detail} — "
                        f"B(반등 {ma.rebound_pct:+.1f}% "
                        f"VWAP {'위' if ma.price_above_vwap else '아래'}) / "
                        f"C(눌림목 MA5>MA20:{'✓' if ma.ma5_above_ma20 else '✗'} "
                        f"등락 {ma.change_rate_pct:+.1f}%)"
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
            cond_v_or_pr    = (
                minute_analysis.is_v_rebound or minute_analysis.is_pulldown_recovery
            )
            cond_v_spike    = minute_analysis.rebound_volume_spike

            score = sum([
                cond_macd_cross, cond_macd_accel,
                cond_volume, cond_above_ma5,
                cond_above_vwap, cond_low_rising,
                cond_v_or_pr, cond_v_spike,
            ])

            v_label = (
                'V자✓' if pass_v else
                'PR✓'  if pass_pr else
                'V/PR✗'
            )
            mode = v_label if (pass_v or pass_pr) else ("B반등" if pass_rebound else "C눌림목")
            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
                f"VWAP {'위✓' if cond_above_vwap else '아래✗'}",
                f"저점 {'상승✓' if cond_low_rising else '하락✗'}",
                v_label,
                f"반등spike {'✓' if cond_v_spike else '✗'}",
            ]
            summary = " | ".join(tags)

            # NEUTRAL: PR/V 포함 필수 + 5점 이상
            if not has_pr_or_v:
                return Signal(
                    type=SignalType.HOLD,
                    reason="NEUTRAL_C_BLOCKED",
                )

            if score >= 5:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"[중립][{mode}] 진입 {score}/8 — {summary}",
                )

            return Signal(
                type=SignalType.HOLD,
                reason=f"[중립][{mode}] 점수 부족 {score}/8 (최소 5점) — {summary}",
            )

        # ── 보유 중 → 매도 판단 (BULLISH와 동일) ─────────────────
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
                reason=f"[중립] 손절 — 평균단가 대비 {current_pnl_pct:+.1f}% ({stop_loss.stop_loss_price:,}원 하회)",
            )

        # ② 구간형 트레일링 스탑
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
                        f"[중립] 트레일링 스탑 — 최고가 {highest_price:,}원 대비 {trailing.from_high_pct:.1f}% 하락 "
                        f"(트레일링 폭 -{trailing.trail_pct:.1f}% / 보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"[중립] 트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing.trailing_stop_price:,}원 (폭 -{trailing.trail_pct:.1f}%) / 현재 {current_pnl_pct:+.1f}%"
                ),
            )

        # ③ 추세 꺾임 — 점수제 (보유 수익 +0.5% 이상일 때만 활성화)
        if has_indicators and rsi is not None and current_pnl_pct >= 0.5:
            sell_score = 0
            sell_reasons = []
            # +1점: RSI 과매수
            if rsi >= self.config.trend_reversal_rsi:
                sell_score += 1
                sell_reasons.append(f"RSI {rsi:.1f}")
            # +1점: RSI 하락 전환
            if rsi_direction < 0:
                sell_score += 1
                sell_reasons.append("RSI↓")
            # +1점: MACD 히스토그램 축소
            if macd_hist_dir < 0:
                sell_score += 1
                sell_reasons.append("MACD축소")
            # +1점: VWAP 아래로 이탈
            if minute_analysis is not None and not minute_analysis.price_above_vwap:
                sell_score += 1
                sell_reasons.append("VWAP이탈")
            # +1점: MA5 아래로 이탈
            if not above_ma5:
                sell_score += 1
                sell_reasons.append("MA5이탈")
            price_fallen = (
                (minute_analysis is not None and not minute_analysis.price_above_vwap)
                or not above_ma5
            )
            if sell_score >= 3 and price_fallen:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"[중립] 추세 꺾임 {sell_score}/5점 — "
                        f"{'·'.join(sell_reasons)} "
                        f"(보유 {current_pnl_pct:+.1f}%)"
                    ),
                    # 2026-07-28: VWAP/MA5 이탈을 점수에 반영하는
                    # 지표 기반 SELL — stale 분봉 데이터로는 신뢰 불가.
                    requires_fresh_minute_data=True,
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
                f"손절 {stop_loss.stop_loss_price:,}원"
            ),
        )
