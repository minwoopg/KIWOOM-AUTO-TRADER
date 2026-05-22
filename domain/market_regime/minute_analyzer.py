from __future__ import annotations

"""분봉 분석기 (Minute Bar Analyzer).

분봉 데이터로 단타 진입 타이밍을 판단하는 지표를 계산합니다.

계산하는 지표:
    - VWAP (당일 거래량 가중 평균가)
    - 분봉 저점 상승 여부 (눌림목 후 반등 확인)
    - 당일 고가 대비 현재가 위치 (눌림목 구간 확인)
    - 당일 등락률
    - 거래대금 (거래량 × 가격)
    - 조건 C: 상승 추세 중 눌림목 (MA5>MA20 + 등락률 -1%~-8%)
"""

from dataclasses import dataclass
from domain.models import MinuteBar


@dataclass
class MinuteAnalysis:
    """분봉 분석 결과를 담는 객체입니다."""

    vwap: float
    price_above_vwap: bool
    low_rising: bool
    pullback_pct: float
    is_valid_pullback: bool
    change_rate_pct: float
    is_valid_change_rate: bool
    rebound_pct: float           # 당일 저점 대비 반등률
    is_valid_rebound: bool       # B조건: 반등률 유효 여부 (+2% 이상 + VWAP 위)
    trading_value: int
    is_valid_trading_value: bool
    day_high: int
    day_low: int
    is_valid_pulldown: bool      # C조건: 상승 추세 중 눌림목 (-1%~-8% + MA5>MA20)
    ma5_above_ma20: bool         # MA5 > MA20 여부 (상승 추세 확인용)

    def score(self) -> int:
        """진입 타이밍 점수를 계산합니다 (0~5점)."""
        return sum([
            self.price_above_vwap,
            self.low_rising,
            self.is_valid_pullback,
            self.is_valid_change_rate or self.is_valid_rebound or self.is_valid_pulldown,
            self.is_valid_trading_value,
        ])

    def summary(self) -> str:
        """로그용 요약 문자열을 반환합니다."""
        change_tag   = f"등락 {'유효✓' if self.is_valid_change_rate else '무효✗'}({self.change_rate_pct:+.1f}%)"
        rebound_tag  = f"반등 {'유효✓' if self.is_valid_rebound else '무효✗'}({self.rebound_pct:+.1f}%)"
        pulldown_tag = f"눌림목C {'유효✓' if self.is_valid_pulldown else '무효✗'}(MA5>MA20:{'✓' if self.ma5_above_ma20 else '✗'})"
        tags = [
            f"VWAP {'위✓' if self.price_above_vwap else '아래✗'}({self.vwap:,.0f})",
            f"저점 {'상승✓' if self.low_rising else '하락✗'}",
            f"눌림 {'적절✓' if self.is_valid_pullback else '불량✗'}({self.pullback_pct:+.1f}%)",
            change_tag,
            rebound_tag,
            pulldown_tag,
            f"거래대금 {'충분✓' if self.is_valid_trading_value else '부족✗'}({self.trading_value//100_000_000}억)",
        ]
        return " | ".join(tags)


class MinuteAnalyzer:
    """분봉 데이터를 분석하는 클래스입니다."""

    def __init__(
        self,
        min_trading_value: int = 1_000_000_000,
        pullback_min_pct: float = -7.0,
        pullback_max_pct: float = -1.0,
        change_rate_min: float = 2.0,
        change_rate_max: float = 18.0,
        low_rising_bars: int = 3,
        rebound_min_pct: float = 2.0,
    ) -> None:
        self.min_trading_value  = min_trading_value
        self.pullback_min_pct   = pullback_min_pct
        self.pullback_max_pct   = pullback_max_pct
        self.change_rate_min    = change_rate_min
        self.change_rate_max    = change_rate_max
        self.low_rising_bars    = low_rising_bars
        self.rebound_min_pct    = rebound_min_pct
        # 조건 C: 상승 추세 중 눌림목
        self.pulldown_min_pct   = -8.0   # 당일 등락률 하한
        self.pulldown_max_pct   = -1.0   # 당일 등락률 상한

    def analyze(self, bars: list[MinuteBar], prev_close: int) -> MinuteAnalysis | None:
        if len(bars) < self.low_rising_bars + 1:
            return None

        current_price = bars[-1].close_price
        if current_price <= 0 or prev_close <= 0:
            return None

        # ── VWAP 계산 ─────────────────────────────────────────────
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

        # ── 눌림목 계산 ───────────────────────────────────────────
        pullback_pct = (current_price - day_high) / day_high * 100
        is_valid_pullback = self.pullback_min_pct <= pullback_pct <= self.pullback_max_pct

        # ── 분봉 저점 상승 여부 ───────────────────────────────────
        recent_lows = [bars[-(i+1)].low_price for i in range(self.low_rising_bars)]
        low_rising = all(recent_lows[i] > recent_lows[i+1] for i in range(len(recent_lows)-1))

        # ── 등락률 ────────────────────────────────────────────────
        change_rate_pct = (current_price - prev_close) / prev_close * 100
        is_valid_change_rate = self.change_rate_min <= change_rate_pct <= self.change_rate_max

        # ── B조건: 당일 저점 대비 반등률 ─────────────────────────
        rebound_pct = (current_price - day_low) / day_low * 100 if day_low > 0 else 0.0
        is_valid_rebound = (
            rebound_pct >= self.rebound_min_pct
            and price_above_vwap
        )

        # ── 거래대금 ──────────────────────────────────────────────
        trading_value = bars[-1].acc_volume * current_price
        is_valid_trading_value = trading_value >= self.min_trading_value

        # ── C조건: 상승 추세 중 눌림목 ───────────────────────────
        closes = [b.close_price for b in bars if b.close_price > 0]
        ma5  = sum(closes[-5:])  / min(5,  len(closes)) if closes else 0
        ma20 = sum(closes[-20:]) / min(20, len(closes)) if closes else 0
        ma5_above_ma20 = ma5 > ma20

        is_valid_pulldown = (
            self.pulldown_min_pct <= change_rate_pct <= self.pulldown_max_pct
            and ma5_above_ma20
            and price_above_vwap
        )

        return MinuteAnalysis(
            vwap=vwap,
            price_above_vwap=price_above_vwap,
            low_rising=low_rising,
            pullback_pct=pullback_pct,
            is_valid_pullback=is_valid_pullback,
            change_rate_pct=change_rate_pct,
            is_valid_change_rate=is_valid_change_rate,
            rebound_pct=rebound_pct,
            is_valid_rebound=is_valid_rebound,
            trading_value=trading_value,
            is_valid_trading_value=is_valid_trading_value,
            day_high=day_high,
            day_low=day_low,
            is_valid_pulldown=is_valid_pulldown,
            ma5_above_ma20=ma5_above_ma20,
        )
