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

        2026-07-24 (9차 수정, GPT 코드리뷰): ERROR 상태도 이 자동
        동기화 대상에서 제외해야 함 — 기존엔 ERROR가 이 제외 목록에
        없어서, 이상치를 감지해 ERROR로 전이된 바로 다음 폴링에서
        조용히 OPEN/FLAT으로 "자동 복구"되어 버렸음. CRITICAL 로그가
        한 번 찍히고 그 다음 폴링에서 바로 사라지는 셈이라 놓치기
        매우 쉬움. ERROR는 사람이 실제 계좌를 확인하고 명시적으로
        처리하기 전까지 유지되어야 함.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        state.known_quantity = broker_quantity
        if state.lifecycle in (
            PositionLifecycle.BUY_PENDING, PositionLifecycle.SELL_PENDING,
            PositionLifecycle.ERROR,
        ):
            return  # PENDING/ERROR 중엔 여기서 강제 전이하지 않음
        state.lifecycle = PositionLifecycle.OPEN if broker_quantity > 0 else PositionLifecycle.FLAT
        if prev != state.lifecycle:
            self._log_event(symbol, "SYNC", prev, state.lifecycle, broker_quantity)

    def on_buy_requested(self, symbol: str, quantity: int, order_id: str) -> None:
        """매수 주문 요청을 상태머신에 반영합니다.

        2026-07-24 (10차 수정, GPT 코드리뷰): 이 시점에는 이미
        브로커에 매수 요청이 나간 뒤이므로(place_order() 호출 후
        상태머신에 통지하는 구조), 상태머신이 실제 매수 자체를
        막을 수는 없습니다 — 할 수 있는 건 "지금 상황을 정확히
        반영하는 것"뿐입니다.

        기존엔 ERROR 상태에서도 이 함수가 무조건 BUY_PENDING으로
        덮어써서, 사람이 아직 확인하지 않은 ERROR가 재매수 신호
        한 번으로 조용히 사라지는 경로가 있었음(재현 확인 — ERROR
        진입 후 known_quantity가 우연히 재매수 기대값과 맞아떨어지면
        confirm_buy_from_broker()가 그대로 OPEN으로 확정해버림).
        ERROR 상태에서는 lifecycle을 그대로 유지하고, "ERROR 상태
        중에도 매수 시도가 있었다"는 사실만 로그로 남김 — 사람이
        확인할 때 이 이력도 함께 봐야 하므로.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        if prev == PositionLifecycle.ERROR:
            self._log_event(
                symbol, "BUY_REQUESTED", prev, prev,
                detail=f"qty={quantity} (ERROR 상태 유지 — 매수 시도가 있었으나 "
                       f"이전 이상치가 아직 미해결 상태)",
            )
            return
        state.lifecycle = PositionLifecycle.BUY_PENDING
        state.pending_order_id = order_id
        state.pending_quantity = quantity
        state.requested_at = datetime.now()
        self._log_event(symbol, "BUY_REQUESTED", prev, state.lifecycle, detail=f"qty={quantity}")

    def on_buy_result(self, symbol: str, accepted: bool) -> None:
        """매수 주문 접수 결과만 처리합니다. 실제 체결 확인은 다음 폴링의
        confirm_buy_from_broker()가 담당합니다.

        2026-07-22→23 (BUY_PENDING/OPEN 실체결 확인, GPT 제안): 기존엔
        accepted=True이면 이 함수가 즉시 OPEN으로 확정하고
        known_quantity를 "요청 수량 그대로" 신뢰했음(실제 브로커
        재조회 없이) — SELL_PENDING이 다음 폴링에서 실제 잔고와 대조해
        전량/부분/미확정을 구분하는 것과 비대칭이었음. accepted=False
        (거부)는 여기서 바로 처리하고, accepted=True(접수 성공)는
        BUY_PENDING을 유지한 채 다음 폴링의 confirm_buy_from_broker()
        가 실제 잔고 변화를 보고 확정하도록 변경.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        if not accepted:
            # 거부됨 — 매수 전 상태로 복귀. 매수 전엔 항상 FLAT이었다고 가정
            # (재매수 중 거부는 OPEN 상태에서의 추가매수 시나리오이므로 별도 처리 필요할 수 있음)
            state.lifecycle = PositionLifecycle.FLAT if state.known_quantity == 0 else PositionLifecycle.OPEN
            state.last_error = "BUY_REJECTED"
            state.pending_order_id = None
            state.pending_quantity = 0
            self._log_event(symbol, "BUY_RESULT", prev, state.lifecycle, detail="BUY_REJECTED")
            return
        # 접수만 확인됨 — BUY_PENDING 유지, 상태 전이 없음(로그도 안 남김,
        # 확정은 confirm_buy_from_broker()의 몫)

    def confirm_buy_from_broker(self, symbol: str, broker_quantity: int) -> None:
        """BUY_PENDING 상태인 종목의 다음 폴링에서 실제 브로커 잔고로 체결을 확인합니다.

        broker_quantity는 매수 시도 '이후' 다음 폴링에서 조회한 실제
        잔고 — 이 값과 pending_quantity(요청 수량), known_quantity
        (매수 시도 당시 보유 수량, 재매수라면 0이 아닐 수 있음)를
        비교해 전량/부분/미확정 체결을 판정합니다. on_sell_result()와
        대칭되는 구조.

        2026-07-24 (9차 수정, GPT 코드리뷰): broker_quantity가
        expected_min(known+pending, 즉 "요청대로 다 체결됐을 때
        기대하는 수량")을 초과하는 경우를 "전량 체결"로 뭉뚱그려
        받아들이고 있었음 — 요청 100주인데 잔고가 500주로 확인돼도
        경고 없이 그대로 확정해버림(동시성 문제/데이터 이상 등을
        놓칠 수 있는 fail-open 방향). "이상치"(예상 범위를 벗어난
        수량)는 자동으로 확정하지 않고 ERROR 상태로 전이시켜 CRITICAL
        로그를 남기도록 분리.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        if state.lifecycle != PositionLifecycle.BUY_PENDING:
            return  # BUY_PENDING이 아니면 호출 대상 아님

        expected_min = state.known_quantity + state.pending_quantity
        if broker_quantity == expected_min:
            # 요청한 수량만큼 정확히 잔고가 늘어남 -> 전량 체결로 판단
            state.lifecycle = PositionLifecycle.OPEN
            state.known_quantity = broker_quantity
            state.last_filled_at = datetime.now()
            state.pending_order_id = None
            state.pending_quantity = 0
            state.last_error = None
            self._log_event(symbol, "BUY_CONFIRMED", prev, state.lifecycle, broker_quantity, "FILLED")
        elif broker_quantity > expected_min:
            # 2026-07-24: 요청분보다 많이 늘어난 이상 상황 — 자동으로
            # "체결됐다"고 확정하지 않고 ERROR로 전이. 원인 후보:
            # 동시에 다른 매수가 겹침(재매수 경합), 브로커 API 응답
            # 오류, 외부 매매 개입 등. 사람이 실제 계좌를 확인해야
            # 하는 상황이므로 CRITICAL로 남김.
            #
            # 2026-07-24 (11차 수정, GPT 코드리뷰): lifecycle만
            # ERROR로 바꾸고 known_quantity는 그대로 뒀었음 — 이러면
            # ERROR 진입 시점의 known_quantity가 "실제로 관찰된 값
            # (broker_quantity)"이 아니라 "매수 시도 전의 낡은 값"에
            # 머물러서, 이 시점에 CRITICAL 로그나 다른 로직이
            # known_quantity를 참조하면 부정확한 값을 보게 됨.
            # known_quantity를 실제 관찰값으로 갱신 — "우리가 마지막
            # 으로 확인한 사실"을 정확히 반영. pending_*는 일부러
            # 지우지 않고 남겨서 "무엇을 기대했다가 어긋났는지"도
            # 함께 보존(사람이 확인할 때 원인 추적에 필요).
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNEXPECTED_QUANTITY_EXCESS"
            self._log_event(
                symbol, "BUY_CONFIRMED", prev, state.lifecycle, broker_quantity,
                f"UNEXPECTED_QUANTITY_EXCESS(expected={expected_min}, actual={broker_quantity})",
            )
        elif broker_quantity > state.known_quantity:
            # 늘긴 늘었는데 요청한 만큼은 아님 -> 부분체결, 계속 대기
            # (남은 수량에 대한 재주문 여부는 상위 로직의 몫 — 여기서는
            # 상태만 정확히 반영)
            state.known_quantity = broker_quantity
            state.last_error = "PARTIAL_FILL"
            self._log_event(symbol, "BUY_CONFIRMED", prev, prev, broker_quantity, "PARTIAL_FILL_PENDING")
        else:
            # 잔고 변화 없음 -> 아직 브로커 API에 미반영, BUY_PENDING 유지
            self._log_event(symbol, "BUY_CONFIRMED", prev, prev, broker_quantity, "PENDING_STILL_UNCONFIRMED")

    def on_sell_requested(self, symbol: str, quantity: int, order_id: str) -> None:
        """매도 주문 요청을 상태머신에 반영합니다.

        2026-07-24 (10차 수정, GPT 코드리뷰): 매수와 달리 매도는
        ERROR 상태(불확실한 수량)를 정리하려는 정상적인 시도일 수
        있어 SELL_PENDING 전이 자체는 허용 — 다만 "ERROR 상태에서
        매도가 시작됐다"는 이력은 detail에 남겨서, 나중에 사람이
        로그를 볼 때 이 매도가 이상치 정리 과정이었다는 맥락을
        알 수 있게 함.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        was_error = prev == PositionLifecycle.ERROR
        state.lifecycle = PositionLifecycle.SELL_PENDING
        state.pending_order_id = order_id
        state.pending_quantity = quantity
        state.requested_at = datetime.now()
        detail = f"qty={quantity}"
        if was_error:
            detail += " (직전 ERROR 상태에서 매도 시도 — 이상치 정리 시도로 추정)"
        self._log_event(symbol, "SELL_REQUESTED", prev, state.lifecycle, detail=detail)

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
        elif broker_quantity > state.known_quantity:
            # 2026-07-24 (9차 수정, GPT 코드리뷰): 매도 요청 중인데
            # 잔고가 오히려 늘어난 이상 상황 — 기존엔 이것도 "부분체결"
            # else 분기에 섞여 있었음(잘못된 해석: 매도인데 수량이
            # 늘면 부분체결일 수 없음). 동시에 다른 매수가 겹쳤거나
            # 데이터 이상일 수 있어 자동 확정하지 않고 ERROR로 전이.
            # (2026-07-24, 11차 수정): known_quantity도 실제 관찰값으로
            # 갱신 — confirm_buy_from_broker()와 동일한 원칙. 로그
            # 메시지에는 갱신 "전" 값을 남겨야 "얼마에서 얼마로
            # 튀었는지" 알 수 있으므로 미리 변수에 저장해둠.
            known_before = state.known_quantity
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNEXPECTED_QUANTITY_INCREASE"
            self._log_event(
                symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity,
                f"UNEXPECTED_QUANTITY_INCREASE(known_before={known_before}, actual={broker_quantity})",
            )
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

    def acknowledge_error(self, symbol: str, broker_quantity: int, note: str) -> None:
        """ERROR 상태를 사람이 확인 후 명시적으로 해제합니다.

        2026-07-24 (11차 수정, GPT 코드리뷰): 지금까지 ERROR를 벗어날
        방법이 프로세스 재시작(시작 시 브로커 잔고로 재초기화)뿐이었음
        — 실전 운영 중 사람이 계좌를 직접 확인하고 정정하려 해도
        상태머신을 다시 정상 궤도로 되돌릴 인터페이스가 없었음.

        호출 시 broker_quantity(사람이 실제로 확인한 정확한 수량)로
        known_quantity를 갱신하고, 그 수량에 맞는 lifecycle
        (OPEN/FLAT)로 전이시킵니다. pending_* 필드도 정리합니다.
        누가 어떤 근거로 해제했는지 note로 남길 수 있음 — 로그에
        "ERROR였다가 사람이 확인 후 해제됐다"는 이력이 명확히 남도록.
        이 메서드는 shadow 모드에서도 안전합니다 — 실제 매매 판정에는
        관여하지 않고 관찰용 상태만 정정합니다.

        2026-07-24 (12차 수정, GPT 코드리뷰): broker_quantity가
        음수여도, note가 빈 문자열이어도 예외 없이 조용히 받아들여
        지고 있었음(재현: -100을 넣으면 known_quantity=-100이라는
        있을 수 없는 값이 그대로 들어가고, 우연히 "0보다 크지 않다"
        판정에 걸려 FLAT으로 확정됨). 사람이 실제 계좌를 확인하고
        호출하는 마지막 안전장치인 만큼, 입력값 자체를 신뢰하지
        않고 검증 — 음수는 ValueError로 거부, note는 필수 인자로
        전환(기본값 제거)해 "왜 해제하는지" 근거 없이 호출하는 걸
        원천 차단.
        """
        if broker_quantity < 0:
            raise ValueError(
                f"broker_quantity는 0 이상이어야 합니다: {broker_quantity} "
                f"(실제 계좌에서 확인한 정확한 보유수량을 입력하세요)"
            )
        if not note or not note.strip():
            raise ValueError(
                "note는 필수입니다 — 어떤 근거로(예: 'HTS 직접 확인', "
                "'고객센터 문의 결과') ERROR를 해제하는지 반드시 기록하세요."
            )

        state = self.get(symbol)
        prev = state.lifecycle
        if prev != PositionLifecycle.ERROR:
            # ERROR가 아닌데 호출된 경우도 안전하게 처리 — 상태만
            # 정확히 재동기화하고 별다른 경고 없이 넘어감(오용 방지를
            # 위한 방어적 처리, 예외는 던지지 않음).
            state.known_quantity = broker_quantity
            state.lifecycle = PositionLifecycle.OPEN if broker_quantity > 0 else PositionLifecycle.FLAT
            return

        state.known_quantity = broker_quantity
        state.lifecycle = PositionLifecycle.OPEN if broker_quantity > 0 else PositionLifecycle.FLAT
        state.pending_order_id = None
        state.pending_quantity = 0
        state.last_error = None
        detail = f"ERROR 해제 — 확인된 잔고={broker_quantity}"
        if note:
            detail += f" | {note}"
        self._log_event(symbol, "ERROR_ACKNOWLEDGED", prev, state.lifecycle, broker_quantity, detail)
