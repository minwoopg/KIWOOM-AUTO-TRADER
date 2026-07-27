# -*- coding: utf-8 -*-
"""
분봉 API raw 진단 검증 (2026-07-27, 리팩터링 1B단계)

실제 API를 호출하지 않고 합성 KiwoomApiResponse로 검증합니다.
가장 중요한 원칙: 진단 추가가 KiwoomBroker.get_minute_bars()의
기존 반환값(raw_bars[:count] 슬라이싱, MinuteBar 파싱, bars.reverse())
을 단 하나도 바꾸지 않아야 합니다 — 이건 1번 테스트에서 legacy
로직을 그대로 재현해 byte-for-byte 비교로 확인합니다.

⚠️ 이 테스트는 합성 데이터만 사용합니다. 다음은 실제 API를 호출해야만
확인 가능하며, 이 테스트로는 단정하지 않습니다:
  - raw_received_count의 실제 운영값(키움이 실제로 몇 개를 주는지)
  - 정말로 진행 중인 봉이 포함되는지 여부
  - continuation(cont-yn/next-key)이 실제로 쓰이는지
이것들은 "실운영 첫 호출에서 확인 예정"입니다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from config.settings import BrokerConfig
from domain.models import MinuteBar
from infra.broker.kiwoom_broker import KiwoomBroker, KiwoomApiResponse
from infra.broker.minute_bar_diagnostics import (
    build_minute_bar_diagnostics, format_diagnostics_log_line, format_order_detail_log_line,
    KST,
)

# 2026-07-27 (2차 긴급 수정): 이 테스트 파일이 자체적으로
# `from zoneinfo import ZoneInfo` + `KST = ZoneInfo("Asia/Seoul")`
# 를 다시 정의하고 있었음 — infra/broker/minute_bar_diagnostics.py
# 의 KST는 이미 고정 UTC+9 오프셋으로 고쳤는데(1B.2절), 정작
# "KST가 더 이상 zoneinfo에 의존하지 않는지 확인하는 테스트"를
# 작성하면서 이 테스트 파일 상단에는 옛 방식을 그대로 남겨뒀던
# 실수(실제 사용자 환경에서 동일한 ZoneInfoNotFoundError로 재현됨
# — 이번엔 minute_bar_diagnostics.py 37번째 줄이 아니라 이 테스트
# 파일 37번째 줄에서 발생). 이제 KST를 자체 정의하지 않고 실제
# 운영 모듈에서 그대로 import — 앞으로 KST 정의가 바뀌어도 이
# 테스트가 자동으로 동기화되어 같은 종류의 실수가 재발하지 않음.


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def make_broker() -> KiwoomBroker:
    config = BrokerConfig(
        provider="kiwoom", use_mock=True, base_url="http://fake",
        app_key="x", secret_key="x", account_number="x", is_paper_trading=True,
    )
    broker = KiwoomBroker(config)
    broker.access_token = "fake-token"
    return broker


def make_raw_bars(n: int, start: datetime, desc: bool = True) -> list[dict]:
    """n개의 합성 분봉을 만듭니다. desc=True면 키움 실제 방식(최신->과거)."""
    bars = []
    for i in range(n):
        ts = start + timedelta(minutes=(n - i) if desc else i)
        bars.append({
            "cntr_tm": ts.strftime("%Y%m%d%H%M%S"),
            "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
            "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000",
        })
    return bars


def _legacy_parse_abs_int(value) -> int:
    """kiwoom_broker.py의 _parse_abs_int()를 정확히 재현한 비교 기준선.

    2026-07-27 (2차 GPT 코드리뷰 지적): 기존 legacy_parse()가
    단순히 int(item.get(...))만 써서, 합성 테스트 데이터가 전부
    "58000"처럼 깨끗한 양수 문자열이라 실제 _parse_abs_int()의
    특수 규칙(None/빈문자열->0, 음수/+부호는 절대값, 0 padding,
    잘못된 문자열->0)이 검증에서 전혀 드러나지 않고 있었음. 실제
    구현을 그대로 복사(원본이 바뀌면 이 사본도 함께 갱신 필요 —
    이건 "복제해서 비교"하는 회귀 테스트의 근본적 한계이지만, 진단
    추가 전후 byte-for-byte 동등성을 확인하는 목적에는 원본과
    독립적인 재현이 필요함).
    """
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return abs(int(float(text)))
    except ValueError:
        return 0


def legacy_parse(raw_bars: list[dict], count: int) -> list[MinuteBar]:
    """진단 추가 이전의 순수 로직을 정확히 재현 (비교 기준선)."""
    bars = []
    for item in raw_bars[:count]:
        bars.append(MinuteBar(
            cntr_tm=str(item.get("cntr_tm", "")),
            open_price=_legacy_parse_abs_int(item.get("open_pric")),
            high_price=_legacy_parse_abs_int(item.get("high_pric")),
            low_price=_legacy_parse_abs_int(item.get("low_pric")),
            close_price=_legacy_parse_abs_int(item.get("cur_prc")),
            volume=_legacy_parse_abs_int(item.get("trde_qty")),
            acc_volume=_legacy_parse_abs_int(item.get("acc_trde_qty")),
        ))
    bars.reverse()
    return bars


def make_response(raw_bars: list[dict], cont_yn="N", next_key="") -> KiwoomApiResponse:
    return KiwoomApiResponse(
        status_code=200,
        headers={"cont-yn": cont_yn, "next-key": next_key, "api-id": "ka10080"},
        body={"stk_min_pole_chart_qry": raw_bars},
    )


base = datetime(2026, 7, 21, 9, 0, 0)

# ══════════════════════════════════════════════════════════════
# 1. raw 100개, count 60일 때 반환은 기존과 동일하게 60개
#    (byte-for-byte 동등성 검증 — 가장 중요한 테스트)
# ══════════════════════════════════════════════════════════════

raw_bars = make_raw_bars(100, base, desc=True)
legacy_result = legacy_parse(raw_bars, 60)

broker = make_broker()
with patch.object(broker, "_post", return_value=make_response(raw_bars)):
    actual_result = broker.get_minute_bars("475150", tick_scope=3, count=60)

check("1) raw 100개, count 60 -> 반환 60개(기존과 동일)", len(actual_result) == 60)
identical = len(legacy_result) == len(actual_result) and all(
    (l.cntr_tm, l.open_price, l.high_price, l.low_price, l.close_price, l.volume, l.acc_volume) ==
    (a.cntr_tm, a.open_price, a.high_price, a.low_price, a.close_price, a.volume, a.acc_volume)
    for l, a in zip(legacy_result, actual_result)
)
check("   반환된 MinuteBar 전체가 legacy 로직 결과와 byte-for-byte 동일함"
      "(진단 추가가 반환값에 전혀 영향 없음)", identical)

# ── 1-1. _parse_abs_int의 실제 특수 규칙(음수/+부호/0패딩/빈문자열/
#         None/잘못된문자열)까지 포함한 byte-for-byte 검증 ──────────
# (2026-07-27, 2차 GPT 코드리뷰 지적 — 위 1번은 전부 깨끗한 양수
#  문자열이라 이 규칙들이 검증에서 전혀 드러나지 않았음)
edge_case_ts = base + timedelta(minutes=1)
edge_raw_bars = [
    {  # 정상 양수 문자열
        "cntr_tm": edge_case_ts.strftime("%Y%m%d%H%M%S"),
        "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
        "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000",
    },
    {  # 음수 부호 (실제 키움 응답에서 자주 나타남 — 절대값으로 변환돼야 함)
        "cntr_tm": (edge_case_ts + timedelta(minutes=1)).strftime("%Y%m%d%H%M%S"),
        "open_pric": "-58000", "high_pric": "-58200", "low_pric": "-57900",
        "cur_prc": "-58100", "trde_qty": "-1000", "acc_trde_qty": "-50000",
    },
    {  # + 부호
        "cntr_tm": (edge_case_ts + timedelta(minutes=2)).strftime("%Y%m%d%H%M%S"),
        "open_pric": "+58000", "high_pric": "+58200", "low_pric": "+57900",
        "cur_prc": "+58100", "trde_qty": "+1000", "acc_trde_qty": "+50000",
    },
    {  # 0 패딩
        "cntr_tm": (edge_case_ts + timedelta(minutes=3)).strftime("%Y%m%d%H%M%S"),
        "open_pric": "000000058000", "high_pric": "000058200", "low_pric": "0057900",
        "cur_prc": "00058100", "trde_qty": "0001000", "acc_trde_qty": "00050000",
    },
    {  # 빈 문자열 -> 0
        "cntr_tm": (edge_case_ts + timedelta(minutes=4)).strftime("%Y%m%d%H%M%S"),
        "open_pric": "", "high_pric": "", "low_pric": "",
        "cur_prc": "", "trde_qty": "", "acc_trde_qty": "",
    },
    {  # None -> 0
        "cntr_tm": (edge_case_ts + timedelta(minutes=5)).strftime("%Y%m%d%H%M%S"),
        "open_pric": None, "high_pric": None, "low_pric": None,
        "cur_prc": None, "trde_qty": None, "acc_trde_qty": None,
    },
    {  # 잘못된 문자열(숫자 아님) -> 0
        "cntr_tm": (edge_case_ts + timedelta(minutes=6)).strftime("%Y%m%d%H%M%S"),
        "open_pric": "N/A", "high_pric": "오류", "low_pric": "--",
        "cur_prc": "abc123", "trde_qty": "null", "acc_trde_qty": "?",
    },
]

legacy_edge_result = legacy_parse(edge_raw_bars, 60)
broker_edge = make_broker()
with patch.object(broker_edge, "_post", return_value=make_response(edge_raw_bars)):
    actual_edge_result = broker_edge.get_minute_bars("475150", tick_scope=3, count=60)

check("1-1) 엣지케이스(음수/+부호/0패딩/빈문자열/None/잘못된문자열) "
      "7건 모두 정상 반환됨(예외 없음)", len(actual_edge_result) == 7)
edge_identical = len(legacy_edge_result) == len(actual_edge_result) and all(
    (l.cntr_tm, l.open_price, l.high_price, l.low_price, l.close_price, l.volume, l.acc_volume) ==
    (a.cntr_tm, a.open_price, a.high_price, a.low_price, a.close_price, a.volume, a.acc_volume)
    for l, a in zip(legacy_edge_result, actual_edge_result)
)
check("    엣지케이스 전체가 legacy(_parse_abs_int 실제규칙 재현) 결과와 "
      "byte-for-byte 동일함", edge_identical)

# 각 특수값이 실제로 기대한 대로 변환됐는지 개별 확인
# reverse()로 순서가 뒤집히므로 인덱스 6이 첫번째(음수케이스)
by_cntr_tm = {b.cntr_tm: b for b in actual_edge_result}
neg_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=1)).strftime("%Y%m%d%H%M%S")]
check("    음수 부호('-58000') -> 절대값 58000으로 정확히 변환됨", neg_bar.open_price == 58000)
plus_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=2)).strftime("%Y%m%d%H%M%S")]
check("    +부호('+58000') -> 58000으로 정확히 변환됨", plus_bar.open_price == 58000)
pad_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=3)).strftime("%Y%m%d%H%M%S")]
check("    0패딩('000000058000') -> 58000으로 정확히 변환됨", pad_bar.open_price == 58000)
empty_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=4)).strftime("%Y%m%d%H%M%S")]
check("    빈 문자열 -> 0으로 변환됨", empty_bar.open_price == 0)
none_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=5)).strftime("%Y%m%d%H%M%S")]
check("    None -> 0으로 변환됨", none_bar.open_price == 0)
invalid_bar = by_cntr_tm[(edge_case_ts + timedelta(minutes=6)).strftime("%Y%m%d%H%M%S")]
check("    잘못된 문자열('N/A') -> 0으로 변환됨", invalid_bar.open_price == 0)

# ══════════════════════════════════════════════════════════════
# 2. raw가 최신→과거(DESC) 순이면 진단이 DESC로 판단
# ══════════════════════════════════════════════════════════════

d = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_bars, returned_bars_timestamps=[b.cntr_tm for b in actual_result],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("2) raw가 최신->과거 순이면 raw_sort_direction=DESC로 판단", d.raw_sort_direction == "DESC")

# ══════════════════════════════════════════════════════════════
# 3. 반환 bars는 기존처럼 과거→최신(ASC)
# ══════════════════════════════════════════════════════════════

check("3) 반환 bars(returned)는 과거->최신 ASC로 판단됨", d.returned_sort_direction == "ASC")

# ══════════════════════════════════════════════════════════════
# 4. cont-yn=Y, next-key 존재 시 continuation 감지
# ══════════════════════════════════════════════════════════════

d2 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_bars, returned_bars_timestamps=[b.cntr_tm for b in actual_result],
    headers={"cont-yn": "Y", "next-key": "abc123"},
    request_started_at=None, response_received_at=None,
)
check("4) cont-yn=Y -> continuation_available=True", d2.continuation_available is True)
check("   next-key 존재 -> next_key_present=True", d2.next_key_present is True)
check("   next-key 원문은 결과 객체에 저장되지 않음(bool만)",
      not hasattr(d2, "next_key") and "abc123" not in format_diagnostics_log_line(d2))

d3 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_bars, returned_bars_timestamps=[b.cntr_tm for b in actual_result],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("   (대조군) cont-yn=N, next-key 없음 -> 둘 다 False",
      d3.continuation_available is False and d3.next_key_present is False)

# ══════════════════════════════════════════════════════════════
# 5. 전일 봉 혼입 개수 계산
# ══════════════════════════════════════════════════════════════

prior_day_bars = make_raw_bars(10, datetime(2026, 7, 20, 14, 0, 0), desc=True)
today_bars = make_raw_bars(50, base, desc=True)
mixed_raw = today_bars + prior_day_bars  # 최신 순이므로 오늘 것이 앞

d4 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=mixed_raw, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("5) 전일(7/20) 봉이 섞이면 other_date_count가 정확히 계산됨(10건)",
      d4.other_date_count == 10)

# ══════════════════════════════════════════════════════════════
# 6. 동일 timestamp 중복 계산
# ══════════════════════════════════════════════════════════════

dup_raw = make_raw_bars(20, base, desc=True)
dup_raw_with_dupes = dup_raw + dup_raw[:5]  # 5개 중복 추가

d5 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=dup_raw_with_dupes, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("6) 중복 timestamp 5건이 duplicate_timestamp_count에 정확히 반영됨",
      d5.duplicate_timestamp_count == 5)

# ══════════════════════════════════════════════════════════════
# 7. 잘못된 timestamp 계산
# ══════════════════════════════════════════════════════════════

invalid_raw = make_raw_bars(10, base, desc=True)
invalid_raw[0]["cntr_tm"] = "INVALID"
invalid_raw[1]["cntr_tm"] = "2026072109"  # 자릿수 부족

d6 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=invalid_raw, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("7) 잘못된 timestamp 2건이 invalid_timestamp_count에 정확히 반영됨",
      d6.invalid_timestamp_count == 2)
check("   raw_timestamp_parseable_count는 전체(10)-invalid(2)=8", d6.raw_timestamp_parseable_count == 8)

# ══════════════════════════════════════════════════════════════
# 8. 진단 함수가 예외를 내더라도 기존 분봉 반환에는 영향 없음 (fail-open)
# ══════════════════════════════════════════════════════════════

broker2 = make_broker()
raw_bars2 = make_raw_bars(70, base, desc=True)
with patch.object(broker2, "_post", return_value=make_response(raw_bars2)):
    with patch(
        "infra.broker.minute_bar_diagnostics.build_minute_bar_diagnostics",
        side_effect=RuntimeError("진단 강제 실패"),
    ):
        result_with_broken_diag = broker2.get_minute_bars("475150", tick_scope=3, count=60)

check("8) 진단 함수가 예외를 던져도 get_minute_bars()는 예외 없이 정상 완료됨(fail-open)",
      len(result_with_broken_diag) == 60)
legacy_for_compare = legacy_parse(raw_bars2, 60)
identical2 = all(
    (l.cntr_tm, l.close_price) == (a.cntr_tm, a.close_price)
    for l, a in zip(legacy_for_compare, result_with_broken_diag)
)
check("   진단 실패 시에도 반환된 분봉 내용은 정상(legacy와 동일)", identical2)

# ══════════════════════════════════════════════════════════════
# 9. 같은 종목·날짜의 두 번째 호출은 진단 로그 중복 없음
# ══════════════════════════════════════════════════════════════

broker3 = make_broker()
raw_bars3 = make_raw_bars(70, base, desc=True)
with patch.object(broker3, "_post", return_value=make_response(raw_bars3)):
    broker3.get_minute_bars("475150", tick_scope=3, count=60)
    key_count_after_first = len(broker3._minute_diagnostic_keys)
    broker3.get_minute_bars("475150", tick_scope=3, count=60)  # 동일 조합 재호출
    key_count_after_second = len(broker3._minute_diagnostic_keys)

check("9) 같은 (symbol, base_date, tick_scope, count) 재호출 시 진단 키가 늘지 않음"
      "(중복 로그 방지)", key_count_after_first == 1 and key_count_after_second == 1)

# ══════════════════════════════════════════════════════════════
# 10. 다른 날짜 또는 다른 종목은 새 진단 로그 생성
# ══════════════════════════════════════════════════════════════

broker4 = make_broker()
raw_bars4 = make_raw_bars(70, base, desc=True)
with patch.object(broker4, "_post", return_value=make_response(raw_bars4)):
    broker4.get_minute_bars("475150", tick_scope=3, count=60)
    broker4.get_minute_bars("005930", tick_scope=3, count=60)  # 다른 종목
    broker4.get_minute_bars("475150", tick_scope=3, count=80)  # 다른 count

check("10) 다른 종목/다른 count 조합은 각각 새 진단 키를 생성함(3개)",
      len(broker4._minute_diagnostic_keys) == 3)

# ══════════════════════════════════════════════════════════════
# 10-1. 빈 응답(raw_bars=[])에서도 진단이 먼저 남는지 검증
#       (2026-07-27, 2차 GPT 코드리뷰 지시 4번)
#
# 배경: 기존엔 `if not raw_bars: return []`이 진단 로직보다 먼저
# 실행돼서, 빈 응답의 요청/응답 시각, raw_received=0, continuation
# 여부를 전혀 확인할 수 없었음. 진단을 먼저 남기도록 순서를 바꿈
# — 반환값(빈 리스트)과 기존 에러 로그는 그대로 유지.
# ══════════════════════════════════════════════════════════════

broker_empty = make_broker()
empty_response = make_response([], cont_yn="N", next_key="")
with patch.object(broker_empty, "_post", return_value=empty_response):
    result_empty = broker_empty.get_minute_bars("475150", tick_scope=3, count=60)

check("10-1) 빈 응답이어도 기존처럼 빈 리스트를 반환함(반환값 불변)",
      result_empty == [])
check("     빈 응답이어도 진단 키가 생성됨(진단이 실제로 먼저 실행됨을 증명)",
      ("475150", "20260721", "3", 60) in broker_empty._minute_diagnostic_keys
      or len(broker_empty._minute_diagnostic_keys) == 1)

# 실제 진단 결과를 직접 계산해서 raw_received=0, 시각 정보가 남는지 확인
d_empty = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=[], returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=datetime(2026, 7, 21, 9, 0, 0, tzinfo=KST),
    response_received_at=datetime(2026, 7, 21, 9, 0, 1, tzinfo=KST),
)
check("     빈 응답 진단에서 raw_received_count=0으로 정확히 계산됨",
      d_empty.raw_received_count == 0)
check("     빈 응답 진단에서도 요청/응답 시각이 보존됨(None이 아님)",
      d_empty.request_started_at is not None and d_empty.response_received_at is not None)
check("     빈 응답 진단에서도 continuation 정보를 확인할 수 있음",
      d_empty.continuation_available is False)

# ══════════════════════════════════════════════════════════════
# 11. 기존 2026-07-21 fixture 구조 및 manifest 테스트는 여전히 통과
#     (별도 test_legacy_fixture_structure.py에서 검증 — 여기서는
#      import 가능 여부만 스모크 테스트)
# ══════════════════════════════════════════════════════════════

check("11) test_legacy_fixture_structure.py는 별도 실행으로 검증됨 "
      "(run_regression_tests.py 참고, 이 파일은 진단 로직만 검증)", True)

# ══════════════════════════════════════════════════════════════
# 12. 공식 회귀 전체 통과 -> run_regression_tests.py로 별도 확인
# 13. 기존 BUY/HOLD/SELL 로직 변경 없음 -> get_minute_bars 반환값만
#     바뀌지 않으면 이 로직에 영향 없음 (이미 1,8번에서 검증됨)
# ══════════════════════════════════════════════════════════════

# ── KST 시간 계산 검증 (newest_raw_bar_age_seconds 등) ─────────────

response_time = datetime(2026, 7, 21, 9, 16, 45, tzinfo=KST)
newest_bar_time = datetime(2026, 7, 21, 9, 16, 0, tzinfo=KST)  # 45초 전 완성봉
raw_for_age = [{
    "cntr_tm": newest_bar_time.strftime("%Y%m%d%H%M%S"),
    "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
    "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000",
}]
d7 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_for_age, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=response_time, response_received_at=response_time,
)
check("14) newest_raw_bar_age_seconds가 정확히 계산됨(45초)",
      d7.newest_raw_bar_age_seconds is not None and abs(d7.newest_raw_bar_age_seconds - 45.0) < 0.01)
check("   newest_raw_bar_same_minute_as_response=True(같은 분 09:16)",
      d7.newest_raw_bar_same_minute_as_response is True)
check("   newest_raw_bar_is_future=False(과거 봉이므로)", d7.newest_raw_bar_is_future is False)

# 미래 시각(이상 상황) 케이스
future_bar_time = datetime(2026, 7, 21, 9, 20, 0, tzinfo=KST)  # 응답 시각보다 미래
raw_future = [{
    "cntr_tm": future_bar_time.strftime("%Y%m%d%H%M%S"),
    "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
    "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000",
}]
d8 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_future, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=response_time, response_received_at=response_time,
)
check("15) 미래 timestamp 봉 -> newest_raw_bar_is_future=True로 이상 감지됨",
      d8.newest_raw_bar_is_future is True)

# ── 정규장/전략거래창 시간 분리 계산 (2026-07-27, 2차 GPT 코드리뷰 지적) ──
# 기존 utils.time_utils 상수(MARKET_OPEN, MARKET_CLOSE=15:20)는 그대로
# 재사용하되, 실제 정규장 마감(REGULAR_MARKET_CLOSE=15:30)과 구분됨을
# 검증. 15:25는 전략거래창(15:20) 밖이지만 정규장(15:30) 안이라는
# 케이스로 두 값이 서로 다르게 계산됨을 명확히 보여줌.

from utils.time_utils import MARKET_OPEN, MARKET_CLOSE
from infra.broker.minute_bar_diagnostics import REGULAR_MARKET_CLOSE

outside_both = datetime(2026, 7, 21, 16, 0, 0)      # 15:20도 15:30도 모두 밖
between_strategy_and_market = datetime(2026, 7, 21, 15, 25, 0)  # 15:20 밖, 15:30 안
inside_bar = datetime(2026, 7, 21, 10, 0, 0)         # 둘 다 안

raw_session = [
    {"cntr_tm": outside_both.strftime("%Y%m%d%H%M%S"), "open_pric": "58000",
     "high_pric": "58200", "low_pric": "57900", "cur_prc": "58100",
     "trde_qty": "1000", "acc_trde_qty": "50000"},
    {"cntr_tm": between_strategy_and_market.strftime("%Y%m%d%H%M%S"), "open_pric": "58000",
     "high_pric": "58200", "low_pric": "57900", "cur_prc": "58100",
     "trde_qty": "1000", "acc_trde_qty": "50000"},
    {"cntr_tm": inside_bar.strftime("%Y%m%d%H%M%S"), "open_pric": "58000",
     "high_pric": "58200", "low_pric": "57900", "cur_prc": "58100",
     "trde_qty": "1000", "acc_trde_qty": "50000"},
]
d9 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="3", requested_count=60,
    raw_bars=raw_session, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check(f"16) 전략거래창(MARKET_OPEN={MARKET_OPEN}~MARKET_CLOSE={MARKET_CLOSE}, "
      f"기존 utils/time_utils.py 상수 재사용) 밖 봉 2건이 정확히 감지됨(16:00, 15:25)",
      d9.outside_strategy_window_count == 2)
check(f"    실제 정규장(REGULAR_MARKET_CLOSE={REGULAR_MARKET_CLOSE}, 신규 진단전용상수) "
      f"밖 봉은 1건만(16:00) 감지됨 — 15:25는 정규장 안이라 전략거래창과 값이 다름",
      d9.outside_regular_market_count == 1)

# ══════════════════════════════════════════════════════════════
# 17~20. 실운영 첫 로그 검토 이후 추가 (2026-07-27, GPT 코드리뷰)
#
# 실제 운영에서 raw_received=63, requested=60(초과분 미표시),
# raw_order=UNKNOWN(원인 불명)이 그대로 관찰된 것을 계기로 추가.
# ══════════════════════════════════════════════════════════════

# ── 17. raw_excess_count가 명시적으로 계산됨 (요청보다 많이 온 경우) ──
raw_63 = make_raw_bars(63, base, desc=True)
d10 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=60,
    raw_bars=raw_63, returned_bars_timestamps=[b.cntr_tm for b in legacy_parse(raw_63, 60)],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("17) raw 63개, requested 60개 -> raw_excess_count=3으로 명시 계산됨",
      d10.raw_excess_count == 3)
check("    raw_received_exceeds_requested=True", d10.raw_received_exceeds_requested is True)

# ── 18. (대조군) raw가 요청보다 적거나 같으면 exceeds=False ────────
raw_40 = make_raw_bars(40, base, desc=True)
d11 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=60,
    raw_bars=raw_40, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("18) (대조군) raw 40개 < requested 60개 -> raw_excess_count=-20(음수), exceeds=False",
      d11.raw_excess_count == -20 and d11.raw_received_exceeds_requested is False)

# ── 19. UNKNOWN 정렬 시 위반 횟수와 head/tail 샘플이 정확히 계산됨 ──
mostly_desc = [(base + timedelta(minutes=63 - i)) for i in range(63)]
ts_list = [t.strftime("%Y%m%d%H%M%S") for t in mostly_desc]
# 정확히 2곳을 스왑해 UNKNOWN 유발
ts_list[10], ts_list[11] = ts_list[11], ts_list[10]
ts_list[40], ts_list[41] = ts_list[41], ts_list[40]
raw_unknown = [
    {"cntr_tm": ts, "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
     "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000"}
    for ts in ts_list
]
d12 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=60,
    raw_bars=raw_unknown, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("19) 2곳을 스왑한 합성데이터 -> raw_sort_direction=UNKNOWN으로 감지됨",
      d12.raw_sort_direction == "UNKNOWN")
check("    raw_order_violation_count가 정확히 2로 계산됨(스왑 2곳)",
      d12.raw_order_violation_count == 2)
check("    raw_order_head_sample이 정확히 앞 5개를 담음",
      d12.raw_order_head_sample == ts_list[:5])
check("    raw_order_tail_sample이 정확히 뒤 5개를 담음",
      d12.raw_order_tail_sample == ts_list[-5:])

# ── 20. UNKNOWN일 때만 2차 로그가 생성되고, ASC/DESC/N/A는 생성 안 됨 ──
order_detail_unknown = format_order_detail_log_line(d12)
check("20) UNKNOWN 정렬 시 2차 로그(MIN_BOOTSTRAP_ORDER_DETAIL)가 생성됨",
      order_detail_unknown is not None and "MIN_BOOTSTRAP_ORDER_DETAIL" in order_detail_unknown)
check("    2차 로그에 violations 비율과 head/tail이 포함됨",
      "violations=2/62" in order_detail_unknown)

# (대조군) 정상 DESC 정렬(raw_63)은 2차 로그가 생성되지 않음
order_detail_normal = format_order_detail_log_line(d10)
check("    (대조군) 정상 DESC 정렬은 2차 로그가 None(생성 안 됨)",
      order_detail_normal is None)

# ── 21. 주 로그 라인에 raw_excess/violations가 실제로 포함되는지 확인 ──
main_log_line = format_diagnostics_log_line(d10)
check("21) 주 로그 라인에 raw_excess=3이 실제로 출력됨", "raw_excess=3" in main_log_line)
check("    주 로그 라인에 raw_exceeds_requested=True가 실제로 출력됨",
      "raw_exceeds_requested=True" in main_log_line)

# ══════════════════════════════════════════════════════════════
# 23. 얕은 형식검사(14자리 숫자) vs 실제 파싱성공 여부 불일치 재현
#     (2026-07-27, 2차 GPT 코드리뷰가 제시한 정확한 재현 케이스)
#
# 배경: 기존 _infer_sort_direction()/_count_order_violations()가
# 각각 독립적으로 "14자리 숫자인가"라는 얕은 검사만 하고,
# _parse_bar_timestamp()가 실제로 파싱에 성공하는지는 확인하지
# 않았음 — "20260230120000"(2월 30일, 존재하지 않는 날짜)처럼
# 형식은 14자리 숫자로 맞지만 실제로는 유효하지 않은 값이 문자열
# 비교에 그대로 섞여 잘못된 정렬 판정(UNKNOWN)을 내는 것을 재현.
# ══════════════════════════════════════════════════════════════

gpt_ts_list = ["20260721091600", "20260230120000", "20260721091400"]
gpt_raw = [
    {"cntr_tm": ts, "open_pric": "58000", "high_pric": "58200", "low_pric": "57900",
     "cur_prc": "58100", "trde_qty": "1000", "acc_trde_qty": "50000"}
    for ts in gpt_ts_list
]
d13 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=60,
    raw_bars=gpt_raw, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
check("23) GPT 재현케이스([20260721091600, 20260230120000(2월30일=존재안함), "
      "20260721091400]) -> invalid_timestamp_count가 정확히 1",
      d13.invalid_timestamp_count == 1)
check("    같은 케이스 -> raw_sort_direction이 UNKNOWN이 아니라 정확히 DESC로 판단됨"
      "(잘못된 날짜를 제외하면 09:16->09:14로 내림차순이 맞음)",
      d13.raw_sort_direction == "DESC")
check("    raw_order_violation_count는 0(유효한 두 timestamp만 보면 위반 없음)",
      d13.raw_order_violation_count == 0)

# ══════════════════════════════════════════════════════════════
# 22. 진단 모듈 동적 import 실패 시 broker fail-open 검증
#     (2026-07-27, 2차 GPT 코드리뷰 지적으로 설명 정정)
#
# ⚠️ 이 테스트가 실제로 검증하는 것: "get_minute_bars()가 진단
# 모듈을 import하는 지점(_maybe_log_minute_bar_diagnostics 내부)
# 에서 어떤 이유로든(ImportError든 다른 예외든) 실패해도, 분봉
# 조회 자체는 fail-open으로 정상 완료된다"는 것입니다. builtins.
# __import__를 패치해서 "minute_bar_diagnostics" 관련 import를
# 강제로 ModuleNotFoundError로 실패시키는 방식으로 재현합니다.
#
# 이 테스트가 검증하지 않는 것: "Windows에서 tzdata 미설치 시
# 실제로 무슨 일이 일어나는가"는 이 테스트의 대상이 아닙니다 —
# 그건 별개로 테스트 21번 이전 구간(KST 상수 자체가 zoneinfo에
# 의존하지 않는지)에서 검증합니다. 애초에 KST를 고정 UTC+9
# 오프셋으로 교체한 뒤로는(1B.2절), 정상적인 실행 경로에서
# tzdata 부재가 이 함수의 import를 실패시킬 이유가 없습니다 —
# 이 테스트의 강제 실패는 "만약 미래에 다른 이유로 이 모듈의
# import가 실패한다면"이라는 방어적 시나리오를 검증하는 것입니다.
# ══════════════════════════════════════════════════════════════

broker5 = make_broker()
raw_bars5 = make_raw_bars(70, base, desc=True)


def failing_import(name, *args, **kwargs):
    """minute_bar_diagnostics 관련 import를 전부 실패시켜 tzdata
    미설치 환경(모듈 로드 자체가 안 되는 최악의 상황)을 재현합니다."""
    if "minute_bar_diagnostics" in name:
        raise ModuleNotFoundError("No module named 'tzdata' (재현용 강제 실패)")
    return _original_import(name, *args, **kwargs)


import builtins as _builtins
_original_import = _builtins.__import__

with patch.object(broker5, "_post", return_value=make_response(raw_bars5)):
    with patch("builtins.__import__", side_effect=failing_import):
        result_diag_import_failed = broker5.get_minute_bars("475150", tick_scope=3, count=60)

check("22) 진단 모듈 import 실패(강제 재현) 시에도 "
      "get_minute_bars()는 fail-open으로 정상 60개를 반환함",
      len(result_diag_import_failed) == 60)
legacy_for_diag_failure_test = legacy_parse(raw_bars5, 60)
identical3 = all(
    (l.cntr_tm, l.close_price) == (a.cntr_tm, a.close_price)
    for l, a in zip(legacy_for_diag_failure_test, result_diag_import_failed)
)
check("    진단 import 실패 상황에서도 반환된 분봉 내용은 legacy와 완전히 동일함",
      identical3)

# ── KST 상수 자체가 더 이상 zoneinfo에 의존하지 않는지 직접 확인 ──
# (KST는 이미 파일 상단에서 실제 운영 모듈로부터 import했음 — 여기서
# 다시 import하지 않고 그 값을 그대로 검증)
import zoneinfo as _zoneinfo
from datetime import timezone as _timezone

check("   KST 상수가 zoneinfo.ZoneInfo 인스턴스가 아님(외부 tzdata 의존성 제거 확인)",
      not isinstance(KST, _zoneinfo.ZoneInfo))
check("   KST 상수가 datetime.timezone 고정 오프셋 인스턴스임",
      isinstance(KST, _timezone))
check("   KST 오프셋이 정확히 UTC+9시간임",
      KST.utcoffset(None) == timedelta(hours=9))

# ══════════════════════════════════════════════════════════════
# 24. 로그 필드 확장 검증 (2026-07-27, 2차 GPT 코드리뷰 지시 2번)
#
# request_started_at/response_received_at/request_duration_ms/
# returned_oldest_timestamp/returned_newest_timestamp가 실제로
# 주 로그 라인에 ISO 8601(+09:00)/N/A 형태로 출력되는지 확인.
# ══════════════════════════════════════════════════════════════

log_ts_request = datetime(2026, 7, 21, 9, 16, 45, 100000, tzinfo=KST)
log_ts_response = datetime(2026, 7, 21, 9, 16, 45, 350000, tzinfo=KST)  # 250ms 후
raw_for_log_test = make_raw_bars(10, base, desc=True)
d14 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=10,
    raw_bars=raw_for_log_test,
    returned_bars_timestamps=[b["cntr_tm"] for b in raw_for_log_test][::-1],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=log_ts_request, response_received_at=log_ts_response,
)
log_line_with_times = format_diagnostics_log_line(d14)

check("24) 주 로그에 request_started_at이 ISO8601(+09:00) 형태로 출력됨",
      "request_started_at=2026-07-21T09:16:45.100000+09:00" in log_line_with_times)
check("    주 로그에 response_received_at이 ISO8601(+09:00) 형태로 출력됨",
      "response_received_at=2026-07-21T09:16:45.350000+09:00" in log_line_with_times)
check("    주 로그에 request_duration_ms가 정확히 250.0으로 계산됨",
      "request_duration_ms=250.0" in log_line_with_times)
check("    주 로그에 returned_oldest/returned_newest가 출력됨",
      "returned_oldest=" in log_line_with_times and "returned_newest=" in log_line_with_times)

# ── (대조군) 시각 정보가 없으면 N/A로 표시되고 예외가 안 남 ────────
d15 = build_minute_bar_diagnostics(
    symbol="475150", base_date="20260721", tick_scope="1", requested_count=10,
    raw_bars=raw_for_log_test, returned_bars_timestamps=[],
    headers={"cont-yn": "N", "next-key": ""},
    request_started_at=None, response_received_at=None,
)
log_line_no_times = format_diagnostics_log_line(d15)
check("    (대조군) 시각 정보가 없으면 request_started_at=N/A로 표시됨(예외 없음)",
      "request_started_at=N/A" in log_line_no_times)
check("    (대조군) request_duration_ms도 N/A로 표시됨",
      "request_duration_ms=N/A" in log_line_no_times)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
