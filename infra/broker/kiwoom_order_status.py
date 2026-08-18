from __future__ import annotations

"""ka10075(미체결조회)/ka10076(체결조회) 원본 응답으로부터
`BrokerOrder`를 만드는 순수 함수 모음입니다.

1P0.8-B.1(read-only 진단 프로브)로 확보한 실측 캡처 4건
(`tests/fixtures/order_reconciliation/`, README 참고)을 근거로
설계했습니다 — 이 모듈의 판정 로직은 "실측으로 확인된 시그니처"만
반영하고, 확인되지 않은 조합(대표적으로 부분체결)은 의도적으로
`BrokerOrderStatus.UNKNOWN`으로 fail-close합니다. 자세한 배경은
`domain/models.py`의 `BrokerOrderStatus` docstring과
`CHANGELOG_v1.6.md`의 "1P0.8-B.1 실측 2차/3차" 섹션을 참고하세요.

아직 `Broker`/`KiwoomBroker` 인터페이스에는 연결하지 않았습니다
(1P0.8-C에서 `get_order_status()`/`get_open_orders()`로 연결 예정).
이 파일은 그 전 단계 — raw dict를 받아 `BrokerOrder`를 반환하는
순수 함수라 실제 API 호출 없이도 테스트할 수 있습니다.

2026-08-18 (GPT 리뷰 반영, dependency cleanup): 숫자 파싱은
`infra/broker/kiwoom_parsing.parse_abs_int()`(의존성 없는 공용
helper)만 사용합니다 — 예전에는 `KiwoomBroker._parse_abs_int()`를
직접 import해서 재사용했는데, 이 모듈이 스스로 선언한 "순수 함수"
성격과도 맞지 않고, 1P0.8-C에서 `KiwoomBroker`가 이 모듈을
import하게 되면(`get_order_status()` 구현을 위해) 순환 import가
되는 문제가 있었습니다. 이제 이 모듈은 `KiwoomBroker`를 전혀
import하지 않습니다.
"""

import math
from typing import Any, Iterable

from domain.models import BrokerOrder, BrokerOrderStatus, OrderSide
from infra.broker.kiwoom_parsing import parse_abs_int


def normalize_order_id(order_id: Any) -> str:
    """키움 주문번호 비교용 정규화.

    2026-08-14 (1P0.8-B.1 실측 2차 분석): `ka10076` 응답의 `ord_no`는
    7자리 0-padding(`"0157897"`)으로 내려오는데, 우리가 주문 시점에
    받는 `order_id`(예: `"157897"`)나 사용자가 입력하는 값은 그
    padding이 없을 수 있습니다. `zfill()`로 자릿수를 맞추는 대신
    **양쪽 다 앞자리 0을 제거해서 비교**합니다 — 자릿수 가정을 코드에
    박아넣지 않기 위함입니다(자릿수가 언젠가 바뀌어도 이 비교는
    깨지지 않음).

    2026-08-18 (GPT 리뷰 반영, B.2 강화): 빈 문자열이나 순수 0("0000000"
    등)은 **빈 문자열을 그대로 반환**합니다 — 예전에는 `"0"` 하나로
    합쳐서 취급했는데, 그러면 `entry.get("ord_no", "")`처럼 `ord_no`
    필드 자체가 없는 항목(정규화하면 역시 빈 문자열)과 우연히
    매칭될 위험이 있었습니다. 호출부(`derive_broker_order_status`)가
    정규화 결과가 빈 문자열이면 곧바로 UNKNOWN으로 fail-close하고
    매칭 자체를 시도하지 않도록 처리합니다.
    """

    raw = str(order_id or "").strip()
    if not raw:
        return ""
    return raw.lstrip("0")


def _find_matching(entries: Iterable[dict], target_normalized: str) -> dict | None:
    if not target_normalized:
        # 빈/무효 order_id는 어떤 항목과도 매칭시키지 않음 — 호출부에서
        # 이미 걸러내지만(derive_broker_order_status 시작부), 이 함수
        # 단독으로도 안전하도록 동일하게 방어.
        return None
    for entry in entries:
        candidate = normalize_order_id(entry.get("ord_no", ""))
        if candidate and candidate == target_normalized:
            return entry
    return None


