from __future__ import annotations

"""분봉 분석기 (Minute Bar Analyzer).

분봉 데이터로 단타 진입 타이밍을 판단하는 지표를 계산합니다.

계산하는 지표:
    - VWAP (당일 거래량 가중 평균가)
    - 분봉 저점 상승 여부 (눌림목 후 반등 확인)
    - 당일 고가 대비 현재가 위치 (눌림목 구간 확인)
    - 당일 등락률
    - 거래대금 (거래량 × 가격)
"""

from dataclasses import dataclass
from domain.models import MinuteBar


@dataclass
class MinuteAnalysis:
    """분봉 분석 결과를 담는 객체입니다."""

    vwap: float                  # 당일 VWAP
    price_above_vwap: bool       # 현재가 > VWAP
    low_rising: bool             # 최근 N봉 저점이 높아지는 중
    pullback_pct: float          # 당일 고가 대비 현재가 하락률 (음수)
    is_valid_pullback: bool      # 눌림목 구간인가 (-1% ~ -7%)
    change_rate_pct: float       # 당일 등락률
    is_valid_change_rate: bool   # 등락률 유효 범위 (+2% ~ +18%)
    trading_value: int           # 거래대금 (원)
    is_valid_trading_value: bool # 거래대금 기준 이상
    day_high: int                # 당일 고가
    day_low: int                 # 당일 저가

    def score(self) -> int:
        """진입 타이밍 점수를 계산합니다 (0~5점)."""
        return sum([
            self.price_above_vwap,
            self.low_rising,
            self.is_valid_pullback,
            self.is_valid_change_rate,
            self.is_valid_trading_value,
        ])

    def summary(self) -> str:
        """로그용 요약 문자열을 반환합니다."""
        tags = [
            f"VWAP {'위✓' if self.price_above_vwap else '아래✗'}({self.vwap:,.0f})",
            f"저점 {'상승✓' if self.low_rising else '하락✗'}",
            f"눌림 {'적절✓' if self.is_valid_pullback else '불량✗'}({self.pullback_pct:+.1f}%)",
            f"등락 {'유효✓' if self.is_valid_change_rate else '무효✗'}({self.change_rate_pct:+.1f}%)",
            f"거래대금 {'충분✓' if self.is_valid_trading_value else '부족✗'}({self.trading_value//100_000_000}억)",
        ]
        return " | ".join(tags)


class MinuteAnalyzer:
    """분봉 데이터를 분석하는 클래스입니다."""

    def __init__(
        self,
        min_trading_value: int = 1_000_000_000,  # 거래대금 최소 10억
        pullback_min_pct: float = -7.0,           # 눌림목 최대 -7%
        pullback_max_pct: float = -1.0,           # 눌림목 최소 -1%
        change_rate_min: float = 2.0,             # 등락률 최소 +2%
        change_rate_max: float = 18.0,            # 등락률 최대 +18%
        low_rising_bars: int = 3,                 # 저점 상승 확인할 봉 수
    ) -> None:
        self.min_trading_value  = min_trading_value
        self.pullback_min_pct   = pullback_min_pct
        self.pullback_max_pct   = pullback_max_pct
        self.change_rate_min    = change_rate_min
        self.change_rate_max    = change_rate_max
        self.low_rising_bars    = low_rising_bars

    def analyze(self, bars: list[MinuteBar], prev_close: int) -> MinuteAnalysis | None:
        """분봉 리스트를 분석해서 MinuteAnalysis 결과를 반환합니다.

        Parameters
        ----------
        bars       : 분봉 리스트 (과거 → 최신 순)
        prev_close : 전일 종가 (등락률 계산용)

        Returns
        -------
        MinuteAnalysis or None (데이터 부족 시)
        """
        if len(bars) < self.low_rising_bars + 1:
            return None

        current_price = bars[-1].close_price
        if current_price <= 0 or prev_close <= 0:
            return None

        # ── VWAP 계산 ─────────────────────────────────────────────
        # VWAP = Σ(전형가 × 거래량) / Σ거래량
        # 전형가 = (고가 + 저가 + 종가) / 3
        total_pv = sum(
            ((b.high_price + b.low_price + b.close_price) / 3) * b.volume
            for b in bars
        )
        total_vol = sum(b.volume for b in bars)
        vwap = total_pv / total_vol if total_vol > 0 else current_price
        price_above_vwap = current_price > vwap

        # ── 당일 고가/저가 ────────────────────────────────────────
        day_high = max(b.high_price for b in bars)
        day_low  = min(b.low_price  for b in bars)

        # ── 눌림목 계산 (당일 고가 대비 현재가 위치) ──────────────
        pullback_pct = (current_price - day_high) / day_high * 100
        is_valid_pullback = self.pullback_min_pct <= pullback_pct <= self.pullback_max_pct

        # ── 분봉 저점 상승 여부 ───────────────────────────────────
        # 최근 N봉의 저점이 계속 높아지고 있으면 반등 신호
        recent_lows = [bars[-(i+1)].low_price for i in range(self.low_rising_bars)]
        low_rising = all(recent_lows[i] > recent_lows[i+1] for i in range(len(recent_lows)-1))

        # ── 등락률 ────────────────────────────────────────────────
        change_rate_pct = (current_price - prev_close) / prev_close * 100
        is_valid_change_rate = self.change_rate_min <= change_rate_pct <= self.change_rate_max

        # ── 거래대금 ──────────────────────────────────────────────
        # 누적 거래량 × 현재가로 근사
        trading_value = bars[-1].acc_volume * current_price
        is_valid_trading_value = trading_value >= self.min_trading_value

        return MinuteAnalysis(
            vwap=vwap,
            price_above_vwap=price_above_vwap,
            low_rising=low_rising,
            pullback_pct=pullback_pct,
            is_valid_pullback=is_valid_pullback,
            change_rate_pct=change_rate_pct,
            is_valid_change_rate=is_valid_change_rate,
            trading_value=trading_value,
            is_valid_trading_value=is_valid_trading_value,
            day_high=day_high,
            day_low=day_low,
        )
