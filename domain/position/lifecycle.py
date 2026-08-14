# -*- coding: utf-8 -*-
"""포지션 생명주기 상태 머신 (2026-07-22 shadow 시작 → 2026-08-10 enforce).

GPT 검토(7.12/7.14절 관련)에서 제안된 5단계 상태 머신을 구현합니다.

2026-08-12 (1P0.7.1, GPT 코드리뷰 지적): 이 docstring이 여전히
"shadow 모드로만 동작"이라고 돼 있었는데, **1P0.2부터 이미 실제
주문 경로를 enforce로 차단하고 있어 현재 코드와 정반대였습니다.**
다음 개발자가 "shadow니까 실제 주문에는 영향 없겠네"라고 잘못
판단할 위험이 있어 정정합니다.

**현재 상태 (1P0.7.1 기준)**:
- `_try_buy()` / `_try_sell()`이 이 모듈의 `would_block_buy_detail()`
  / `decide_sell()`을 실제로 호출해 **주문을 차단(enforce)**합니다.
  더 이상 shadow(관측만)가 아닙니다.
- BUY_PENDING/SELL_PENDING/orphan/ERROR는 HARD block으로, forced
  (손절·강제청산)로도 우회하지 못합니다(1P0.7).
- **부분 해결(2026-08-14, 1P0.8-A.1)**: `pending_order_id`/
  `orphan_order_id`는 이제 `confirm_pending_order_id()`를 통해
  place_order() accepted 직후 실제 브로커 주문번호(`ord_no`)로
  교체됩니다 — 더 이상 모든 주문이 `"pending"` 리터럴을 공유하지
  않습니다. **다만 이것은 식별자를 연결한 것일 뿐입니다.** 이 번호로
  실제 조회(`get_open_orders()`/`get_order_status()`)·취소
  (`cancel_order()`)를 하는 Broker 인터페이스는 아직 없습니다
  (1P0.8-B/C에서 예정). 따라서 특정 주문의 FILLED/CANCELLED/
  REJECTED terminal 확인은 여전히 불가능하고, 이 상태는 실제 주문
  상태를 몰라도 시스템이 스스로 자신 있게 재개하지 않도록 막는
  **방어막일 뿐, 근본 해결이 아닙니다**.
- **재시작 시 전부 소실**: lifecycle 상태와
  `_pending_sell_side_effects`는 모두 메모리 전용입니다. 프로그램이
  재시작되면 BUY_PENDING/SELL_PENDING/orphan/보류 중이던 side-effect
  컨텍스트가 전부 사라지고, 새 프로세스는 브로커 잔고만 보고
  초기화합니다 — 그 사이 미체결로 남아있던 원 주문의 존재를 모르게
  됩니다. **이것이 현재 완전 무인 실계좌 운영의 배포 blocker입니다**
  (GPT 판정).

설계 배경:
- 이 시스템의 `place_order()`는 동기 호출이라 원래 의미의 "주문 대기"
  상태는 없음. BUY_PENDING/SELL_PENDING은 "주문은 접수됐는데 브로커
  잔고 API에 아직 반영 안 된 짧은 구간"을 의미.
- 상태는 영속화하지 않음(휘발성) — 재시작 시 브로커 실제 잔고를
  기준으로 다시 초기화하는 게 안전. state.json에 저장된 옛 PENDING
  상태를 신뢰하면 오히려 위험할 수 있음. 다만 이 "안전한 재초기화"가
  위의 재시작 소실 문제를 없애주지는 않음 — 잔고만으로는 미체결
  주문의 존재 자체를 알 수 없기 때문.
- 전이는 브로커 응답(accepted 여부, 잔고 수량 변화)이 있어야만
  일어남 — 요청을 보냈다는 사실만으로는 상태를 바꾸지 않음.

다음 단계: 실제 `order_id` 연결은 **1P0.8-A.1에서 완료**
(`confirm_pending_order_id()`). 남은 것은 브로커에
`get_open_orders()` / `get_order_status()` / `cancel_order()`를
추가해 이 번호로 특정 주문의 terminal 상태(FILLED/CANCELLED/
REJECTED)를 직접 확인하고, 재시작 시 outstanding order를
reconciliation하는 것(1P0.8-B 이후, 미착수). 그 전까지는 시간·잔고
변화로 추론하는 현재 방식의 한계 안에 있습니다.
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



# ══════════════════════════════════════════════════════════════
# 매도 결정 — 단일 진입점 (1P0.5)
# ══════════════════════════════════════════════════════════════
# 1P0.1~1P0.4에서 차단 규칙이 6종으로 늘었고, 조합에 따라 매도가
# 아예 나가지 않는 구간이 실제로 생겼습니다(재현 확인):
#   - orphan TTL 600초 동안 비강제 매도 전면 차단
#   - MAX_RETRY 도달 후 '체결 성공' 외에는 리셋 경로 없음 → 영구 차단
#
# 규칙을 흩어놓지 않고 하나의 함수에서 **명시적 우선순위**로 평가하고,
# 어떤 경우에도 최종 청산 경로가 열려 있음을 보장합니다.


class SellDecision(str, Enum):
    ALLOW = "ALLOW"                  # 정상 매도
    ALLOW_FORCED = "ALLOW_FORCED"    # 손절·강제청산 (SOFT block만 우회)
    THROTTLED = "THROTTLED"          # 최소 간격 미달
    BLOCKED = "BLOCKED"              # 차단 (HARD 또는 SOFT)
    # 2026-08-10 (1P0.7, GPT 코드리뷰): ALLOW_ESCALATED를 제거했습니다.
    # "시간이 지나면 사유 불문 허용"은 orphan/BUY_PENDING/ERROR처럼
    # 원 주문 상태를 모르는 상황에서 이중 주문으로 이어질 수 있습니다
    # (재현: orphan 5분 경과 → forced SELL 허용 → 이중 매도 가능).
    # HARD block은 시간이 지나도 여전히 BLOCKED이며, 다만 이 상태를
    # RECONCILIATION_REQUIRED로 승격해 CRITICAL로 노출합니다 — 매도를
    # 열어주는 것이 아니라 "사람이 지금 확인해야 한다"는 신호입니다.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class SellDecisionResult:
    decision: SellDecision
    code: str = ""
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in (SellDecision.ALLOW, SellDecision.ALLOW_FORCED)


@dataclass
class SymbolPositionState:
    """종목 하나의 현재 생명주기 상태와 관련 메타데이터."""

    lifecycle: PositionLifecycle = PositionLifecycle.FLAT
    pending_order_id: str | None = None
    pending_quantity: int = 0          # 진행 중인 주문의 요청 수량
    known_quantity: int = 0            # 마지막으로 확인한 실제 보유수량(관찰값)
    requested_at: datetime | None = None
    last_filled_at: datetime | None = None
    last_error: str | None = None

    # 2026-08-10 (1P0.1, 재현 확인): 주문 시작 시점의 **불변 스냅샷**.
    #
    # 기존 `expected_min = known_quantity + pending_quantity`는 부분체결로
    # known_quantity가 늘어날 때마다 목표수량이 함께 움직였습니다.
    # 8/10 047040 실제 로그:
    #     BUY 353 요청 → broker 146 → known=146
    #     다음 기대값 146+353 = 499  ← 도달 불가능
    #     353주 전량 체결 후에도 PENDING_STILL_UNCONFIRMED 반복
    #
    # expected_final_quantity는 그 주문이 terminal이 될 때까지 절대
    # 변경하지 않습니다. known_quantity는 관찰값 역할만 유지하고,
    # **목표 계산에 다시 쓰지 않습니다.**
    base_quantity_before_order: int = 0    # 주문 직전 실제 보유수량
    requested_quantity: int = 0            # 이번 주문의 요청 수량
    expected_final_quantity: int = 0       # base + requested (불변)
    observed_quantity: int = 0             # 관찰된 최신 브로커 수량
    sell_base_quantity: int = 0            # SELL 주문 직전 보유수량(불변)

    # 2026-08-10 (1P0.2): 브로커 거부에 대한 재시도 backoff.
    # 8/10 047040은 "매도가능수량 부족"을 16초 간격으로 11회 반복해
    # 브로커 rate limit 위험과 로그 노이즈를 만들었습니다.
    # 2026-08-10 (1P0.3): PENDING 진입 시각. 브로커 응답이 오지 않는
    # 구간이 무한정 이어지면 청산 자체가 막히므로 타임아웃이 필요합니다.
    pending_since: datetime | None = None
    partial_fill_since: datetime | None = None

    # 2026-08-10 (1P0.4, 재현 확인): 타임아웃으로 OPEN/FLAT을 확정해도
    # **브로커에 남아 있는 원 주문은 사라지지 않습니다**. 1P0.3은
    # pending_order_id를 None으로 지워 그 사실 자체를 잊었고, 그 뒤
    # 새 SELL을 내면 원 주문 + 새 주문이 동시에 체결될 수 있습니다.
    # 주문을 취소할 수 없으므로(브로커 API 미지원) 최소한 "미확인
    # 주문이 있다"는 상태를 보존하고, 그 구간의 매도를 제한합니다.
    # 2026-08-10 (1P0.5): 연속 차단 추적. 어떤 사유든 오래 막히면
    # 청산 경로를 강제로 열어야 합니다(안전밸브).
    # 1P0.6: ERROR 진입 시각. 자동 회복 판단 기준.
    error_since: datetime | None = None
    last_forced_sell_at: datetime | None = None
    blocked_since: datetime | None = None
    blocked_count: int = 0
    last_block_code: str | None = None

    orphan_order_id: str | None = None
    orphan_since: datetime | None = None
    orphan_expected_delta: int = 0     # 원 주문이 체결되면 예상되는 수량 변화
    sell_reject_count: int = 0
    sell_reject_last_at: datetime | None = None
    sell_reject_last_reason: str | None = None


class PositionStateMachine:
    """종목별 SymbolPositionState를 관리하며 전이를 수행합니다.

    2026-08-13 (1P0.7.2, GPT 코드리뷰 지적): 이 docstring도 "판정
    결과가 아직 실제 매매 로직에 쓰이지 않는다"는 stale한 shadow
    시절 설명이었습니다. 실제로는 1P0.2부터 `_try_buy`/`_try_sell`이
    `would_block_buy_detail()`/`decide_sell()`의 결과로 주문을
    enforce로 차단합니다. TradingService가 매 폴링마다 이 상태머신에
    이벤트를 통지하고, 브로커 잔고와 대조해 판정합니다.

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
        # 1P0.1: 이 주문의 목표를 여기서 한 번만 고정합니다.
        state.pending_since = datetime.now()
        state.partial_fill_since = None
        state.base_quantity_before_order = state.known_quantity
        state.requested_quantity = quantity
        state.expected_final_quantity = state.known_quantity + quantity
        state.observed_quantity = state.known_quantity
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

        # 1P0.1: 불변 스냅샷 기준으로만 판정합니다.
        base = state.base_quantity_before_order
        expected_final = state.expected_final_quantity
        state.observed_quantity = broker_quantity

        if broker_quantity == expected_final:
            # 목표 수량에 정확히 도달 -> 전량 체결
            state.lifecycle = PositionLifecycle.OPEN
            state.known_quantity = broker_quantity
            state.last_filled_at = datetime.now()
            state.pending_order_id = None
            state.pending_quantity = 0
            state.requested_quantity = 0
            state.last_error = None
            state.pending_since = None
            state.partial_fill_since = None
            # 2026-08-10 (1P0.7, 재현 확인): base==0이면 FLAT에서 새로
            # 시작한 포지션입니다 — 이전 사이클의 sell_reject_count/
            # blocked_since가 남아있으면 첫 SELL이 즉시
            # RECONCILIATION_REQUIRED로 오판될 수 있습니다.
            if base == 0:
                self._reset_transient_block_state(state)
            self._log_event(symbol, "BUY_CONFIRMED", prev, state.lifecycle,
                            broker_quantity, f"FILLED(expected_final={expected_final})")
        elif broker_quantity > expected_final:
            # 요청분보다 많이 늘어난 이상 상황 — 자동 확정하지 않고 ERROR.
            # pending_*는 일부러 지우지 않아 "무엇을 기대했다가 어긋났는지"
            # 를 사람이 추적할 수 있게 남깁니다.
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNEXPECTED_QUANTITY_EXCESS"
            state.error_since = datetime.now()
            self._log_event(
                symbol, "BUY_CONFIRMED", prev, state.lifecycle, broker_quantity,
                f"UNEXPECTED_QUANTITY_EXCESS(expected_final={expected_final}, "
                f"actual={broker_quantity})",
            )
        elif base < broker_quantity < expected_final:
            # 부분체결 — 목표에 미달했으므로 BUY_PENDING 유지.
            # known_quantity는 관찰값으로 갱신하되 목표는 건드리지 않습니다.
            state.known_quantity = broker_quantity
            state.last_error = "PARTIAL_FILL"
            if state.partial_fill_since is None:
                state.partial_fill_since = datetime.now()
            self._log_event(symbol, "BUY_CONFIRMED", prev, prev, broker_quantity,
                            f"PARTIAL_FILL_PENDING(base={base}, "
                            f"expected_final={expected_final})")
        elif broker_quantity < base:
            # 주문 전보다 줄어듦 — 외부 매도 등 설명되지 않는 상황
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNEXPECTED_QUANTITY_DECREASE"
            state.error_since = datetime.now()
            self._log_event(
                symbol, "BUY_CONFIRMED", prev, state.lifecycle, broker_quantity,
                f"UNEXPECTED_QUANTITY_DECREASE(base={base}, actual={broker_quantity})",
            )
        else:
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
        # 1P0.1: SELL 주문 직전 보유수량을 고정합니다. 부분체결 판정의
        # 기준을 known_quantity(갱신됨)가 아니라 이 값으로 삼습니다.
        state.pending_since = datetime.now()
        state.partial_fill_since = None
        state.sell_base_quantity = state.known_quantity
        state.observed_quantity = state.known_quantity
        detail = f"qty={quantity}"
        if was_error:
            detail += " (직전 ERROR 상태에서 매도 시도 — 이상치 정리 시도로 추정)"
        self._log_event(symbol, "SELL_REQUESTED", prev, state.lifecycle, detail=detail)

    def on_sell_result(self, symbol: str, accepted: bool, broker_quantity: int,
                       reject_reason: str | None = None) -> None:
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
            # 1P0.2: 연속 거부 횟수를 누적해 backoff 근거로 씁니다.
            state.sell_reject_count += 1
            state.sell_reject_last_at = datetime.now()
            state.sell_reject_last_reason = reject_reason
            self._log_event(
                symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity,
                f"SELL_REJECTED(count={state.sell_reject_count}"
                + (f", reason={reject_reason}" if reject_reason else "") + ")",
            )
            return

        sell_base = state.sell_base_quantity or state.known_quantity
        state.observed_quantity = broker_quantity

        if broker_quantity == 0:
            # 전량 체결 — 여기서만 FLAT
            state.lifecycle = PositionLifecycle.FLAT
            state.known_quantity = 0
            state.sell_base_quantity = 0
            self._reset_transient_block_state(state)
            detail = "FILLED_FULL"
        elif 0 < broker_quantity < sell_base:
            # 2026-08-10 (1P0.2, 재현 확인): 기존엔 부분체결을 OPEN으로
            # 되돌리고 pending 정보를 지웠습니다. 그러면 원 SELL 주문의
            # 나머지가 아직 살아 있는데도 "새 포지션"으로 보여 추가 SELL을
            # 발행합니다.
            #   8/10 047040: SELL 353 accepted → broker 343
            #     → OPEN 전환 → 343주 재매도 → "매도가능수량 부족" 11회
            # 부분체결만으로 OPEN 전환하지 않고 SELL_PENDING을 유지하며,
            # pending order 정보도 보존합니다.
            state.known_quantity = broker_quantity
            state.last_error = "PARTIAL_FILL"
            if state.partial_fill_since is None:
                state.partial_fill_since = datetime.now()
            self._log_event(
                symbol, "SELL_RESULT", prev, prev, broker_quantity,
                f"PARTIAL_FILL_PENDING(sell_base={sell_base}, "
                f"remaining={broker_quantity})",
            )
            return
        elif broker_quantity == sell_base:
            # 매도 시도 당시 수량과 동일 — 아직 API 미반영, SELL_PENDING 유지
            self._log_event(symbol, "SELL_RESULT", prev, prev, broker_quantity, "PENDING_STILL_UNCONFIRMED")
            return
        elif broker_quantity > sell_base:
            # 2026-07-24 (9차 수정, GPT 코드리뷰): 매도 요청 중인데
            # 잔고가 오히려 늘어난 이상 상황 — 기존엔 이것도 "부분체결"
            # else 분기에 섞여 있었음(잘못된 해석: 매도인데 수량이
            # 늘면 부분체결일 수 없음). 동시에 다른 매수가 겹쳤거나
            # 데이터 이상일 수 있어 자동 확정하지 않고 ERROR로 전이.
            # (2026-07-24, 11차 수정): known_quantity도 실제 관찰값으로
            # 갱신 — confirm_buy_from_broker()와 동일한 원칙. 로그
            # 메시지에는 갱신 "전" 값을 남겨야 "얼마에서 얼마로
            # 튀었는지" 알 수 있으므로 미리 변수에 저장해둠.
            known_before = sell_base
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNEXPECTED_QUANTITY_INCREASE"
            state.error_since = datetime.now()
            self._log_event(
                symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity,
                f"UNEXPECTED_QUANTITY_INCREASE(known_before={known_before}, actual={broker_quantity})",
            )
            return
        else:
            # 위 분기가 모든 경우를 덮으므로 여기 오면 안 됩니다.
            state.lifecycle = PositionLifecycle.ERROR
            state.known_quantity = broker_quantity
            state.last_error = "UNREACHABLE_SELL_BRANCH"
            state.error_since = datetime.now()
            detail = f"UNREACHABLE(sell_base={sell_base}, actual={broker_quantity})"

        state.last_filled_at = datetime.now()
        state.pending_order_id = None
        state.pending_quantity = 0
        # 체결이 진행됐으므로 거부 backoff 해제
        state.sell_reject_count = 0
        state.sell_reject_last_at = None
        state.sell_reject_last_reason = None
        state.pending_since = None
        state.partial_fill_since = None
        self._log_event(symbol, "SELL_RESULT", prev, state.lifecycle, broker_quantity, detail)

    def confirm_pending_order_id(self, symbol: str, order_id: str) -> None:
        """place_order() 접수(accepted) 직후, "pending" placeholder를
        브로커의 실제 주문번호로 교체합니다.

        2026-08-14 (1P0.8-A.1, GPT 코드리뷰 제안 — 근본 한계 4번 착수):
        `on_buy_requested()`/`on_sell_requested()`는 `place_order()`를
        호출하기 **전에** PSM에 `"pending"` 리터럴을 먼저 기록합니다
        (요청 자체를 반영하는 것뿐이라 이 시점엔 실제 주문번호를 아직
        모름). 그런데 `KiwoomBroker.place_order()`는 이미
        `response.body["ord_no"]`를 `OrderResult.order_id`로 정상
        반환하고 있었습니다 — 다만 그 값이 그 뒤로 한 번도 PSM에
        다시 연결되지 않아 `"pending"`이 그대로 남아있었습니다. 이
        메서드는 place_order() 응답을 받은 직후(BUY/SELL 공통) 호출해
        실제 값으로 승격시킵니다.

        `orphan_order_id`는 orphan 전이 시 `pending_order_id`를 그대로
        복사하므로(위 관련 코드 참고), 이 수정으로 orphan도 함께
        실제 주문번호를 갖게 됩니다 — 이전엔 모든 orphan이 동일한
        `"pending"` 문자열을 공유해 서로 구분이 불가능했습니다.

        accepted였는데 브로커가 order_id를 비워서 반환하면(응답 이상)
        추측하지 않고 `"UNKNOWN_ORDER_ID"` sentinel로 남겨 이후 order
        status 조회(1P0.8-B)가 이 주문을 절대 추적할 수 없다는 사실을
        명시적으로 남깁니다 — lifecycle을 ERROR로 전이시키지는
        않습니다(그러면 이 종목의 강제 손절 매도까지 HARD block되어
        더 위험합니다).

        2026-08-14 (1P0.8-A.1 재검토, GPT 코드리뷰 지적): 이 메서드가
        남기는 것은 `_log_event()`를 통한 CSV 이벤트 기록뿐이며,
        `self.app_logger.critical()`을 호출하지 않으므로 실제로는
        운영자가 즉시 보게 될 CRITICAL 경고가 없었습니다("추적키를
        잃은 접수 성공 주문"은 심각한 상황이라 반드시 있어야 함).
        PSM은 상태 관리만 책임지고, CRITICAL 알림은 호출부인
        TradingService가 `result.order_id`를 직접 확인해 남기는
        구조로 분리했습니다(`_try_buy`/`_try_sell_unchecked` 참고).

        공백만 있는 order_id(`" "` 등)를 유효한 값으로 오인하지
        않도록, 이 메서드에서 먼저 `strip()`합니다.

        호출 시점에 이미 다른 사유(거부 등)로 `pending_order_id`가
        `None`으로 지워졌다면 아무 것도 하지 않습니다.
        """
        state = self.get(symbol)
        if state.pending_order_id is None:
            return
        order_id = str(order_id or "").strip()
        if not order_id:
            state.pending_order_id = "UNKNOWN_ORDER_ID"
            state.last_error = "ACCEPTED_WITHOUT_ORDER_ID"
            self._log_event(
                symbol, "ORDER_ID_MISSING", state.lifecycle, state.lifecycle,
                detail="accepted=True인데 브로커 응답에 order_id가 없음 — "
                       "이 주문은 이후 order status 조회로 추적 불가",
            )
            return
        state.pending_order_id = order_id
        self._log_event(
            symbol, "ORDER_ID_CONFIRMED", state.lifecycle, state.lifecycle,
            detail=f"order_id={order_id}",
        )

    # ── 주문 전 guard (1P0.2에서 enforce로 전환) ────────────────
    # 2026-08-10: 1P0.1에서는 shadow(경고만)였습니다. 그러나 8/10
    # 실서버에서 "매도가능수량 부족" 11회가 실제로 발생했고, 이는
    # **정상 운영에서 나오면 안 되는 실패**입니다. 브로커 rate limit
    # 위험도 있어 shadow 상태로 며칠 더 두는 것이 오히려 위험하다고
    # 판단해 enforce로 올립니다.
    #
    # 상태머신은 브로커 응답으로만 전이하므로, guard가 잘못 잠기면
    # 청산 자체가 막힐 수 있습니다. 그래서 (1) SELL은 최대 대기
    # 시간을 두고, (2) 하드 손절·강제청산 경로는 force=True로 우회
    # 가능하게 하고, (3) 모든 차단을 [LIFECYCLE_BLOCK]으로 남깁니다.
    # 재시도 backoff — 연속 거부 n회마다 대기 시간을 늘립니다.
    SELL_RETRY_BACKOFF_SECONDS = (0, 30, 60, 120, 300)
    SELL_REJECT_MAX_RETRY = 5
    # 2026-08-10 (1P0.5): 거부 카운터가 '체결 성공'으로만 리셋돼,
    # MAX_RETRY 도달 후 영구 차단되는 경로가 있었습니다(재현 확인).
    # 마지막 거부로부터 이 시간이 지나면 카운터를 감쇠시켜
    # 재시도 기회를 회복합니다.
    SELL_REJECT_DECAY_SEC = 300

    # 2026-08-10 (1P0.3): PENDING 타임아웃.
    # 1P0.2는 부분체결 시 SELL_PENDING을 유지하면서 _try_sell을 막아,
    # 잔여수량이 **영원히 매도되지 않는** 상태를 만들었습니다(재현 확인).
    # 브로커 응답이 오지 않는 구간을 무한정 신뢰하지 않습니다.
    SELL_PENDING_TIMEOUT_SEC = 60        # 이후 잔여수량 재매도 허용
    BUY_PENDING_TIMEOUT_SEC = 120        # 이후 관측값 기준으로 상태 확정

    def _elapsed(self, at: "datetime | None") -> float:
        return (datetime.now() - at).total_seconds() if at else 0.0

    # 2026-08-10 (1P0.7): TTL로 orphan을 자동 해제하지 않습니다
    # (TTL 경과는 주문 종료의 증거가 아님). 진단 로그의 "age" 표시용
    # 참고값으로만 남깁니다.
    ORPHAN_TTL_SEC = 600

    def has_orphan_order(self, symbol: str) -> bool:
        """orphan 주문이 미확인 상태인가.

        2026-08-10 (1P0.7, GPT 코드리뷰 재현 확인): TTL 경과는 주문
        종료의 증거가 아닙니다. TTL로 자동 해제하던 것을 제거했습니다
        — orphan은 명시적 확인(`observe_for_orphan`의 정확한 목표
        도달, 또는 `acknowledge_orphan`) 없이는 영원히 유지됩니다.
        """
        return bool(self.get(symbol).orphan_order_id)

    def clear_orphan(self, symbol: str, note: str = "") -> None:
        """orphan 주문이 해소됐다고 표시합니다."""
        state = self.get(symbol)
        if not state.orphan_order_id:
            return
        prev_id = state.orphan_order_id
        state.orphan_order_id = None
        state.orphan_since = None
        state.orphan_expected_delta = 0
        self._log_event(symbol, "ORPHAN_CLEARED", state.lifecycle, state.lifecycle,
                        state.known_quantity, f"order={prev_id} {note}")

    def acknowledge_orphan(self, symbol: str, note: str) -> None:
        """사람이 브로커에서 원 주문 상태를 직접 확인한 뒤 해제합니다.

        2026-08-10 (1P0.7): 자동 해제 경로(부분 잔고 변화, TTL)를
        제거했으므로, 명확한 증거가 없는 orphan은 이 메서드로만
        해소됩니다. `note`에 확인 근거를 남기십시오.
        """
        if not note:
            raise ValueError("acknowledge_orphan에는 확인 근거(note)가 필요합니다")
        self.clear_orphan(symbol, f"ACKNOWLEDGED: {note}")

    def observe_for_orphan(self, symbol: str, broker_quantity: int) -> str | None:
        """잔고가 **정확히 목표에 도달**했을 때만 orphan을 자동 해소합니다.

        2026-08-10 (1P0.7, GPT 코드리뷰, 재현 확인): 이전엔 "잔고가
        줄기만 하면" SELL orphan을 해소했습니다. 343→300은 43주가
        추가로 체결된 것뿐일 수 있고, 원 주문의 나머지 300주는 여전히
        브로커에 살아있을 수 있습니다. 부분 변화는 증거가 아닙니다.

        이 시스템은 항상 전량매도만 하므로(설계 불변), SELL orphan의
        유일한 확실한 종료 증거는 **잔고 0 도달**입니다. BUY orphan은
        `expected_final_quantity`(주문 시작 시 고정된 목표)에 정확히
        도달했을 때만 확정합니다. 그 외의 모든 변화는 진단 로그만
        남기고 orphan을 유지합니다 — 필요하면 `acknowledge_orphan()`
        으로 사람이 명시적으로 해소하십시오.
        """
        state = self.get(symbol)
        if not state.orphan_order_id:
            return None
        if state.orphan_expected_delta < 0 and broker_quantity == 0:
            note = "잔고 0 도달 (SELL orphan 완전 체결 확인)"
            self.clear_orphan(symbol, note)
            return note
        if state.orphan_expected_delta > 0 and broker_quantity == state.expected_final_quantity:
            note = f"목표수량({state.expected_final_quantity}) 도달 (BUY orphan 완전 체결 확인)"
            self.clear_orphan(symbol, note)
            return note
        if broker_quantity != state.known_quantity:
            self._log_event(
                symbol, "ORPHAN_PARTIAL_OBSERVED", state.lifecycle, state.lifecycle,
                broker_quantity,
                f"order={state.orphan_order_id} 잔고 변화 감지"
                f"({state.known_quantity}→{broker_quantity})했으나 목표 미도달 — "
                f"orphan 유지(진단 전용, 자동 해소 아님)",
            )
        return None

    def sell_pending_stale(self, symbol: str) -> bool:
        """SELL_PENDING이 타임아웃을 넘겼는가 (`resolve_stale_pending`용)."""
        state = self.get(symbol)
        if state.lifecycle != PositionLifecycle.SELL_PENDING:
            return False
        base = state.partial_fill_since or state.pending_since
        return self._elapsed(base) >= self.SELL_PENDING_TIMEOUT_SEC

    def _reset_transient_block_state(self, state: SymbolPositionState,
                                      *, clear_orphan: bool = True) -> None:
        """새 포지션 사이클 시작(FLAT 확정) 시 임시 차단 흔적을 지웁니다.

        2026-08-10 (1P0.7, GPT 코드리뷰, 재현 확인): 이전 사이클의
        `sell_reject_count`/`blocked_since`가 다음 사이클로 넘어가,
        새로 산 종목의 **첫 SELL이 즉시 RECONCILIATION_REQUIRED로
        승격**되는 사고가 재현됐습니다. FLAT 확정 시점에 반드시
        초기화합니다.

        2026-08-10 (1P0.7.1, GPT 코드리뷰 P0, 재현 확인): 기존엔 항상
        orphan까지 함께 지웠는데, **BUY_PENDING이 0주 체결로 타임아웃
        되면 방금 이 함수를 부르기 직전에 orphan을 새로 만들어두고,
        곧바로 이 함수가 그 orphan을 지워버렸습니다**:
        ```
        BUY 174 accepted → 120초 동안 0주 체결
        → orphan 생성(원 주문이 여전히 살아있을 수 있음)
        → lifecycle=FLAT이니까 reset → 방금 만든 orphan을 삭제
        → BUY guard=None, SELL decide.allowed=True
        ```
        "FLAT이 됐다"는 것과 "이번에 만든 orphan이 종료됐다"는 것은
        다른 이야기입니다 — 여기서는 잔고가 0이라 FLAT일 뿐, 원 BUY
        주문의 생사는 전혀 확인되지 않았습니다. 호출부가 orphan을
        같은 호출에서 막 만들었다면 `clear_orphan=False`로 보존을
        명시해야 합니다.
        """
        state.sell_reject_count = 0
        state.sell_reject_last_at = None
        state.sell_reject_last_reason = None
        state.blocked_since = None
        state.blocked_count = 0
        state.last_block_code = None
        state.last_forced_sell_at = None
        state.error_since = None
        if clear_orphan:
            # orphan은 FLAT이 됐다는 것 자체가 강한 종료 증거일 때만 정리.
            state.orphan_order_id = None
            state.orphan_since = None
            state.orphan_expected_delta = 0

    def resolve_stale_pending(self, symbol: str, broker_quantity: int) -> str | None:
        """타임아웃된 PENDING을 관측값 기준으로 확정합니다.

        2026-08-10 (1P0.3): 상태머신이 브로커 응답만 기다리다 잠기는
        것을 막는 탈출구입니다. 매 폴링에서 호출하며, 타임아웃 전에는
        아무것도 하지 않습니다.

        **주의(1P0.7)**: 이 함수는 lifecycle을 SELL_PENDING/BUY_PENDING
        에서 벗어나게 할 뿐, `decide_sell()`/`would_block_buy_detail()`
        guard를 풀어주지 않습니다 — 잔여수량이 있으면 orphan으로
        이관되어 계속 HARD block됩니다. 잔여수량이 0이면(=완전 청산
        확인) FLAT으로 확정하고 임시 차단 상태를 초기화합니다.
        """
        state = self.get(symbol)
        prev = state.lifecycle
        if prev == PositionLifecycle.SELL_PENDING:
            base = state.partial_fill_since or state.pending_since
            if self._elapsed(base) < self.SELL_PENDING_TIMEOUT_SEC:
                return None
            if broker_quantity <= 0:
                state.lifecycle = PositionLifecycle.FLAT
                state.known_quantity = 0
            else:
                # 잔여수량이 남았으므로 OPEN으로 되돌리되, orphan HARD
                # block으로 이관해 새 SELL을 계속 막습니다(아래).
                state.lifecycle = PositionLifecycle.OPEN
                state.known_quantity = broker_quantity
            if broker_quantity > 0 and state.pending_order_id:
                state.orphan_order_id = state.pending_order_id
                state.orphan_since = datetime.now()
                state.orphan_expected_delta = -broker_quantity
            state.pending_order_id = None
            state.pending_quantity = 0
            state.pending_since = None
            state.partial_fill_since = None
            if state.lifecycle == PositionLifecycle.FLAT:
                self._reset_transient_block_state(state)
            detail = (f"SELL_PENDING_TIMEOUT({self.SELL_PENDING_TIMEOUT_SEC}s) "
                      f"→ {state.lifecycle.value}(qty={broker_quantity})"
                      + (f" ORPHAN({state.orphan_order_id}) — 여전히 HARD block"
                         if state.orphan_order_id else ""))
            self._log_event(symbol, "PENDING_TIMEOUT", prev, state.lifecycle,
                            broker_quantity, detail)
            return detail
        if prev == PositionLifecycle.BUY_PENDING:
            base = state.partial_fill_since or state.pending_since
            if self._elapsed(base) < self.BUY_PENDING_TIMEOUT_SEC:
                return None
            state.lifecycle = (PositionLifecycle.OPEN if broker_quantity > 0
                               else PositionLifecycle.FLAT)
            state.known_quantity = max(0, broker_quantity)
            # 2026-08-10 (1P0.7.1, GPT 코드리뷰 P0, 재현 확인): 0주
            # 체결로 타임아웃돼도 원 주문이 브로커에 살아있을 수
            # 있으므로 **broker_quantity와 무관하게** 항상 orphan으로
            # 이관합니다. 기존엔 broker_quantity 조건이 없어 이 부분은
            # 이미 항상 실행됐지만, 바로 아래 reset이 그걸 지웠습니다.
            orphan_created_now = False
            if state.pending_order_id:
                state.orphan_order_id = state.pending_order_id
                state.orphan_since = datetime.now()
                state.orphan_expected_delta = max(
                    0, state.expected_final_quantity - broker_quantity)
                orphan_created_now = True
            state.pending_order_id = None
            state.pending_quantity = 0
            state.requested_quantity = 0
            state.pending_since = None
            state.partial_fill_since = None
            if state.lifecycle == PositionLifecycle.FLAT:
                # 1P0.7.1: 방금 orphan을 만들었다면 이 reset이 그걸
                # 지우면 안 됩니다 — FLAT(잔고 0)과 "원 주문 종료 확인"
                # 은 다른 이야기입니다. orphan을 보존한 채 나머지
                # 임시 차단 상태만 정리합니다.
                self._reset_transient_block_state(
                    state, clear_orphan=not orphan_created_now)
            detail = (f"BUY_PENDING_TIMEOUT({self.BUY_PENDING_TIMEOUT_SEC}s) "
                      f"→ {state.lifecycle.value}(qty={broker_quantity})"
                      + (f" ORPHAN({state.orphan_order_id}) — 여전히 HARD block"
                         if state.orphan_order_id else ""))
            self._log_event(symbol, "PENDING_TIMEOUT", prev, state.lifecycle,
                            broker_quantity, detail)
            return detail
        return None

    def decay_sell_rejects(self, symbol: str) -> bool:
        """마지막 거부 후 충분히 지났으면 카운터를 감쇠합니다 (SOFT block 전용)."""
        state = self.get(symbol)
        if not state.sell_reject_count or state.sell_reject_last_at is None:
            return False
        if self._elapsed(state.sell_reject_last_at) < self.SELL_REJECT_DECAY_SEC:
            return False
        state.sell_reject_count = max(0, state.sell_reject_count - 1)
        state.sell_reject_last_at = datetime.now()
        if state.sell_reject_count == 0:
            state.sell_reject_last_reason = None
        return True

    def sell_backoff_remaining(self, symbol: str) -> float:
        """다음 SELL 재시도까지 남은 초. 0이면 즉시 가능."""
        self.decay_sell_rejects(symbol)
        state = self.get(symbol)
        if not state.sell_reject_count or state.sell_reject_last_at is None:
            return 0.0
        idx = min(state.sell_reject_count, len(self.SELL_RETRY_BACKOFF_SECONDS) - 1)
        wait = self.SELL_RETRY_BACKOFF_SECONDS[idx]
        elapsed = (datetime.now() - state.sell_reject_last_at).total_seconds()
        return max(0.0, wait - elapsed)

    # 2026-08-10 (1P0.7, GPT 코드리뷰): 차단을 HARD와 SOFT로 명확히
    # 분리합니다.
    #   HARD  — 미확인 주문(BUY_PENDING/SELL_PENDING/orphan) 또는 ERROR.
    #           forced로도 절대 우회할 수 없고, 시간이 지나도 ALLOW로
    #           풀리지 않습니다. MAX_BLOCK_DURATION_SEC을 넘기면
    #           RECONCILIATION_REQUIRED로 승격되어 CRITICAL 로그를
    #           남기지만 **여전히 매도를 허용하지 않습니다** — 손절이
    #           막히더라도, "이미 살아있을 수 있는 주문 위에 새 주문을
    #           더 얹는 것"보다 사람의 확인을 기다리는 편이 안전하다는
    #           판단입니다(재현: orphan 상태에서 forced가 새 SELL을
    #           허용해 이중 매도로 이어질 수 있었음).
    #   SOFT  — 재시도 backoff/MAX_RETRY. 이건 우리 시스템의 자체
    #           재시도 제한일 뿐 브로커 주문 상태와 무관하므로, forced는
    #           우회할 수 있고 시간(decay)으로 자연히 풀립니다.
    MAX_BLOCK_DURATION_SEC = 300
    FORCED_SELL_MIN_INTERVAL_SEC = 30

    def _evaluate_hard_sell_blocks(self, symbol: str) -> tuple[str, str] | None:
        state = self.get(symbol)
        if state.lifecycle == PositionLifecycle.BUY_PENDING:
            return ("BLOCK_BUY_PENDING_SELL",
                    f"pending_order={state.pending_order_id}, "
                    f"expected_final={state.expected_final_quantity}, "
                    f"observed={state.observed_quantity}")
        if state.lifecycle == PositionLifecycle.SELL_PENDING:
            return ("BLOCK_DUPLICATE_SELL",
                    f"pending_order={state.pending_order_id}, "
                    f"sell_base={state.sell_base_quantity}, "
                    f"observed={state.observed_quantity}")
        if self.has_orphan_order(symbol):
            return ("BLOCK_SELL_ORPHAN_ORDER",
                    f"order={state.orphan_order_id}, "
                    f"age={self._elapsed(state.orphan_since):.0f}s")
        if state.lifecycle == PositionLifecycle.ERROR:
            return ("BLOCK_SELL_ERROR_STATE", f"last_error={state.last_error}")
        return None

    def _evaluate_soft_sell_blocks(self, symbol: str) -> tuple[str, str] | None:
        state = self.get(symbol)
        self.decay_sell_rejects(symbol)
        remain = self.sell_backoff_remaining(symbol)
        if remain > 0:
            return ("BLOCK_SELL_RETRY_BACKOFF",
                    f"count={state.sell_reject_count}, wait={remain:.0f}s, "
                    f"reason={state.sell_reject_last_reason}")
        if state.sell_reject_count >= self.SELL_REJECT_MAX_RETRY:
            return ("BLOCK_SELL_MAX_RETRY",
                    f"count={state.sell_reject_count}, "
                    f"reason={state.sell_reject_last_reason}")
        return None

    def _evaluate_sell_blocks(self, symbol: str,
                              now: "datetime | None" = None) -> tuple[str, str] | None:
        """하위호환 — HARD를 먼저, 없으면 SOFT를 확인합니다."""
        return self._evaluate_hard_sell_blocks(symbol) or self._evaluate_soft_sell_blocks(symbol)

    def check_block_escalation(self, symbol: str,
                               now: "datetime | None" = None) -> str | None:
        """HARD block 지속 시간을 **폴링마다** 확인합니다.

        2026-08-10 (1P0.6→1P0.7): `decide_sell()`이 SELL 신호가 있을
        때만 호출되므로, 신호가 없는 동안 차단이 조용히 지속됩니다.
        이 함수를 sync 루프에서 매 폴링 호출해 HARD block만 감시하고
        CRITICAL을 즉시 노출합니다. **상태를 바꾸거나 매도를 허용하지
        않습니다** — RECONCILIATION_REQUIRED는 사람의 개입 신호일
        뿐입니다.
        """
        now = now or datetime.now()
        state = self.get(symbol)
        hard = self._evaluate_hard_sell_blocks(symbol)
        if hard is None:
            if state.blocked_since is not None:
                self._clear_block_tracking(state)
            return None
        code, detail = hard
        if state.blocked_since is None:
            state.blocked_since = now
        state.last_block_code = code
        blocked_for = (now - state.blocked_since).total_seconds()
        if blocked_for >= self.MAX_BLOCK_DURATION_SEC:
            return (f"RECONCILIATION_REQUIRED(code={code}, "
                    f"blocked_for={blocked_for:.0f}s, detail={detail})")
        return None

    def decide_sell(self, symbol: str, *, forced: bool = False,
                    now: "datetime | None" = None) -> SellDecisionResult:
        """매도 가능 여부를 단일 진입점에서 결정합니다.

        **우선순위**
          1. HARD block — BUY_PENDING/SELL_PENDING/orphan/ERROR.
             forced로도 우회 불가. 5분 초과 시 RECONCILIATION_REQUIRED
             (여전히 BLOCKED — 사람 개입 필요).
          2. forced — HARD block이 없을 때만 평가. throttle 적용.
          3. SOFT block — 재시도 backoff/MAX_RETRY. forced가 우회.
          4. ALLOW
        """
        now = now or datetime.now()
        state = self.get(symbol)

        # ── 1. HARD block ────────────────────────────────────────
        hard = self._evaluate_hard_sell_blocks(symbol)
        if hard is not None:
            code, detail = hard
            if state.blocked_since is None:
                state.blocked_since = now
            state.blocked_count += 1
            state.last_block_code = code
            blocked_for = (now - state.blocked_since).total_seconds()
            if blocked_for >= self.MAX_BLOCK_DURATION_SEC:
                self._log_event(
                    symbol, "RECONCILIATION_REQUIRED", state.lifecycle, state.lifecycle,
                    state.known_quantity,
                    f"{code} 지속 {blocked_for:.0f}s — 자동 해제하지 않음, "
                    f"사람 확인 필요: {detail}",
                )
                return SellDecisionResult(
                    SellDecision.RECONCILIATION_REQUIRED, code, detail)
            return SellDecisionResult(SellDecision.BLOCKED, code, detail)

        # HARD block이 없으면 그 추적은 정리(SOFT는 별도 카운터로 관리).
        if state.blocked_since is not None:
            self._clear_block_tracking(state)

        # ── 2. forced ───────────────────────────────────────────
        if forced:
            last = state.last_forced_sell_at
            if last and (now - last).total_seconds() < self.FORCED_SELL_MIN_INTERVAL_SEC:
                return SellDecisionResult(
                    SellDecision.THROTTLED, "FORCED_SELL_THROTTLED",
                    f"min_interval={self.FORCED_SELL_MIN_INTERVAL_SEC}s")
            state.last_forced_sell_at = now
            return SellDecisionResult(SellDecision.ALLOW_FORCED, "FORCED")

        # ── 3. SOFT block ───────────────────────────────────────
        soft = self._evaluate_soft_sell_blocks(symbol)
        if soft is not None:
            code, detail = soft
            return SellDecisionResult(SellDecision.BLOCKED, code, detail)

        return SellDecisionResult(SellDecision.ALLOW)

    @staticmethod
    def _clear_block_tracking(state: SymbolPositionState) -> None:
        state.blocked_since = None
        state.blocked_count = 0
        state.last_block_code = None

    def would_block_sell(self, symbol: str) -> str | None:
        """하위호환 조회용. 실제 판단은 `decide_sell()`을 쓰십시오."""
        block = self._evaluate_sell_blocks(symbol)
        if block is None:
            return None
        return f"{block[0]}({block[1]})"

    def would_block_buy_detail(self, symbol: str) -> tuple[str, str] | None:
        """(안정적 code, 상세) 형태로 반환합니다.

        2026-08-10 (1P0.7, GPT 코드리뷰): SELL_PENDING과 orphan에서도
        신규 BUY가 가능했습니다(재현: 원 주문 상태를 모르면 해당
        종목의 모든 신규 자동 주문을 잠가야 함). SELL 쪽 HARD block과
        동일한 조건을 공유합니다.
        """
        state = self.get(symbol)
        if state.lifecycle == PositionLifecycle.BUY_PENDING:
            return ("BLOCK_BUY_WHILE_PENDING",
                    f"pending_order={state.pending_order_id}, "
                    f"expected_final={state.expected_final_quantity}, "
                    f"observed={state.observed_quantity}")
        if state.lifecycle == PositionLifecycle.SELL_PENDING:
            return ("BLOCK_BUY_WHILE_SELL_PENDING",
                    f"pending_order={state.pending_order_id}, "
                    f"sell_base={state.sell_base_quantity}")
        if self.has_orphan_order(symbol):
            return ("BLOCK_BUY_ORPHAN_ORDER",
                    f"order={state.orphan_order_id}, "
                    f"age={self._elapsed(state.orphan_since):.0f}s")
        if state.lifecycle == PositionLifecycle.ERROR:
            return ("BLOCK_BUY_IN_ERROR", f"last_error={state.last_error}")
        return None

    def would_block_buy(self, symbol: str) -> str | None:
        """이 시점에 BUY를 내보내면 미결/미확인 주문과 겹치는지."""
        detail = self.would_block_buy_detail(symbol)
        if detail is None:
            return None
        return f"{detail[0]}({detail[1]})"

    def check_invariant(self, symbol: str, broker_quantity: int) -> str | None:
        # 1P0.4: orphan 주문이 살아 있으면 잔고 불일치가 정상입니다 —
        # CRITICAL 오탐을 막기 위해 이 구간은 검사에서 제외합니다.
        if self.has_orphan_order(symbol):
            return None
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
        이 메서드는 사람이 명시적으로 호출해야만 동작합니다 — 자동
        폴링 경로에서는 절대 호출되지 않으므로, 잘못된 값을 넣어도
        영향 범위가 이 종목의 lifecycle 정정 한 건으로 한정됩니다.

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