def _parse_side(io_tp_nm: Any) -> OrderSide | None:
    """`io_tp_nm` 필드("+매수"/"-매도" 부호 접두사 컨벤션, 1P0.8-B.1
    실측 2차에서 확인)에서 매매 방향을 파싱합니다. 알 수 없는 값은
    `None`(모르면 채우지 않음 — 0/기본값으로 위장하지 않음)."""

    text = str(io_tp_nm or "")
    if "매수" in text:
        return OrderSide.BUY
    if "매도" in text:
        return OrderSide.SELL
    return None


def _parse_optional_qty(value: Any) -> int | None:
    """수량/가격 필드를 선택적 정수로 파싱합니다.

    2026-08-18 (GPT 리뷰 반영): `kiwoom_parsing.parse_abs_int()`는
    범용 helper라 파싱 불가 입력을 예외 없이 `0`으로 fail-close
    합니다(이 파일 밖의 기존 호출부들과 동작을 맞추기 위한 설계).
    하지만 이 함수(`BrokerOrder`용) 입장에서는 "실제로 0"과 "값을
    이해하지 못함"을 구분해야 합니다 — 구분하지 못하면 수량
    signature 비교(`derive_broker_order_status()`의 `open_qty == 0`
    같은 조건)가 malformed 입력을 우연히 통과시켜 잘못된 OPEN/
    FILLED로 이어질 위험이 있습니다. 그래서 먼저 `float()`로
    숫자로 해석 가능한지만 직접 확인하고(파싱 성공 여부 자체를
    얻기 위함), 해석 가능할 때만 `parse_abs_int()`로 최종 절대값
    변환을 위임합니다(부호 접두사 등 공용 규칙 재사용, 로직
    중복 없음).

    2026-08-18 (GPT 리뷰 반영, closure 2차): `float()`은
    `"nan"`/`"inf"`/`"-inf"`뿐 아니라 `"1e309"`처럼 float 표현
    범위를 넘어서는 값도 예외를 던지지 않고 `inf`로 통과시킵니다.
    이런 값을 그냥 `parse_abs_int()`에 넘기면(정수 변환 불가 →
    범용 helper 규칙대로 `0`) "실제로 0"과 다시 구분이 안 되므로,
    `math.isfinite()`로 유한한 값인지 한 번 더 확인한 뒤에만
    최종 변환을 위임합니다.

    해석 불가능하거나(파싱 자체 실패) 유한하지 않으면(`nan`/`inf`)
    `None`을 반환해 signature 비교가
    항상 불일치하도록 만들고, 결과적으로 `UNKNOWN`으로
    fail-close됩니다 — 예외로 죽지 않습니다.
    """

    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        numeric = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric):
        # 2026-08-18 (GPT 리뷰 반영, closure): `float()`은 "nan"/
        # "inf"/"-inf"뿐 아니라 `"1e309"`처럼 float 표현 범위를
        # 넘어서는 값도 예외 없이 `inf`로 통과시킵니다. 이런 값은
        # `parse_abs_int()`에서 정수로 변환할 수 없어 그냥 `0`으로
        # fail-close되는데(범용 helper의 설계), 이 함수(`BrokerOrder`용)
        # 입장에서는 그 `0`이 "실제로 0"과 구분되지 않아 수량
        # signature를 우연히 통과시킬 위험이 있었습니다. 유한하지
        # 않은 값은 여기서 미리 걸러 `None`으로 fail-close합니다.
        return None
    return parse_abs_int(text)


