from __future__ import annotations

"""1P0.8-B.2: BrokerOrder/BrokerOrderStatus 모델 + 판정 로직 테스트.

`infra/broker/kiwoom_order_status.py`의 `derive_broker_order_status()`를
두 갈래로 검증합니다:
1. `tests/fixtures/order_reconciliation/`의 실측 JSONL 4건(1P0.8-B.1
   실측 2차/3차로 확보) — 진짜 서버 응답으로 FILLED/OPEN/UNKNOWN
   판정이 실제와 맞는지 확인.
2. 실측으로는 아직 못 만든 조합(부분체결 유사 ord_stt, 잘못된
   order_id, 빈 응답 등)에 대한 synthetic 단위 테스트 — fail-closed
   동작(UNKNOWN으로 떨어지는지)과 예외를 던지지 않는지를 확인.
"""

import json
import pathlib
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


from domain.models import BrokerOrderStatus, OrderSide
from infra.broker.kiwoom_order_status import derive_broker_order_status, normalize_order_id
import infra.broker.kiwoom_order_status as _order_status_module

FIXTURE_DIR = pathlib.Path("tests/fixtures/order_reconciliation")


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def _poll_pairs(records: list[dict]) -> list[tuple[list[dict], list[dict]]]:
    """레코드를 (oso, cntr) 쌍의 리스트로 묶습니다.

    캡처 파일은 폴링 라운드마다 ka10075 레코드 1개 + ka10076 레코드
    1개가 순서대로 나란히 옵니다(원본 프로브의 기록 순서 그대로).
    """

    pairs = []
    oso = None
    for rec in records:
        if rec["api_id"] == "ka10075":
            oso = rec["response_body"].get("oso", [])
        elif rec["api_id"] == "ka10076":
            cntr = rec["response_body"].get("cntr", [])
            if oso is not None:
                pairs.append((oso, cntr))
                oso = None
    return pairs


# ── 1. 실측 fixture 검증 ─────────────────────────────────────────

# 1-1/1-2: 전량체결 fixture 2건 — 모든 폴링 라운드에서 FILLED
_full_fill_fixtures = [
    ("20260814_151548_005930_market_buy_full_fill.jsonl", "005930", "157897", 5, 273000),
    ("20260814_151909_009150_market_buy_full_fill.jsonl", "009150", "163276", 1, 1557000),
]
for filename, symbol, order_id, expected_qty, expected_price in _full_fill_fixtures:
    records = _load_jsonl(FIXTURE_DIR / filename)
    pairs = _poll_pairs(records)
    check(f"1-{filename}) 폴링 라운드가 4개 이상 존재", len(pairs) >= 4)
    for i, (oso, cntr) in enumerate(pairs):
        result = derive_broker_order_status(order_id, symbol, oso, cntr)
        check(f"1-{filename}) 라운드{i}: status=FILLED", result.status is BrokerOrderStatus.FILLED)
        check(f"1-{filename}) 라운드{i}: filled_quantity={expected_qty}", result.filled_quantity == expected_qty)
        check(f"1-{filename}) 라운드{i}: filled_price={expected_price}", result.filled_price == expected_price)
        check(f"1-{filename}) 라운드{i}: side=BUY", result.side is OrderSide.BUY)
        check(f"1-{filename}) 라운드{i}: raw_cntr_entry 보존됨", result.raw_cntr_entry is not None)

# 1-3: 진짜 미체결 fixture — 모든 폴링 라운드에서 OPEN
_unfilled_records = _load_jsonl(FIXTURE_DIR / "20260818_090527_005930_limit_buy_unfilled.jsonl")
_unfilled_pairs = _poll_pairs(_unfilled_records)
check("1-3) 미체결 fixture: 폴링 라운드 4개 이상", len(_unfilled_pairs) >= 4)
for i, (oso, cntr) in enumerate(_unfilled_pairs):
    result = derive_broker_order_status("13557", "005930", oso, cntr)
    check(f"1-3) 라운드{i}: status=OPEN", result.status is BrokerOrderStatus.OPEN)
    check(f"1-3) 라운드{i}: open_quantity=1", result.open_quantity == 1)
    check(f"1-3) 라운드{i}: filled_quantity=0", result.filled_quantity == 0)
    check(f"1-3) 라운드{i}: requested_quantity=1", result.requested_quantity == 1)
    check(f"1-3) 라운드{i}: order_type_raw='보통'(지정가)", result.order_type_raw == "보통")
    check(f"1-3) 라운드{i}: side=BUY", result.side is OrderSide.BUY)
    check(f"1-3) 라운드{i}: raw_oso_entry 보존됨", result.raw_oso_entry is not None)

