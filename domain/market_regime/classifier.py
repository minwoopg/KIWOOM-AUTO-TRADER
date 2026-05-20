from __future__ import annotations

"""장세 분류기 — REBOUND 장세 추가 버전."""

from config.settings import MarketRegimeConfig
from domain.models import MarketRegime, PriceBar


class MarketRegimeClassifier:

    def __init__(self, config: MarketRegimeConfig) -> None:
        self.config = config

    def classify(self, bars: list[PriceBar]) -> tuple[MarketRegime, str]:
        min_bars = max(self.config.long_ma_days, self.config.rsi_period + 1)
        macd_min_bars = self.config.macd_slow + self.config.macd_signal
        if len(bars) < min_bars:
            return MarketRegime.UNKNOWN, f"데이터 부족: {len(bars)}봉 (최소 {min_bars}봉 필요)"

        closes  = [bar.close_price for bar in bars]
        volumes = [bar.volume for bar in bars]

        valid_closes = [c for c in closes if c > 0]
        if len(valid_closes) < min_bars:
            return MarketRegime.UNKNOWN, "유효하지 않은 일봉 데이터: 종가 0인 봉이 너무 많음"
        if len(set(closes[-5:])) == 1:
            return MarketRegime.UNKNOWN, f"유효하지 않은 일봉 데이터: 최근 5일 종가가 모두 동일 ({closes[-1]:,}원)"

        rsi_check = self._calc_rsi(closes, self.config.rsi_period)
        if rsi_check == 0.0 or rsi_check == 100.0:
            return MarketRegime.UNKNOWN, f"유효하지 않은 일봉 데이터: RSI {rsi_check:.1f} 극단값"

        use_macd = len(bars) >= macd_min_bars

        short_ma = self._calc_ma(closes, self.config.short_ma_days)
        long_ma  = self._calc_ma(closes, self.config.long_ma_days)
        current_price = closes[-1]

        ma_bullish = short_ma > long_ma and current_price > short_ma
        ma_bearish = short_ma < long_ma and current_price < short_ma

        rsi = self._calc_rsi(closes, self.config.rsi_period)
        rsi_direction = self._calc_rsi_direction(closes, self.config.rsi_period)

        rsi_overbought = rsi >= self.config.rsi_overbought
        rsi_oversold   = rsi <= self.config.rsi_oversold
        rsi_rising     = rsi_direction > 0
        rsi_falling    = rsi_direction < 0
        rsi_dir_tag    = "↑" if rsi_rising else ("↓" if rsi_falling else "→")

        if use_macd:
            macd_line, signal_line = self._calc_macd(
                closes, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal
            )
            macd_golden = macd_line > signal_line
            macd_dead   = macd_line < signal_line
        else:
            macd_golden = ma_bullish
            macd_dead   = ma_bearish
            macd_line   = 0.0
            signal_line = 0.0

        volume_surge = self._is_volume_surge(volumes, self.config.volume_surge_ratio)
        volume_tag   = " + 거래량 급증✓" if volume_surge else ""
        macd_mode    = "" if use_macd else " [MACD 데이터 부족-MA 대체]"

        # ── REBOUND 판단 (바닥권 반등 초입) ──────────────────────
        # 조건: RSI 과매도 + RSI Signal 골든크로스 + MACD 히스토그램 반전
        if use_macd and rsi_oversold:
            rsi_signal_val, rsi_signal_cross = self._calc_rsi_signal(closes, self.config.rsi_period)

            # MACD 히스토그램 반전 여부 (음수 구간에서 증가 시작)
            macd_hist_now  = macd_line - signal_line
            if len(bars) >= macd_min_bars + 2:
                macd_prev, sig_prev = self._calc_macd(
                    closes[:-2], self.config.macd_fast, self.config.macd_slow, self.config.macd_signal
                )
                macd_hist_prev = macd_prev - sig_prev
                hist_reversing = macd_hist_now < 0 and macd_hist_now > macd_hist_prev
            else:
                hist_reversing = False

            if rsi_signal_cross == 1 and hist_reversing and not ma_bullish:
                return MarketRegime.REBOUND, (
                    f"바닥권 반등 — RSI {rsi:.1f}↑ Signal 골든크로스 "
                    f"+ MACD 히스토그램 반전({macd_hist_now:+.1f})"
                    f"{volume_tag}"
                )

        # ── 상승장 판단 ───────────────────────────────────────────
        if ma_bullish and not rsi_overbought and rsi_rising and macd_golden:
            return MarketRegime.BULLISH, (
                f"MA 상승({short_ma:,.0f}>{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 상승 중 "
                f"+ MACD 골든크로스({macd_line:+.1f})"
                f"{volume_tag}{macd_mode}"
            )

        if ma_bullish and not rsi_overbought and not rsi_falling and macd_golden:
            return MarketRegime.BULLISH, (
                f"MA 상승({short_ma:,.0f}>{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 보합 "
                f"+ MACD 골든크로스({macd_line:+.1f})"
                f"{volume_tag}{macd_mode}"
            )

        # ── 하락장 판단 ───────────────────────────────────────────
        if ma_bearish and not rsi_oversold and rsi_falling and macd_dead:
            return MarketRegime.BEARISH, (
                f"MA 하락({short_ma:,.0f}<{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 하락 중 "
                f"+ MACD 데드크로스({macd_line:+.1f})"
                f"{volume_tag}{macd_mode}"
            )

        if ma_bearish and not rsi_oversold and not rsi_rising and macd_dead:
            return MarketRegime.BEARISH, (
                f"MA 하락({short_ma:,.0f}<{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 보합 "
                f"+ MACD 데드크로스({macd_line:+.1f})"
                f"{volume_tag}{macd_mode}"
            )

        if ma_bullish and rsi_falling and macd_dead:
            return MarketRegime.SIDEWAYS, (
                f"MA 상승이나 RSI {rsi:.1f}{rsi_dir_tag} 하락 + MACD 데드크로스 — 모멘텀 약화 중"
            )

        if ma_bearish and rsi_rising and macd_golden:
            return MarketRegime.SIDEWAYS, (
                f"MA 하락이나 RSI {rsi:.1f}{rsi_dir_tag} 상승 + MACD 골든크로스 — 반등 시도 중"
            )

        if ma_bullish and not rsi_overbought and macd_dead:
            return MarketRegime.SIDEWAYS, (
                f"MA 상승이나 MACD 데드크로스({macd_line:+.1f}) RSI {rsi:.1f}{rsi_dir_tag} — 전환 준비 중"
            )

        if rsi_overbought:
            return MarketRegime.SIDEWAYS, f"RSI {rsi:.1f}{rsi_dir_tag} 과매수 — 과열 구간"

        if rsi_oversold and rsi_falling:
            return MarketRegime.BEARISH, f"RSI {rsi:.1f}{rsi_dir_tag} 과매도 + 하락 중 — 추가 하락 가능"

        if rsi_oversold and rsi_rising:
            return MarketRegime.SIDEWAYS, f"RSI {rsi:.1f}{rsi_dir_tag} 과매도 + 반등 중 — MACD 확인 후 진입 검토"

        return MarketRegime.SIDEWAYS, (
            f"MA {short_ma:,.0f}/{long_ma:,.0f} RSI {rsi:.1f}{rsi_dir_tag} MACD {macd_line:+.1f} — 추세 불명확"
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

    @classmethod
    def _calc_rsi_direction(cls, closes: list[float], period: int, lookback: int = 3) -> int:
        if len(closes) < period + lookback + 1:
            return 0
        rsi_now  = cls._calc_rsi(closes, period)
        rsi_prev = cls._calc_rsi(closes[:-lookback], period)
        diff = rsi_now - rsi_prev
        if diff >= 2.0:
            return 1
        if diff <= -2.0:
            return -1
        return 0

    @classmethod
    def _calc_rsi_signal(
        cls, closes: list[float], rsi_period: int, signal_period: int = 9
    ) -> tuple[float, int]:
        """RSI Signal선(EMA)과 골든크로스 여부를 반환합니다.

        Returns: (signal_value, cross_direction)
            cross_direction: +1 골든크로스, -1 데드크로스, 0 보합
        """
        needed = rsi_period + signal_period + 2
        if len(closes) < needed:
            return 0.0, 0

        # RSI 시리즈 생성 (signal_period + 2개)
        rsi_series = []
        for i in range(signal_period + 2):
            end = len(closes) - (signal_period + 1 - i)
            if end < rsi_period + 1:
                rsi_series.append(50.0)
            else:
                rsi_series.append(cls._calc_rsi(closes[:end], rsi_period))

        signal_emas = cls._calc_ema(rsi_series, signal_period)
        if len(signal_emas) < 2:
            return 0.0, 0

        rsi_now    = cls._calc_rsi(closes, rsi_period)
        rsi_prev   = cls._calc_rsi(closes[:-1], rsi_period)
        signal_now  = signal_emas[-1]
        signal_prev = signal_emas[-2]

        cross = 0
        if rsi_prev <= signal_prev and rsi_now > signal_now:
            cross = 1   # 골든크로스
        elif rsi_prev >= signal_prev and rsi_now < signal_now:
            cross = -1  # 데드크로스

        return signal_now, cross

    @classmethod
    def _calc_macd_hist_direction(
        cls, closes: list[float], fast: int, slow: int, signal_period: int, lookback: int = 2
    ) -> int:
        if len(closes) < slow + signal_period + lookback:
            return 0
        macd_now,  sig_now  = cls._calc_macd(closes, fast, slow, signal_period)
        macd_prev, sig_prev = cls._calc_macd(closes[:-lookback], fast, slow, signal_period)
        hist_now  = macd_now  - sig_now
        hist_prev = macd_prev - sig_prev
        if hist_now > hist_prev + 0.1:
            return 1
        if hist_now < hist_prev - 0.1:
            return -1
        return 0

    @staticmethod
    def _calc_ema(values: list[float], period: int) -> list[float]:
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
        fast_emas = cls._calc_ema(closes, fast)
        slow_emas = cls._calc_ema(closes, slow)
        if not fast_emas or not slow_emas:
            return 0.0, 0.0
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
        if len(volumes) < period + 1:
            return False
        avg_volume = sum(volumes[-period - 1:-1]) / period
        if avg_volume == 0:
            return False
        return (volumes[-1] / avg_volume) >= ratio