def derive_broker_order_status(
    order_id: Any,
    symbol: str,
    oso_entries: Iterable[dict] | None,
    cntr_entries: Iterable[dict] | None,
) -> BrokerOrder:
    """ka10075 `oso` 배열 + ka10076 `cntr` 배열에서 `order_id` 하나에
    대한 `BrokerOrder`를 판정합니다.

    호출부는 두 응답의 `response_body["oso"]`/`response_body["cntr"]`를
    그대로 넘기면 됩니다(리스트가 아니거나 `None`이면 빈 리스트로
    취급 — 방어적으로 처리, 예외를 던지지 않습니다).

    판정 순서(실측 근거는 클래스/모듈 docstring 참고):
    1. order_id 정규화 결과가 빈 문자열(원본이 빈 값/전부 0)이면
       매칭을 아예 시도하지 않고 곧바로 UNKNOWN.
    2. `cntr`에 매칭되고, 그 항목이 **실측된 전량체결 signature**
       (`ord_stt == "체결"` AND `ord_qty == cntr_qty` AND
       `oso_qty == 0`, 세 필드 모두 파싱 성공)와 정확히 일치하면
       FILLED로 확정.
    3. `cntr`엔 없고 `oso`에 매칭되고, 그 항목이 **실측된 완전미체결
       signature** (`ord_stt == "접수"` AND `cntr_qty == 0` AND
       `oso_qty == ord_qty`, 세 필드 모두 파싱 성공)와 정확히
       일치하면 OPEN.
    4. 그 외 전부 UNKNOWN — `cntr`/`oso`에 매칭은 되지만 수량
       조합이 위 실측 signature와 다른 경우(부분체결/정정 등으로
       추정되나 미실측 — fail-close), 둘 다에 매칭이 없는 경우
       (취소되었거나, 조회 범위 밖이거나, order_id 자체가 잘못됐을
       수 있음 — 이 함수만으로는 구분 불가, 반환값에 "취소됨"이라는
       의미를 담지 않습니다).

    2026-08-18 (GPT 리뷰 반영, B.2 강화): 처음 구현은 `cntr`에 매칭만
    되면 무조건 FILLED, `oso`에서 `ord_stt == "접수"`이면 무조건
    OPEN이었습니다 — 이건 "부분체결은 미실측이라 UNKNOWN으로
    fail-close한다"는 이 모듈 자체의 원칙과 어긋났습니다(미래에
    부분체결이 `ord_stt="체결"`이면서 `cntr_qty < ord_qty`로 오거나,
    `ord_stt="접수"`이면서 `cntr_qty > 0`으로 올 가능성을 배제할
    실측 근거가 없었기 때문). 지금은 실제로 확보한 4건 fixture의
    수량 조합까지 정확히 일치할 때만 FILLED/OPEN을 반환합니다.
    """

    target = normalize_order_id(order_id)
    if not target:
        return BrokerOrder(
            order_id=str(order_id or ""),
            symbol=symbol,
            status=BrokerOrderStatus.UNKNOWN,
        )

    oso_list = list(oso_entries) if oso_entries else []
    cntr_list = list(cntr_entries) if cntr_entries else []

    cntr_match = _find_matching(cntr_list, target)
    if cntr_match is not None:
        ord_stt = cntr_match.get("ord_stt", "")
        requested_qty = _parse_optional_qty(cntr_match.get("ord_qty"))
        filled_qty = _parse_optional_qty(cntr_match.get("cntr_qty"))
        open_qty = _parse_optional_qty(cntr_match.get("oso_qty"))
        is_observed_full_fill = (
            ord_stt == "체결"
            and requested_qty is not None
            and filled_qty is not None
            and open_qty is not None
            and requested_qty == filled_qty
            and open_qty == 0
        )
        status = BrokerOrderStatus.FILLED if is_observed_full_fill else BrokerOrderStatus.UNKNOWN
        return BrokerOrder(
            order_id=str(order_id),
            symbol=symbol,
            status=status,
            side=_parse_side(cntr_match.get("io_tp_nm")),
            requested_quantity=requested_qty,
            filled_quantity=filled_qty,
            filled_price=_parse_optional_qty(cntr_match.get("cntr_pric")) if is_observed_full_fill else None,
            order_type_raw=cntr_match.get("trde_tp"),
            raw_oso_entry=None,
            raw_cntr_entry=dict(cntr_match),
        )

    oso_match = _find_matching(oso_list, target)
    if oso_match is not None:
        ord_stt = oso_match.get("ord_stt", "")
        requested_qty = _parse_optional_qty(oso_match.get("ord_qty"))
        open_qty = _parse_optional_qty(oso_match.get("oso_qty"))
        filled_qty = _parse_optional_qty(oso_match.get("cntr_qty"))
        is_observed_unfilled = (
            ord_stt == "접수"
            and requested_qty is not None
            and open_qty is not None
            and filled_qty is not None
            and filled_qty == 0
            and open_qty == requested_qty
        )
        status = BrokerOrderStatus.OPEN if is_observed_unfilled else BrokerOrderStatus.UNKNOWN
        return BrokerOrder(
            order_id=str(order_id),
            symbol=symbol,
            status=status,
            side=_parse_side(oso_match.get("io_tp_nm")),
            requested_quantity=requested_qty,
            open_quantity=open_qty,
            filled_quantity=filled_qty,
            order_type_raw=oso_match.get("trde_tp"),
            raw_oso_entry=dict(oso_match),
            raw_cntr_entry=None,
        )

    return BrokerOrder(
        order_id=str(order_id),
        symbol=symbol,
        status=BrokerOrderStatus.UNKNOWN,
    )