# 1-4: 취소 후 재조회 fixture — 모든 폴링 라운드에서 UNKNOWN(취소를
# "확정"하지 않음 — 모듈 docstring 참고), 원본 두 배열 다 비어 있음
_cancel_records = _load_jsonl(FIXTURE_DIR / "20260818_090621_005930_limit_buy_after_cancel.jsonl")
_cancel_pairs = _poll_pairs(_cancel_records)
check("1-4) 취소 후 fixture: 폴링 라운드 4개 이상", len(_cancel_pairs) >= 4)
for i, (oso, cntr) in enumerate(_cancel_pairs):
    check(f"1-4) 라운드{i}: 원본 oso가 실제로 빈 배열", oso == [])
    check(f"1-4) 라운드{i}: 원본 cntr가 실제로 빈 배열", cntr == [])
    result = derive_broker_order_status("13557", "005930", oso, cntr)
    check(f"1-4) 라운드{i}: status=UNKNOWN(취소를 확정하지 않음)", result.status is BrokerOrderStatus.UNKNOWN)
    check(f"1-4) 라운드{i}: raw_oso_entry/raw_cntr_entry 둘 다 None", result.raw_oso_entry is None and result.raw_cntr_entry is None)


# ── 2. order_id 정규화(0-padding 무시 비교) ──────────────────────
check("2-1) '0013557' == '13557' 정규화 후 동일", normalize_order_id("0013557") == normalize_order_id("13557"))
check("2-2) '0157897' == '157897' 정규화 후 동일", normalize_order_id("0157897") == normalize_order_id("157897"))
check("2-3) 앞자리 0 전부 제거됨", normalize_order_id("0000123") == "123")
# 2026-08-18 (GPT 리뷰 반영): 전부 0/빈 문자열은 더 이상 "0" 하나로
# 합쳐 취급하지 않습니다 — ord_no 필드가 아예 없는 항목을
# normalize_order_id("")로 정규화한 값("")과 우연히 매칭될 수
# 있었기 때문입니다. 지금은 둘 다 빈 문자열이며, 빈 문자열은
# derive_broker_order_status()에서 매칭을 시도하기 전에 UNKNOWN으로
# 즉시 fail-close됩니다(아래 2-7/2-8).
check("2-4) 전부 0이면 빈 문자열(무효 처리)", normalize_order_id("0000000") == "")
check("2-5) 빈 문자열은 빈 문자열 그대로(무효 처리)", normalize_order_id("") == "")
check("2-6) 정규화 후 실제로 미체결 fixture에서 order_id 매칭됨(0-padding 무시)", (
    derive_broker_order_status("0013557", "005930", _unfilled_pairs[0][0], _unfilled_pairs[0][1]).status
    is BrokerOrderStatus.OPEN
))
check("2-7) order_id=''는 매칭 시도 없이 즉시 UNKNOWN(ord_no 없는 항목과 오매칭 방지)", (
    derive_broker_order_status("", "005930", [{}], []).status is BrokerOrderStatus.UNKNOWN
))
check("2-8) order_id='0000000'도 즉시 UNKNOWN(무효 order_id를 유효한 것처럼 취급하지 않음)", (
    derive_broker_order_status("0000000", "005930", [{"ord_no": ""}], []).status is BrokerOrderStatus.UNKNOWN
))


# ── 3. fail-closed 동작 — 실측 안 된 조합/방어적 입력 ────────────

