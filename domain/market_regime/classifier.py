from __future__ import annotations

"""장세 분류기 (Market Regime Classifier).

현재 구현 (3단계):
- 이동평균 골든크로스/데드크로스 (5일선 vs 20일선)
- RSI 수치 + 방향(상승 중 / 하락 중) 판단
- MACD 골든크로스/데드크로스
- 거래량 급증 여부

RSI 방향 판단이 중요한 이유:
    RSI 28이라도 아직 하락 중이면 → 더 내려갈 수 있음 → 매수 금지
    RSI 28이고 상승 중이면       → 바닥 찍고 반등 시작 → 진짜 매수 타점

    엑셀 자료 기준:
    "30 이하 & 상승 ↑ + 거래량급증 + 골든크로스 → V자 반등 (강력 매수)"
    "30 이하 & 하락 ↓ + 데드크로스 → 추세 완전 붕괴 (폭락)"
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

        # ── RSI + 방향 ────────────────────────────────────────────
        rsi = self._calc_rsi(closes, self.config.rsi_period)
        rsi_direction = self._calc_rsi_direction(closes, self.config.rsi_period)
        # direction: +1 = 상승 중, -1 = 하락 중, 0 = 보합

        rsi_overbought  = rsi >= self.config.rsi_overbought
        rsi_oversold    = rsi <= self.config.rsi_oversold
        rsi_rising      = rsi_direction > 0
        rsi_falling     = rsi_direction < 0

        rsi_dir_tag = "↑" if rsi_rising else ("↓" if rsi_falling else "→")

        # ── MACD ──────────────────────────────────────────────────
        macd_line, signal_line = self._calc_macd(
            closes, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal,
        )
        macd_golden = macd_line > signal_line
        macd_dead   = macd_line < signal_line

        # ── 거래량 급증 ───────────────────────────────────────────
        volume_surge = self._is_volume_surge(volumes, self.config.volume_surge_ratio)
        volume_tag = " + 거래량급증" if volume_surge else ""

        # ── 최종 판단 ─────────────────────────────────────────────
        #
        # 핵심 원칙 (엑셀 자료 기반):
        #   RSI 과매도(30↓) + 상승 중 + MACD 골든크로스 = 강력 매수 타점
        #   RSI 과매도(30↓) + 하락 중 + MACD 데드크로스 = 추가 폭락 위험
        #   RSI 과매수(70↑) + 하락 중 + MACD 데드크로스 = 고점 반전
        #   RSI 과매수(70↑) + 상승 중 + MACD 골든크로스 = 강력 상승 지속

        # ── 상승장 판단 ───────────────────────────────────────────
        # 최강 상승: MA 상승 + RSI 정상 + RSI 상승 중 + MACD 골든크로스
        if ma_bullish and not rsi_overbought and rsi_rising and macd_golden:
            return MarketRegime.BULLISH, (
                f"MA 상승({short_ma:,.0f}>{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 상승 중 "
                f"+ MACD 골든크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # RSI 방향은 보합이지만 나머지 조건 충족
        if ma_bullish and not rsi_overbought and not rsi_falling and macd_golden:
            return MarketRegime.BULLISH, (
                f"MA 상승({short_ma:,.0f}>{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 보합 "
                f"+ MACD 골든크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # ── 하락장 판단 ───────────────────────────────────────────
        # 최강 하락: MA 하락 + RSI 정상 + RSI 하락 중 + MACD 데드크로스
        if ma_bearish and not rsi_oversold and rsi_falling and macd_dead:
            return MarketRegime.BEARISH, (
                f"MA 하락({short_ma:,.0f}<{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 하락 중 "
                f"+ MACD 데드크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # RSI 방향은 보합이지만 나머지 조건 충족
        if ma_bearish and not rsi_oversold and not rsi_rising and macd_dead:
            return MarketRegime.BEARISH, (
                f"MA 하락({short_ma:,.0f}<{long_ma:,.0f}) "
                f"+ RSI {rsi:.1f}{rsi_dir_tag} 보합 "
                f"+ MACD 데드크로스({macd_line:+.1f})"
                f"{volume_tag}"
            )

        # ── RSI 방향에 따른 추가 판단 ─────────────────────────────
        # MA 상승 + RSI 하락 중 → 모멘텀 약화, 상승 전환 아직 이름
        if ma_bullish and rsi_falling and macd_dead:
            return MarketRegime.SIDEWAYS, (
                f"MA 상승이나 RSI {rsi:.1f}{rsi_dir_tag} 하락 + MACD 데드크로스 "
                f"— 모멘텀 약화 중"
            )

        # MA 하락 + RSI 상승 중 → 반등 시도, 아직 추세 전환 확인 안 됨
        if ma_bearish and rsi_rising and macd_golden:
            return MarketRegime.SIDEWAYS, (
                f"MA 하락이나 RSI {rsi:.1f}{rsi_dir_tag} 상승 + MACD 골든크로스 "
                f"— 반등 시도 중"
            )

        # MA 상승 + MACD 아직 데드크로스
        if ma_bullish and not rsi_overbought and macd_dead:
            return MarketRegime.SIDEWAYS, (
                f"MA 상승이나 MACD 데드크로스({macd_line:+.1f}) RSI {rsi:.1f}{rsi_dir_tag} "
                f"— 전환 준비 중"
            )

        # RSI 극단값 처리
        if rsi_overbought:
            return MarketRegime.SIDEWAYS, (
                f"RSI {rsi:.1f}{rsi_dir_tag} 과매수 — 과열 구간, 신규 진입 위험"
            )

        if rsi_oversold and rsi_falling:
            return MarketRegime.BEARISH, (
                f"RSI {rsi:.1f}{rsi_dir_tag} 과매도 + 하락 중 — 추가 하락 가능"
            )

        if rsi_oversold and rsi_rising:
            return MarketRegime.SIDEWAYS, (
                f"RSI {rsi:.1f}{rsi_dir_tag} 과매도 + 반등 중 — MACD 확인 후 진입 검토"
            )

        # 그 외 모든 경우
        return MarketRegime.SIDEWAYS, (
            f"MA {short_ma:,.0f}/{long_ma:,.0f} "
            f"RSI {rsi:.1f}{rsi_dir_tag} "
            f"MACD {macd_line:+.1f} — 추세 불명확"
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
        """RSI의 방향(기울기)을 판단합니다.

        최근 N일간의 RSI 변화를 보고 방향을 반환합니다.
        lookback: 비교할 과거 일수 (기본 3일)

        Returns
        -------
        +1 : RSI 상승 중 (반등 신호)
        -1 : RSI 하락 중 (추가 하락 위험)
         0 : 보합 (방향성 불명확)
        """
        if len(closes) < period + lookback + 1:
            return 0

        # 현재 RSI와 N일 전 RSI를 비교
        rsi_now  = cls._calc_rsi(closes, period)
        rsi_prev = cls._calc_rsi(closes[:-lookback], period)

        diff = rsi_now - rsi_prev

        # 변화폭이 2 이상이어야 방향성 있다고 판단 (노이즈 제거)
        if diff >= 2.0:
            return 1   # 상승 중
        if diff <= -2.0:
            return -1  # 하락 중
        return 0       # 보합

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
