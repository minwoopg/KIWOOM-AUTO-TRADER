from __future__ import annotations

"""장세 분류기 (Market Regime Classifier).

현재 구현 (2단계):
- 이동평균 골든크로스/데드크로스 (5일선 vs 20일선)
- RSI 필터 (과매수/과매도 여부)
- MACD 골든크로스/데드크로스 (추세 방향 확인)
- 거래량 급증 여부 (추세 신뢰도 보조 확인)
"""

from config.settings import MarketRegimeConfig
from domain.models import MarketRegime, PriceBar


class MarketRegimeClassifier:
    """일봉 데이터를 받아 장세를 분류하는 클래스입니다."""

    def __init__(self, config: MarketRegimeConfig) -> None:
        self.config = config

    def classify(self, bars: list[PriceBar]) -> tuple[MarketRegime, str]:
        """장세를 분류하고 (결과, 사유)를 함께 반환합니다."""
        min_bars = max(
            self.config.long_ma_days,
            self.config.rsi_period + 1,
            self.config.macd_slow + self.config.macd_signal,
        )
        if len(bars) < min_bars:
            return (
                MarketRegime.UNKNOWN,
                f"데이터 부족: {len(bars)}봉 (최소 {min_bars}봉 필요)",
            )

        closes  = [bar.close_price for bar in bars]
        volumes = [bar.volume for bar in bars]

        # ── 이동평균 ──────────────────────────────────────────────
        short_ma = self._calc_ma(closes, self.config.short_ma_days)
        long_ma  = self._calc_ma(closes, self.config.long_ma_days)
        current_price = closes[-1]

        ma_bullish = short_ma > long_ma and current_price > short_ma
        ma_bearish = short_ma < long_ma and current_price < short_ma

        # ── RSI ───────────────────────────────────────────────────
        rsi = self._calc_rsi(closes, self.config.rsi_period)
        rsi_overbought = rsi >= self.config.rsi_overbought
        rsi_oversold   = rsi <= self.config.rsi_oversold

        # ── MACD ──────────────────────────────────────────────────
        macd_line, signal_line = self._calc_macd(
            closes, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal,
        )
        macd_golden = macd_line > signal_line
        macd_dead   = macd_line < signal_line

        # ── 거래량 급증 여부 ───────────────────────────────────────
        volume_surge = self._is_volume_surge(volumes, self.config.volume_surge_ratio)
        volume_tag = " + 거래량급증" if volume_surge else ""

        # ── 최종 판단 ─────────────────────────────────────────────
        # 상승장: MA 상승 + RSI 과매수 아님 + MACD 골든크로스
        if ma_bullish and not rsi_overbought and macd_golden:
            return MarketRegime.BULLISH, (
                f"MA 상승({short_ma:,.0f}>{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f} 정상 "
                f"+ MACD 골든크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # 하락장: MA 하락 + RSI 과매도 아님 + MACD 데드크로스
        if ma_bearish and not rsi_oversold and macd_dead:
            return MarketRegime.BEARISH, (
                f"MA 하락({short_ma:,.0f}<{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f} 정상 "
                f"+ MACD 데드크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # MA 상승인데 MACD 아직 데드크로스 → 상승 전환 준비 중
        if ma_bullish and not rsi_overbought and macd_dead:
            return MarketRegime.SIDEWAYS, (
                f"MA 상승이나 MACD 아직 데드크로스({macd_line:+.1f}) — 전환 준비 중"
            )

        # MA 하락인데 MACD 골든크로스 → 기술적 반등 시도
        if ma_bearish and not rsi_oversold and macd_golden:
            return MarketRegime.SIDEWAYS, (
                f"MA 하락이나 MACD 골든크로스({macd_line:+.1f}) — 기술적 반등 시도"
            )

        # RSI 극단값
        if ma_bullish and rsi_overbought:
            return MarketRegime.SIDEWAYS, f"MA 상승이나 RSI {rsi:.1f} 과매수 — 과열 구간"

        if ma_bearish and rsi_oversold:
            return MarketRegime.SIDEWAYS, f"MA 하락이나 RSI {rsi:.1f} 과매도 — 반등 가능성"

        # 그 외
        return MarketRegime.SIDEWAYS, (
            f"MA {short_ma:,.0f}/{long_ma:,.0f} RSI {rsi:.1f} MACD {macd_line:+.1f} — 추세 불명확"
        )

    # ── 지표 계산 헬퍼 ────────────────────────────────────────────

    @staticmethod
    def _calc_ma(closes: list[float], period: int) -> float:
        if len(closes) < period:
            return 0.0
        return sum(closes[-period:]) / period

    @staticmethod
    def _calc_rsi(closes: list[float], period: int) -> float:
        if len(closes) < period + 1:
            return 50.0
        changes = [closes[i] - closes[i - 1] for i in range(-period, 0)]
        gains  = [c for c in changes if c > 0]
        losses = [abs(c) for c in changes if c < 0]
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    @staticmethod
    def _calc_ema(values: list[float], period: int) -> list[float]:
        """지수 이동평균(EMA) 시리즈를 계산합니다. MACD에 사용됩니다."""
        if len(values) < period:
            return []
        k = 2 / (period + 1)
        emas = [sum(values[:period]) / period]
        for price in values[period:]:
            emas.append(price * k + emas[-1] * (1 - k))
        return emas

    @classmethod
    def _calc_macd(
        cls, closes: list[float], fast: int, slow: int, signal_period: int
    ) -> tuple[float, float]:
        """MACD 라인과 시그널 라인의 현재값을 반환합니다.

        Returns: (macd_line, signal_line)
            macd_line > signal_line → 골든크로스
            macd_line < signal_line → 데드크로스
        """
        fast_emas = cls._calc_ema(closes, fast)
        slow_emas = cls._calc_ema(closes, slow)
        if not fast_emas or not slow_emas:
            return 0.0, 0.0

        # fast/slow EMA 길이 맞추기
        diff = len(fast_emas) - len(slow_emas)
        aligned_fast = fast_emas[diff:] if diff > 0 else fast_emas
        aligned_slow = slow_emas[-diff:] if diff < 0 else slow_emas

        macd_series = [f - s for f, s in zip(aligned_fast, aligned_slow)]
        if len(macd_series) < signal_period:
            return 0.0, 0.0

        signal_emas = cls._calc_ema(macd_series, signal_period)
        if not signal_emas:
            return 0.0, 0.0

        return macd_series[-1], signal_emas[-1]

    @staticmethod
    def _is_volume_surge(volumes: list[int], ratio: float, period: int = 20) -> bool:
        """오늘 거래량이 최근 N일 평균 대비 ratio배 이상이면 급증으로 판단합니다."""
        if len(volumes) < period + 1:
            return False
        avg_volume = sum(volumes[-period - 1:-1]) / period
        if avg_volume == 0:
            return False
        return (volumes[-1] / avg_volume) >= ratio