# 3-1: oso에 있지만 ord_stt가 "접수"도 "체결"도 아닌 값(예: 부분체결로
# 추정되나 미실측) — UNKNOWN으로 fail-close, 원본 그대로 보존
_synthetic_oso_unclear = [{
    "ord_no": "0099999", "ord_stt": "일부체결(미실측 추정값)",
    "ord_qty": "10", "oso_qty": "4", "cntr_qty": "6", "trde_tp": "보통",
    "io_tp_nm": "+매수",
}]
_r3_1 = derive_broker_order_status("99999", "005930", _synthetic_oso_unclear, [])
check("3-1) 미실측 ord_stt는 UNKNOWN으로 fail-close(부분체결을 함부로 단정하지 않음)",
      _r3_1.status is BrokerOrderStatus.UNKNOWN)
check("3-1) fail-close해도 raw_oso_entry는 그대로 보존(디버깅 가능)", _r3_1.raw_oso_entry == _synthetic_oso_unclear[0])

# 3-1b/3-1c: 2026-08-18 (GPT 리뷰 반영) — 3-1보다 더 위험한 두 경우.
# ord_stt 자체는 "접수"/"체결"과 정확히 같지만, 수량 조합이 우리가
# 실측한 signature와 다른 "그럴듯한 부분체결" 모양입니다. 이건 예전
# 구현(ord_stt만 보고 판정)이라면 잘못 OPEN/FILLED로 판정했을
# 케이스입니다 — 지금은 수량까지 실측 signature와 정확히 일치해야만
# OPEN/FILLED를 내도록 강화했으므로 반드시 UNKNOWN이어야 합니다.

# 3-1b: oso에서 ord_stt="접수"인데 수량이 부분체결 모양(10 중 6체결/4미체결)
_synthetic_oso_partial_shaped = [{
    "ord_no": "0088888", "ord_stt": "접수",
    "ord_qty": "10", "oso_qty": "4", "cntr_qty": "6",
    "trde_tp": "보통", "io_tp_nm": "+매수",
}]
_r3_1b = derive_broker_order_status("88888", "005930", _synthetic_oso_partial_shaped, [])
check("3-1b) ord_stt='접수'라도 수량이 실측 완전미체결 signature와 다르면 UNKNOWN(OPEN 아님)",
      _r3_1b.status is BrokerOrderStatus.UNKNOWN)

# 3-1c: cntr에서 ord_stt="체결"인데 수량이 부분체결 모양(10 중 6체결/4미체결)
_synthetic_cntr_partial_shaped = [{
    "ord_no": "0088888", "ord_stt": "체결",
    "ord_qty": "10", "cntr_qty": "6", "oso_qty": "4", "cntr_pric": "1000",
    "trde_tp": "보통", "io_tp_nm": "+매수",
}]
_r3_1c = derive_broker_order_status("88888", "005930", [], _synthetic_cntr_partial_shaped)
check("3-1c) ord_stt='체결'이라도 수량이 실측 전량체결 signature와 다르면 UNKNOWN(FILLED 아님)",
      _r3_1c.status is BrokerOrderStatus.UNKNOWN)
check("3-1c) fail-close해도 raw_cntr_entry는 그대로 보존(디버깅 가능)",
      _r3_1c.raw_cntr_entry == _synthetic_cntr_partial_shaped[0])

# 3-2: order_id가 어디에도 없음(정상 UNKNOWN, 오류 아님)
_r3_2 = derive_broker_order_status("55555", "005930", [{"ord_no": "0011111", "ord_stt": "접수"}], [])
check("3-2) 매칭 안 되는 order_id는 UNKNOWN(예외 없이)", _r3_2.status is BrokerOrderStatus.UNKNOWN)

# 3-3: oso/cntr가 None이어도 예외 없이 UNKNOWN
try:
    _r3_3 = derive_broker_order_status("1", "005930", None, None)
    check("3-3) oso/cntr가 None이어도 예외 없이 처리됨", True)
    check("3-3) None 입력은 UNKNOWN", _r3_3.status is BrokerOrderStatus.UNKNOWN)
