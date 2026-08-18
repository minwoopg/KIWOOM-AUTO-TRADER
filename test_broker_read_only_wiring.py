from __future__ import annotations

"""1P0.8-C: Broker.get_open_orders()/get_order_status() read-only wiring 테스트.

범위(민우님 승인, CHANGELOG_v1.6.md "1P0.8-C" 참고): Broker 인터페이스에
get_open_orders()/get_order_status()를 추가하고, KiwoomBroker는 이를
ka10075(oso)/ka10076(cntr) 조회 + infra/broker/kiwoom_order_status.py의
derive_broker_order_status()로 구현하며, MockBroker는 동일 계약을
테스트 가능하게 구현합니다. TradingService 자동 호출/PSM 상태
변경/orphan 자동 해제/ERROR 자동 복구/cancel_order/BUY-SELL 판정
변경/restart reconciliation은 전부 범위 밖입니다 — 이 파일도 그 범위를
벗어나지 않습니다.

이 파일이 특히 검증하려는 것: get_order_status()가 UNKNOWN을 반환하는
경우에도 추가 HTTP 재조회나 내부 추론이 절대 없다는 것(민우님이 명시적으로
요구한 제약) — _CountingSession으로 실제 호출 횟수를 센다.
"""

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"[PASS] {label}")
        passed += 1
    else:
        print(f"[FAIL] {label}")
        failed += 1


from config.settings import BrokerConfig
from domain.models import BrokerOrder, BrokerOrderStatus, OrderRequest, OrderSide
from infra.broker.base import Broker
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker


def _build_broker() -> KiwoomBroker:
    config = BrokerConfig(
        provider="kiwoom",
        use_mock=True,
        base_url="https://mockapi.kiwoom.com",
        app_key="test-app-key",
        secret_key="test-secret-key",
        account_number="00000000",
        is_paper_trading=True,
    )
    broker = KiwoomBroker(config)
    broker.access_token = "test-token"
    return broker


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        # 2026-08-18 (1P0.8-C.1 closure): 실제 키움 응답은 항상 cont-yn
        # 헤더를 명시적으로 내려줍니다(단일 페이지면 "N"). 이 테스트
        # 파일의 fake response들은 전부 단일 페이지 시나리오이므로,
        # headers를 명시하지 않으면 기본값으로 cont-yn="N"을 넣어
        # 실제 응답 형태를 흉내냅니다 — _fetch_paginated_rows()가
        # cont-yn=="N"일 때만 정상 종료로 취급하도록 강화됐기 때문에,
        # 헤더가 아예 없는 fake response(빈 문자열)는 이제
        # "알 수 없는 cont-yn"으로 fail-close됩니다(의도된 동작).
        self.headers = headers if headers is not None else {"cont-yn": "N", "next-key": ""}
        self.text = str(body)

    def json(self):
        return self._body


class _ApiIdRoutedSession:
    """api-id 헤더로 ka10075/ka10076 응답을 분리해 돌려주는 fake session.

    호출 횟수와 (endpoint, api-id) 조합을 기록해, wiring이 "추가
    재조회 없이 각각 정확히 한 번씩만" 부르는지 검증할 수 있게 합니다.
    """

    def __init__(self, oso_body: dict, cntr_body: dict):
        self._oso_body = oso_body
        self._cntr_body = cntr_body
        self.calls: list[tuple[str, str, dict]] = []  # (endpoint, api_id, payload)

    def post(self, url, headers=None, json=None, timeout=None):
        api_id = (headers or {}).get("api-id", "")
        endpoint = url.split("mockapi.kiwoom.com", 1)[-1]
        self.calls.append((endpoint, api_id, json))
        if api_id == "ka10075":
            return _FakeResponse(200, self._oso_body)
        if api_id == "ka10076":
            return _FakeResponse(200, self._cntr_body)
        return _FakeResponse(404, {"return_code": -1, "return_msg": "unexpected api-id"})


# ══════════════════════════════════════════════════════════════
# 0. Broker 인터페이스 계약 — 두 구현체 모두 인스턴스화 가능해야 함
#    (abstractmethod가 하나라도 안 채워지면 TypeError로 즉시 드러남)
# ══════════════════════════════════════════════════════════════

check("0-1) KiwoomBroker는 Broker의 서브클래스", issubclass(KiwoomBroker, Broker))
check("0-2) MockBroker는 Broker의 서브클래스", issubclass(MockBroker, Broker))
check(
    "0-3) Broker에 get_open_orders/get_order_status가 정의됨",
    hasattr(Broker, "get_open_orders") and hasattr(Broker, "get_order_status"),
)


# ══════════════════════════════════════════════════════════════
# 1. KiwoomBroker.get_order_status() — 전량체결(FILLED) 실측 shape
# ══════════════════════════════════════════════════════════════

