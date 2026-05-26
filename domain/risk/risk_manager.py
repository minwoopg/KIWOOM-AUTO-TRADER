from __future__ import annotations

"""주문 전 마지막 안전장치 역할을 하는 모듈.

전략이 BUY를 냈다고 해서 바로 주문하지 않고,
리스크 규칙에 위배되지 않는지 여기서 한번 더 점검합니다.
"""

import csv
from datetime import date, datetime
from pathlib import Path

from config.settings import RiskConfig, TradingConfig
from domain.models import AccountBalance, OrderRequest, RuntimeState
from infra.storage.skip_reason import SkipReason


class RiskManager:
    """주문 가능 여부를 검사하는 리스크 관리자입니다."""

    def __init__(
        self,
        trading_config: TradingConfig,
        risk_config: RiskConfig,
        trade_log_file: str = "logs/trades.csv",
    ) -> None:
        self.trading_config = trading_config
        self.risk_config = risk_config
        self._trade_log_file = Path(trade_log_file)

    # ── 일일 실현 손익 계산 ───────────────────────────────────────

    def _calc_daily_realized_pnl(self, target_date: date | None = None) -> int:
        """trades.csv에서 당일 실현 손익(원)을 계산합니다.

        - price=0인 행은 체결가 미기록으로 간주하고 제외합니다.
        - 파일이 없거나 읽기 오류 시 0을 반환합니다 (안전 방향).
        """
        target_date = target_date or date.today()

        if not self._trade_log_file.exists():
            return 0

        buys: dict[str, list[tuple[int, int]]] = {}
        sells: dict[str, list[tuple[int, int]]] = {}

        try:
            with self._trade_log_file.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("accepted", "").lower() != "true":
                        continue
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                    except (ValueError, KeyError):
                        continue
                    if ts.date() != target_date:
                        continue

                    price = int(row.get("price", 0) or 0)
                    qty   = int(row.get("quantity", 0) or 0)
                    if price <= 0 or qty <= 0:
                        continue

                    symbol = row["symbol"]
                    if row["side"] == "BUY":
                        buys.setdefault(symbol, []).append((price, qty))
                    elif row["side"] == "SELL":
                        sells.setdefault(symbol, []).append((price, qty))
        except Exception:
            return 0

        pnl = 0
        for symbol, sell_list in sells.items():
            buy_list = buys.get(symbol, [])
            if not buy_list:
                continue
            total_buy_qty  = sum(q for _, q in buy_list)
            total_sell_qty = sum(q for _, q in sell_list)
            if total_buy_qty == 0 or total_sell_qty == 0:
                continue
            avg_buy  = sum(p * q for p, q in buy_list)  / total_buy_qty
            avg_sell = sum(p * q for p, q in sell_list) / total_sell_qty
            matched_qty = min(total_buy_qty, total_sell_qty)
            pnl += int((avg_sell - avg_buy) * matched_qty)

        return pnl

    # ── 주문 가능 여부 검사 ───────────────────────────────────────

    def can_place_order(
        self,
        order: OrderRequest,
        balance: AccountBalance,
        state: RuntimeState,
    ) -> tuple[bool, str]:
        """주문이 가능한지 검사하고, 불가능하면 이유를 함께 돌려줍니다."""

        # 종목당 하루 1회 진입 제한
        if not self.trading_config.allow_multiple_entries_per_symbol_per_day:
            if order.symbol in state.bought_symbols_today:
                return False, SkipReason.ALREADY_HOLDING

        # 최대 보유 종목 수 제한
        current_symbols = {position.symbol for position in balance.positions}
        if order.symbol not in current_symbols and len(current_symbols) >= self.trading_config.max_positions:
            return False, SkipReason.MAX_POSITIONS

        # 최소 현금 버퍼 유지
        estimated_amount = (order.price or 0) * order.quantity
        if balance.cash - estimated_amount < self.risk_config.min_cash_buffer:
            return False, SkipReason.RISK_LIMIT

        # 주문 금액 상한 제한
        if estimated_amount > self.risk_config.max_order_amount:
            return False, SkipReason.RISK_LIMIT

        # ── 일일 최대 손실 한도 ──────────────────────────────────
        daily_pnl = self._calc_daily_realized_pnl()
        if daily_pnl <= -abs(self.risk_config.max_daily_loss_amount):
            return (
                False,
                f"{SkipReason.DAILY_LOSS_LIMIT} "
                f"({daily_pnl:,}원 / 한도 -{abs(self.risk_config.max_daily_loss_amount):,}원)",
            )

        # ── 연속 손절 한도 ───────────────────────────────────────
        max_consec = getattr(self.risk_config, "max_consecutive_losses", 0)
        if max_consec > 0 and state.consecutive_losses >= max_consec:
            return (
                False,
                f"{SkipReason.CONSECUTIVE_LOSS_LIMIT} "
                f"({state.consecutive_losses}회 연속 손절 / 한도 {max_consec}회)",
            )

        return True, "ok"