except Exception:
    check("3-3) oso/cntr가 None이어도 예외 없이 처리됨", False)

# 3-4: cntr 매칭이 oso 매칭보다 우선(둘 다 있는 극단적 상황 — 실측엔
# 없었지만 방어적으로 cntr을 신뢰해야 함, 모듈 docstring 판정 순서
# 참고). 2026-08-18 강화: cntr 쪽 수량도 실측 전량체결 signature
# (ord_qty==cntr_qty, oso_qty==0)를 정확히 만족해야 FILLED이므로
# oso_qty="0"을 명시.
_r3_4 = derive_broker_order_status(
    "77777", "005930",
    [{"ord_no": "0077777", "ord_stt": "접수", "ord_qty": "1", "oso_qty": "1", "cntr_qty": "0"}],
    [{"ord_no": "0077777", "ord_stt": "체결", "ord_qty": "1", "cntr_qty": "1", "oso_qty": "0", "cntr_pric": "1000", "io_tp_nm": "+매수"}],
)
check("3-4) oso/cntr 둘 다 매칭되면 cntr(체결이력)이 우선 — FILLED", _r3_4.status is BrokerOrderStatus.FILLED)

# 3-5: 필드가 아예 없는 빈 dict 항목이어도 예외 없이 처리
try:
    _r3_5 = derive_broker_order_status("1", "005930", [{}], [])
    check("3-5) 빈 dict 항목도 예외 없이 처리됨(order_id 매칭 안 돼 UNKNOWN)", _r3_5.status is BrokerOrderStatus.UNKNOWN)
except Exception:
    check("3-5) 빈 dict 항목도 예외 없이 처리됨", False)


# ── 4. dependency cleanup(2026-08-18, GPT 리뷰 반영) ──────────────
# 1P0.8-C에서 KiwoomBroker가 이 모듈(kiwoom_order_status.py)을
# import해 get_order_status()를 구현하게 됩니다. 그때 이 모듈이
# 거꾸로 KiwoomBroker를 import하고 있으면 순환 import가 됩니다.
# 이 모듈은 "raw dict → BrokerOrder 순수 함수"라고 스스로 선언한
# 성격과도 맞지 않았으므로, KiwoomBroker 의존을 완전히 제거하고
# infra/broker/kiwoom_parsing.py(의존성 없는 공용 helper)만 쓰도록
# 고쳤습니다. 아래는 그 의존성 제거가 실제로 됐는지 소스 검사로
# 확인합니다.
import inspect

_order_status_source = inspect.getsource(_order_status_module)
check("4-1) kiwoom_order_status.py가 infra.broker.kiwoom_broker를 import하지 않음(순환 import 방지)",
      "import infra.broker.kiwoom_broker" not in _order_status_source
      and "from infra.broker.kiwoom_broker" not in _order_status_source)
# AST로 검사 — docstring/주석 안의 "KiwoomBroker" 언급(설명 목적)은
# 허용하고, 실제 코드에서 이름으로 참조하는 경우만 검출합니다
# (문자열 substring 검사로는 docstring 설명 문구까지 걸려 오탐 발생).
import ast

_order_status_ast = ast.parse(_order_status_source)
_kiwoom_broker_name_refs = [
    node for node in ast.walk(_order_status_ast)
    if isinstance(node, ast.Name) and node.id == "KiwoomBroker"
]
check("4-2) kiwoom_order_status.py에 KiwoomBroker를 실제로 사용하는 코드가 없음(설명용 docstring 언급은 허용)",
      len(_kiwoom_broker_name_refs) == 0)
check("4-3) kiwoom_order_status.py가 kiwoom_parsing.parse_abs_int를 사용함",
      "from infra.broker.kiwoom_parsing import parse_abs_int" in _order_status_source)
check("4-4) 모듈이 실제로 정상 import/실행 가능함(순환 import였다면 여기까지 오지 못했을 것)", True)

