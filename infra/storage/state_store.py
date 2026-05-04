from __future__ import annotations

"""간단한 JSON 상태 저장소.

DB를 쓰지 않는 첫 버전에서는 이 정도 저장소면 충분합니다.
"""

import json
from pathlib import Path

from domain.models import RuntimeState


class JsonStateStore:
    """RuntimeState를 JSON 파일로 저장/복원하는 클래스입니다."""

    def __init__(self, path: str) -> None:
        """상태 파일 경로를 저장하고 부모 폴더를 준비합니다."""

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeState:
        """파일이 있으면 상태를 읽고, 없으면 빈 상태를 반환합니다."""

        if not self.path.exists():
            return RuntimeState()

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return RuntimeState(
            bought_symbols_today=set(raw.get("bought_symbols_today", [])),
            last_order_id_by_symbol=raw.get("last_order_id_by_symbol", {}),
        )

    def save(self, state: RuntimeState) -> None:
        """현재 상태를 JSON 파일에 저장합니다."""

        payload = {
            "bought_symbols_today": sorted(state.bought_symbols_today),
            "last_order_id_by_symbol": state.last_order_id_by_symbol,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
