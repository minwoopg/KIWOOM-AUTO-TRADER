from __future__ import annotations

"""1P0.8-B.1: 키움 ka10075(미체결요청)/ka10076(체결요청) read-only 진단 프로브.

이 스크립트가 "하지 않는" 것 (범위 밖, 매우 중요):
- `place_order()` / `cancel_order()`를 호출하지 않습니다. 주문 자체는 항상
  기존 프로그램이나 HTS/영웅문에서 사람이 직접 넣습니다. 이 스크립트는
  이미 접수된 주문의 `order_id`(ord_no)를 받아 **조회만** 합니다.
- `TradingService`나 `PositionStateMachine`을 전혀 import하지 않습니다.
  매매 판단/상태 전이에는 어떤 영향도 주지 않습니다.
- 키움 응답 필드를 우리 도메인 상태(PENDING/FILLED/CANCELLED 등)로
  해석·정규화하지 않습니다. 그 작업은 이 진단으로 실측 데이터를 먼저
  확보한 뒤 1P0.8-B.2에서 합니다.

이 스크립트가 "하는" 것:
- 지정한 시간 간격(기본 T+0/1/3/10초)마다 ka10075, ka10076을 각각 호출해
  응답을 가공 없이(민감정보 redaction 제외) JSONL 한 줄에 하나씩 기록합니다.
- 미체결/체결/취소 등 어떤 상태 전이가 실측으로 나타나는지는 사람이
  JSONL을 직접 읽어 판단합니다.

사용 예 (프로젝트 루트에서 실행, `.env`가 같은 위치에 있어야 함):
    python tools/order_reconciliation_probe.py \\
        --symbol 005930 --order-id 0000069 \\
        --side BUY --requested-quantity 1 \\
        --scenario market_buy_full_fill

    `python -m tools.order_reconciliation_probe ...`로 실행해도 동일하게
    동작합니다. 둘 다 실행 시점의 현재 디렉터리를 기준으로 `.env`/
    `config/settings.yaml`을 찾으므로, 반드시 프로젝트 루트에서
    실행하세요.

안전장치 (fail-closed):
1. `settings.broker.base_url`이 정확히 `https://mockapi.kiwoom.com`
   (포트 없음 또는 443)이 아니면 즉시 거부합니다 — 실전 계좌 조회와
   평문(HTTP) 전송(토큰이 암호화 없이 나갈 수 있음)을 함께 막습니다.
   이 검증은 `main()` 진입 시점뿐 아니라 실제 HTTP 호출을 수행하는
   `call_kiwoom_api()` 맨 앞에서도 다시 한번 수행합니다(defense-in-
   depth) — 나중에 누군가 `call_kiwoom_api()`를 다른 base_url로 직접
   호출해도(예: 실전 URL을 실수로 하드코딩) 네트워크 호출 자체가
   나가지 않습니다. 우회 옵션(플래그로 이 검증을 끄는 기능)은 의도적
   으로 제공하지 않습니다.
2. 호출 가능한 api-id는 `ka10075`/`ka10076` 두 개로 하드코딩되어
   있습니다. 다른 값이 들어오면(코드 수정 실수 포함) 즉시 예외.
3. `order_id`가 None/빈 문자열/공백뿐/`"pending"`/`"UNKNOWN_ORDER_ID"`
   (PSM의 placeholder·sentinel 리터럴, 1P0.8-A.1 참고)이면 실행을
   거부합니다 — 조회 불가능한 값으로 키움 API를 호출하지 않습니다.
   (참고: 이 값들은 애초에 요청 바디에 들어가지도 않습니다 — ka10075/
   ka10076 요청 바디는 종목코드 기준 조회이며 order_id를 파라미터로
   받지 않습니다. `--order-id`는 이 JSONL 레코드에 붙는 라벨/메타데이터
   일 뿐입니다. 그래도 애초에 조회 목적이 성립하지 않는 값이면 거부.)
4. JSONL에는 `authorization`/access token/appkey/secretkey/계좌번호를
   절대 기록하지 않습니다. 요청 헤더 전체를 덤프하지 않고, 응답 헤더는
   `api-id`/`cont-yn`/`next-key`만 allow-list로 뽑습니다. 응답 바디는
   원본을 그대로 저장하되, `acnt_no` 등 계좌번호로 보이는 키는 값만
   `"[REDACTED]"`로 치환합니다(구조/다른 필드는 그대로 보존).
5. 연속조회(pagination)를 명시적으로 따라갑니다. 키움 REST는 응답에
   `cont-yn=Y`가 오면 다음 페이지가 더 있다는 뜻이고, 그 `next-key`를
   다음 요청 헤더에 그대로 넣어야 조회를 이어갈 수 있습니다(공식
   가이드). 첫 페이지에 우리가 찾는 주문이 없다고 해서 조회를 끊으면
   "이 주문이 미체결/체결 목록에 없었다"가 아니라 "첫 페이지만
   봤다"를 "없었다"로 오인하는 위험한 데이터가 만들어집니다. 이
   프로브는 상태를 해석하지 않으므로, 각 페이지를 그대로 JSONL
   한 줄씩으로 남기고(`page_index`/`request_cont_yn`/
   `request_next_key` 메타데이터 포함) `MAX_CONTINUATION_PAGES`
   (기본 20)에 도달하면 계속 진행하는 대신 그 사실 자체를 명시하는
   레코드를 하나 더 남기고 멈춥니다(무한 루프 방지 + 침묵 없는 중단).
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

# ── 안전장치 상수 ────────────────────────────────────────────────
ALLOWED_API_IDS = frozenset({"ka10075", "ka10076"})
ALLOWED_BASE_URL_HOST = "mockapi.kiwoom.com"
ALLOWED_BASE_URL_SCHEME = "https"
ALLOWED_BASE_URL_PORTS = (None, 443)  # None = URL에 포트가 명시 안 됨(기본 443)
ALLOWED_RESPONSE_HEADER_KEYS = ("api-id", "cont-yn", "next-key")
FORBIDDEN_ORDER_IDS = {"pending", "UNKNOWN_ORDER_ID"}
MAX_CONTINUATION_PAGES = 20

# 응답 바디에서 값만 redact할 키(대소문자 무시, 부분일치).
# ka10075 실측 예시 응답에 "acnt_no"가 실제로 포함되어 있음을 확인했음
# (2026-08-14, 민우님 제공 공식 예시 JSON) — raw dump 원칙보다 계좌정보
# 비저장 원칙이 우선이므로 이 키들만 값을 가립니다.
SENSITIVE_BODY_KEY_MARKERS = (
    "acnt_no", "account_no", "accno", "authorization",
    "access_token", "accesstoken", "appkey", "app_key",
    "secretkey", "secret_key", "app_secret", "token",
)

KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1

ENDPOINT = "/api/dostk/acnt"


class ProbeConfigError(ValueError):
    """CLI 인자/설정이 안전 요건을 충족하지 못해 실행을 거부할 때."""


class OrderIdRejected(ProbeConfigError):
    """order_id가 실제 조회 가능한 값이 아닐 때."""


class NonMockDomainRejected(ProbeConfigError):
    """base_url이 모의투자 도메인이 아닐 때."""


class DisallowedApiId(ProbeConfigError):
    """ka10075/ka10076 이외의 api-id를 호출하려고 할 때."""


# ══════════════════════════════════════════════════════════════
# 검증 헬퍼 — 순수 함수, 네트워크 호출 없음 (테스트 용이)
# ══════════════════════════════════════════════════════════════

def validate_order_id(raw: Any) -> str:
    """order_id가 실제로 조회 의미가 있는 값인지 확인하고 정규화합니다.

    None / "" / 공백뿐 / "pending" / "UNKNOWN_ORDER_ID"는 모두 거부합니다.
    뒤의 두 값은 PositionStateMachine이 남기는 placeholder(주문 접수
    전 임시값) / sentinel(order_id 누락 이상 응답 표시, 1P0.8-A.1
    참고) 리터럴이며, 실제 브로커 주문번호가 아닙니다.
    """
    if raw is None:
        raise OrderIdRejected("order_id가 없습니다(None) — 실제 ord_no를 전달하세요")
    value = str(raw).strip()
    if not value:
        raise OrderIdRejected("order_id가 비어있거나 공백뿐입니다")
    if value in FORBIDDEN_ORDER_IDS:
        raise OrderIdRejected(
            f"order_id={value!r}는 실제 브로커 주문번호가 아니라 내부 "
            f"placeholder/sentinel 리터럴입니다(PositionStateMachine, "
            f"1P0.8-A.1 참고) — 이 값으로는 조회 목적이 성립하지 않습니다"
        )
    return value


def assert_mock_domain(base_url: str) -> None:
    """base_url이 정확히 `https://mockapi.kiwoom.com`(포트 없음/443)인지 확인합니다.

    세 가지를 모두 요구합니다(2026-08-14, GPT 재검토 반영 — 강화):
    - scheme이 정확히 "https"(http는 거부 — 토큰이 평문으로 나가는 것을 방지)
    - host가 정확히 "mockapi.kiwoom.com"(부분 문자열 포함이 아니라 정확히
      일치. 예: "mockapi.kiwoom.com.evil.example" 같은 우회 방지)
    - port가 명시 안 됐거나(None, https 기본값) 443(비표준 포트 차단)

    `main()` 진입 시점뿐 아니라 `call_kiwoom_api()` 맨 앞에서도 다시
    호출됩니다 — 호출부 하나가 이 검증을 우회해도 실제 네트워크 계층
    에서 다시 막히도록 하기 위함(defense-in-depth).
    """
    parsed = urlparse(base_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if (
        scheme != ALLOWED_BASE_URL_SCHEME
        or host != ALLOWED_BASE_URL_HOST
        or port not in ALLOWED_BASE_URL_PORTS
    ):
        raise NonMockDomainRejected(
            f"이 진단 스크립트는 https://{ALLOWED_BASE_URL_HOST}(포트 없음 또는 "
            f"443)에서만 실행할 수 있습니다. 현재 base_url={base_url!r} "
            f"(scheme={scheme!r}, host={host!r}, port={port!r}). 실전 계좌 조회와 "
            f"평문 전송을 막기 위한 안전장치이며, 우회 옵션은 의도적으로 제공하지 "
            f"않습니다."
        )


def parse_intervals(raw: str) -> list[float]:
    """"0,1,3,10" 같은 문자열을 정렬·중복 제거된 초 단위 리스트로 바꿉니다."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ProbeConfigError("--intervals가 비어 있습니다")
    try:
        values = sorted({float(p) for p in parts})
    except ValueError as exc:
        raise ProbeConfigError(f"--intervals에 숫자가 아닌 값이 있습니다: {raw!r}") from exc
    for v in values:
        if v < 0:
            raise ProbeConfigError(f"--intervals는 음수를 허용하지 않습니다: {v}")
    return values


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_BODY_KEY_MARKERS)


