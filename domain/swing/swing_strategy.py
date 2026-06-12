"""
스윙 전략 매도 로직.

보유 포지션의 수익 쿠션 상태에 따라 매도 조건을 판단합니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class SwingExitReason(str, Enum):
    STOP_LOSS            = "손절 -5%"
    TIME_STOP            = "기간손절 3일 초과"
    CUSHION_PARTIAL      = "수익쿠션 부분보호매도"
    CUSHION_FULL         = "수익쿠션 전량청산"
    TAKE_PROFIT_PARTIAL  = "1차 부분익절 +15%"
    TRAILING_AFTER_TP    = "익절후 트레일링"
    MA5_BREAK            = "MA5 이탈"
    GAP_DOWN_IMMEDIATE   = "갭하락 즉시손절"
    GAP_DOWN_WATCH       = "갭하락 감시"
    HOLD                 = "보유"


@dataclass
class SwingPosition:
    """스윙 보유 포지션 상태."""
    symbol:          str
    entry_price:     int
    quantity:        int
    entry_date:      date
    avg_price:       int = 0            # 부분매도 후 평균단가

    # 수익 추적
    peak_profit_pct: float = 0.0        # 지금까지 최고 수익률
    peak_price:      int   = 0          # 최고가
    cushion_hit:     bool  = False      # +5% 수익 쿠션 달성 여부
    partial_sold:    bool  = False      # 1차 부분익절 완료 여부
    cushion_partial_sold: bool = False  # 수익쿠션 부분보호매도 완료 여부

    def __post_init__(self):
        if self.avg_price == 0:
            self.avg_price = self.entry_price
        if self.peak_price == 0:
            self.peak_price = self.entry_price


@dataclass
class SwingExitSignal:
    """매도 신호."""
    reason:       SwingExitReason
    sell_ratio:   float    # 매도 비율 (0.33=33%, 1.0=전량)
    message:      str


class SwingStrategy:
    """스윙 포지션 매도 조건 판단."""

    def __init__(
        self,
        # 손절
        no_cushion_stop_loss_pct: float   = -5.0,
        # 수익 쿠션
        cushion_trigger_pct: float        = 5.0,
        cushion_partial_drawdown_pct: float = -5.0,
        cushion_partial_ratio: float      = 0.33,
        cushion_full_drawdown_pct: float  = -8.0,
        # 1차 부분익절
        take_profit_1_pct: float          = 15.0,
        take_profit_1_ratio: float        = 0.33,
        # 익절 후 트레일링
        after_tp_trailing_pct: float      = -7.0,
        exit_if_below_ma5: bool           = True,
        # 기간 손절
        time_stop_days: int               = 3,
        time_stop_min_profit_pct: float   = 0.0,
        # 갭하락
        gap_down_watch_pct: float         = -3.0,
        gap_down_immediate_pct: float     = -5.0,
        gap_down_wait_minutes: int        = 5,
    ):
        self.no_cushion_stop       = no_cushion_stop_loss_pct
        self.cushion_trigger       = cushion_trigger_pct
        self.cushion_partial_dd    = cushion_partial_drawdown_pct
        self.cushion_partial_ratio = cushion_partial_ratio
        self.cushion_full_dd       = cushion_full_drawdown_pct
        self.take_profit_1         = take_profit_1_pct
        self.take_profit_1_ratio   = take_profit_1_ratio
        self.after_tp_trailing     = after_tp_trailing_pct
        self.exit_if_below_ma5     = exit_if_below_ma5
        self.time_stop_days        = time_stop_days
        self.time_stop_min_profit  = time_stop_min_profit_pct
        self.gap_down_watch        = gap_down_watch_pct
        self.gap_down_immediate    = gap_down_immediate_pct
        self.gap_down_wait_minutes = gap_down_wait_minutes

    def update_peak(self, pos: SwingPosition, current_price: int) -> None:
        """최고가 및 수익 쿠션 상태 업데이트."""
        profit_pct = (current_price - pos.avg_price) / pos.avg_price * 100
        if current_price > pos.peak_price:
            pos.peak_price       = current_price
            pos.peak_profit_pct  = profit_pct
        if profit_pct >= self.cushion_trigger:
            pos.cushion_hit = True

    def check_exit(
        self,
        pos: SwingPosition,
        current_price: int,
        today: date,
        ma5: float = 0,
        is_open: bool = False,
        prev_close: int = 0,
    ) -> SwingExitSignal | None:
        """
        현재 상태에서 매도 신호를 반환합니다.
        None이면 보유 유지.

        Args:
            pos: 보유 포지션
            current_price: 현재가
            today: 오늘 날짜
            ma5: 일봉 MA5 (청산 조건용)
            is_open: 장 시작 직후 여부 (갭하락 체크용)
            prev_close: 전일 종가 (갭하락 계산용)
        """
        profit_pct = (current_price - pos.avg_price) / pos.avg_price * 100
        hold_days  = (today - pos.entry_date).days

        # 최고가 업데이트
        self.update_peak(pos, current_price)

        # ── 1순위: 갭하락 대응 (장 시작 직후) ────────────────────
        if is_open and prev_close > 0:
            gap_pct = (current_price - prev_close) / prev_close * 100
            if gap_pct <= self.gap_down_immediate:
                return SwingExitSignal(
                    reason=SwingExitReason.GAP_DOWN_IMMEDIATE,
                    sell_ratio=1.0,
                    message=(
                        f"갭하락 즉시손절 — "
                        f"전일 {prev_close:,}원 대비 {gap_pct:+.1f}%"
                    ),
                )
            if gap_pct <= self.gap_down_watch:
                return SwingExitSignal(
                    reason=SwingExitReason.GAP_DOWN_WATCH,
                    sell_ratio=0.0,
                    message=(
                        f"갭하락 감시 중 — "
                        f"전일 대비 {gap_pct:+.1f}% "
                        f"({self.gap_down_wait_minutes}분 관찰)"
                    ),
                )

        # ── 2순위: 기본 손절 (수익 쿠션 없음) ───────────────────
        if not pos.cushion_hit and profit_pct <= self.no_cushion_stop:
            return SwingExitSignal(
                reason=SwingExitReason.STOP_LOSS,
                sell_ratio=1.0,
                message=(
                    f"손절 — 수익 쿠션 없음 | "
                    f"현재 {profit_pct:+.1f}% (기준 {self.no_cushion_stop}%)"
                ),
            )

        # ── 3순위: 기간 손절 ────────────────────────────────────
        if (hold_days >= self.time_stop_days
                and profit_pct <= self.time_stop_min_profit):
            return SwingExitSignal(
                reason=SwingExitReason.TIME_STOP,
                sell_ratio=1.0,
                message=(
                    f"기간 손절 — {hold_days}일 보유 | "
                    f"수익률 {profit_pct:+.1f}%"
                ),
            )

        # ── 4순위: 1차 부분익절 (+15%) ───────────────────────────
        if not pos.partial_sold and profit_pct >= self.take_profit_1:
            pos.partial_sold = True
            return SwingExitSignal(
                reason=SwingExitReason.TAKE_PROFIT_PARTIAL,
                sell_ratio=self.take_profit_1_ratio,
                message=(
                    f"1차 부분익절 {self.take_profit_1_ratio*100:.0f}% 매도 — "
                    f"수익률 {profit_pct:+.1f}%"
                ),
            )

        # ── 5순위: 수익 쿠션 이후 부분 보호매도 ─────────────────
        if (pos.cushion_hit
                and not pos.cushion_partial_sold
                and not pos.partial_sold):
            peak_drawdown = (
                (current_price - pos.peak_price) / pos.peak_price * 100
            )
            if peak_drawdown <= self.cushion_partial_dd:
                pos.cushion_partial_sold = True
                return SwingExitSignal(
                    reason=SwingExitReason.CUSHION_PARTIAL,
                    sell_ratio=self.cushion_partial_ratio,
                    message=(
                        f"수익 쿠션 부분 보호매도 — "
                        f"고점 {pos.peak_price:,}원 대비 {peak_drawdown:+.1f}%"
                    ),
                )

        # ── 6순위: 수익 쿠션 이후 전량 청산 ─────────────────────
        if pos.cushion_hit:
            peak_drawdown = (
                (current_price - pos.peak_price) / pos.peak_price * 100
            )
            if peak_drawdown <= self.cushion_full_dd:
                return SwingExitSignal(
                    reason=SwingExitReason.CUSHION_FULL,
                    sell_ratio=1.0,
                    message=(
                        f"수익 쿠션 전량 청산 — "
                        f"고점 {pos.peak_price:,}원 대비 {peak_drawdown:+.1f}%"
                    ),
                )

        # ── 7순위: 익절 후 트레일링 / MA5 이탈 ──────────────────
        if pos.partial_sold:
            peak_drawdown = (
                (current_price - pos.peak_price) / pos.peak_price * 100
            )
            if peak_drawdown <= self.after_tp_trailing:
                return SwingExitSignal(
                    reason=SwingExitReason.TRAILING_AFTER_TP,
                    sell_ratio=1.0,
                    message=(
                        f"익절 후 트레일링 — "
                        f"고점 {pos.peak_price:,}원 대비 {peak_drawdown:+.1f}%"
                    ),
                )
            if self.exit_if_below_ma5 and ma5 > 0 and current_price < ma5:
                return SwingExitSignal(
                    reason=SwingExitReason.MA5_BREAK,
                    sell_ratio=1.0,
                    message=(
                        f"MA5 이탈 — "
                        f"현재가 {current_price:,} < MA5 {ma5:,.0f}"
                    ),
                )

        return None  # 보유 유지
