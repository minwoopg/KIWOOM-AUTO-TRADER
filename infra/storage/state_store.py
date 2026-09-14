from __future__ import annotations

"""간단한 JSON 상태 저장소."""

import json
import os
import tempfile
from pathlib import Path

from domain.models import RuntimeState


class JsonStateStore:
    """RuntimeState를 JSON 파일로 저장/복원하는 클래스입니다."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> tuple[RuntimeState, dict[str, int]]:
        """파일이 있으면 상태를 읽고, 없으면 빈 상태를 반환합니다."""
        if not self.path.exists():
            return RuntimeState(), {}

        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        state = RuntimeState(
            bought_symbols_today        = set(raw.get("bought_symbols_today", [])),
            last_order_id_by_symbol     = raw.get("last_order_id_by_symbol", {}),
            last_sold_at_by_symbol      = raw.get("last_sold_at_by_symbol", {}),
            entry_time_by_symbol        = raw.get("entry_time_by_symbol", {}),
            consecutive_losses          = int(raw.get("consecutive_losses", 0)),
            peak_price_by_symbol        = {
                k: int(v) for k, v in raw.get("peak_price_by_symbol", {}).items()
            },
            symbol_loss_count_today     = raw.get("symbol_loss_count_today", {}),
            symbol_entry_count_today    = raw.get("symbol_entry_count_today", {}),
            symbol_stoploss_at          = raw.get("symbol_stoploss_at", {}),
            symbol_trail_loss_at        = raw.get("symbol_trail_loss_at", {}),
            symbol_block_today          = set(raw.get("symbol_block_today", [])),
            _last_run_date              = raw.get("_last_run_date"),
            unresolved_order_intents    = raw.get("unresolved_order_intents", {}),
            entry_watch_normal_eval_seen_by_symbol = raw.get(
                "entry_watch_normal_eval_seen_by_symbol", {}
            ),
        )
        highest_price = {k: int(v) for k, v in raw.get("highest_price", {}).items()}
        return state, highest_price

    def save(self, state: RuntimeState, highest_price: dict[str, int] | None = None) -> None:
        """현재 상태를 JSON 파일에 저장합니다."""
        payload = {
            "_last_run_date":           state._last_run_date,
            "unresolved_order_intents": state.unresolved_order_intents,
            "bought_symbols_today":     sorted(state.bought_symbols_today),
            "last_order_id_by_symbol":  state.last_order_id_by_symbol,
            "last_sold_at_by_symbol":   state.last_sold_at_by_symbol,
            "entry_time_by_symbol":     state.entry_time_by_symbol,
            "consecutive_losses":       state.consecutive_losses,
            "peak_price_by_symbol":     state.peak_price_by_symbol,
            "symbol_loss_count_today":  state.symbol_loss_count_today,
            "symbol_entry_count_today": state.symbol_entry_count_today,
            "symbol_stoploss_at":       state.symbol_stoploss_at,
            "symbol_trail_loss_at":     state.symbol_trail_loss_at,
            "symbol_block_today":       sorted(state.symbol_block_today),
            "highest_price":            highest_price or {},
            "entry_watch_normal_eval_seen_by_symbol": state.entry_watch_normal_eval_seen_by_symbol,
        }
        # A crash must not truncate the last valid risk state.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
