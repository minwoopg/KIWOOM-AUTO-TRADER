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
        bb_percent_b: float | None = None,
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
                # 일봉 지표가 없으면 매수하지 않음 (ver1.1 fallback 제거)
                return Signal(type=SignalType.HOLD, reason="지표 없음 — 일봉 데이터 대기 중")

            # ── [1단계] 분봉 2차 필터 ─────────────────────────────
            if minute_analysis is not None:
                # 거래대금 체크
                if not minute_analysis.is_valid_trading_value:
                    return Signal(
                        type=SignalType.HOLD,
                        reason=f"거래대금 부족 — {minute_analysis.trading_value//100_000_000}억",
                    )

                # A/B/C/V/PR 조건 판단
                pass_change   = minute_analysis.is_valid_change_rate   # A: 상승 돌파

                # ── 패턴 D: 갭 급등 후 눌림목 ─────────────────
                # 당일 +5% 이상 급등 후 VWAP 위에서 눌림 발생 시
                # 기존 A/B/C/V/PR과 독립적으로 인정
                cr   = minute_analysis.change_rate_pct
                pb   = minute_analysis.pullback_pct if hasattr(minute_analysis, 'pullback_pct') else 0.0
                # 패턴 D 상하한 (2026-06-30: 상한 추가)
                # +5~10% 갭상승 후 눌림목만 인정. +10% 초과는 고점추격으로 간주해 D 비활성.
                # 근거: 6/29 475150(+17.8%)·141080(+13.7%)이 D로 진입 후 즉시 손절.
                GAP_PULLBACK_MIN = 5.0
                GAP_PULLBACK_MAX = 10.0
                pass_gap_pullback = (
                    GAP_PULLBACK_MIN <= cr <= GAP_PULLBACK_MAX  # 당일 +5~10% 급등
                    and minute_analysis.price_above_vwap         # VWAP 위 유지
                    and -3.0 <= pb <= -0.1                       # 고점 대비 -0.1~-3% 눌림
                )
                # pass_rebound = minute_analysis.is_valid_rebound  # B: 저점 반등 — 단독 비활성화 (2일 연속 손실)
                pass_rebound  = False  # B 단독 비활성 중
                pass_pulldown = minute_analysis.is_valid_pulldown       # C: 눌림목
                pass_v        = minute_analysis.is_v_rebound            # V: V자 반등
                pass_pr       = minute_analysis.is_pulldown_recovery    # PR: 눌림목 재상승

                if not any([pass_change, pass_rebound, pass_pulldown, pass_v, pass_pr]):
                    ma = minute_analysis
                    # ── 세분화 실패 사유 ──────────────────────
                    if not ma.price_above_vwap:
                        detail = "NO_PAT_BELOW_VWAP"
                    elif not ma.is_valid_change_rate:
                        detail = f"NO_PAT_A_RATE({ma.change_rate_pct:+.1f}%)"
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
                            f"{detail} — "
                            f"A(등락 {ma.change_rate_pct:+.1f}%) / "
                            f"B(반등 {ma.rebound_pct:+.1f}% VWAP {'위' if ma.price_above_vwap else '아래'}) / "
                            f"C(MA5>MA20:{'✓' if ma.ma5_above_ma20 else '✗'}) / "
                            f"V(낙폭{ma.v_drop_pct:+.1f}% 반등{ma.v_rise_pct:+.1f}%) / "
                            f"PR(저점전환:{'✓' if ma.pr_low_turning else '✗'} 거래량:{'✓' if ma.pr_volume_expanding else '✗'})"
                        ),
                    )

                # ── 눌림목 조건 — 모드별로 다르게 적용 ──────────────
                # A(상승 돌파): 고가 대비 -2% 이내면 통과 (강한 종목은 고가 근처에서 매수)
                # B(저점 반등): 눌림목 조건 없음 (이미 저점에서 반등 확인)
                # C(눌림목):   고가 대비 -1%~-7% 유지
                if pass_change and not pass_pulldown:
                    # A조건 — 고가 대비 -2% 이내 허용
                    if minute_analysis.pullback_pct < -2.0:
                        return Signal(
                            type=SignalType.HOLD,
                            reason=(
                                f"[A] 상승 돌파 — 고가에서 너무 밀림 "
                                f"({minute_analysis.pullback_pct:+.1f}%, 허용 -2% 이내)"
                            ),
                        )
                elif pass_pulldown and not pass_change and not pass_rebound:
                    # C조건 — 기존 눌림목 -1%~-7% 엄격 적용
                    if not minute_analysis.is_valid_pullback:
                        return Signal(
                            type=SignalType.HOLD,
                            reason=(
                                f"[C] 눌림목 — 고가 대비 {minute_analysis.pullback_pct:+.1f}% "
                                f"(유효범위 -1%~-7%)"
                            ),
                        )
                # B조건(저점 반등)은 눌림목 체크 없음

            # ── [2단계] 일봉 타이밍 점수 (4가지) ─────────────────
            cond_macd_cross = macd > macd_signal
            cond_macd_accel = macd_hist_dir > 0
            cond_volume     = volume_surge
            cond_above_ma5  = above_ma5

            # ── [3단계] 분봉 타이밍 점수 (8점 체계) ──────────────
            cond_above_vwap = minute_analysis.price_above_vwap if minute_analysis else False
            cond_low_rising = minute_analysis.low_rising       if minute_analysis else False
            cond_v_or_pr    = (
                (minute_analysis.is_v_rebound if minute_analysis else False)
                or (minute_analysis.is_pulldown_recovery if minute_analysis else False)
            )
            cond_v_spike    = minute_analysis.rebound_volume_spike if minute_analysis else False
            cond_gap_pullback = pass_gap_pullback if minute_analysis else False

            score = sum([
                cond_macd_cross, cond_macd_accel,
                cond_volume, cond_above_ma5,
                cond_above_vwap, cond_low_rising,
                cond_v_or_pr, cond_v_spike,
            ])

            v_label = (
                'V자✓' if (minute_analysis and minute_analysis.is_v_rebound) else
                'PR✓'  if (minute_analysis and minute_analysis.is_pulldown_recovery) else
                'V/PR✗'
            )
            gap_label = f"갭눌림D✓({minute_analysis.change_rate_pct:+.1f}%)" if cond_gap_pullback else "갭눌림D✗"
            tags = [
                f"MACD {'골든✓' if cond_macd_cross else '데드✗'}",
                f"모멘텀 {'가속✓' if cond_macd_accel else '둔화✗'}",
                f"거래량 {'급증✓' if cond_volume else '보통✗'}",
                f"MA5 {'위✓' if cond_above_ma5 else '아래✗'}",
                f"VWAP {'위✓' if cond_above_vwap else '아래✗'}",
                f"저점 {'상승✓' if cond_low_rising else '하락✗'}",
                v_label,
                f"반등spike {'✓' if cond_v_spike else '✗'}",
                gap_label,
            ]
            summary = " | ".join(tags)

            # ── 추격매수 차단: 이미 +3% 이상 오른 상태에서 MACD 데드 ──
            # 2026-06-15: 005930이 +5% 갭상승 후 MACD 데드 상태에서
            # 3~4점으로 반복 진입 → 3연속 손절 발생.
            # 동행지표(모멘텀/MA5/VWAP)만으로 채워지는 3~4점을 걸러내고
            # 최소 5점(최적 타점)만 진입 허용.
            chasing_overheated = (
                minute_analysis is not None
                and minute_analysis.change_rate_pct >= 3.0
                and not cond_macd_cross  # MACD 데드
            )
            # ── 볼린저 상단 돌파 추격매수 완화 조치 (2026-07-02) ──────
            # %B>=1.0(상단 돌파)에서 4점(강한 진입)만으로 매수하던 걸 막고
            # 5점 이상(최적 타점)만 허용. 완전 차단은 아님 — 표본 부족(11건,
            # 그중 >1.0은 1건뿐)이라 강한 신호(5점+)까지 막을 근거는 없음.
            bb_overheated = bb_percent_b is not None and bb_percent_b >= 1.0

            # ── 상승여력 부족 게이트 (2026-07-09) ──────────────────────
            # trades.csv 전체 이력: 상승여력<1% 95건 승률36%/평균-0.24%,
            # 1~2% 구간 20건 승률60%/평균+0.03% — <1% 구간이 확연히 열세.
            # 볼린저/추격매수와 동일한 방식으로: 5점 미만이면 HOLD, 5점 이상은 통과.
            _low_upside_guard = getattr(self.config, "low_upside_guard_enabled", False)
            _upside_pct = (
                getattr(minute_analysis, "upside_to_recent_high_pct", None)
                if minute_analysis is not None else None
            )
            _min_upside_pct = getattr(self.config, "min_upside_to_recent_high_pct", 1.0)
            low_upside_blocked = (
                _low_upside_guard
                and _upside_pct is not None
                and _upside_pct < _min_upside_pct
            )
            # 패턴D(갭눌림목)는 원래 고점 근처에서 나오는 패턴이라 상승여력이
            # 구조적으로 낮게 잡히기 쉽다. apply_to_pattern_d=False면 패턴D는
            # 이 게이트에서 예외로 둔다 (아래 patternD 분기가 정상 도달하도록).
            _upside_guard_exempts_pattern_d = (
                cond_gap_pullback
                and not getattr(self.config, "low_upside_guard_apply_to_pattern_d", False)
            )
            low_upside_gate_active = low_upside_blocked and not _upside_guard_exempts_pattern_d

            min_score = 5 if (chasing_overheated or bb_overheated or low_upside_gate_active) else 3

            # ── 종목별 진입 문턱 상향 (2026-07-06) ────────────────────
            # 특정 종목이 구조적으로 저품질 진입을 반복 생산할 때 사용.
            # 000660: replay 67건 A/B패턴 5분순수익 -0.61%, 실거래 손절
            _symbol_overrides = getattr(self.config, "symbol_min_score_override", None) or {}
            _symbol_min = _symbol_overrides.get(market_price.symbol)
            symbol_overheated = _symbol_min is not None and score < _symbol_min
            if _symbol_min is not None:
                min_score = max(min_score, _symbol_min)

            if score >= 5:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"최적 타점 {score}/8 — {summary}",
                )
            if chasing_overheated:
                # 추격매수 구간: 5점 미만은 전부 HOLD (갭D 포함)
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"추격매수 차단 {score}/8 — 당일 "
                        f"{minute_analysis.change_rate_pct:+.1f}% + MACD데드 "
                        f"→ 최소 {min_score}점 필요 — {summary}"
                    ),
                )
            if bb_overheated:
                # 볼린저 상단 돌파 구간: 5점 미만은 HOLD (4점 강한진입도 차단)
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"볼린저 상단돌파 차단 {score}/8 — %B={bb_percent_b:.2f} "
                        f"→ 최소 {min_score}점 필요 — {summary}"
                    ),
                )
            if low_upside_gate_active:
                # 상승여력 부족 구간: 5점 미만은 HOLD (4점 강한진입도 차단)
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"상승여력부족 차단 {score}/8 — 상승여력={_upside_pct:.2f}% "
                        f"→ 최소 {min_score}점 필요 — {summary}"
                    ),
                )
            if symbol_overheated:
                # 종목별 문턱: 해당 종목은 min_score 미만이면 전부 HOLD
                return Signal(
                    type=SignalType.HOLD,
                    reason=(
                        f"종목별 진입제한 {score}/8 — {market_price.symbol} "
                        f"→ 최소 {_symbol_min}점 필요 — {summary}"
                    ),
                )
            if score == 4:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"강한 진입 {score}/8 — {summary}",
                )
            if score == 3 and not getattr(self.config, "disable_score3_buy", False):
                return Signal(
                    type=SignalType.BUY,
                    reason=f"보수적 진입 {score}/8 — {summary}",
                )

            # ── 갭 급등 눌림목 패턴 D — 점수 2점이어도 허용 ──
            # (상승여력 게이트는 위 low_upside_gate_active에서 이미
            #  low_upside_guard_apply_to_pattern_d 설정을 반영해 처리됨)
            if cond_gap_pullback and score >= 2:
                return Signal(
                    type=SignalType.BUY,
                    reason=f"[갭D] 갭 급등 눌림 {score}/8 — {summary}",
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

        # ② 구간형 트레일링 스탑
        # 수익 구간에 따라 트레일링 폭을 다르게 적용합니다.
        # 2026-06-22 개편: 손익비 개선 — 시작점을 +1.2%로 늦추고 전 구간 폭 확대.
        #   (기존엔 +0.5%부터 -1.0%로 추적해서 본전 근처 조기청산이 빈번,
        #    이길 때 평균 +0.23% vs 질 때 -1.14%로 손익비가 1:4.9로 거꾸로였음)
        trailing_start_price = int(average_price * 1.012)  # +1.2% 이상 시 시작
        if highest_price >= trailing_start_price and highest_price > 0:
            high_pnl_pct = (highest_price - average_price) / average_price * 100
            if high_pnl_pct >= 5.0:
                trail_pct = 2.8
            elif high_pnl_pct >= 3.5:
                trail_pct = 2.2
            elif high_pnl_pct >= 2.0:
                trail_pct = 1.8
            else:  # +1.2~2.0%
                trail_pct = 1.5
            trailing_stop_price = int(highest_price * (1 - trail_pct / 100))
            from_high_pct = (current_price - highest_price) / highest_price * 100
            if current_price <= trailing_stop_price:
                return Signal(
                    type=SignalType.SELL,
                    reason=(
                        f"트레일링 스탑 — 최고가 {highest_price:,}원 대비 {from_high_pct:.1f}% 하락 "
                        f"(트레일링 폭 -{trail_pct:.1f}% / 보유 수익 {current_pnl_pct:+.1f}%)"
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
                        f"추세 꺾임 {sell_score}/5점 — "
                        f"{'·'.join(sell_reasons)} "
                        f"(보유 수익 {current_pnl_pct:+.1f}%)"
                    ),
                )

        # ④ 안전망 익절 (급등 시 +15%)
        if current_price >= safety_net_price:
            return Signal(
                type=SignalType.SELL,
                reason=f"안전망 익절 — 평균단가 대비 +{self.config.take_profit_pct:.0f}% 도달",
            )

        # 트레일링 스탑 진행 상황 표시 (② 실제 청산 로직과 동일한 구간 유지)
        if highest_price >= trailing_start_price and highest_price > 0:
            high_pnl_pct2 = (highest_price - average_price) / average_price * 100
            if high_pnl_pct2 >= 5.0:   trail_pct2 = 2.8
            elif high_pnl_pct2 >= 3.5: trail_pct2 = 2.2
            elif high_pnl_pct2 >= 2.0: trail_pct2 = 1.8
            else:                       trail_pct2 = 1.5
            trailing_stop_price = int(highest_price * (1 - trail_pct2 / 100))
            return Signal(
                type=SignalType.HOLD,
                reason=(
                    f"트레일링 추적 중 — 최고가 {highest_price:,}원 / "
                    f"스탑 {trailing_stop_price:,}원 (폭 -{trail_pct2:.1f}%) / 현재 {current_pnl_pct:+.1f}%"
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
