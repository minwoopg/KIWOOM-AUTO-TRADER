# -*- coding: utf-8 -*-
"""tools/order_reconciliation_probe.py 검증 (2026-08-14, 1P0.8-B.1)

이 프로브는 실제 키움 계좌를 조회하는 read-only 진단 스크립트라
네트워크/설정 파일 없이도 안전장치가 실제로 동작하는지 확인하는 게
핵심입니다. 민우님이 확정한 설계 사양의 테스트 체크리스트 12개를
그대로 절 번호로 따라갑니다:

1. mock 도메인만 허용
2. ka10075/ka10076 이외 api-id 거부
3. authorization이 JSONL에 절대 기록되지 않음
4. account/app secret 등 민감 key redaction
5. UNKNOWN_ORDER_ID 호출 거부
6. 공백 order_id 거부
7. raw body가 변형되지 않고 보존됨
8. cont-yn / next-key header 보존
9. API 오류 응답도 버리지 않고 기록
10. JSONL 한 호출 = 한 레코드 보장
11. run_id / elapsed_ms 유지
12. 조회 실패가 다음 캡처를 조용히 정상값으로 위장하지 않음

2026-08-14 GPT 재검토(2건) 반영 후 추가된 절(14~18):
- mock-domain fail-closed 강화(scheme=https + host + port까지 검증,
  `call_kiwoom_api()` 자체에서도 defense-in-depth 재검증)
- 연속조회(pagination) — cont-yn=Y/next-key를 따라가며 모든 페이지를
  조회, page cap 초과 시 침묵 없이 명시적 레코드로 중단

네트워크 호출은 전혀 하지 않습니다 — fake session/broker로 대체합니다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

import requests

from tools.order_reconciliation_probe import (
    ALLOWED_API_IDS,
    DisallowedApiId,
    NonMockDomainRejected,
    OrderIdRejected,
    ProbeConfigError,
    ProbeContext,
    assert_mock_domain,
    build_ka10075_payload,
    build_ka10076_payload,
    build_record,
    call_kiwoom_api,
    fetch_paginated,
    load_dotenv as probe_load_dotenv,
    main as probe_main,
    parse_intervals,
    redact_sensitive,
    validate_order_id,
)

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


ts_probe_src = open("tools/order_reconciliation_probe.py", encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════
# 1. mock 도메인만 허용
# ══════════════════════════════════════════════════════════════
try:
    assert_mock_domain("https://mockapi.kiwoom.com")
    check("1-1) 정확히 mockapi.kiwoom.com이면 통과", True)
except ProbeConfigError:
    check("1-1) 정확히 mockapi.kiwoom.com이면 통과", False)

for bad_url in (
    "https://api.kiwoom.com",                          # 실전 도메인
    "https://mockapi.kiwoom.com.evil.example",          # 우회 시도
    "https://evil.example/mockapi.kiwoom.com",          # 경로에만 포함
    "http://localhost:8080",                            # 무관한 값
):
    try:
        assert_mock_domain(bad_url)
        check(f"1-2) 실전/우회 도메인 거부: {bad_url}", False)
    except NonMockDomainRejected:
        check(f"1-2) 실전/우회 도메인 거부: {bad_url}", True)


# ══════════════════════════════════════════════════════════════
# 2. ka10075/ka10076 이외 api-id 거부
# ══════════════════════════════════════════════════════════════
check("2-1) 허용 목록이 정확히 ka10075/ka10076 두 개",
      ALLOWED_API_IDS == frozenset({"ka10075", "ka10076"}))


class _FakeResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeSession:
    """호출된 요청을 기록해두는 fake session — 네트워크 호출 없음."""

    def __init__(self, response_by_api_id=None, raise_exc=None):
        self.calls = []
        self._response_by_api_id = response_by_api_id or {}
        self._raise_exc = raise_exc

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        if self._raise_exc is not None:
            raise self._raise_exc
        api_id = (headers or {}).get("api-id")
        return self._response_by_api_id[api_id]


class _FakeSequenceSession:
    """api_id별로 미리 정해둔 응답을 순서대로 하나씩 돌려주는 fake session.

    pagination(cont-yn/next-key) 검증용 — 매 호출의 요청 헤더까지 기록해
    두 번째 페이지 요청이 첫 응답의 next-key를 실제로 그대로 실었는지
    확인할 수 있게 합니다.
    """

    def __init__(self, sequence_by_api_id):
        self.calls = []
        self._queues = {k: list(v) for k, v in sequence_by_api_id.items()}

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        api_id = (headers or {}).get("api-id")
        queue = self._queues.get(api_id, [])
        if not queue:
            raise AssertionError(f"테스트 설정보다 많은 호출이 발생함: api_id={api_id}")
        return queue.pop(0)


class _FakeInfiniteContinuationSession:
    """항상 cont-yn=Y를 돌려주는(끝나지 않는) fake session — page cap 검증용."""

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        api_id = (headers or {}).get("api-id")
        return _FakeResponse(
            200,
            {"api-id": api_id, "cont-yn": "Y", "next-key": f"KEY{len(self.calls)}"},
            {"oso": [], "return_code": 0, "return_msg": "완료"},
        )


_ok_body_ka10075 = {
    "oso": [
        {
            "acnt_no": "1234567890",
            "ord_no": "0000069",
            "stk_cd": "005930",
            "ord_stt": "접수",
            "ord_qty": "1",
            "cntr_qty": "0",
        }
    ],
    "return_code": 0,
    "return_msg": " 조회가 완료되었습니다.",
}

_session_ok = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(
            200,
            {"api-id": "ka10075", "cont-yn": "N", "next-key": "", "authorization": "Bearer SHOULD-NOT-LEAK"},
            _ok_body_ka10075,
        ),
    }
)

try:
    call_kiwoom_api(_session_ok, "https://mockapi.kiwoom.com", "FAKE-TOKEN", "kt10000", {})
    check("2-2) kt10000(주문 API)은 즉시 거부되고 네트워크 호출 자체를 안 함", False)
except DisallowedApiId:
    check("2-2) kt10000(주문 API)은 즉시 거부되고 네트워크 호출 자체를 안 함",
          len(_session_ok.calls) == 0)


# ══════════════════════════════════════════════════════════════
# 3. authorization이 JSONL에 절대 기록되지 않음
# ══════════════════════════════════════════════════════════════
_session_auth = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(
            200,
            {"api-id": "ka10075", "cont-yn": "N", "next-key": "",
             "authorization": "Bearer SHOULD-NOT-LEAK", "Set-Cookie": "irrelevant"},
            _ok_body_ka10075,
        ),
    }
)
_status3, _headers3, _body3 = call_kiwoom_api(
    _session_auth, "https://mockapi.kiwoom.com", "REAL-SECRET-TOKEN", "ka10075",
    build_ka10075_payload("005930"),
)
check("3-1) 응답 헤더에 allow-list(api-id/cont-yn/next-key) 이외 키가 없음",
      set(_headers3.keys()) == {"api-id", "cont-yn", "next-key"})
check("3-2) authorization 헤더 값이 결과 어디에도 없음",
      "authorization" not in _headers3
      and "SHOULD-NOT-LEAK" not in json.dumps(_headers3))
check("3-3) 요청에 사용한 실제 토큰 문자열이 반환값 어디에도 없음",
      "REAL-SECRET-TOKEN" not in json.dumps({"headers": _headers3, "body": _body3}))
check("3-4) 요청 헤더 자체를 반환값에 포함하지 않음(요청 헤더 전체 덤프 금지)",
      "call_kiwoom_api" in ts_probe_src
      and "request_headers" not in ts_probe_src)


# ══════════════════════════════════════════════════════════════
# 4. account/app secret 등 민감 key redaction
# ══════════════════════════════════════════════════════════════
_nested = {
    "oso": [
        {"acnt_no": "1234567890", "ord_no": "0000069", "stk_cd": "005930"},
        {"acnt_no": "9999999999", "ord_no": "0000070", "stk_cd": "005930"},
    ],
    "return_code": 0,
}
_redacted = redact_sensitive(_nested)
check("4-1) 리스트 안에 중첩된 acnt_no도 모두 redact됨",
      all(item["acnt_no"] == "[REDACTED]" for item in _redacted["oso"]))
check("4-2) acnt_no 이외 필드(ord_no, stk_cd, return_code)는 그대로 보존",
      _redacted["oso"][0]["ord_no"] == "0000069"
      and _redacted["oso"][0]["stk_cd"] == "005930"
      and _redacted["return_code"] == 0)
check("4-3) appkey/secretkey 등 다른 민감 키도 redact",
      redact_sensitive({"appkey": "AK123", "secretkey": "SK456", "safe": "ok"})
      == {"appkey": "[REDACTED]", "secretkey": "[REDACTED]", "safe": "ok"})
check("4-4) 원본 dict는 변형되지 않음(순수 함수)",
      _nested["oso"][0]["acnt_no"] == "1234567890")


# ══════════════════════════════════════════════════════════════
# 5. UNKNOWN_ORDER_ID 호출 거부
# ══════════════════════════════════════════════════════════════
for bad_id in (None, "UNKNOWN_ORDER_ID", "pending"):
    try:
        validate_order_id(bad_id)
        check(f"5) order_id={bad_id!r} 거부됨", False)
    except OrderIdRejected:
        check(f"5) order_id={bad_id!r} 거부됨", True)

check("5-2) 유효한 order_id는 정상 통과(strip됨)",
      validate_order_id("  0000069  ") == "0000069")


# ══════════════════════════════════════════════════════════════
# 6. 공백 order_id 거부
# ══════════════════════════════════════════════════════════════
for bad_id in ("", "   ", "\t\n"):
    try:
        validate_order_id(bad_id)
        check(f"6) order_id={bad_id!r} 거부됨", False)
    except OrderIdRejected:
        check(f"6) order_id={bad_id!r} 거부됨", True)


# ══════════════════════════════════════════════════════════════
# 7. raw body가 변형되지 않고 보존됨
# ══════════════════════════════════════════════════════════════
_session_raw = _FakeSession(
    response_by_api_id={
        "ka10076": _FakeResponse(
            200,
            {"api-id": "ka10076", "cont-yn": "N", "next-key": ""},
            {
                "cntr": [
                    {"ord_no": "0000037", "cntr_pric": "158200", "cntr_qty": "1",
                     "ord_stt": "체결", "oso_qty": "0"},
                ],
                "return_code": 0,
                "return_msg": " 조회가 완료되었습니다.",
            },
        ),
    }
)
_status7, _headers7, _body7 = call_kiwoom_api(
    _session_raw, "https://mockapi.kiwoom.com", "TOKEN", "ka10076",
    build_ka10076_payload("005930"),
)
check("7-1) 비민감 필드 값이 정확히 그대로 보존됨(cntr_pric, cntr_qty, ord_stt)",
      _body7["cntr"][0]["cntr_pric"] == "158200"
      and _body7["cntr"][0]["cntr_qty"] == "1"
      and _body7["cntr"][0]["ord_stt"] == "체결")
check("7-2) return_code/return_msg도 그대로 보존됨",
      _body7["return_code"] == 0 and "완료" in _body7["return_msg"])
check("7-3) 리스트 길이·순서도 그대로 보존됨",
      len(_body7["cntr"]) == 1)


# ══════════════════════════════════════════════════════════════
# 8. cont-yn / next-key header 보존
# ══════════════════════════════════════════════════════════════
_session_cont = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(
            200,
            {"api-id": "ka10075", "cont-yn": "Y", "next-key": "NEXTKEY123"},
            _ok_body_ka10075,
        ),
    }
)
_status8, _headers8, _body8 = call_kiwoom_api(
    _session_cont, "https://mockapi.kiwoom.com", "TOKEN", "ka10075",
    build_ka10075_payload("005930"),
)
check("8-1) cont-yn 값이 정확히 보존됨", _headers8["cont-yn"] == "Y")
check("8-2) next-key 값이 정확히 보존됨", _headers8["next-key"] == "NEXTKEY123")
check("8-3) api-id 값도 보존됨", _headers8["api-id"] == "ka10075")


# ══════════════════════════════════════════════════════════════
# 9. API 오류 응답도 버리지 않고 기록
# ══════════════════════════════════════════════════════════════
_session_err = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(
            200,
            {"api-id": "ka10075", "cont-yn": "N", "next-key": ""},
            {"return_code": 5, "return_msg": "업무 오류: 조회 실패"},
        ),
    }
)
_status9, _headers9, _body9 = call_kiwoom_api(
    _session_err, "https://mockapi.kiwoom.com", "TOKEN", "ka10075",
    build_ka10075_payload("005930"),
)
check("9-1) return_code!=0인 업무 오류도 예외 없이 반환됨(버리지 않음)",
      _status9 == 200 and _body9["return_code"] == 5)
check("9-2) 오류 메시지 내용도 그대로 보존됨", "업무 오류" in _body9["return_msg"])

_session_http_err = _FakeSession(
    response_by_api_id={
        "ka10076": _FakeResponse(500, {"api-id": "ka10076", "cont-yn": "N", "next-key": ""},
                                  {"return_msg": "internal error"}),
    }
)
_status9b, _headers9b, _body9b = call_kiwoom_api(
    _session_http_err, "https://mockapi.kiwoom.com", "TOKEN", "ka10076",
    build_ka10076_payload("005930"),
)
check("9-3) HTTP 500도 예외를 던지지 않고 http_status 그대로 반환됨",
      _status9b == 500)


# ══════════════════════════════════════════════════════════════
# 10. JSONL 한 호출 = 한 레코드 보장 (main() 통합 테스트)
# ══════════════════════════════════════════════════════════════
class _FakeBrokerConfig:
    def __init__(self, base_url):
        self.base_url = base_url
        self.app_key = "FAKE"
        self.secret_key = "FAKE"
        self.provider = "kiwoom"
        self.use_mock = False
        self.account_number = "REDACT-ME"
        self.is_paper_trading = True


class _FakeSettings:
    def __init__(self, base_url):
        self.broker = _FakeBrokerConfig(base_url)


class _FakeKiwoomBroker:
    """실제 infra.broker.kiwoom_broker.KiwoomBroker를 대체하는 테스트용 가짜.

    authenticate()가 네트워크를 타지 않고 바로 토큰을 채웁니다.
    """

    def __init__(self, config):
        self.config = config
        self.access_token = None
        self.session = _FakeSession(
            response_by_api_id={
                "ka10075": _FakeResponse(
                    200, {"api-id": "ka10075", "cont-yn": "N", "next-key": ""},
                    _ok_body_ka10075,
                ),
                "ka10076": _FakeResponse(
                    200, {"api-id": "ka10076", "cont-yn": "N", "next-key": ""},
                    {"cntr": [], "return_code": 0, "return_msg": "완료"},
                ),
            }
        )

    def authenticate(self):
        self.access_token = "FAKE-TOKEN-FOR-TEST"


with tempfile.TemporaryDirectory() as _tmp10:
    _out_path10 = Path(_tmp10) / "probe10.jsonl"
    with patch("config.settings.load_settings", lambda path: _FakeSettings("https://mockapi.kiwoom.com")), \
         patch("infra.broker.kiwoom_broker.KiwoomBroker", _FakeKiwoomBroker):
        _rc10 = probe_main([
            "--symbol", "005930", "--order-id", "0000069",
            "--side", "BUY", "--requested-quantity", "1",
            "--scenario", "test_full_fill",
            "--intervals", "0",
            "--out", str(_out_path10),
        ])

    check("10-1) main()이 성공 종료(exit code 0)", _rc10 == 0)
    _lines10 = _out_path10.read_text(encoding="utf-8").strip().splitlines()
    check("10-2) intervals='0' 하나 → ka10075+ka10076 = 정확히 2줄",
          len(_lines10) == 2)
    _records10 = [json.loads(line) for line in _lines10]
    check("10-3) 각 줄이 유효한 JSON 객체 하나(파싱 성공)", len(_records10) == 2)
    check("10-4) api_id가 각각 ka10075, ka10076으로 서로 다름",
          {r["api_id"] for r in _records10} == {"ka10075", "ka10076"})


# ══════════════════════════════════════════════════════════════
# 11. run_id / elapsed_ms 유지
# ══════════════════════════════════════════════════════════════
check("11-1) run_id에 종목코드가 포함됨",
      all("005930" in r["run_id"] for r in _records10))
check("11-2) 같은 run 안에서는 run_id가 모두 동일",
      len({r["run_id"] for r in _records10}) == 1)
check("11-3) elapsed_ms가 0 이상의 정수",
      all(isinstance(r["elapsed_ms"], int) and r["elapsed_ms"] >= 0 for r in _records10))
check("11-4) schema_version=1, environment='mock'",
      all(r["schema_version"] == 1 and r["environment"] == "mock" for r in _records10))
check("11-5) side/requested_quantity/scenario가 CLI 인자 그대로 반영됨",
      all(r["side"] == "BUY" and r["requested_quantity"] == 1
          and r["scenario"] == "test_full_fill" for r in _records10))
check("11-6) order_id가 정규화된 값으로 기록됨",
      all(r["order_id"] == "0000069" for r in _records10))

_now11 = datetime.now(timezone.utc)
check("11-7) build_record가 captured_at_kst를 ISO8601로 남김(파싱 가능)",
      bool(datetime.fromisoformat(_records10[0]["captured_at_kst"])))


# ══════════════════════════════════════════════════════════════
# 12. 조회 실패가 다음 캡처를 조용히 정상값으로 위장하지 않음
# ══════════════════════════════════════════════════════════════
_session_fail = _FakeSession(raise_exc=requests.exceptions.ConnectionError("connection refused"))
_status12, _headers12, _body12 = call_kiwoom_api(
    _session_fail, "https://mockapi.kiwoom.com", "TOKEN", "ka10075",
    build_ka10075_payload("005930"),
)
check("12-1) 네트워크 예외 시 http_status=None(정상 200으로 위장하지 않음)",
      _status12 is None)
check("12-2) 실패 사실이 response_body에 명시적으로 남음",
      "probe_transport_error" in _body12)
check("12-3) 예외 메시지 원문('connection refused')이 그대로 노출되지 않음"
      "(토큰 등이 예외 문자열에 섞여 나올 수 있어 타입명만 기록)",
      "connection refused" not in json.dumps(_body12)
      and _body12["probe_transport_error"] == "ConnectionError")
check("12-4) 실패해도 예외를 다시 던지지 않고 값으로 반환(호출부가 다음 캡처를 계속할 수 있음)",
      isinstance(_body12, dict))


# ══════════════════════════════════════════════════════════════
# 14. mock-domain fail-closed 강화 (2026-08-14 GPT 재검토 #1)
# ══════════════════════════════════════════════════════════════
for bad_url in (
    "http://mockapi.kiwoom.com",        # scheme이 http — 토큰 평문 전송 위험
    "https://mockapi.kiwoom.com:8443",  # 비표준 포트
    "ftp://mockapi.kiwoom.com",         # 엉뚱한 scheme
    "https://mockapi.kiwoom.com:80",    # https인데 80포트(비표준)
):
    try:
        assert_mock_domain(bad_url)
        check(f"14-1) 강화된 검증이 거부: {bad_url}", False)
    except NonMockDomainRejected:
        check(f"14-1) 강화된 검증이 거부: {bad_url}", True)

for good_url in ("https://mockapi.kiwoom.com", "https://mockapi.kiwoom.com:443"):
    try:
        assert_mock_domain(good_url)
        check(f"14-2) 정상 URL은 통과: {good_url}", True)
    except ProbeConfigError:
        check(f"14-2) 정상 URL은 통과: {good_url}", False)


# ══════════════════════════════════════════════════════════════
# 15. call_kiwoom_api() 자체의 defense-in-depth (2026-08-14 GPT 재검토 #1)
# ══════════════════════════════════════════════════════════════
_session15a = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "N", "next-key": ""}, _ok_body_ka10075),
    }
)
try:
    call_kiwoom_api(_session15a, "https://api.kiwoom.com", "TOKEN", "ka10075",
                     build_ka10075_payload("005930"))
    check("15-1) call_kiwoom_api()에 실전 URL을 직접 넘겨도 거부됨(main()의 검증을 우회해도 안전)", False)
except NonMockDomainRejected:
    check("15-1) call_kiwoom_api()에 실전 URL을 직접 넘겨도 거부됨(main()의 검증을 우회해도 안전)",
          len(_session15a.calls) == 0)

_session15b = _FakeSession(
    response_by_api_id={
        "ka10075": _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "N", "next-key": ""}, _ok_body_ka10075),
    }
)
try:
    call_kiwoom_api(_session15b, "http://mockapi.kiwoom.com", "TOKEN", "ka10075",
                     build_ka10075_payload("005930"))
    check("15-2) call_kiwoom_api()에 http(평문) URL을 직접 넘겨도 거부됨", False)
except NonMockDomainRejected:
    check("15-2) call_kiwoom_api()에 http(평문) URL을 직접 넘겨도 거부됨",
          len(_session15b.calls) == 0)


# ══════════════════════════════════════════════════════════════
# 16. 연속조회(pagination) — cont-yn/next-key 따라가기 (2026-08-14 GPT 재검토 #2)
# ══════════════════════════════════════════════════════════════
_pages16 = []


def _capture16(page_index, request_cont_yn, request_next_key, http_status, response_headers, response_body):
    _pages16.append({
        "page_index": page_index,
        "request_cont_yn": request_cont_yn,
        "request_next_key": request_next_key,
        "http_status": http_status,
        "response_headers": response_headers,
        "response_body": response_body,
    })


_session16 = _FakeSequenceSession({
    "ka10075": [
        _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "Y", "next-key": "PAGE2KEY"},
                      {"oso": [{"ord_no": "AAA"}], "return_code": 0}),
        _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "Y", "next-key": "PAGE3KEY"},
                      {"oso": [{"ord_no": "BBB"}], "return_code": 0}),
        _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "N", "next-key": ""},
                      {"oso": [{"ord_no": "CCC"}], "return_code": 0}),
    ]
})
fetch_paginated(_session16, "https://mockapi.kiwoom.com", "TOKEN", "ka10075",
                 build_ka10075_payload("005930"), _capture16)

check("16-1) cont-yn=Y,Y,N 응답 3개를 전부 페이지로 조회함", len(_pages16) == 3)
check("16-2) page_index가 1,2,3 순서로 증가", [p["page_index"] for p in _pages16] == [1, 2, 3])
check("16-3) 첫 요청은 cont-yn=N/next-key=''(첫 페이지 기본값)",
      _pages16[0]["request_cont_yn"] == "N" and _pages16[0]["request_next_key"] == "")
check("16-4) 두 번째 요청은 첫 응답의 next-key(PAGE2KEY)를 그대로 사용",
      _pages16[1]["request_cont_yn"] == "Y" and _pages16[1]["request_next_key"] == "PAGE2KEY")
check("16-5) 세 번째 요청은 두 번째 응답의 next-key(PAGE3KEY)를 그대로 사용",
      _pages16[2]["request_cont_yn"] == "Y" and _pages16[2]["request_next_key"] == "PAGE3KEY")
check("16-6) 실제 HTTP 요청 헤더에도 cont-yn/next-key가 정확히 실림(2번째 호출)",
      _session16.calls[1]["headers"]["cont-yn"] == "Y"
      and _session16.calls[1]["headers"]["next-key"] == "PAGE2KEY")
check("16-7) cont-yn=N인 마지막 페이지에서 정상 종료(추가 호출 없음, 총 3회)",
      len(_session16.calls) == 3)
check("16-8) 각 페이지의 response_body가 서로 다른 원본 그대로 보존됨(첫 페이지에 없어도 끊지 않음)",
      [p["response_body"]["oso"][0]["ord_no"] for p in _pages16] == ["AAA", "BBB", "CCC"])


# ══════════════════════════════════════════════════════════════
# 17. page cap 초과 시 명시적 오류/중단 (2026-08-14 GPT 재검토 #2)
# ══════════════════════════════════════════════════════════════
_pages17 = []


def _capture17(page_index, request_cont_yn, request_next_key, http_status, response_headers, response_body):
    _pages17.append((page_index, request_cont_yn, request_next_key, http_status, response_headers, response_body))


_session17 = _FakeInfiniteContinuationSession()
fetch_paginated(_session17, "https://mockapi.kiwoom.com", "TOKEN", "ka10075",
                 build_ka10075_payload("005930"), _capture17, max_pages=3)

check("17-1) 실제 네트워크 호출은 max_pages(3)회로 제한됨(무한 루프 방지)",
      len(_session17.calls) == 3)
check("17-2) on_page는 실제 페이지 3개 + synthetic cap 레코드 1개 = 4번 호출됨",
      len(_pages17) == 4)
_last17 = _pages17[-1]
check("17-3) synthetic 레코드는 추가 네트워크 호출 없이 http_status=None",
      _last17[3] is None)
check("17-4) synthetic 레코드가 cap 초과 사실과 max_pages 값을 명시함",
      isinstance(_last17[5], dict)
      and _last17[5].get("probe_page_cap_exceeded") is True
      and _last17[5].get("max_continuation_pages") == 3)
check("17-5) synthetic 레코드의 page_index는 max_pages+1(4) — 어느 페이지에서 끊겼는지 알 수 있음",
      _last17[0] == 4)
check("17-6) 실제 페이지 1~3의 http_status는 정상(200)으로 남아있음(cap 자체가 실패로 오인되지 않음)",
      all(p[3] == 200 for p in _pages17[:3]))


# ══════════════════════════════════════════════════════════════
# 18. main() 통합 — pagination이 실제로 여러 JSONL 레코드를 만듦
# ══════════════════════════════════════════════════════════════
class _FakeKiwoomBrokerPaginated:
    """ka10075는 2페이지(Y→N), ka10076은 1페이지만 있는 시나리오."""

    def __init__(self, config):
        self.config = config
        self.access_token = None
        self.session = _FakeSequenceSession({
            "ka10075": [
                _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "Y", "next-key": "P2"},
                              {"oso": [{"ord_no": "X1"}], "return_code": 0}),
                _FakeResponse(200, {"api-id": "ka10075", "cont-yn": "N", "next-key": ""},
                              {"oso": [{"ord_no": "X2"}], "return_code": 0}),
            ],
            "ka10076": [
                _FakeResponse(200, {"api-id": "ka10076", "cont-yn": "N", "next-key": ""},
                              {"cntr": [], "return_code": 0}),
            ],
        })

    def authenticate(self):
        self.access_token = "FAKE-TOKEN-FOR-TEST"


with tempfile.TemporaryDirectory() as _tmp18:
    _out_path18 = Path(_tmp18) / "probe18.jsonl"
    with patch("config.settings.load_settings", lambda path: _FakeSettings("https://mockapi.kiwoom.com")), \
         patch("infra.broker.kiwoom_broker.KiwoomBroker", _FakeKiwoomBrokerPaginated):
        _rc18 = probe_main([
            "--symbol", "005930", "--order-id", "0000069",
            "--intervals", "0",
            "--out", str(_out_path18),
        ])

    check("18-1) main()이 성공 종료", _rc18 == 0)
    _records18 = [json.loads(line) for line in _out_path18.read_text(encoding="utf-8").strip().splitlines()]
    check("18-2) ka10075 2페이지 + ka10076 1페이지 = 총 3줄",
          len(_records18) == 3)
    _ka10075_records18 = sorted(
        (r for r in _records18 if r["api_id"] == "ka10075"), key=lambda r: r["page_index"],
    )
    check("18-3) ka10075 레코드가 정확히 2개, page_index=1,2",
          [r["page_index"] for r in _ka10075_records18] == [1, 2])
    check("18-4) 두 번째 페이지 레코드의 request_cont_yn/request_next_key가 첫 응답 값과 일치",
          _ka10075_records18[1]["request_cont_yn"] == "Y"
          and _ka10075_records18[1]["request_next_key"] == "P2")
    check("18-5) 각 페이지의 response_body가 서로 다름(X1, X2 — 뭉개지지 않음)",
          _ka10075_records18[0]["response_body"]["oso"][0]["ord_no"] == "X1"
          and _ka10075_records18[1]["response_body"]["oso"][0]["ord_no"] == "X2")
    _ka10076_records18 = [r for r in _records18 if r["api_id"] == "ka10076"]
    check("18-6) ka10076은 1페이지만(cont-yn=N이라 정상 종료)", len(_ka10076_records18) == 1
          and _ka10076_records18[0]["page_index"] == 1)


# ══════════════════════════════════════════════════════════════
# 13. 범위 밖 안전장치 — 소스 레벨 구조 검증
# ══════════════════════════════════════════════════════════════
# 13-1~13-3: docstring/주석에서 "이 스크립트는 이걸 안 한다"고 설명하는
# 문구 자체는 당연히 소스에 남아있으므로(안전 설계 설명), 실제 "호출"
# 형태(".place_order(", ".cancel_order(", "import ... PositionStateMachine"
# 등)만 정밀하게 검사합니다 — 단순 문자열 부재 검사는 이 문구들 때문에
# 항상 실패하므로 부적절합니다.
check("13-1) broker.place_order(...) 형태의 실제 호출이 없음",
      ".place_order(" not in ts_probe_src)
check("13-2) broker.cancel_order(...) 형태의 실제 호출이나 def cancel_order가 없음",
      ".cancel_order(" not in ts_probe_src
      and "def cancel_order" not in ts_probe_src)
check("13-3) PositionStateMachine/TradingService를 import하지 않음(매매 판단에 영향 없음)",
      "import PositionStateMachine" not in ts_probe_src
      and "PositionStateMachine as" not in ts_probe_src
      and "from domain.position" not in ts_probe_src
      and "from domain.service" not in ts_probe_src
      and "import TradingService" not in ts_probe_src)
check("13-4) 요청 바디가 order_id를 파라미터로 담지 않음(ka10075/ka10076 모두 종목 기준 조회)",
      "order_id" not in json.dumps(build_ka10075_payload("005930"))
      and "order_id" not in json.dumps(build_ka10076_payload("005930")))


# ══════════════════════════════════════════════════════════════
# 19. .env 로딩 — 실측 캡처 중 발견된 인증 실패 버그 수정 (2026-08-14)
# ══════════════════════════════════════════════════════════════
# 민우님이 실제 모의계좌로 실행했을 때 "App Key와 Secret Key 검증에
# 실패했습니다"(return_code=3)로 인증이 거부됐습니다. 원인은 계정/키
# 등록 문제가 아니라, 이 프로브가 load_settings() 전에 .env를 읽지
# 않아서 settings.yaml의 ${KIWOOM_APP_KEY} 치환이 조용히 빈 문자열이
# 된 것이었습니다(config/settings.py의 _substitute_env는 환경변수가
# 없으면 예외 없이 ""로 대체). app/main.py는 이미 자체 load_dotenv()를
# 갖고 있었는데 이 프로브에는 그 호출 자체가 빠져 있었습니다.
_ENV_TEST_KEYS = ("PROBE_TEST_APP_KEY", "PROBE_TEST_SECRET_KEY", "PROBE_TEST_ALREADY_SET")
_saved_env19 = {k: os.environ.get(k) for k in _ENV_TEST_KEYS}
try:
    for _k in _ENV_TEST_KEYS:
        os.environ.pop(_k, None)

    with tempfile.TemporaryDirectory() as _tmp19:
        _env_path19 = Path(_tmp19) / ".env"
        _env_path19.write_text(
            "# comment line, and a blank line below should be skipped\n"
            "\n"
            "PROBE_TEST_APP_KEY=file-app-key\n"
            "PROBE_TEST_SECRET_KEY=file-secret-key\n",
            encoding="utf-8",
        )
        probe_load_dotenv(str(_env_path19))
        check("19-1) .env의 KEY=VALUE가 os.environ에 채워짐(주석/빈 줄은 건너뜀)",
              os.environ.get("PROBE_TEST_APP_KEY") == "file-app-key"
              and os.environ.get("PROBE_TEST_SECRET_KEY") == "file-secret-key")

    os.environ["PROBE_TEST_ALREADY_SET"] = "original-value"
    with tempfile.TemporaryDirectory() as _tmp19b:
        _env_path19b = Path(_tmp19b) / ".env"
        _env_path19b.write_text("PROBE_TEST_ALREADY_SET=from-file\n", encoding="utf-8")
        probe_load_dotenv(str(_env_path19b))
        check("19-2) 이미 설정된 환경변수는 .env 값으로 덮어쓰지 않음(setdefault, main.py와 동일 동작)",
              os.environ.get("PROBE_TEST_ALREADY_SET") == "original-value")

    try:
        probe_load_dotenv(str(Path(_tmp19b) / "does-not-exist.env"))
        check("19-3) .env 파일이 없어도 예외를 던지지 않고 조용히 넘어감", True)
    except Exception:
        check("19-3) .env 파일이 없어도 예외를 던지지 않고 조용히 넘어감", False)
finally:
    for _k, _v in _saved_env19.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

_load_dotenv_pos19 = ts_probe_src.find("load_dotenv(args.env_file)")
_load_settings_pos19 = ts_probe_src.find("settings = load_settings(args.settings)")
check("19-4) main() 소스에서 load_dotenv()가 load_settings()보다 먼저 호출됨(버그 재발 방지)",
      _load_dotenv_pos19 != -1 and _load_settings_pos19 != -1
      and _load_dotenv_pos19 < _load_settings_pos19)


# ══════════════════════════════════════════════════════════════
# 20. main() 통합 — .env가 실제로 load_settings() 호출 전에 반영됨
# ══════════════════════════════════════════════════════════════
_captured_env20 = {}


def _fake_load_settings_capturing_env(path):
    _captured_env20["PROBE_TEST_INTEGRATION_KEY"] = os.environ.get("PROBE_TEST_INTEGRATION_KEY")
    return _FakeSettings("https://mockapi.kiwoom.com")


_saved20 = os.environ.get("PROBE_TEST_INTEGRATION_KEY")
os.environ.pop("PROBE_TEST_INTEGRATION_KEY", None)
try:
    with tempfile.TemporaryDirectory() as _tmp20:
        _env_path20 = Path(_tmp20) / "custom.env"
        _env_path20.write_text("PROBE_TEST_INTEGRATION_KEY=from-env-file\n", encoding="utf-8")
        _out_path20 = Path(_tmp20) / "probe20.jsonl"
        with patch("config.settings.load_settings", _fake_load_settings_capturing_env), \
             patch("infra.broker.kiwoom_broker.KiwoomBroker", _FakeKiwoomBroker):
            _rc20 = probe_main([
                "--symbol", "005930", "--order-id", "0000069",
                "--intervals", "0",
                "--out", str(_out_path20),
                "--env-file", str(_env_path20),
            ])
        check("20-1) main()이 성공 종료", _rc20 == 0)
        check("20-2) load_settings() 호출 시점에 이미 .env 값이 os.environ에 반영돼 있음(호출 순서 실증)",
              _captured_env20.get("PROBE_TEST_INTEGRATION_KEY") == "from-env-file")
finally:
    if _saved20 is None:
        os.environ.pop("PROBE_TEST_INTEGRATION_KEY", None)
    else:
        os.environ["PROBE_TEST_INTEGRATION_KEY"] = _saved20


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