cntr_filled_body = {
    "cntr": [
        {
            "ord_no": "0157897",
            "io_tp_nm": "+매수",
            "ord_qty": "5",
            "cntr_qty": "5",
            "oso_qty": "0",
            "ord_stt": "체결",
            "trde_tp": "시장가",
            "cntr_pric": "273000",
        }
    ],
    "return_code": 0,
    "return_msg": " 조회가 완료되었습니다.",
}
oso_empty_body = {"oso": [], "return_code": 0, "return_msg": " 조회가 완료되었습니다."}

broker1 = _build_broker()
session1 = _ApiIdRoutedSession(oso_body=oso_empty_body, cntr_body=cntr_filled_body)
broker1.session = session1
result1 = broker1.get_order_status(order_id="157897", symbol="005930")

check("1-1) get_order_status()는 BrokerOrder를 반환함", isinstance(result1, BrokerOrder))
check("1-2) 전량체결 shape → FILLED", result1.status == BrokerOrderStatus.FILLED)
check("1-3) FILLED면 filled_price가 채워짐", result1.filled_price == 273000)
check(
    "1-4) ka10075/ka10076 정확히 한 번씩만 호출됨(추가 재조회 없음)",
    len(session1.calls) == 2,
)
check(
    "1-5) 두 호출 모두 /api/dostk/acnt 엔드포인트를 씀",
    all(endpoint == "/api/dostk/acnt" for endpoint, _, _ in session1.calls),
)
check(
    "1-6) api-id가 각각 ka10075/ka10076으로 정확히 나뉨",
    {api_id for _, api_id, _ in session1.calls} == {"ka10075", "ka10076"},
)


# ══════════════════════════════════════════════════════════════
# 2. KiwoomBroker.get_order_status() — 미체결(OPEN) 실측 shape
# ══════════════════════════════════════════════════════════════

oso_open_body = {
    "oso": [
        {
            "ord_no": "0013557",
            "io_tp_nm": "+매수",
            "ord_qty": "1",
            "cntr_qty": "0",
            "oso_qty": "1",
            "ord_stt": "접수",
            "trde_tp": "보통",
        }
    ],
    "return_code": 0,
    "return_msg": " 조회가 완료되었습니다.",
}
cntr_empty_body = {"cntr": [], "return_code": 0, "return_msg": " 조회가 완료되었습니다."}

broker2 = _build_broker()
session2 = _ApiIdRoutedSession(oso_body=oso_open_body, cntr_body=cntr_empty_body)
broker2.session = session2
result2 = broker2.get_order_status(order_id="13557", symbol="005930")

check("2-1) 미체결 shape → OPEN", result2.status == BrokerOrderStatus.OPEN)
check("2-2) OPEN이면 filled_price는 None(체결 미확정)", result2.filled_price is None)
check("2-3) 여기서도 정확히 두 번만 호출됨", len(session2.calls) == 2)


# ══════════════════════════════════════════════════════════════
# 3. KiwoomBroker.get_order_status() — 매칭 없음(UNKNOWN) & 재조회 없음 확인
# ══════════════════════════════════════════════════════════════

broker3 = _build_broker()
session3 = _ApiIdRoutedSession(oso_body=oso_empty_body, cntr_body=cntr_empty_body)
broker3.session = session3
result3 = broker3.get_order_status(order_id="999999", symbol="005930")

check("3-1) 매칭 없음 → UNKNOWN", result3.status == BrokerOrderStatus.UNKNOWN)
check(
    "3-2) UNKNOWN이 나와도 추가 재조회 없이 정확히 두 번만 호출됨"
    "(민우님이 명시적으로 요구한 '재추론/재조회 금지' 검증)",
    len(session3.calls) == 2,
)


# ══════════════════════════════════════════════════════════════
# 4. KiwoomBroker.get_open_orders() — oso 목록 전체를 판정해서 반환
# ══════════════════════════════════════════════════════════════

oso_multi_body = {
    "oso": [
        {
            "ord_no": "0013557",
            "io_tp_nm": "+매수",
            "ord_qty": "1",
            "cntr_qty": "0",
            "oso_qty": "1",
            "ord_stt": "접수",
            "trde_tp": "보통",
        },
        {
            # 부분체결 모양(수량 signature 불일치) — UNKNOWN으로 fail-close 되어야 함
            "ord_no": "0099001",
            "io_tp_nm": "+매수",
            "ord_qty": "10",
            "cntr_qty": "4",
            "oso_qty": "6",
            "ord_stt": "접수",
            "trde_tp": "보통",
        },
    ],
    "return_code": 0,
    "return_msg": " 조회가 완료되었습니다.",
}

broker4 = _build_broker()
session4 = _ApiIdRoutedSession(oso_body=oso_multi_body, cntr_body=cntr_empty_body)
broker4.session = session4
result4 = broker4.get_open_orders(symbol="005930")

