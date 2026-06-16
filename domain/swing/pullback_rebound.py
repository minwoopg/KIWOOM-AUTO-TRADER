"""
일봉 기준 눌림목-반등 탐지 모듈.

분봉 V자 탐지(_detect_v_rebound)와 컨셉은 비슷하지만,
5~20일 구간의 일봉을 대상으로 "고점 → 저점 → 막 반등 시작" 패턴을 찾는다.

설계 (2026-06-16 확정):
    1. 5~20일 구간 전체에서 [고점→저점] 후보를 탐색
       - 눌림폭이 -10~-20% 범위에 가장 가까운 구간을 채택
    2. 저점 시점이 오늘(0일 전) 또는 어제(1일 전)면 진입 후보
       - 그보다 오래된 저점이면 "이미 반등 다 끝남" 판단, 차단
    3. 아직 반등이 확정되지 않은 상태에서 선점 매수하는 전략이므로
       리스크 관리가 중요: 손절선은 [저점가 이탈] OR [매수가 대비 -5%]
       중 먼저 닿는 쪽으로 호출부에서 계산한다 (이 모듈은 탐지만 담당).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from domain.models import PriceBar


@dataclass(frozen=True)
class PullbackReboundResult:
    """눌림목-반등 탐지 결과."""

    detected:        bool     # 패턴 감지 여부
    peak_price:      int      # 고점가
    peak_days_ago:   int      # 고점이 며칠 전이었는지
    trough_price:    int      # 저점가
    trough_days_ago: int      # 저점이 며칠 전이었는지 (0=오늘, 1=어제)
    drawdown_pct:    float    # 고점 대비 저점 낙폭 (%, 음수)
    rebound_pct:     float    # 저점 대비 현재가 반등폭 (%, 0 이상)
    fail_reason:     str      # 감지 실패 시 사유

    @property
    def stop_loss_price(self) -> int:
        """
        권장 손절가.
        저점가를 이탈하면 눌림목 추정이 틀렸다고 보고 즉시 손절.
        """
        return self.trough_price


def detect_pullback_rebound(
    bars: list[PriceBar],
    current_price: int,
    lookback_min: int = 5,
    lookback_max: int = 20,
    drawdown_min_pct: float = -20.0,
    drawdown_max_pct: float = -10.0,
    max_trough_age_days: int = 1,
) -> Optional[PullbackReboundResult]:
    """
    5~20일 구간에서 [고점 → 저점] 눌림 패턴을 탐색하고,
    저점이 최근(오늘 또는 어제)인지 확인한다.

    Args:
        bars: 일봉 데이터 (과거→최신 순, 최신 봉은 "어제"까지로 간주).
              오늘 데이터는 current_price로 별도 반영한다.
        current_price: 오늘 장중 현재가.
        lookback_min/max: 고점→저점 탐색 구간 (일).
        drawdown_min_pct/max_pct: 눌림폭 허용 범위
            (drawdown_min_pct가 더 깊은 쪽, 예: -20.0 ~ -10.0).
        max_trough_age_days: 저점이 며칠 전까지면 "최근"으로 인정할지
            (0=오늘만, 1=오늘 또는 어제까지).

    저점 시리즈는 [과거 종가들] + [오늘 현재가]를 합쳐서 구성한다.
    즉 "오늘이 저점일 가능성"도 함께 본다 (옵션 B 설계).
    """
    if len(bars) < lookback_min + 1:
        return None

    # 종가 시리즈에 오늘 현재가를 추가 (시리즈 마지막 = 오늘)
    closes = [b.close_price for b in bars] + [current_price]
    n = len(closes)  # closes[-1] = 오늘, closes[-2] = 어제, ...

    best: Optional[PullbackReboundResult] = None
    best_fit_diff = 999.0  # drawdown 목표 범위 중앙값과의 거리 (가장 적합한 후보 선택용)
    target_mid = (drawdown_min_pct + drawdown_max_pct) / 2  # 예: -15.0

    # 탐색 구간 길이를 5~20일 사이로 바꿔가며 가장 적합한 [고점→저점]을 찾는다
    for window in range(lookback_min, min(lookback_max, n - 1) + 1):
        segment = closes[-(window + 1):]  # 오늘 포함 window+1개 종가

        # 저점 위치 탐색 (오늘 제외하고 탐색하면 "어제 이전 저점"만 잡히므로
        # 오늘도 포함해서 탐색해야 "오늘이 저점"인 경우를 잡을 수 있다)
        trough_idx = min(range(len(segment)), key=lambda i: segment[i])
        trough_price = segment[trough_idx]
        trough_days_ago = (len(segment) - 1) - trough_idx  # 0=오늘

        # 고점은 저점 이전 구간에서 탐색 (고점 → 저점 순서 보장)
        pre_segment = segment[:trough_idx]
        if not pre_segment:
            continue
        peak_idx = max(range(len(pre_segment)), key=lambda i: pre_segment[i])
        peak_price = pre_segment[peak_idx]
        peak_days_ago = (len(segment) - 1) - peak_idx

        if peak_price <= 0 or peak_price <= trough_price:
            continue

        drawdown_pct = (trough_price - peak_price) / peak_price * 100

        # 눌림폭이 목표 범위(drawdown_min_pct ~ drawdown_max_pct) 안에 있는지
        # 예: -20.0 <= drawdown_pct <= -10.0
        if not (drawdown_min_pct <= drawdown_pct <= drawdown_max_pct):
            continue

        # 목표 범위 중앙값과 가장 가까운 후보를 채택
        fit_diff = abs(drawdown_pct - target_mid)
        if fit_diff < best_fit_diff:
            rebound_pct = (
                (current_price - trough_price) / trough_price * 100
                if trough_price > 0 else 0.0
            )
            best_fit_diff = fit_diff
            best = PullbackReboundResult(
                detected=True,
                peak_price=int(peak_price),
                peak_days_ago=peak_days_ago,
                trough_price=int(trough_price),
                trough_days_ago=trough_days_ago,
                drawdown_pct=round(drawdown_pct, 2),
                rebound_pct=round(rebound_pct, 2),
                fail_reason="",
            )

    if best is None:
        return PullbackReboundResult(
            detected=False, peak_price=0, peak_days_ago=0,
            trough_price=0, trough_days_ago=0,
            drawdown_pct=0.0, rebound_pct=0.0,
            fail_reason=(
                f"눌림폭 {drawdown_min_pct}~{drawdown_max_pct}% 구간 내 "
                f"고점→저점 패턴 없음 ({lookback_min}~{lookback_max}일 탐색)"
            ),
        )

    # 저점이 너무 오래 전이면(예: 3일 전) 이미 반등이 끝났을 가능성 → 차단
    if best.trough_days_ago > max_trough_age_days:
        return PullbackReboundResult(
            detected=False,
            peak_price=best.peak_price, peak_days_ago=best.peak_days_ago,
            trough_price=best.trough_price, trough_days_ago=best.trough_days_ago,
            drawdown_pct=best.drawdown_pct, rebound_pct=best.rebound_pct,
            fail_reason=(
                f"저점 {best.trough_days_ago}일 전 — "
                f"최대 {max_trough_age_days}일 이내만 진입 후보 "
                f"(이미 반등 진행됨, 추격매수 위험)"
            ),
        )

    return best
