from __future__ import annotations

"""장세 분류기 (Market Regime Classifier).

이 모듈은 일봉 데이터를 받아서 현재가 장세를 판단합니다.

현재 구현 (1단계):
- 이동평균 골든크로스/데드크로스 (5일선 vs 20일선)
- RSI 필터 (과매수/과매도 여부)

나중에 지표를 추가하려면?
- 이 파일의 _calc_* 메서드를 하나 추가하고
- classify() 안의 판단 로직에 조건을 덧붙이면 됩니다.
- 다른 파일은 건드릴 필요가 없습니다.

사용 예:
    bars = broker.get_daily_prices("005930", days=30)
    classifier = MarketRegimeClassifier(config)
    regime = classifier.classify(bars)
    # regime → MarketRegime.BULLISH / SIDEWAYS / BEARISH / UNKNOWN
"""

from config.settings import MarketRegimeConfig
from domain.models import MarketRegime, PriceBar


class MarketRegimeClassifier:
    """일봉 데이터를 받아 장세를 분류하는 클래스입니다."""

    def __init__(self, config: MarketRegimeConfig) -> None:
        self.config = config

    def classify(self, bars: list[PriceBar]) -> tuple[MarketRegime, str]:
        """장세를 분류하고 (결과, 사유) 를 함께 반환합니다.

        Parameters
        ----------
        bars:
            최신 날짜가 맨 뒤(bars[-1])에 오도록 정렬된 일봉 리스트

        Returns
        -------
        (MarketRegime, reason_str)
        """

        min_bars = max(self.config.long_ma_days, self.config.rsi_period + 1)
        if len(bars) < min_bars:
            return (
                MarketRegime.UNKNOWN,
                f"데이터 부족: {len(bars)}봉 (최소 {min_bars}봉 필요)",
            )

        closes = [bar.close_price for bar in bars]

        # ── 이동평균 계산 ──────────────────────────────────────────
        short_ma = self._calc_ma(closes, self.config.short_ma_days)
        long_ma = self._calc_ma(closes, self.config.long_ma_days)
        current_price = closes[-1]

        ma_bullish = short_ma > long_ma and current_price > short_ma
        ma_bearish = short_ma < long_ma and current_price < short_ma

        # ── RSI 계산 ───────────────────────────────────────────────
        rsi = self._calc_rsi(closes, self.config.rsi_period)
        rsi_overbought = rsi >= self.config.rsi_overbought   # 과매수 → 상승 신호 약화
        rsi_oversold   = rsi <= self.config.rsi_oversold     # 과매도 → 하락 신호 약화

        # ── 최종 판단 ──────────────────────────────────────────────
        #
        # 상승장 조건:
        #   - 단기MA > 장기MA 이고 현재가 > 단기MA (추세 확인)
        #   - AND RSI 과매수(70+) 가 아닐 것 (이미 너무 오른 상태 제외)
        #
        # 하락장 조건:
        #   - 단기MA < 장기MA 이고 현재가 < 단기MA (추세 확인)
        #   - AND RSI 과매도(30-) 가 아닐 것 (반등 가능성 있는 상태 제외)
        #
        # 횡보장: 위 두 조건에 해당하지 않는 모든 경우

        if ma_bullish and not rsi_overbought:
            reason = (
                f"단기MA({self.config.short_ma_days}일) {short_ma:,.0f} > "
                f"장기MA({self.config.long_ma_days}일) {long_ma:,.0f}, "
                f"현재가 {current_price:,} > 단기MA, "
                f"RSI {rsi:.1f} (과매수 아님)"
            )
            return MarketRegime.BULLISH, reason

        if ma_bullish and rsi_overbought:
            # MA는 상승 신호인데 RSI가 과매수 → 횡보로 판단 (쉬어가는 구간)
            reason = (
                f"MA 상승 신호이나 RSI {rsi:.1f} 과매수 — 일시적 과열로 보고 횡보 처리"
            )
            return MarketRegime.SIDEWAYS, reason

        if ma_bearish and not rsi_oversold:
            reason = (
                f"단기MA({self.config.short_ma_days}일) {short_ma:,.0f} < "
                f"장기MA({self.config.long_ma_days}일) {long_ma:,.0f}, "
                f"현재가 {current_price:,} < 단기MA, "
                f"RSI {rsi:.1f} (과매도 아님)"
            )
            return MarketRegime.BEARISH, reason

        if ma_bearish and rsi_oversold:
            # MA는 하락 신호인데 RSI가 과매도 → 횡보 (반등 가능성)
            reason = (
                f"MA 하락 신호이나 RSI {rsi:.1f} 과매도 — 반등 가능성으로 횡보 처리"
            )
            return MarketRegime.SIDEWAYS, reason

        # 횡보 사유: 어느 조건도 뚜렷하게 충족 못 함
        reason = (
            f"단기MA {short_ma:,.0f} / 장기MA {long_ma:,.0f} / "
            f"현재가 {current_price:,} / RSI {rsi:.1f} — "
            f"뚜렷한 추세 없음"
        )
        return MarketRegime.SIDEWAYS, reason

    # ── 지표 계산 헬퍼 ────────────────────────────────────────────────────────
    # 나중에 MACD, 볼린저밴드, ADX 등을 추가할 때
    # 같은 패턴으로 _calc_xxx 메서드를 추가하면 됩니다.

    @staticmethod
    def _calc_ma(closes: list[float], period: int) -> float:
        """단순 이동평균(SMA)을 계산합니다."""
        if len(closes) < period:
            return 0.0
        return sum(closes[-period:]) / period

    @staticmethod
    def _calc_rsi(closes: list[float], period: int) -> float:
        """RSI(Relative Strength Index)를 계산합니다.

        Wilder의 방식이 아닌 단순 평균 방식을 사용합니다.
        - 최근 N일의 상승폭 평균 / (상승폭 평균 + 하락폭 평균) × 100
        - 계산이 단순하고 검증하기 쉬워 1단계에 적합합니다.
        """
        if len(closes) < period + 1:
            return 50.0  # 데이터 부족 시 중립값 반환

        changes = [closes[i] - closes[i - 1] for i in range(-period, 0)]
        gains = [c for c in changes if c > 0]
        losses = [abs(c) for c in changes if c < 0]

        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
