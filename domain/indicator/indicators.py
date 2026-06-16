"""
ATR(Average True Range)과 볼린저밴드 계산 모듈.

단타 및 스윙 전략에서 로그 기록용으로 사용합니다.
실제 매수 차단은 데이터 축적 후 결정합니다.
"""
from __future__ import annotations
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Sequence, Optional


@dataclass(frozen=True)
class ATRResult:
    atr: float            # ATR 값 (원화 기준)
    atr_pct: float        # ATR / 현재가 × 100 (%)
    period: int


@dataclass(frozen=True)
class BollingerResult:
    mid: float
    upper: float
    lower: float
    percent_b: float      # (현재가 - 하단) / (상단 - 하단), 0~1 범위
    bandwidth_pct: float  # (상단 - 하단) / 중심선 × 100 (%)
    position: str         # BELOW_LOWER / LOWER_ZONE / MID_LOWER / MID_UPPER / UPPER_ZONE / ABOVE_UPPER


def calc_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    current_price: Optional[float] = None,
) -> Optional[ATRResult]:
    """
    ATR 계산 (Wilder 방식 / 단순 TR 평균).

    Args:
        highs, lows, closes: 일봉 데이터 (과거→최신 순, 최소 period+1개)
        period: ATR 기간 (기본 14)
        current_price: 장중 현재가 (오늘 마지막 봉 대용)
    """
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None

    h = list(highs)
    l = list(lows)
    c = list(closes)

    # 장중: 오늘 봉을 현재가로 대체
    if current_price and current_price > 0:
        h[-1] = max(h[-1], float(current_price))
        l[-1] = min(l[-1], float(current_price))
        c[-1] = float(current_price)

    trs = []
    for i in range(1, len(c)):
        tr = max(
            h[i] - l[i],
            abs(h[i] - c[i-1]),
            abs(l[i] - c[i-1]),
        )
        trs.append(tr)

    recent = trs[-period:]
    if not recent:
        return None

    atr = mean(recent)
    ref_price = c[-1] if c[-1] > 0 else atr
    return ATRResult(
        atr=round(atr, 2),
        atr_pct=round(atr / ref_price * 100, 3),
        period=period,
    )


def calc_bollinger(
    closes: Sequence[float],
    current_price: Optional[float] = None,
    period: int = 20,
    stddev_mult: float = 2.0,
) -> Optional[BollingerResult]:
    """
    볼린저밴드 계산 (일봉 종가 기준).

    Args:
        closes: 일봉 종가 (과거→최신 순, 최소 period개)
        current_price: 장중 현재가
        period: 이동평균 기간 (기본 20)
        stddev_mult: 표준편차 배수 (기본 2.0)
    """
    if len(closes) < period:
        return None

    vals = list(closes)
    if current_price and current_price > 0:
        vals[-1] = float(current_price)

    window = [float(v) for v in vals[-period:]]
    mid = mean(window)
    sigma = pstdev(window)

    if mid <= 0 or sigma <= 0:
        return None

    upper = mid + stddev_mult * sigma
    lower = mid - stddev_mult * sigma

    cur = float(vals[-1])
    percent_b = (cur - lower) / (upper - lower) if upper > lower else 0.5
    bandwidth_pct = (upper - lower) / mid * 100.0

    if percent_b < 0:
        pos = "BELOW_LOWER"
    elif percent_b < 0.2:
        pos = "LOWER_ZONE"
    elif percent_b < 0.5:
        pos = "MID_LOWER"
    elif percent_b < 0.8:
        pos = "MID_UPPER"
    elif percent_b <= 1.0:
        pos = "UPPER_ZONE"
    else:
        pos = "ABOVE_UPPER"

    return BollingerResult(
        mid=round(mid, 2),
        upper=round(upper, 2),
        lower=round(lower, 2),
        percent_b=round(percent_b, 4),
        bandwidth_pct=round(bandwidth_pct, 2),
        position=pos,
    )
