"""
스윙 전략용 일봉 분석기.

일봉 데이터를 받아 MA10/MA20/MA5/52주고점/거래량비율 등을 계산하고
진입 조건 충족 여부를 반환합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from domain.models import PriceBar
from domain.swing.pullback_rebound import (
    detect_pullback_rebound,
    PullbackReboundResult,
)


@dataclass
class SwingAnalysis:
    """일봉 기반 스윙 분석 결과."""

    symbol: str
    current_price: int
    day_rate_pct: float          # 당일 등락률 (전일 종가 대비)

    # 이동평균
    ma5:  float                  # 5일 이동평균
    ma10: float                  # 10일 이동평균
    ma20: float                  # 20일 이동평균
    ma10_distance_pct: float     # 현재가 vs MA10 거리 (%)
    ma20_rising: bool            # MA20 상승 중 여부
    above_ma20: bool             # 현재가 > MA20

    # 거래량
    volume_ratio_20d: float      # 당일 거래량 / 20일 평균 거래량
    trading_value: int           # 당일 거래대금 (원)

    # 52주 고점
    high_52w: int                # 52주 최고가
    drawdown_from_52w_pct: float # 현재가 vs 52주 고점 낙폭 (%)

    # 봉 위치
    close_position_pct: float    # 당일 봉에서 종가 위치 (0=저가, 100=고가)

    # 점수
    score: int                   # 진입 점수 (0~8)
    score_detail: str            # 점수 상세 설명

    # 진입 가능 여부
    entry_ok: bool               # 최소 진입 조건 충족
    block_reason: str            # 차단 사유 (entry_ok=False 시)

    # 눌림목-반등 패턴 (있으면 진입가의 동적 손절가 계산용)
    pullback_result: Optional[PullbackReboundResult] = None

    def __str__(self) -> str:
        return (
            f"[스윙분석] {self.symbol} | "
            f"현재가 {self.current_price:,}원 | "
            f"등락 {self.day_rate_pct:+.1f}% | "
            f"MA10거리 {self.ma10_distance_pct:+.1f}% | "
            f"MA20{'↑' if self.ma20_rising else '↓'} | "
            f"거래량비율 {self.volume_ratio_20d:.1f}x | "
            f"52주고점대비 {self.drawdown_from_52w_pct:+.1f}% | "
            f"점수 {self.score}/8"
        )


class SwingAnalyzer:
    """일봉 데이터로 스윙 진입 조건을 분석합니다."""

    def __init__(
        self,
        ma10_dist_min: float = -2.0,
        ma10_dist_max: float = 1.0,
        require_ma20_rising: bool = True,
        block_if_below_ma20: bool = True,
        max_52w_drawdown: float = -30.0,
        day_rate_min: float = -5.0,
        day_rate_max: float = 3.0,
        min_trading_value: int = 100_000_000_000,  # 1,000억
        min_volume_ratio_20d: float = 1.5,
        block_if_close_near_low_pct: float = 15.0,
        min_score: int = 7,
        watchlist: list[str] | None = None,
        # 눌림목-반등 패턴 (2026-06-16 신규)
        enable_pullback_pattern: bool = True,
        pullback_lookback_min: int = 5,
        pullback_lookback_max: int = 20,
        pullback_drawdown_min_pct: float = -20.0,
        pullback_drawdown_max_pct: float = -10.0,
        pullback_max_trough_age_days: int = 1,
        pullback_bonus_score: int = 2,
    ):
        self.ma10_dist_min          = ma10_dist_min
        self.ma10_dist_max          = ma10_dist_max
        self.require_ma20_rising    = require_ma20_rising
        self.block_if_below_ma20    = block_if_below_ma20
        self.max_52w_drawdown       = max_52w_drawdown
        self.day_rate_min           = day_rate_min
        self.day_rate_max           = day_rate_max
        self.min_trading_value      = min_trading_value
        self.min_volume_ratio_20d   = min_volume_ratio_20d
        self.block_near_low_pct     = block_if_close_near_low_pct
        self.min_score              = min_score
        self.watchlist              = set(watchlist or [])
        self.enable_pullback        = enable_pullback_pattern
        self.pullback_lookback_min  = pullback_lookback_min
        self.pullback_lookback_max  = pullback_lookback_max
        self.pullback_dd_min        = pullback_drawdown_min_pct
        self.pullback_dd_max        = pullback_drawdown_max_pct
        self.pullback_max_age       = pullback_max_trough_age_days
        self.pullback_bonus         = pullback_bonus_score

    def analyze(
        self,
        symbol: str,
        bars: list[PriceBar],
        current_price: int,
        trading_value: int,
    ) -> Optional[SwingAnalysis]:
        """
        일봉 데이터로 스윙 분석 결과를 반환합니다.

        Args:
            symbol: 종목코드
            bars: 최근 N일 일봉 (과거→최신 순, 최소 22개 이상)
            current_price: 현재가 (장중 실시간)
            trading_value: 당일 거래대금 (원)
        """
        if len(bars) < 22:
            return None

        closes  = [b.close_price for b in bars]
        volumes = [b.volume      for b in bars]
        highs   = [b.high_price  for b in bars]

        # ── 이동평균 계산 (전일 기준 — 당일 미완성봉 제외) ──────
        prev_closes = closes[:-1]  # 전일까지

        ma5  = sum(prev_closes[-5:])  / 5  if len(prev_closes) >= 5  else 0
        ma10 = sum(prev_closes[-10:]) / 10 if len(prev_closes) >= 10 else 0
        ma20 = sum(prev_closes[-20:]) / 20 if len(prev_closes) >= 20 else 0

        # MA20 상승 여부 (최근 3일 MA20 기울기)
        if len(prev_closes) >= 23:
            ma20_3d_ago = sum(prev_closes[-23:-3]) / 20
            ma20_rising = ma20 > ma20_3d_ago
        else:
            ma20_rising = False

        # ── 당일 등락률 ──────────────────────────────────────────
        prev_close   = closes[-2] if len(closes) >= 2 else closes[-1]
        day_rate_pct = (current_price - prev_close) / prev_close * 100

        # ── MA10 거리 ────────────────────────────────────────────
        ma10_dist_pct = (current_price - ma10) / ma10 * 100 if ma10 > 0 else 0

        # ── 52주 고점 (최근 252봉) ────────────────────────────────
        recent_252 = highs[-252:]
        high_52w   = max(recent_252) if recent_252 else current_price
        drawdown_52w_pct = (current_price - high_52w) / high_52w * 100

        # ── 20일 평균 거래량 ─────────────────────────────────────
        avg_vol_20d    = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 1
        today_vol      = volumes[-1]
        volume_ratio   = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0

        # ── 봉 위치 (당일 저가~고가 범위 내 종가 위치) ───────────
        today_bar   = bars[-1]
        bar_range   = today_bar.high_price - today_bar.low_price
        close_pos   = (
            (current_price - today_bar.low_price) / bar_range * 100
            if bar_range > 0 else 50
        )

        # ── 차단 조건 체크 ───────────────────────────────────────
        block_reason = ""

        if day_rate_pct < self.day_rate_min:
            block_reason = f"당일 급락 {day_rate_pct:+.1f}% (최소 {self.day_rate_min}%)"
        elif day_rate_pct > self.day_rate_max:
            block_reason = f"당일 급등 {day_rate_pct:+.1f}% (최대 {self.day_rate_max}%)"
        elif self.block_if_below_ma20 and current_price < ma20:
            block_reason = f"현재가 MA20 아래 ({current_price:,} < {ma20:,.0f})"
        elif self.require_ma20_rising and not ma20_rising:
            block_reason = "MA20 하락 중"
        elif drawdown_52w_pct < self.max_52w_drawdown:
            block_reason = f"52주 고점 대비 {drawdown_52w_pct:+.1f}% 초과 하락"
        elif trading_value < self.min_trading_value:
            block_reason = (
                f"거래대금 부족 {trading_value/1e8:.0f}억 "
                f"(최소 {self.min_trading_value/1e8:.0f}억)"
            )
        elif close_pos < self.block_near_low_pct:
            block_reason = f"당일 저가 부근 마감 ({close_pos:.0f}% 위치)"

        entry_ok = (block_reason == "")

        # ── 점수 계산 ────────────────────────────────────────────
        score = 0
        score_tags = []

        # MA10 근처 (+2점)
        if self.ma10_dist_min <= ma10_dist_pct <= self.ma10_dist_max:
            score += 2
            score_tags.append(f"MA10근처✓({ma10_dist_pct:+.1f}%)")
        else:
            score_tags.append(f"MA10거리✗({ma10_dist_pct:+.1f}%)")

        # MA20 상승 (+2점)
        if ma20_rising:
            score += 2
            score_tags.append("MA20↑✓")
        else:
            score_tags.append("MA20↓✗")

        # 거래대금 1,000억 이상 (+1점)
        if trading_value >= self.min_trading_value:
            score += 1
            score_tags.append(f"거래대금✓({trading_value/1e8:.0f}억)")
        else:
            score_tags.append(f"거래대금✗({trading_value/1e8:.0f}억)")

        # 거래량 150% 이상 (+1점)
        if volume_ratio >= self.min_volume_ratio_20d:
            score += 1
            score_tags.append(f"거래량✓({volume_ratio:.1f}x)")
        else:
            score_tags.append(f"거래량✗({volume_ratio:.1f}x)")

        # 52주 고점 대비 -30% 이내 (+1점)
        if drawdown_52w_pct >= self.max_52w_drawdown:
            score += 1
            score_tags.append(f"52주✓({drawdown_52w_pct:+.1f}%)")
        else:
            score_tags.append(f"52주✗({drawdown_52w_pct:+.1f}%)")

        # 당일 등락률 정상 범위 (+1점)
        if self.day_rate_min <= day_rate_pct <= self.day_rate_max:
            score += 1
            score_tags.append(f"등락✓({day_rate_pct:+.1f}%)")
        else:
            score_tags.append(f"등락✗({day_rate_pct:+.1f}%)")

        # watchlist 포함 (+1점)
        if symbol in self.watchlist:
            score += 1
            score_tags.append("관심종목✓")

        # ── 눌림목-반등 패턴 탐지 (+2점 보너스) ─────────────────
        # 5~20일 구간에서 고점→저점(-10~-20%) 패턴을 찾고,
        # 저점이 오늘 또는 어제(1일 이내)면 보너스 점수 + 동적 손절가 계산.
        pullback_result = None
        if self.enable_pullback:
            pullback_result = detect_pullback_rebound(
                bars=bars[:-1],  # 오늘 미완성봉 제외, 현재가는 별도 전달
                current_price=current_price,
                lookback_min=self.pullback_lookback_min,
                lookback_max=self.pullback_lookback_max,
                drawdown_min_pct=self.pullback_dd_min,
                drawdown_max_pct=self.pullback_dd_max,
                max_trough_age_days=self.pullback_max_age,
            )
            if pullback_result and pullback_result.detected:
                score += self.pullback_bonus
                score_tags.append(
                    f"눌림반등✓(고점{pullback_result.peak_days_ago}일전→"
                    f"저점{pullback_result.trough_days_ago}일전 "
                    f"{pullback_result.drawdown_pct:+.1f}%)"
                )
            elif pullback_result:
                score_tags.append(f"눌림반등✗({pullback_result.fail_reason})")

        score_detail = " | ".join(score_tags)

        return SwingAnalysis(
            symbol               = symbol,
            current_price        = current_price,
            day_rate_pct         = day_rate_pct,
            ma5                  = ma5,
            ma10                 = ma10,
            ma20                 = ma20,
            ma10_distance_pct    = ma10_dist_pct,
            ma20_rising          = ma20_rising,
            above_ma20           = current_price >= ma20,
            volume_ratio_20d     = volume_ratio,
            trading_value        = trading_value,
            high_52w             = high_52w,
            drawdown_from_52w_pct= drawdown_52w_pct,
            close_position_pct   = close_pos,
            score                = score,
            score_detail         = score_detail,
            entry_ok             = entry_ok and score >= self.min_score,
            block_reason         = block_reason if block_reason else (
                f"점수 부족 {score}/{self.min_score}" if score < self.min_score else ""
            ),
            pullback_result      = pullback_result,
        )