def redact_sensitive(value: Any) -> Any:
    """dict/list를 재귀적으로 순회하며 민감 키의 값만 "[REDACTED]"로 치환합니다.

    구조(키 존재 여부, 리스트 순서/길이, 비민감 필드 값)는 그대로
    보존합니다 — "raw body는 가공하지 않는다" 원칙과 "계좌정보는 절대
    저장하지 않는다" 원칙을 함께 지키기 위한 최소한의 예외 처리입니다.
    """
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive_key(str(k)) else redact_sensitive(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(v) for v in value]
    return value


def build_ka10075_payload(symbol: str) -> dict[str, str]:
    """미체결요청(ka10075) 요청 바디. 필드는 민우님이 제공한 공식 샘플 그대로."""
    return {
        "all_stk_tp": "1",  # 0:전체, 1:종목
        "trde_tp": "0",     # 0:전체, 1:매도, 2:매수
        "stk_cd": symbol,
        "stex_tp": "0",     # 0:통합, 1:KRX, 2:NXT
    }


def build_ka10076_payload(symbol: str) -> dict[str, str]:
    """체결요청(ka10076) 요청 바디. 필드는 민우님이 제공한 공식 샘플 그대로.

    ord_no는 일부러 빈 문자열로 둡니다("이 주문번호보다 과거에 체결된
    내역 조회" 필터라 특정 주문 하나만 골라내는 필터가 아님, 공식 샘플
    설명 참고) — 종목 전체 체결이력을 그대로 받아 사람이 판단합니다.
    """
    return {
        "stk_cd": symbol,
        "qry_tp": "1",   # 0:전체, 1:종목
        "sell_tp": "0",  # 0:전체, 1:매도, 2:매수
        "ord_no": "",
        "stex_tp": "0",
    }


