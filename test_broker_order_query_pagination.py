from __future__ import annotations

"""1P0.8-C.1: ka10075/ka10076 read-only 주문조회 페이지네이션 테스트.

배경(GPT 리뷰, 2026-08-18): 1P0.8-C 최초 구현은 ka10075/ka10076을
단일 페이지만 조회했습니다. B.1에서 이미 "cont-yn=Y/next-key
연속조회를 반드시 따라가야 한다"고 검증해 둔 원칙을, 실제 Broker
조회 경로(KiwoomBroker._fetch_open_orders_raw/_fetch_fill_history_raw)
에는 반영하지 않은 채로 두면 "대상 order_id가 2페이지에 있는데
1페이지만 보고 UNKNOWN"이라는 위험한 시나리오가 가능했습니다. 이
파일은 그 gap이 실제로 닫혔는지 검증합니다.

범위: 여전히 read-only wiring입니다. TradingService/PSM/orphan
자동 해제/ERROR 자동 복구/cancel/BUY-SELL 판정/restart
reconciliation은 이 라운드에서도 건드리지 않습니다.
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
from domain.models import BrokerOrderStatus
from infra.broker.kiwoom_broker import (
    KiwoomBroker,
    KiwoomPaginationIncompleteError,
    ORDER_QUERY_MAX_CONTINUATION_PAGES,
)


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
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class _PaginatedSession:
    """api-id별로 미리 정해둔 페이지 시퀀스를 순서대로 돌려주는 fake session.

    각 페이지는 (response_body, cont_yn, next_key) 튜플입니다. 요청받은
    cont-yn/next-key가 이전 응답이 알려준 값과 일치하는지도 기록해
    "제대로 이어서 요청했는지"를 검증할 수 있게 합니다.
    """

    def __init__(self, pages_by_api_id: dict[str, list[tuple[dict, str, str]]]):
        self._pages_by_api_id = pages_by_api_id
        self._cursor: dict[str, int] = {api_id: 0 for api_id in pages_by_api_id}
        self.calls: list[tuple[str, str, str]] = []  # (api_id, request_cont_yn, request_next_key)

    def post(self, url, headers=None, json=None, timeout=None):
        api_id = (headers or {}).get("api-id", "")
        request_cont_yn = (headers or {}).get("cont-yn", "")
        request_next_key = (headers or {}).get("next-key", "")
        self.calls.append((api_id, request_cont_yn, request_next_key))

        pages = self._pages_by_api_id.get(api_id, [])
        idx = self._cursor.get(api_id, 0)
        if idx >= len(pages):
            return _FakeResponse(200, {"return_code": 0, "return_msg": "no more pages"})

        body, resp_cont_yn, resp_next_key = pages[idx]
        self._cursor[api_id] = idx + 1
        return _FakeResponse(
            200,
            body,
            headers={"cont-yn": resp_cont_yn, "next-key": resp_next_key, "api-id": api_id},
        )


def _oso_entry(ord_no: str, ord_qty: str, oso_qty: str, cntr_qty: str = "0", ord_stt: str = "접수") -> dict:
    return {
        "ord_no": ord_no,
        "io_tp_nm": "+매수",
        "ord_qty": ord_qty,
        "cntr_qty": cntr_qty,
        "oso_qty": oso_qty,
        "ord_stt": ord_stt,
        "trde_tp": "보통",
    }


def _cntr_entry(ord_no: str, ord_qty: str, cntr_qty: str, oso_qty: str = "0", ord_stt: str = "체결", cntr_pric: str = "10000") -> dict:
    return {
        "ord_no": ord_no,
        "io_tp_nm": "+매수",
        "ord_qty": ord_qty,
        "cntr_qty": cntr_qty,
        "oso_qty": oso_qty,
        "ord_stt": ord_stt,
        "trde_tp": "시장가",
        "cntr_pric": cntr_pric,
    }


# ══════════════════════════════════════════════════════════════
# 1. ka10075(oso) 2페이지 연속조회 — 양쪽 페이지 항목이 합쳐져야 함
# ══════════════════════════════════════════════════════════════

oso_page1 = {"oso": [_oso_entry("0000001", "1", "1")], "return_code": 0, "return_msg": ""}
oso_page2 = {"oso": [_oso_entry("0000002", "1", "1")], "return_code": 0, "return_msg": ""}
cntr_single_empty = {"cntr": [], "return_code": 0, "return_msg": ""}

broker1 = _build_broker()
session1 = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [
            (oso_page1, "Y", "NEXT-A"),
            (oso_page2, "N", ""),
        ],
        "ka10076": [
            (cntr_single_empty, "N", ""),
        ],
    }
)
broker1.session = session1
result1 = broker1.get_open_orders(symbol="005930")

check("1-1) 2페이지에 걸친 oso 항목이 모두 결과에 반영됨(2건)", len(result1) == 2)
order_ids_1 = {o.order_id.lstrip("0") for o in result1}
check("1-2) 1페이지 주문(1)과 2페이지 주문(2) 둘 다 포함됨", order_ids_1 == {"1", "2"})
ka10075_calls_1 = [c for c in session1.calls if c[0] == "ka10075"]
check("1-3) ka10075는 정확히 2번 호출됨(페이지당 1번)", len(ka10075_calls_1) == 2)
check("1-4) 첫 요청은 cont-yn=N/next-key=''", ka10075_calls_1[0][1:] == ("N", ""))
check(
    "1-5) 두 번째 요청은 1페이지 응답이 알려준 cont-yn=Y/next-key=NEXT-A를 그대로 사용",
    ka10075_calls_1[1][1:] == ("Y", "NEXT-A"),
)


# ══════════════════════════════════════════════════════════════
# 2. ka10076(cntr) 페이지네이션 — 대상 주문이 2페이지에만 있어도 찾아야 함
#    (이게 바로 GPT 리뷰가 지적한 원래 버그 시나리오)
# ══════════════════════════════════════════════════════════════

cntr_page1_no_match = {"cntr": [_cntr_entry("0009999", "3", "3")], "return_code": 0, "return_msg": ""}
cntr_page2_match = {
    "cntr": [_cntr_entry("0012345", "7", "7", cntr_pric="55000")],
    "return_code": 0,
    "return_msg": "",
}
oso_single_empty = {"oso": [], "return_code": 0, "return_msg": ""}

broker2 = _build_broker()
session2 = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [
            (oso_single_empty, "N", ""),
        ],
        "ka10076": [
            (cntr_page1_no_match, "Y", "NEXT-B"),
            (cntr_page2_match, "N", ""),
        ],
    }
)
broker2.session = session2
result2 = broker2.get_order_status(order_id="12345", symbol="005930")

check(
    "2-1) 대상 주문이 cntr 2페이지에만 있어도 FILLED로 정확히 판정됨"
    "(1페이지만 봤다면 UNKNOWN이었을 시나리오)",
    result2.status == BrokerOrderStatus.FILLED,
)
check("2-2) 체결가도 2페이지 항목에서 정확히 가져옴", result2.filled_price == 55000)
ka10076_calls_2 = [c for c in session2.calls if c[0] == "ka10076"]
check("2-3) ka10076은 정확히 2번 호출됨(페이지당 1번)", len(ka10076_calls_2) == 2)


# ══════════════════════════════════════════════════════════════
# 3. page cap 도달 — 부분 결과를 정상 결과처럼 반환하지 않고 예외로 fail-close
# ══════════════════════════════════════════════════════════════

# cont-yn=Y가 cap을 넘어서도 끝없이 이어지는 상황을 흉내냄
never_ending_page = {"cntr": [_cntr_entry("0000001", "1", "1")], "return_code": 0, "return_msg": ""}


class _InfinitePaginationSession:
    """cont-yn=Y를 영원히 반환 — page cap 초과 상황 재현용."""

    def __init__(self):
        self.call_count = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.call_count += 1
        return _FakeResponse(
            200,
            never_ending_page,
            headers={"cont-yn": "Y", "next-key": f"NEXT-{self.call_count}", "api-id": (headers or {}).get("api-id", "")},
        )


broker3 = _build_broker()
session3 = _InfinitePaginationSession()
broker3.session = session3

try:
    broker3._fetch_fill_history_raw(symbol="005930")
    check("3-1) page cap 초과 시 KiwoomPaginationIncompleteError가 발생함", False)
except KiwoomPaginationIncompleteError:
    check("3-1) page cap 초과 시 KiwoomPaginationIncompleteError가 발생함", True)

check(
    "3-2) cap을 넘겨서까지 계속 호출하지 않고 정확히 cap만큼만 호출함"
    f"(ORDER_QUERY_MAX_CONTINUATION_PAGES={ORDER_QUERY_MAX_CONTINUATION_PAGES})",
    session3.call_count == ORDER_QUERY_MAX_CONTINUATION_PAGES,
)

# get_order_status()/get_open_orders() 상위 메서드도 이 예외를 삼키지 않고 그대로 전파해야 함
broker3b = _build_broker()
session3b = _InfinitePaginationSession()
broker3b.session = session3b
try:
    broker3b.get_order_status(order_id="1", symbol="005930")
    check("3-3) get_order_status()도 예외를 삼키지 않고 그대로 전파함", False)
except KiwoomPaginationIncompleteError:
    check("3-3) get_order_status()도 예외를 삼키지 않고 그대로 전파함", True)


# ══════════════════════════════════════════════════════════════
# 4. get_open_orders() — oso가 여러 빈 페이지를 거쳐도 결국 비어있으면 cntr 생략
# ══════════════════════════════════════════════════════════════

oso_empty_page1 = {"oso": [], "return_code": 0, "return_msg": ""}
oso_empty_page2 = {"oso": [], "return_code": 0, "return_msg": ""}

broker4 = _build_broker()
session4 = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [
            (oso_empty_page1, "Y", "NEXT-C"),
            (oso_empty_page2, "N", ""),
        ],
        "ka10076": [
            (cntr_single_empty, "N", ""),
        ],
    }
)
broker4.session = session4
result4 = broker4.get_open_orders(symbol="005930")

check("4-1) 여러 페이지를 거쳐도 결국 oso가 비어있으면 빈 리스트", result4 == [])
ka10075_calls_4 = [c for c in session4.calls if c[0] == "ka10075"]
ka10076_calls_4 = [c for c in session4.calls if c[0] == "ka10076"]
check("4-2) ka10075는 두 페이지 모두 조회함(끝까지 확인 후 판단)", len(ka10075_calls_4) == 2)
check(
    "4-3) 전체 페이지를 다 봤는데도 oso가 비어있으면 ka10076은 호출하지 않음",
    len(ka10076_calls_4) == 0,
)


# ══════════════════════════════════════════════════════════════
# 5. get_order_status()는 oso가 비어있어도 cntr 생략 최적화를 적용하지 않음
#    (이미 FILLED된 주문은 oso=[]가 정상이므로 cntr을 반드시 봐야 함)
# ══════════════════════════════════════════════════════════════

cntr_with_match = {"cntr": [_cntr_entry("0000042", "2", "2", cntr_pric="99000")], "return_code": 0, "return_msg": ""}

broker5 = _build_broker()
session5 = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [
            (oso_single_empty, "N", ""),
        ],
        "ka10076": [
            (cntr_with_match, "N", ""),
        ],
    }
)
broker5.session = session5
result5 = broker5.get_order_status(order_id="42", symbol="005930")

check(
    "5-1) get_order_status()는 oso가 비어있어도 ka10076을 호출해 FILLED를 정확히 찾음",
    result5.status == BrokerOrderStatus.FILLED,
)
ka10076_calls_5 = [c for c in session5.calls if c[0] == "ka10076"]
check("5-2) ka10076이 실제로 호출됨(생략되지 않음)", len(ka10076_calls_5) == 1)


# ══════════════════════════════════════════════════════════════
# 6. 1P0.8-C.1 closure: cont-yn 프로토콜 fail-close 보강
#    (GPT 리뷰 2차 — "N"일 때만 정상 종료, 그 외 전부 fail-close)
# ══════════════════════════════════════════════════════════════

oso_first_page = {"oso": [_oso_entry("0000001", "1", "1")], "return_code": 0, "return_msg": ""}


def _run_single_page_and_expect_pagination_error(oso_pages_after_first):
    """1페이지는 정상, 그 다음 응답 (body, cont_yn, next_key)이 프로토콜
    위반을 담고 있을 때 KiwoomPaginationIncompleteError가 나는지 확인."""
    broker = _build_broker()
    session = _PaginatedSession(
        pages_by_api_id={
            "ka10075": [oso_pages_after_first],
            "ka10076": [(cntr_single_empty, "N", "")],
        }
    )
    broker.session = session
    broker._fetch_open_orders_raw(symbol="005930")


# 6-1) cont-yn=Y인데 next-key가 비어있음 — "더 볼 페이지가 있다"는데 갈 방법이 없는 모순
try:
    _run_single_page_and_expect_pagination_error((oso_first_page, "Y", ""))
    check("6-1) cont-yn=Y + next-key='' → KiwoomPaginationIncompleteError", False)
except KiwoomPaginationIncompleteError:
    check("6-1) cont-yn=Y + next-key='' → KiwoomPaginationIncompleteError", True)

# 6-2) cont-yn이 빈 문자열 — 완결 여부를 서버가 알려주지 않은 상태
try:
    _run_single_page_and_expect_pagination_error((oso_first_page, "", ""))
    check("6-2) cont-yn='' → KiwoomPaginationIncompleteError", False)
except KiwoomPaginationIncompleteError:
    check("6-2) cont-yn='' → KiwoomPaginationIncompleteError", True)

# 6-3) cont-yn이 "N"/"Y" 어느 쪽도 아닌 값
try:
    _run_single_page_and_expect_pagination_error((oso_first_page, "X", ""))
    check("6-3) cont-yn='X'(알 수 없는 값) → KiwoomPaginationIncompleteError", False)
except KiwoomPaginationIncompleteError:
    check("6-3) cont-yn='X'(알 수 없는 값) → KiwoomPaginationIncompleteError", True)

# 6-4) 응답 배열이 list가 아님(dict) — 응답 구조 자체를 신뢰할 수 없음
broker6b = _build_broker()
session6b = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [({"oso": {}, "return_code": 0, "return_msg": ""}, "N", "")],
        "ka10076": [(cntr_single_empty, "N", "")],
    }
)
broker6b.session = session6b
try:
    broker6b._fetch_open_orders_raw(symbol="005930")
    check("6-4) oso={}(list 아님) → KiwoomPaginationIncompleteError", False)
except KiwoomPaginationIncompleteError:
    check("6-4) oso={}(list 아님) → KiwoomPaginationIncompleteError", True)

# 6-5) 응답 배열 내부에 dict가 아닌 원소가 섞여 있음
broker6c = _build_broker()
session6c = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [(oso_single_empty, "N", "")],  # 이 테스트는 ka10076만 호출하므로 참고용
        "ka10076": [
            ({"cntr": [_cntr_entry("0000001", "1", "1"), "broken"], "return_code": 0, "return_msg": ""}, "N", ""),
        ],
    }
)
broker6c.session = session6c
try:
    broker6c._fetch_fill_history_raw(symbol="005930")
    check("6-5) cntr 내부에 dict 아닌 원소 섞임 → KiwoomPaginationIncompleteError", False)
except KiwoomPaginationIncompleteError:
    check("6-5) cntr 내부에 dict 아닌 원소 섞임 → KiwoomPaginationIncompleteError", True)

# 6-6) 회귀 확인 — 정상 케이스(cont-yn=N, 정상 종료 / cont-yn=Y+next-key, 다음 페이지)는 여전히 그대로 동작
broker6d = _build_broker()
session6d = _PaginatedSession(
    pages_by_api_id={
        "ka10075": [
            (oso_first_page, "Y", "NEXT-Z"),
            ({"oso": [_oso_entry("0000002", "1", "1")], "return_code": 0, "return_msg": ""}, "N", ""),
        ],
        "ka10076": [(cntr_single_empty, "N", "")],
    }
)
broker6d.session = session6d
result6d = broker6d.get_open_orders(symbol="005930")
check(
    "6-6) 정상 프로토콜(N 종료 / Y+next-key 계속)은 회귀 없이 그대로 동작(2건 모두 반영)",
    len(result6d) == 2,
)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")

import sys

sys.exit(1 if failed else 0)
