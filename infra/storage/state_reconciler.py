from __future__ import annotations

"""시작 시 state.json과 실제 잔고를 동기화하는 모듈.

문제 상황:
    - 재시작 후 이미 매도한 종목의 peak_price가 state.json에 남아 있음
    - 보유 종목인데 entry_time, peak_price가 없어 트레일링/손절이 이상하게 작동
    - 날짜가 바뀌었는데 consecutive_losses, bought_symbols_today가 초기화 안 됨

해결:
    main.py에서 TradingService 시작 직전에 reconcile() 한 번 호출
"""

from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.models import AccountBalance, RuntimeState


class StateReconciler:
    """state.json과 실제 잔고를 동기화합니다."""

    def __init__(self, app_logger) -> None:
        self.app_logger = app_logger

    def reconcile(
        self,
        state: "RuntimeState",
        highest_price: dict[str, int],
        balance: "AccountBalance",
    ) -> tuple["RuntimeState", dict[str, int]]:
        """state와 실제 잔고를 비교해 불일치를 정리합니다.

        수행 작업:
            1. 날짜 변경 시 일별 상태 초기화
            2. 이미 매도한 종목의 peak_price 제거
            3. 보유 종목인데 peak_price 없으면 현재가로 초기화
            4. consecutive_losses 날짜 검증
        """
        today = date.today()
        holding_symbols = {p.symbol for p in balance.positions}

        # ── 1. 날짜 변경 시 일별 상태 초기화 ────────────────────
        last_date_str = getattr(state, "_last_run_date", None)
        if last_date_str != today.isoformat():
            prev = state.bought_symbols_today.copy()
            state.bought_symbols_today = set()
            state.consecutive_losses   = 0
            state._last_run_date       = today.isoformat()
            if prev:
                self.app_logger.info(
                    f"[RECONCILE] 날짜 변경 → 일별 상태 초기화 "
                    f"(이전 매수 종목: {sorted(prev)})"
                )

        # ── 2. 이미 매도한 종목의 peak_price 제거 ───────────────
        stale = [sym for sym in list(highest_price) if sym not in holding_symbols]
        for sym in stale:
            del highest_price[sym]
            self.app_logger.info(
                f"[RECONCILE] {sym} | 보유 없음 → peak_price 제거"
            )
        state.peak_price_by_symbol = {
            sym: v for sym, v in state.peak_price_by_symbol.items()
            if sym in holding_symbols
        }
        state.entry_time_by_symbol = {
            sym: v for sym, v in state.entry_time_by_symbol.items()
            if sym in holding_symbols
        }

        # ── 3. 보유 종목인데 peak_price 없으면 현재가로 초기화 ──
        for position in balance.positions:
            sym = position.symbol
            if sym not in highest_price:
                cur = position.current_price if hasattr(position, "current_price") \
                      else position.average_price
                highest_price[sym] = cur
                self.app_logger.info(
                    f"[RECONCILE] {sym} | 보유 중이나 peak_price 없음 "
                    f"→ 현재가 {cur:,}원으로 초기화"
                )

        self.app_logger.info(
            f"[RECONCILE] 완료 — 보유 {len(holding_symbols)}종목 / "
            f"peak_price {len(highest_price)}종목"
        )

        return state, highest_price