# ── 5. malformed 수량 필드 → 예외 없이 UNKNOWN(fail-close) ────────
# GPT 리뷰 지적: _parse_optional_qty()가 내부적으로 파싱 불가능한
# 값을 만나도 예외를 던지지 않고 None으로 fail-close해야, 수량
# signature 비교가 자연히 불일치해 UNKNOWN으로 떨어집니다.

# 5-1: oso 매칭 항목의 ord_qty가 숫자로 해석 불가능한 문자열
_synthetic_oso_malformed_qty = [{
    "ord_no": "0013557", "ord_stt": "접수",
    "ord_qty": "???", "oso_qty": "1", "cntr_qty": "0",
    "trde_tp": "보통", "io_tp_nm": "+매수",
}]
try:
    _r5_1 = derive_broker_order_status("13557", "005930", _synthetic_oso_malformed_qty, [])
    check("5-1) ord_qty가 비정상 문자열이어도 예외 없이 처리됨", True)
    check("5-1) 파싱 불가 수량은 signature 불충족 → UNKNOWN", _r5_1.status is BrokerOrderStatus.UNKNOWN)
    check("5-1) requested_quantity는 파싱 실패 시 None(0으로 위장하지 않음)", _r5_1.requested_quantity is None)
except Exception:
    check("5-1) ord_qty가 비정상 문자열이어도 예외 없이 처리됨", False)

# 5-2: cntr 매칭 항목의 cntr_qty가 숫자로 해석 불가능한 문자열
_synthetic_cntr_malformed_qty = [{
    "ord_no": "0013557", "ord_stt": "체결",
    "ord_qty": "1", "cntr_qty": "not-a-number", "oso_qty": "0",
    "cntr_pric": "1000", "io_tp_nm": "+매수",
}]
try:
    _r5_2 = derive_broker_order_status("13557", "005930", [], _synthetic_cntr_malformed_qty)
    check("5-2) cntr_qty가 비정상 문자열이어도 예외 없이 처리됨", True)
    check("5-2) 파싱 불가 수량은 signature 불충족 → UNKNOWN", _r5_2.status is BrokerOrderStatus.UNKNOWN)
except Exception:
    check("5-2) cntr_qty가 비정상 문자열이어도 예외 없이 처리됨", False)

# 5-3: 아예 리스트/dict가 아닌 이상한 타입이 값으로 들어와도 예외 없이 처리
try:
    _r5_3 = derive_broker_order_status("13557", "005930", [{"ord_no": "0013557", "ord_stt": "접수", "ord_qty": [1, 2, 3], "oso_qty": "1", "cntr_qty": "0"}], [])
    check("5-3) 필드값이 리스트처럼 완전히 엉뚱한 타입이어도 예외 없이 처리됨", True)
    check("5-3) UNKNOWN으로 fail-close", _r5_3.status is BrokerOrderStatus.UNKNOWN)
except Exception:
    check("5-3) 필드값이 리스트처럼 완전히 엉뚱한 타입이어도 예외 없이 처리됨", False)

# 5-5/5-6/5-7: 2026-08-18 (GPT 리뷰 반영, closure 2차) — float()은
# 예외 없이 nan/inf/overflow-to-inf를 통과시킵니다. 이 값들이
# math.isfinite() 검증 없이 parse_abs_int()로 그대로 넘어가면
# 정수 변환 실패로 "0"이 되어(실제로 0인지 몰라서 못 채웠는지
# 구분 불가), 예를 들어 아래처럼 실측 완전미체결 signature를
# 우연히 만족시켜 잘못 OPEN으로 판정될 위험이 있었습니다:
#   ord_qty=1, oso_qty=1, cntr_qty="nan", ord_stt="접수"
#   → (보강 전) cntr_qty가 0으로 위장 → OPEN 오판
#   → (보강 후) cntr_qty가 None → signature 불충족 → UNKNOWN
for _nonfinite_value, _label in [("nan", "nan"), ("inf", "inf"), ("1e309", "1e309(overflow-to-inf)")]:
    _synthetic_oso_nonfinite_cntr_qty = [{
        "ord_no": "0013557", "ord_stt": "접수",
        "ord_qty": "1", "oso_qty": "1", "cntr_qty": _nonfinite_value,
        "trde_tp": "보통", "io_tp_nm": "+매수",
    }]
    try:
        _r5_nf = derive_broker_order_status("13557", "005930", _synthetic_oso_nonfinite_cntr_qty, [])
        check(f"5-nf-cntr_qty={_label}) 예외 없이 처리됨", True)
        check(f"5-nf-cntr_qty={_label}) UNKNOWN으로 fail-close(OPEN 오판 방지)", _r5_nf.status is BrokerOrderStatus.UNKNOWN)
        check(f"5-nf-cntr_qty={_label}) filled_quantity는 0이 아니라 None(위장 방지)", _r5_nf.filled_quantity is None)
    except Exception:
        check(f"5-nf-cntr_qty={_label}) 예외 없이 처리됨", False)

