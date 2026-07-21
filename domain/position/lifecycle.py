# -*- coding: utf-8 -*-
"""포지션 생명주기 상태 머신 (2026-07-22, shadow 단계).

GPT 검토(7.12/7.14절 관련)에서 제안된 5단계 상태 머신을 구현합니다.
현재는 shadow 모드로만 동작 — 기존 `_sold_today_qty_snapshot` 기반
판정(7.14절)을 대체하지 않고 병행 계산만 하며, 두 방식의 판정이
갈리는 경우를 로그로 남겨 검증합니다. 검증이 끝나면 실제 판정 로직을
이 모듈로 교체합니다.

설계 배경:
- 이 시스템의 `place_order()`는 동기 호출이라 원래 의미의 "주문 대기"
  상태는 없음. BUY_PENDING/SELL_PENDING은 "주문은 접수됐는데 브로커
  잔고 API에 아직 반영 안 된 짧은 구간"을 의미.
- 상태는 영속화하지 않음(휘발성) — 재시작 시 브로커 실제 잔고를
  기준으로 다시 초기화하는 게 안전. state.json에 저장된 옛 PENDING
  상태를 신뢰하면 오히려 위험할 수 있음.
- 전이는 브로커 응답(accepted 여부, 잔고 수량 변화)이 있어야만
  일어남 — 요청을 보냈다는 사실만으로는 상태를 바꾸지 않음.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class PositionLifecycle(str, Enum):
    FLAT = "FLAT"
    BUY_PENDING = "BUY_PENDING"
    OPEN = "OPEN"
    SELL_PENDING = "SELL_PENDING"
    ERROR = "ERROR"


@dataclass
class SymbolPositionState:
    """종목 하나의 현재 생명주기 상태와 관련 메타데이터."""

    lifecycle: PositionLifecycle = PositionLifecycle.FLAT
    pending_order_id: str | None = None
    pending_quantity: int = 0          # 진행 중인 주문의 요청 수량
    known_quantity: int = 0            # 이 상태머신이 마지막으로 확인한 실제 보유수량
    requested_at: datetime | None = None
    last_filled_at: datetime | None = None
    last_error: str | None = None


class PositionStateMachine:
    """종목별 SymbolPositionState를 관리하며 전이를 수행합니다.

    shadow 모드: 이 클래스의 판정 결과는 아직 실제 매매 로직에
    쓰이지 않습니다. TradingService가 매 폴링마다 이 상태머신에
    이벤트를 통지하고, 실제 브로커 잔고와 대조해 기존 판정
    (_sold_today_qty_snapshot)과 다른 결론이 나오는지 로그로만 남깁니다.

    logger가 주어지면(2026-07-22 보강) 모든 이벤트(요청/결과/동기화/
    불변조건검사)를 CSV로 기록합니다 — 전이가 실제로 일어났는지
    여부와 무관하게, "이 이벤트가 호출됐다"는 사실 자체를 남겨서
    SELL_PENDING이 몇 번의 폴링에 걸쳐 유지됐는지 등을 사후 분석할
    수 있게 합니다. logger가 None이면(기본값) 기존과 동일하게
    아무것도 기록하지 않습니다 — 하위호환 유지.
    """

    def __init__(self, logger: "Any | None" = None) -> None:
        self._states: dict[str, SymbolPositionState] = {}
        self._logger = logger  # PositionLifecycleLogger 인스턴스 또는 None

    def _log_event(
        self, symbol: str, event: str, from_lifecycle: PositionLifecycle,
        to_lifecycle: PositionLifecycle, broker_quantity: int | str = "",
        detail: str = "",
    ) -> None:
        if self._logger is None:
            return
        state = self.get(symbol)
        self._logger.append({
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "event": event,
            "from_lifecycle": from_lifecycle.value,
            "to_lifecycle": to_lifecycle.value,
            "broker_quantity": broker_quantity,
            "pending_quantity": state.pending_quantity,
            "known_quantity": state.known_quantity,
            "detail": detail,
        })

    def get(self, symbol: str) -> SymbolPositionState:
        if symbol not in self._states:
            self._states[symbol] = SymbolPositionState()
        return self._states[symbol]

    def sync_from_broker(self, symbol: str, broker_quantity: int) -> None:
        """브로커 잔고를 기준으로 상태를 동기화합니다 (프로세스 시작 시,
        또는 PENDING 상태가 아닐 때 매 폴링마다 호출).

        PENDING 상태 중에는 호출하지 않는 것이 원칙 — 그 동안은
        on_buy_result / on_sell_result가 명시적으로 전이시킴.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        state.known_quantity = broker_quantity
        if state.lifecycle in (PositionLifecycle.BUY_PENDING, PositionLifecycle.SELL_PENDING):
            return  # PENDING 중엔 여기서 강제 전이하지 않음 (로그도 안 남김 — on_buy/sell_result가 담당)
        state.lifecycle = PositionLifecycle.OPEN if broker_quantity > 0 else PositionLifecycle.FLAT
        if prev != state.lifecycle:
            self._log_event(symbol, "SYNC", prev, state.lifecycle, broker_quantity)

    def on_buy_requested(self, symbol: str, quantity: int, order_id: str) -> None:
        state = self.get(symbol)
        prev = state.lifecycle
        state.lifecycle = PositionLifecycle.BUY_PENDING
        state.pending_order_id = order_id
        state.pending_quantity = quantity
        state.requested_at = datetime.now()
        self._log_event(symbol, "BUY_REQUESTED", prev, state.lifecycle, detail=f"qty={quantity}")

    def on_buy_result(self, symbol: str, accepted: bool, broker_quantity: int) -> None:
        state = self.get(symbol)
        prev = state.lifecycle
        if not accepted:
            # 거부됨 — 매수 전 상태로 복귀. 매수 전엔 항상 FLAT이었다고 가정
            # (재매수 중 거부는 OPEN 상태에서의 추가매수 시나리오이므로 별도 처리 필요할 수 있음)
            state.lifecycle = PositionLifecycle.FLAT if state.known_quantity == 0 else PositionLifecycle.OPEN
            state.last_error = "BUY_REJECTED"
            self._log_event(symbol, "BUY_RESULT", prev, state.lifecycle, broker_quantity, "BUY_REJECTED")
            return
        state.lifecycle = PositionLifecycle.OPEN
        state.known_quantity = broker_quantity
        state.last_filled_at = datetime.now()
        state.pending_order_id = None
        state.pending_quantity = 0
        state.last_error = None
        self._log_event(symbol, "BUY_RESULT", prev, state.lifecycle, broker_quantity, "FILLED")

    def on_sell_requested(self, symbol: str, quantity: int, order_id: str) -> None:
        state = self.get(symbol)
        prev = state.lifecycle
        state.lifecycle = PositionLifecycle.SELL_PENDING
        state.pending_order_id = order_id
        state.pending_quantity = quantity
        state.requested_at = datetime.now()
        self._log_event(symbol, "SELL_REQUESTED", prev, state.lifecycle, detail=f"qty={quantity}")

    def on_sell_result(self, symbol: str, accepted: bool, broker_quantity: int) -> None:
        """매도 주문 결과 처리. broker_quantity는 매도 시도 '이후' 다음
        폴링에서 확인한 실제 잔고 — 이 값으로 전량/부분/미체결을 판단.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        if not accepted:
            # 거부됨 — 여전히 보유 중이므로 OPEN으로 복귀
            state.lifecycle = PositionLifecycle.OPEN
            state.last_error = "SELL_REJECTED"
            state.pending_order_id = None
            state.pending_quantity = 0
            self._log_event(symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity, "SELL_REJECTED")
            return

        if broker_quantity == 0:
            # 전량 체결
            state.lifecycle = PositionLifecycle.FLAT
            state.known_quantity = 0
            detail = "FILLED_FULL"
        elif broker_quantity == state.known_quantity:
            # 매도 시도 당시 수량과 동일 — 아직 API 미반영, SELL_PENDING 유지
            self._log_event(symbol, "SELL_RESULT", prev, prev, broker_quantity, "PENDING_STILL_UNCONFIRMED")
            return
        else:
            # 부분체결 — 잔여수량으로 OPEN 유지, 재시도 필요
            state.lifecycle = PositionLifecycle.OPEN
            state.known_quantity = broker_quantity
            state.last_error = "PARTIAL_FILL"
            detail = "PARTIAL_FILL"

        state.last_filled_at = datetime.now()
        state.pending_order_id = None
        state.pending_quantity = 0
        self._log_event(symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity, detail)

    def check_invariant(self, symbol: str, broker_quantity: int) -> str | None:
        """불변조건 위반 여부를 확인합니다.

        위반 예시: 브로커 잔고 > 0인데 로컬 상태가 FLAT — 이건 항상
        버그(7.12절과 같은 유형)이지, PENDING 중의 정상적인 지연이
        아님. PENDING 상태는 이 검사에서 제외 — 그 동안은 불일치가
        나는 게 정상이므로.

        반환값: 위반 시 설명 문자열, 정상이면 None.
        """
        state = self.get(symbol)
        if state.lifecycle == PositionLifecycle.FLAT and broker_quantity > 0:
            msg = (
                f"POSITION_STATE_MISMATCH: {symbol} broker_qty={broker_quantity} "
                f"but local_lifecycle=FLAT"
            )
            self._log_event(
                symbol, "INVARIANT_VIOLATION", state.lifecycle, state.lifecycle,
                broker_quantity, msg,
            )
            return msg
        return None