# ══════════════════════════════════════════════════════════════
# 네트워크 호출 — session은 .post(url, headers=, json=, timeout=)만
# 있으면 되는 최소 인터페이스라, 테스트에서 fake session으로 대체 가능
# ══════════════════════════════════════════════════════════════

def call_kiwoom_api(
    session: Any,
    base_url: str,
    access_token: str,
    api_id: str,
    payload: dict[str, Any],
    cont_yn: str = "N",
    next_key: str = "",
    timeout: int = 10,
) -> tuple[int | None, dict[str, str], Any]:
    """ka10075/ka10076을 호출하고 (http_status, response_headers, response_body)를 반환합니다.

    - base_url이 모의투자 도메인이 아니면(defense-in-depth, `main()`의
      검증과 별개로 여기서도 다시 확인) 네트워크 호출 자체를 하지 않고 예외.
    - api_id가 허용 목록 밖이면 마찬가지로 네트워크 호출 자체를 하지 않고 예외.
    - cont_yn/next_key는 연속조회(pagination) 요청 헤더입니다. 첫 페이지는
      "N"/""(기본값), 다음 페이지부터는 직전 응답의 cont-yn/next-key를
      그대로 넘겨받아 씁니다(공식 가이드 규정).
    - 네트워크 계층 예외(타임아웃/연결 실패 등)는 삼키지 않고 http_status=None,
      토큰이 노출되지 않는 안전한 메시지만 담아 반환합니다 — 실패를 조용히
      정상값으로 위장하지 않기 위함.
    - return_code가 0이 아닌 업무 오류 응답도 그대로 반환합니다(버리지 않음).
    """
    assert_mock_domain(base_url)

    if api_id not in ALLOWED_API_IDS:
        raise DisallowedApiId(
            f"probe는 다음 TR만 허용합니다: {sorted(ALLOWED_API_IDS)} "
            f"(받은 값: {api_id!r})"
        )

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {access_token}",
        "cont-yn": cont_yn,
        "next-key": next_key,
        "api-id": api_id,
    }

    try:
        response = session.post(
            f"{base_url}{ENDPOINT}", headers=headers, json=payload, timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, {}, {"probe_transport_error": type(exc).__name__}

    response_headers = {
        key: response.headers.get(key, "") for key in ALLOWED_RESPONSE_HEADER_KEYS
    }

    try:
        body = response.json()
    except ValueError:
        # JSON이 아닌 응답(에러 페이지 등)도 버리지 않되, 원문을 그대로
        # 담지는 않습니다 — 무엇이 반환될지 보장이 없는 텍스트라 안전하게
        # 앞부분만 미리보기로 남깁니다.
        raw_text = getattr(response, "text", "")
        body = {"probe_raw_text_preview": str(raw_text)[:500]}

    body = redact_sensitive(body)

    return response.status_code, response_headers, body


def fetch_paginated(
    session: Any,
    base_url: str,
    access_token: str,
    api_id: str,
    payload: dict[str, Any],
    on_page: Any,
    max_pages: int = MAX_CONTINUATION_PAGES,
) -> None:
    """cont-yn/next-key를 따라가며 모든 페이지를 원시 그대로, 순서대로 조회합니다.

    페이지마다 `on_page(page_index, request_cont_yn, request_next_key,
    http_status, response_headers, response_body)`를 정확히 1회씩
    호출합니다 — 상태를 해석하지 않고 그대로 전달만 합니다.

    - 응답 `cont-yn`이 "Y"이고 `next-key`가 있으면 다음 페이지를 이어서
      조회합니다. `cont-yn != "Y"`거나 `next-key`가 비어있으면 정상
      종료(더 이상 페이지 없음)입니다.
    - 전송 실패(http_status=None)면 그 페이지를 마지막으로 멈춥니다 —
      실패 이후 페이지를 계속 시도해도 신뢰할 수 있는 데이터가 아니고,
      실패 사실 자체는 이미 그 페이지의 레코드에 명시돼 있습니다.
    - `max_pages`에 도달했는데 마지막 응답이 아직 `cont-yn=Y`라면(더
      볼 데이터가 있다는 뜻), 추가 HTTP 호출 없이 `on_page`를 한 번 더
      호출해 "여기서 캡 때문에 멈췄다"는 사실을 명시하는 synthetic
      레코드(http_status=None, response_body에
      `probe_page_cap_exceeded=True`)를 남깁니다 — 조용히 끊지 않고
      침묵 없이 중단하기 위함입니다. 무한 루프 방지가 목적이며 실제
      네트워크 호출 횟수는 항상 max_pages 이하입니다.
    """
    cont_yn = "N"
    next_key = ""
    page_index = 1

    while True:
        http_status, response_headers, response_body = call_kiwoom_api(
            session, base_url, access_token, api_id, payload,
            cont_yn=cont_yn, next_key=next_key,
        )
        on_page(page_index, cont_yn, next_key, http_status, response_headers, response_body)

        if http_status is None:
            break  # 전송 실패 — 이미 이 레코드로 명시됐으므로 더 진행하지 않음

        resp_cont_yn = response_headers.get("cont-yn", "")
        resp_next_key = response_headers.get("next-key", "")
        if resp_cont_yn != "Y" or not resp_next_key:
            break  # 정상 종료 — 다음 페이지 없음

        if page_index >= max_pages:
            on_page(
                page_index + 1, resp_cont_yn, resp_next_key,
                None, {},
                {
                    "probe_page_cap_exceeded": True,
                    "max_continuation_pages": max_pages,
                },
            )
            break

        page_index += 1
        cont_yn = resp_cont_yn
        next_key = resp_next_key


@dataclass
class ProbeContext:
    symbol: str
    order_id: str
    side: str
    requested_quantity: int | None
    scenario: str
    environment: str


def build_record(
    ctx: ProbeContext,
    run_id: str,
    api_id: str,
    page_index: int,
    request_cont_yn: str,
    request_next_key: str,
    payload: dict[str, Any],
    elapsed_ms: int,
    http_status: int | None,
    response_headers: dict[str, str],
    response_body: Any,
    captured_at: datetime,
) -> dict[str, Any]:
    """JSONL 레코드 하나를 만듭니다(민우님 확정 사양 + pagination 메타데이터).

    `page_index`/`request_cont_yn`/`request_next_key`는 GPT 재검토
    (2026-08-14)에서 추가된 연속조회(pagination) protocol metadata —
    이 레코드가 어떤 페이지 요청이었는지 JSONL만 보고 알 수 있게 합니다.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scenario": ctx.scenario,
        "captured_at_kst": captured_at.astimezone(KST).isoformat(),
        "elapsed_ms": elapsed_ms,
        "environment": ctx.environment,
        "symbol": ctx.symbol,
        "side": ctx.side,
        "order_id": ctx.order_id,
        "requested_quantity": ctx.requested_quantity,
        "api_id": api_id,
        "page_index": page_index,
        "request_cont_yn": request_cont_yn,
        "request_next_key": request_next_key,
        "request_body": payload,
        "http_status": http_status,
        "response_headers": response_headers,
        "response_body": response_body,
    }


def default_out_path(run_id: str) -> Path:
    return Path("diagnostics") / "order_reconciliation_probe" / f"{run_id}.jsonl"


def load_dotenv(path: str = ".env") -> None:
    """`.env`를 읽어 `os.environ`에 채웁니다(이미 설정된 값은 덮어쓰지 않음).

    `app/main.py`의 동명 함수와 동일한 동작입니다. `config/settings.yaml`은
    `app_key: ${KIWOOM_APP_KEY}` 식으로 실제 값을 환경변수 치환에
    의존하는데(`config/settings.py`의 `_substitute_env`), 이 치환은
    환경변수가 없으면 **예외 없이 빈 문자열로 조용히 대체**합니다. `app/
    main.py`는 실행 전에 자체 `load_dotenv()`를 호출해서 이 문제를 피하고
    있었는데, 이 프로브는 그 호출이 빠져 있었습니다(2026-08-14, 실측
    캡처 중 인증 실패로 발견) — 셸에 `KIWOOM_APP_KEY`/`KIWOOM_SECRET_KEY`
    가 전역으로 설정돼 있지 않으면 `load_settings()`가 빈 문자열로 채운
    App Key/Secret Key를 그대로 브로커에 넘겨, 키움이 정확히
    "App Key와 Secret Key 검증에 실패했습니다"로 거부합니다 — 계정/키
    등록 문제처럼 보이지만 실제로는 이 스크립트가 `.env`를 안 읽어서
    생기는 문제였습니다.
    """
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "키움 ka10075(미체결요청)/ka10076(체결요청) read-only 진단 프로브 "
            "(1P0.8-B.1). place_order/cancel_order는 호출하지 않습니다 — "
            "주문은 반드시 기존 프로그램이나 HTS에서 먼저 넣고, 그 order_id를 "
            "여기에 전달하세요."
        )
    )
    parser.add_argument("--symbol", required=True, help="종목코드 6자리 (예: 005930)")
    parser.add_argument(
        "--order-id", required=True,
        help="이미 접수된 실제 주문번호(ord_no). PSM의 'pending'/'UNKNOWN_ORDER_ID' "
             "같은 placeholder는 거부됩니다.",
    )
    parser.add_argument("--side", default="", help="라벨용(BUY/SELL 등, 선택)")
    parser.add_argument(
        "--requested-quantity", type=int, default=None, help="라벨용 요청 수량(선택)",
    )
    parser.add_argument(
        "--scenario", default="", help="라벨용 시나리오명(예: market_buy_full_fill)",
    )
    parser.add_argument(
        "--intervals", default="0,1,3,10",
        help="캡처 시점(초, T+0 기준 콤마 구분). 기본: 0,1,3,10",
    )
    parser.add_argument(
        "--out", default=None,
        help="출력 JSONL 경로(기본: diagnostics/order_reconciliation_probe/<run_id>.jsonl)",
    )
    parser.add_argument(
        "--settings", default="config/settings.yaml", help="settings.yaml 경로",
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="app_key/secret_key 등 환경변수를 채울 .env 경로(기본: .env). "
             "app/main.py와 동일하게, load_settings() 전에 이 파일을 먼저 읽습니다.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=MAX_CONTINUATION_PAGES,
        help=f"연속조회(cont-yn=Y) 최대 페이지 수(무한 루프 방지, 기본 {MAX_CONTINUATION_PAGES})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        order_id = validate_order_id(args.order_id)
        intervals = parse_intervals(args.intervals)
    except ProbeConfigError as exc:
        print(f"[거부] {exc}", file=sys.stderr)
        return 1

    # config.settings/infra.broker.kiwoom_broker는 여기서만 import합니다 —
    # 순수 검증 함수들을 이 모듈 밖에서(테스트 등) 네트워크/설정 파일
    # 의존성 없이 가져다 쓸 수 있게 하기 위함입니다.
    from config.settings import load_settings
    from infra.broker.kiwoom_broker import KiwoomBroker

    # app/main.py와 동일하게, settings.yaml의 ${KIWOOM_APP_KEY} 같은
    # 환경변수 치환이 실제 값을 찾을 수 있도록 .env를 먼저 읽습니다.
    # 이 호출이 없으면 _substitute_env()가 예외 없이 빈 문자열로 채우고,
    # 키움은 "App Key와 Secret Key 검증에 실패했습니다"로 거부합니다.
    load_dotenv(args.env_file)

    settings = load_settings(args.settings)

    try:
        assert_mock_domain(settings.broker.base_url)
    except ProbeConfigError as exc:
        print(f"[거부] {exc}", file=sys.stderr)
        return 1

    broker = KiwoomBroker(settings.broker)
    print(f"[정보] 모의투자 도메인 확인됨: {settings.broker.base_url}")
    print("[정보] 토큰 발급 중...")
    broker.authenticate()
    print("[정보] 토큰 발급 완료")

    ctx = ProbeContext(
        symbol=args.symbol,
        order_id=order_id,
        side=args.side,
        requested_quantity=args.requested_quantity,
        scenario=args.scenario,
        environment="mock",
    )

    start_wall = datetime.now(KST)
    run_id = f"{start_wall:%Y%m%d_%H%M%S}_{args.symbol}"
    out_path = Path(args.out) if args.out else default_out_path(run_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[정보] run_id={run_id}")
    print(f"[정보] 캡처 시점(초): {intervals}")
    print(f"[정보] 출력 파일: {out_path}")

    monotonic_start = time.monotonic()

    with out_path.open("a", encoding="utf-8") as fp:
        def _write_page(api_id, payload):
            def _on_page(page_index, request_cont_yn, request_next_key,
                         http_status, response_headers, response_body):
                captured_at = datetime.now(KST)
                elapsed_ms = int(round((time.monotonic() - monotonic_start) * 1000))
                record = build_record(
                    ctx, run_id, api_id, page_index, request_cont_yn, request_next_key,
                    payload, elapsed_ms, http_status, response_headers, response_body,
                    captured_at,
                )
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                fp.flush()
                if response_body.get("probe_page_cap_exceeded") if isinstance(response_body, dict) else False:
                    print(
                        f"[{elapsed_ms:>6}ms] {api_id} page={page_index} "
                        f"[경고] max_pages({args.max_pages}) 도달 — 추가 페이지 조회를 중단합니다"
                    )
                else:
                    return_code = response_body.get("return_code") if isinstance(response_body, dict) else "?"
                    print(
                        f"[{elapsed_ms:>6}ms] {api_id} page={page_index} "
                        f"cont-yn={request_cont_yn} http={http_status} return_code={return_code}"
                    )
            return _on_page

        for interval_sec in intervals:
            target = monotonic_start + interval_sec
            now = time.monotonic()
            if target > now:
                time.sleep(target - now)

            for api_id, payload in (
                ("ka10075", build_ka10075_payload(args.symbol)),
                ("ka10076", build_ka10076_payload(args.symbol)),
            ):
                fetch_paginated(
                    broker.session, broker.config.base_url, broker.access_token,
                    api_id, payload, _write_page(api_id, payload),
                    max_pages=args.max_pages,
                )

    print(f"[완료] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