# 5-8: oso_qty 쪽도 동일하게 확인(리뷰 권고 — "가능하면 oso_qty 쪽에도")
_synthetic_oso_nonfinite_oso_qty = [{
    "ord_no": "0013557", "ord_stt": "접수",
    "ord_qty": "1", "oso_qty": "nan", "cntr_qty": "0",
    "trde_tp": "보통", "io_tp_nm": "+매수",
}]
try:
    _r5_8 = derive_broker_order_status("13557", "005930", _synthetic_oso_nonfinite_oso_qty, [])
    check("5-8) oso_qty='nan'이어도 예외 없이 처리됨", True)
    check("5-8) UNKNOWN으로 fail-close(OPEN 오판 방지)", _r5_8.status is BrokerOrderStatus.UNKNOWN)
    check("5-8) open_quantity는 None(위장 방지)", _r5_8.open_quantity is None)
except Exception:
    check("5-8) oso_qty='nan'이어도 예외 없이 처리됨", False)

# 5-9: cntr 쪽(FILLED 판정 경로)도 동일하게 확인 — oso_qty="nan"이면
# 실측 전량체결 signature(oso_qty==0)를 만족할 수 없어야 함
_synthetic_cntr_nonfinite_oso_qty = [{
    "ord_no": "0013557", "ord_stt": "체결",
    "ord_qty": "1", "cntr_qty": "1", "oso_qty": "nan",
    "cntr_pric": "1000", "io_tp_nm": "+매수",
}]
try:
    _r5_9 = derive_broker_order_status("13557", "005930", [], _synthetic_cntr_nonfinite_oso_qty)
    check("5-9) cntr 경로에서 oso_qty='nan'이어도 예외 없이 처리됨", True)
    check("5-9) UNKNOWN으로 fail-close(FILLED 오판 방지)", _r5_9.status is BrokerOrderStatus.UNKNOWN)
except Exception:
    check("5-9) cntr 경로에서 oso_qty='nan'이어도 예외 없이 처리됨", False)

# 5-4(회귀 확인): 위 방어 로직을 추가한 뒤에도 정상 fixture 4건의
# 기존 판정은 그대로 유지되는지 재확인(1절에서 이미 검증했지만,
# "malformed 방어가 정상 케이스를 깨지 않았는지"를 이 절에서 한 번
# 더 명시적으로 확인)
_r5_4_open = derive_broker_order_status("13557", "005930", _unfilled_pairs[0][0], _unfilled_pairs[0][1])
check("5-4) malformed 방어 추가 후에도 정상 미체결 fixture는 여전히 OPEN", _r5_4_open.status is BrokerOrderStatus.OPEN)
_full_fill_records_check = _load_jsonl(FIXTURE_DIR / "20260814_151548_005930_market_buy_full_fill.jsonl")
_full_fill_pairs_check = _poll_pairs(_full_fill_records_check)
_r5_4_filled = derive_broker_order_status("157897", "005930", _full_fill_pairs_check[0][0], _full_fill_pairs_check[0][1])
check("5-4) malformed 방어 추가 후에도 정상 전량체결 fixture는 여전히 FILLED", _r5_4_filled.status is BrokerOrderStatus.FILLED)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
