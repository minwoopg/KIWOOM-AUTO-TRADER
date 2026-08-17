from __future__ import annotations

"""1P0.8-P0.1: place_order()의 429/timeout 실패 분류 테스트.

2026-08-14, 319400 실측 P0 사고(SELL 주문이 HTTP 429로 실패했는데
place_order()가 예외를 던져 PSM이 109분간 phantom pending으로
방치된 사고) 대응. 이 파일은 infra/broker/kiwoom_broker.py 레벨의
분류 로직(KiwoomHttpError/KiwoomTransportError, place_order()가
절대 예외를 던지지 않고 OrderResult로 분류해 반환하는지)만
검증합니다. PSM/TradingService 통합 시나리오(BUY/SELL x
definite-reject/ambiguous 4개 축)는 test_partial_fill_lifecycle.py에
있습니다.
"""

import sys

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


import pathlib

import requests

from config.settings import BrokerConfig
from domain.models import OrderRequest, OrderSide
from infra.broker.kiwoom_broker import (
    KiwoomBroker,
    KiwoomHttpError,
    KiwoomTransportError,
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
    # 2026-08-14 (1P0.8-P0.3): place_order()의 429/1700 bounded retry가
    # 테스트에서 실제로 몇 초씩 기다리지 않도록 sleep을 no-op으로
    # 바꿔치기합니다. 재시도 "횟수"/"조건"은 검증하되 실제 대기 시간은
    # 검증 대상이 아닙니다.
    broker._retry_sleep = lambda seconds: None
    return broker


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class _Fake429Session:
    """항상 HTTP 429(rate limit)를 반환 — 319400 실측(kt10001) 재현."""

    def post(self, url, headers=None, json=None, timeout=None):
        return _FakeResponse(
            429,
            {
                "return_code": 5,
                "return_msg": (
                    "허용된 요청 개수를 초과하였습니다"
                    "[1700:허용된 API 요청 개수를 초과하였습니다. 유량=1, API ID=kt10001]"
                ),
            },
        )


class _FakeTimeoutSession:
    """session.post() 자체가 타임아웃 — 응답을 아예 못 받는 경우."""

    def post(self, url, headers=None, json=None, timeout=None):
        raise requests.exceptions.Timeout("connection timed out")


class _FakeConnectionErrorSession:
    def post(self, url, headers=None, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection reset by peer")


class _FakeOKSession:
    """정상 200 + return_code=0 — 회귀 방지용(기존 accepted 경로 안 깨짐)."""

    def post(self, url, headers=None, json=None, timeout=None):
        return _FakeResponse(
            200, {"return_code": 0, "return_msg": " 주문 완료", "ord_no": "0099999"}
        )


# ── 1. place_order() + HTTP 429 (definite reject) ──────────────────
broker1 = _build_broker()
broker1.session = _Fake429Session()
order1 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result1 = broker1.place_order(order1)

check("1-1) place_order()는 429에도 예외를 던지지 않고 반환함", result1 is not None)
check("1-2) 429는 accepted=False", result1.accepted is False)
check(
    "1-3) 429는 is_ambiguous=False (definite reject — 서버 응답을 받았으므로)",
    result1.is_ambiguous is False,
)
check("1-4) message에 HTTP 429가 포함됨", "429" in result1.message)
check("1-5) order_id는 빈 문자열(주문이 실제로 생성되지 않음)", result1.order_id == "")

# ── 2. place_order() + timeout (ambiguous) ──────────────────────────
broker2 = _build_broker()
broker2.session = _FakeTimeoutSession()
order2 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result2 = broker2.place_order(order2)

check("2-1) place_order()는 timeout에도 예외를 던지지 않고 반환함", result2 is not None)
check("2-2) timeout은 accepted=False", result2.accepted is False)
check(
    "2-3) timeout은 is_ambiguous=True (응답 자체를 못 받음 — 접수 여부 불명)",
    result2.is_ambiguous is True,
)

# ── 3. place_order() + connection error (ambiguous) ─────────────────
broker3 = _build_broker()
broker3.session = _FakeConnectionErrorSession()
order3 = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
result3 = broker3.place_order(order3)

check("3-1) connection error도 예외를 던지지 않고 반환함", result3 is not None)
check("3-2) connection error는 accepted=False", result3.accepted is False)
check("3-3) connection error는 is_ambiguous=True", result3.is_ambiguous is True)

# ── 4. place_order() + 정상 200 (회귀 방지) ──────────────────────────
broker4 = _build_broker()
broker4.session = _FakeOKSession()
order4 = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
result4 = broker4.place_order(order4)

check("4-1) 정상 응답은 accepted=True", result4.accepted is True)
check("4-2) 정상 응답은 is_ambiguous=False", result4.is_ambiguous is False)
check("4-3) 정상 응답은 order_id가 채워짐", result4.order_id == "0099999")

# ── 5. _post() 레벨 예외 타입 — 다른 호출부(일봉 조회 등) 회귀 방지 ──
# KiwoomHttpError/KiwoomTransportError는 둘 다 RuntimeError를 상속하므로
# 기존에 RuntimeError/Exception으로 넓게 잡던 다른 모든 _post() 호출부
# (get_daily_prices, get_market_price 등)의 동작은 이번 변경으로
# 바뀌지 않습니다 — 아래에서 상속 관계만 확정적으로 검증합니다.
broker5 = _build_broker()
broker5.session = _Fake429Session()
try:
    broker5._post(endpoint="/api/dostk/stkinfo", api_id="ka10081", payload={})
    check("5-1) 429는 _post() 레벨에서 예외를 던짐", False)
except KiwoomHttpError as exc:
    check("5-1) 429는 _post() 레벨에서 KiwoomHttpError를 던짐", True)
    check(
        "5-2) KiwoomHttpError는 RuntimeError의 서브클래스(기존 호출부 호환)",
        isinstance(exc, RuntimeError),
    )
    check("5-3) status_code가 429로 정확히 기록됨", exc.status_code == 429)
except Exception:
    check("5-1) 429는 _post() 레벨에서 KiwoomHttpError를 던짐", False)

broker6 = _build_broker()
broker6.session = _FakeTimeoutSession()
try:
    broker6._post(endpoint="/api/dostk/stkinfo", api_id="ka10081", payload={})
    check("5-4) timeout은 _post() 레벨에서 예외를 던짐", False)
except KiwoomTransportError as exc:
    check("5-4) timeout은 _post() 레벨에서 KiwoomTransportError를 던짐", True)
    check(
        "5-5) KiwoomTransportError는 RuntimeError의 서브클래스(기존 호출부 호환)",
        isinstance(exc, RuntimeError),
    )
except Exception:
    check("5-4) timeout은 _post() 레벨에서 KiwoomTransportError를 던짐", False)

# ── 6. 소스 검증 ──────────────────────────────────────────────────
_kb_src = pathlib.Path("infra/broker/kiwoom_broker.py").read_text(encoding="utf-8")
check("6-1) 소스에 KiwoomHttpError 정의 존재", "class KiwoomHttpError(RuntimeError)" in _kb_src)
check(
    "6-2) 소스에 KiwoomTransportError 정의 존재",
    "class KiwoomTransportError(RuntimeError)" in _kb_src,
)
check(
    "6-3) place_order()가 KiwoomHttpError를 처리함",
    "except KiwoomHttpError as exc:" in _kb_src,
)
check(
    "6-4) place_order()가 KiwoomTransportError를 처리함",
    "except KiwoomTransportError as exc:" in _kb_src,
)
check(
    "6-5) OrderResult에 is_ambiguous 필드 존재(domain/models.py)",
    "is_ambiguous: bool = False" in pathlib.Path("domain/models.py").read_text(encoding="utf-8"),
)
check(
    "6-6) 소스에 _is_confirmed_rate_limit_reject whitelist 함수 존재",
    "def _is_confirmed_rate_limit_reject(" in _kb_src,
)

# ── 7. HTTP 실패 화이트리스트 좁히기 회귀 테스트 (2026-08-14 P0 재검토) ──
# GPT 코드리뷰 지적: "HTTP 응답을 받았다"는 사실만으로 definitive reject를
# 보장할 수 없습니다. 8/14 실측(319400)으로 안전성이 확인된 단 하나의
# 케이스 — HTTP 429 + return_code=5 + return_msg에 "1700" 포함 — 만
# definitive reject로 whitelist하고, 408/5xx 및 형태가 다른 429는
# is_ambiguous=True로 fail-close해야 합니다. 아래는 그 whitelist가
# 실제로 좁게 동작하는지, status_code만으로 판정하지 않는지를
# BUY/SELL 양쪽에서 검증합니다.


class _FakeStatusSession:
    """임의의 (status_code, body)를 반환하는 범용 fake session."""

    def __init__(self, status_code: int, body: dict):
        self._status_code = status_code
        self._body = body

    def post(self, url, headers=None, json=None, timeout=None):
        return _FakeResponse(self._status_code, self._body)


def _check_ambiguous_for_both_sides(label_prefix: str, session_factory) -> None:
    """주어진 fake session으로 BUY/SELL 양쪽 모두
    accepted=False, is_ambiguous=True 인지 검증합니다."""
    broker_buy = _build_broker()
    broker_buy.session = session_factory()
    order_buy = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
    result_buy = broker_buy.place_order(order_buy)
    check(f"{label_prefix}-BUY) accepted=False", result_buy.accepted is False)
    check(
        f"{label_prefix}-BUY) is_ambiguous=True (whitelist 미해당 — fail-close)",
        result_buy.is_ambiguous is True,
    )

    broker_sell = _build_broker()
    broker_sell.session = session_factory()
    order_sell = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
    result_sell = broker_sell.place_order(order_sell)
    check(f"{label_prefix}-SELL) accepted=False", result_sell.accepted is False)
    check(
        f"{label_prefix}-SELL) is_ambiguous=True (whitelist 미해당 — fail-close)",
        result_sell.is_ambiguous is True,
    )


# 7-408: 요청 형식 오류 등 — 접수 여부 불명, ambiguous
_check_ambiguous_for_both_sides(
    "7-408",
    lambda: _FakeStatusSession(408, {"return_code": 5, "return_msg": "Request Timeout"}),
)

# 7-500: 서버 내부 오류 — 응답 단계에서 실패했을 수 있어 접수 여부 불명
_check_ambiguous_for_both_sides(
    "7-500",
    lambda: _FakeStatusSession(500, {"return_code": -1, "return_msg": "Internal Server Error"}),
)

# 7-502: 게이트웨이 오류
_check_ambiguous_for_both_sides(
    "7-502",
    lambda: _FakeStatusSession(502, {"return_code": -1, "return_msg": "Bad Gateway"}),
)

# 7-503: 서비스 불가
_check_ambiguous_for_both_sides(
    "7-503",
    lambda: _FakeStatusSession(503, {"return_code": -1, "return_msg": "Service Unavailable"}),
)

# 7-504: 게이트웨이 타임아웃 — 백엔드가 실제로 주문을 접수했을 가능성 배제 불가
_check_ambiguous_for_both_sides(
    "7-504",
    lambda: _FakeStatusSession(504, {"return_code": -1, "return_msg": "Gateway Timeout"}),
)

# 7-429-wrong-code: HTTP 429이지만 return_code가 5가 아님 —
# status_code만으로 판정하지 않음을 증명
_check_ambiguous_for_both_sides(
    "7-429wrongcode",
    lambda: _FakeStatusSession(
        429, {"return_code": 3, "return_msg": "허용된 API 요청 개수를 초과하였습니다[1700:...]"}
    ),
)

# 7-429-no-1700: HTTP 429 + return_code=5이지만 return_msg에 "1700"이 없음 —
# 다른 종류의 429(예: 계정 단위 제한 등)까지 뭉뚱그려 definitive로
# 처리하지 않음을 증명
_check_ambiguous_for_both_sides(
    "7-429no1700",
    lambda: _FakeStatusSession(
        429, {"return_code": 5, "return_msg": "허용된 요청 개수를 초과하였습니다[9999:다른 사유]"}
    ),
)

# 7-확인: 원래의 429+1700 whitelist 케이스는 여전히 definitive reject
# (is_ambiguous=False) — 좁히기 작업이 기존 안전 경로를 깨지 않았는지 재확인
broker7ok = _build_broker()
broker7ok.session = _Fake429Session()
order7ok = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result7ok = broker7ok.place_order(order7ok)
check(
    "7-whitelist-still-works) 429+return_code=5+1700은 여전히 is_ambiguous=False",
    result7ok.is_ambiguous is False,
)
check(
    "7-whitelist-still-works) 429+return_code=5+1700은 여전히 accepted=False",
    result7ok.accepted is False,
)

# ── 8. 1P0.8-P0.3: 429/1700 whitelist 전용 bounded retry ────────────
# 민우님 확정 범위: kt10000/kt10001에서 정확히 429+return_code=5+"1700"
# 일 때만 짧은 backoff 후 최대 2회 재시도. timeout/connection reset/
# 408/5xx/형태 불명 429에는 재시도를 절대 적용하지 않는다.


class _CountingSession:
    """post() 호출마다 미리 정해둔 응답을 순서대로 반환하고,
    호출 횟수를 기록하는 fake session. 시퀀스가 소진되면 마지막
    응답을 계속 반환합니다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.call_count += 1
        idx = min(self.call_count - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        status_code, body = item
        return _FakeResponse(status_code, body)


_RATE_LIMIT_BODY = {
    "return_code": 5,
    "return_msg": "허용된 요청 개수를 초과하였습니다[1700:허용된 API 요청 개수를 초과하였습니다. 유량=1, API ID=kt10001]",
}
_OK_BODY = {"return_code": 0, "return_msg": " 주문 완료", "ord_no": "0088888"}


# 8-1: 429/1700이 1번만 나고 그다음 재시도에서 성공 → 최종 accepted=True,
# is_ambiguous=False, session.post()가 정확히 2번 호출됨(최초 1 + 재시도 1)
broker8_1 = _build_broker()
session8_1 = _CountingSession([(429, _RATE_LIMIT_BODY), (200, _OK_BODY)])
broker8_1.session = session8_1
order8_1 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result8_1 = broker8_1.place_order(order8_1)
check("8-1) 1회 429/1700 후 재시도 성공 시 accepted=True", result8_1.accepted is True)
check("8-1) 재시도 성공 시 is_ambiguous=False", result8_1.is_ambiguous is False)
check("8-1) order_id가 재시도 성공 응답값으로 채워짐", result8_1.order_id == "0088888")
check("8-1) session.post()가 정확히 2번 호출됨(최초 + 재시도 1회)", session8_1.call_count == 2)

# 8-2: 429/1700이 max retries(2회)만큼 계속 나면 결국 definitive reject로
# 확정 — session.post()가 정확히 1(최초) + 2(재시도) = 3번 호출됨
broker8_2 = _build_broker()
session8_2 = _CountingSession(
    [(429, _RATE_LIMIT_BODY), (429, _RATE_LIMIT_BODY), (429, _RATE_LIMIT_BODY)]
)
broker8_2.session = session8_2
order8_2 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result8_2 = broker8_2.place_order(order8_2)
check("8-2) 재시도를 모두 소진하면 accepted=False", result8_2.accepted is False)
check(
    "8-2) 재시도를 모두 소진해도 여전히 definitive reject(is_ambiguous=False)",
    result8_2.is_ambiguous is False,
)
check(
    "8-2) session.post()가 정확히 3번 호출됨(최초 1 + 재시도 2, 무한재시도 아님)",
    session8_2.call_count == 3,
)

# 8-3: 첫 시도는 429/1700인데 재시도 중 다른 형태(500)를 만나면 —
# 더 이상 재시도하지 않고 즉시 is_ambiguous=True로 fail-close.
# session.post()는 정확히 2번만 호출됨(3번째 재시도로 넘어가지 않음)
broker8_3 = _build_broker()
session8_3 = _CountingSession(
    [(429, _RATE_LIMIT_BODY), (500, {"return_code": -1, "return_msg": "Internal Server Error"})]
)
broker8_3.session = session8_3
order8_3 = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
result8_3 = broker8_3.place_order(order8_3)
check("8-3) 재시도 도중 다른 형태(500) 발생 시 accepted=False", result8_3.accepted is False)
check(
    "8-3) 재시도 도중 다른 형태(500) 발생 시 is_ambiguous=True(재시도 계속 안 함)",
    result8_3.is_ambiguous is True,
)
check(
    "8-3) 다른 형태를 만나면 즉시 중단 — session.post() 정확히 2번만 호출",
    session8_3.call_count == 2,
)

# 8-4: 첫 시도는 429/1700인데 재시도 중 timeout(응답 자체 없음)을 만나면 —
# 마찬가지로 즉시 is_ambiguous=True, 더 이상 재시도하지 않음


class _FirstRateLimitThenTimeoutSession:
    def __init__(self):
        self.call_count = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.call_count += 1
        if self.call_count == 1:
            return _FakeResponse(429, _RATE_LIMIT_BODY)
        raise requests.exceptions.Timeout("connection timed out")


broker8_4 = _build_broker()
session8_4 = _FirstRateLimitThenTimeoutSession()
broker8_4.session = session8_4
order8_4 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result8_4 = broker8_4.place_order(order8_4)
check("8-4) 재시도 도중 timeout 발생 시 accepted=False", result8_4.accepted is False)
check("8-4) 재시도 도중 timeout 발생 시 is_ambiguous=True(재시도 계속 안 함)", result8_4.is_ambiguous is True)
check("8-4) timeout을 만나면 즉시 중단 — session.post() 정확히 2번만 호출", session8_4.call_count == 2)

# 8-5/8-6: whitelist에 해당하지 않는 케이스(408/5xx/형태 불명 429/timeout)는
# 첫 시도부터 재시도를 아예 시도하지 않아야 함 — session.post() 호출이
# 정확히 1번(재시도 없음)인지로 증명. BUY/SELL 둘 다 확인.


def _check_no_retry_attempted(label_prefix: str, session_factory) -> None:
    broker_buy = _build_broker()
    session_buy = session_factory()
    broker_buy.session = session_buy
    order_buy = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
    broker_buy.place_order(order_buy)
    check(
        f"{label_prefix}-BUY) whitelist 미해당 — 재시도 없이 1번만 호출됨",
        getattr(session_buy, "call_count", 1) == 1,
    )

    broker_sell = _build_broker()
    session_sell = session_factory()
    broker_sell.session = session_sell
    order_sell = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
    broker_sell.place_order(order_sell)
    check(
        f"{label_prefix}-SELL) whitelist 미해당 — 재시도 없이 1번만 호출됨",
        getattr(session_sell, "call_count", 1) == 1,
    )


_check_no_retry_attempted(
    "8-5-408", lambda: _CountingSession([(408, {"return_code": 5, "return_msg": "Request Timeout"})])
)
_check_no_retry_attempted(
    "8-5-500", lambda: _CountingSession([(500, {"return_code": -1, "return_msg": "Internal Server Error"})])
)
_check_no_retry_attempted(
    "8-5-429wrongcode",
    lambda: _CountingSession(
        [(429, {"return_code": 3, "return_msg": "허용된 API 요청 개수를 초과하였습니다[1700:...]"})]
    ),
)
_check_no_retry_attempted("8-6-timeout", _FakeTimeoutSession)
_check_no_retry_attempted("8-6-connection", _FakeConnectionErrorSession)

# 8-7: 정상 200(재시도 불필요)에서는 애초에 루프가 한 번만 돔 —
# 회귀 방지(1번만 호출)
broker8_7 = _build_broker()
session8_7 = _CountingSession([(200, _OK_BODY)])
broker8_7.session = session8_7
order8_7 = OrderRequest(symbol="319400", side=OrderSide.BUY, quantity=10)
result8_7 = broker8_7.place_order(order8_7)
check("8-7) 정상 200은 재시도 없이 1번만 호출됨", session8_7.call_count == 1)
check("8-7) 정상 200은 accepted=True", result8_7.accepted is True)

# 8-8: retry 사이에 broker._retry_sleep이 실제로 호출되는지(대기 로직이
# 존재하는지) 확인 — 호출 횟수가 재시도 횟수와 정확히 일치해야 함
_sleep_calls = []
broker8_8 = _build_broker()
broker8_8._retry_sleep = lambda seconds: _sleep_calls.append(seconds)
session8_8 = _CountingSession([(429, _RATE_LIMIT_BODY), (429, _RATE_LIMIT_BODY), (200, _OK_BODY)])
broker8_8.session = session8_8
order8_8 = OrderRequest(symbol="319400", side=OrderSide.SELL, quantity=202)
result8_8 = broker8_8.place_order(order8_8)
check("8-8) 429/1700 재시도 2회 후 성공 시 accepted=True", result8_8.accepted is True)
check("8-8) _retry_sleep이 재시도 횟수(2번)만큼 호출됨", len(_sleep_calls) == 2)
check(
    "8-8) _retry_sleep에 넘긴 대기시간이 PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS와 일치",
    all(s == 1.5 for s in _sleep_calls),
)

# ── 8-소스 검증 ──────────────────────────────────────────────────
_kb_src_p03 = pathlib.Path("infra/broker/kiwoom_broker.py").read_text(encoding="utf-8")
check(
    "8-9) 소스에 PLACE_ORDER_RATE_LIMIT_MAX_RETRIES == 2 상수 존재",
    "PLACE_ORDER_RATE_LIMIT_MAX_RETRIES = 2" in _kb_src_p03,
)
check(
    "8-10) 소스에 PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS 상수 존재",
    "PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS = 1.5" in _kb_src_p03,
)
check(
    "8-11) place_order()가 _retry_sleep을 통해 대기함(실제 time.sleep 직접 호출 아님 — 테스트 가능성 확보)",
    "self._retry_sleep(PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS)" in _kb_src_p03,
)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