check("4-1) get_open_orders()는 list[BrokerOrder]를 반환함", isinstance(result4, list) and all(isinstance(o, BrokerOrder) for o in result4))
check("4-2) oso에 있던 두 주문 모두 결과에 포함됨(개수 일치)", len(result4) == 2)
statuses_by_id = {o.order_id.lstrip("0") or "0": o.status for o in result4}
check(
    "4-3) 정상 미체결 signature(13557)는 OPEN",
    statuses_by_id.get("13557") == BrokerOrderStatus.OPEN,
)
check(
    "4-4) 부분체결 모양(99001, 6/4 split)은 fail-close되어 UNKNOWN"
    "(오판으로 OPEN에 섞여 나오면 안 됨)",
    statuses_by_id.get("99001") == BrokerOrderStatus.UNKNOWN,
)
check("4-5) ka10075/ka10076도 정확히 한 번씩만 호출됨(주문 개수와 무관)", len(session4.calls) == 2)


# ══════════════════════════════════════════════════════════════
# 5. KiwoomBroker.get_open_orders() — 빈 oso → 빈 리스트, 추가 호출 없음
# ══════════════════════════════════════════════════════════════

broker5 = _build_broker()
session5 = _ApiIdRoutedSession(oso_body=oso_empty_body, cntr_body=cntr_empty_body)
broker5.session = session5
result5 = broker5.get_open_orders(symbol="005930")

check("5-1) 미체결이 없으면 빈 리스트", result5 == [])
check(
    "5-2) oso가 비어있으면 ka10076 호출을 생략함(1P0.8-C.1, API 호출 절약)"
    " — ka10075 한 번만 호출됨",
    len(session5.calls) == 1 and session5.calls[0][1] == "ka10075",
)


# ══════════════════════════════════════════════════════════════
# 6. MockBroker — place_order() 후 get_order_status()가 동일 계약으로 응답
# ══════════════════════════════════════════════════════════════

mock1 = MockBroker()
buy_order = OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=3)
buy_result = mock1.place_order(buy_order)
status_after_buy = mock1.get_order_status(order_id=buy_result.order_id, symbol="005930")

check("6-1) BUY 접수 성공", buy_result.accepted is True)
check("6-2) MockBroker도 BrokerOrder를 반환함", isinstance(status_after_buy, BrokerOrder))
check("6-3) 즉시 전량체결 시뮬레이션이므로 FILLED", status_after_buy.status == BrokerOrderStatus.FILLED)
check("6-4) 요청 수량과 체결 수량이 일치", status_after_buy.filled_quantity == 3)
check("6-5) open_quantity는 0(전량체결)", status_after_buy.open_quantity == 0)
check("6-6) side가 BUY로 기록됨", status_after_buy.side == OrderSide.BUY)


# ══════════════════════════════════════════════════════════════
# 7. MockBroker — 모르는 order_id/symbol 불일치는 UNKNOWN (추측 금지)
# ══════════════════════════════════════════════════════════════

mock2 = MockBroker()
unknown_status = mock2.get_order_status(order_id="MOCK-999999", symbol="005930")
check("7-1) 존재하지 않는 order_id → UNKNOWN", unknown_status.status == BrokerOrderStatus.UNKNOWN)

mock3 = MockBroker()
buy_order3 = OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=1)
buy_result3 = mock3.place_order(buy_order3)
wrong_symbol_status = mock3.get_order_status(order_id=buy_result3.order_id, symbol="000660")
check(
    "7-2) order_id는 알지만 symbol이 다르면 UNKNOWN(교차 매칭 금지)",
    wrong_symbol_status.status == BrokerOrderStatus.UNKNOWN,
)


# ══════════════════════════════════════════════════════════════
# 8. MockBroker — get_open_orders()는 항상 빈 리스트(즉시 전량체결 특성상)
# ══════════════════════════════════════════════════════════════

mock4 = MockBroker()
mock4.place_order(OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=2))
mock4.place_order(OrderRequest(symbol="000660", side=OrderSide.BUY, quantity=1))
check("8-1) 주문을 여러 건 넣어도 get_open_orders()는 빈 리스트", mock4.get_open_orders(symbol="005930") == [])


# ══════════════════════════════════════════════════════════════
# 9. MockBroker — 거절된 주문(insufficient cash)은 기록되지 않음
# ══════════════════════════════════════════════════════════════

mock5 = MockBroker()
huge_order = OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=1_000_000)
rejected_result = mock5.place_order(huge_order)
check("9-1) 현금 부족으로 거절됨", rejected_result.accepted is False)
status_after_reject = mock5.get_order_status(order_id=rejected_result.order_id, symbol="005930")
check(
    "9-2) 거절된 주문의 order_id로 조회하면 UNKNOWN(실제로 체결된 적 없음)",
    status_after_reject.status == BrokerOrderStatus.UNKNOWN,
)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")

import sys

sys.exit(1 if failed else 0)
