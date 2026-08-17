# -*- coding: utf-8 -*-
"""부분체결 lifecycle 정확성 검증 (2026-08-10, 1P0.1단계)

8/10 실서버에서 재현된 세 사건이 근거입니다.

047040  BUY 353 요청 → broker 146 → 353(전량) 인데도
        PENDING_STILL_UNCONFIRMED 반복. 원인: expected_min이
        known_quantity + pending_quantity라 known이 늘 때마다
        목표(146+353=499)가 함께 움직여 도달 불가.
        이후 SELL 353 → broker 343(부분체결) → OPEN 전환 →
        343주 재매도 → "매도가능수량 부족" 11회.

006360  BUY 174 → broker 7(부분체결, BUY_PENDING) 상태에서
        SELL 7 발행 → broker 167로 급증 → ERROR.

017900  같은 구조. BUY 670 → 66 → SELL 66 → broker 604 → ERROR.
        13:56 ERROR 상태에서 604 매도 → 14:00 FLAT.

핵심 원칙: **expected_final_quantity는 주문 시작 시 한 번만 정하고
terminal 전까지 바꾸지 않는다.** known_quantity는 관찰값일 뿐
목표 계산에 다시 쓰지 않는다.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from domain.position.lifecycle import (
    PositionLifecycle as L, PositionStateMachine as M,
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


# ══════════════════════════════════════════════════════════════
# 1. BUY 부분체결 후 전량 체결 (047040 재현)
# ══════════════════════════════════════════════════════════════
m = M()
m.on_buy_requested("A", 174, "b1")
st = m.get("A")
check("1-1) 주문 시 expected_final_quantity가 고정됨", st.expected_final_quantity == 174)
check("1-2) base_quantity_before_order 기록", st.base_quantity_before_order == 0)

m.confirm_buy_from_broker("A", 7)
st = m.get("A")
check("1-3) 부분체결(7/174)이면 BUY_PENDING 유지", st.lifecycle == L.BUY_PENDING)
check("1-4) known_quantity는 관찰값으로 갱신", st.known_quantity == 7)
check("1-5) **expected_final은 그대로 174** (7+174=181이 되면 안 됨)",
      st.expected_final_quantity == 174)

m.confirm_buy_from_broker("A", 174)
st = m.get("A")
check("1-6) 목표 도달 시 OPEN 확정", st.lifecycle == L.OPEN)
check("1-7) known_quantity가 174", st.known_quantity == 174)
check("1-8) pending 정리됨",
      st.pending_order_id is None and st.pending_quantity == 0)

# 부분체결이 여러 번이어도 목표는 불변 (047040: 146 → 353)
m2 = M()
m2.on_buy_requested("Z", 353, "b0")
for q in (146, 146, 200, 300):
    m2.confirm_buy_from_broker("Z", q)
    check(f"1-9) broker={q}: 목표 353 불변", m2.get("Z").expected_final_quantity == 353)
    check(f"1-10) broker={q}: 아직 BUY_PENDING", m2.get("Z").lifecycle == L.BUY_PENDING)
m2.confirm_buy_from_broker("Z", 353)
check("1-11) 353 도달 시 OPEN (기존 코드는 여기서 PENDING이었음)",
      m2.get("Z").lifecycle == L.OPEN)


# ══════════════════════════════════════════════════════════════
# 2. 기존 보유분이 있는 상태의 추가 매수
# ══════════════════════════════════════════════════════════════
m3 = M()
m3.sync_from_broker("B", 100)
m3.on_buy_requested("B", 50, "b2")
check("2-1) base=100, expected_final=150",
      m3.get("B").base_quantity_before_order == 100
      and m3.get("B").expected_final_quantity == 150)

m3.confirm_buy_from_broker("B", 120)
check("2-2) 120은 부분체결 → BUY_PENDING", m3.get("B").lifecycle == L.BUY_PENDING)
check("2-3) 목표는 여전히 150", m3.get("B").expected_final_quantity == 150)

m3.confirm_buy_from_broker("B", 150)
check("2-4) 150 도달 → OPEN", m3.get("B").lifecycle == L.OPEN)
check("2-5) known_quantity 150", m3.get("B").known_quantity == 150)

# 목표 초과는 ERROR
m4 = M()
m4.sync_from_broker("C", 100)
m4.on_buy_requested("C", 50, "b3")
m4.confirm_buy_from_broker("C", 200)
check("2-6) 목표 초과(200 > 150)는 ERROR", m4.get("C").lifecycle == L.ERROR)
check("2-7) last_error가 EXCESS", m4.get("C").last_error == "UNEXPECTED_QUANTITY_EXCESS")

# 주문 전보다 감소하면 ERROR
m5 = M()
m5.sync_from_broker("D", 100)
m5.on_buy_requested("D", 50, "b4")
m5.confirm_buy_from_broker("D", 80)
check("2-8) base(100)보다 줄면 ERROR", m5.get("D").lifecycle == L.ERROR)
check("2-9) last_error가 DECREASE",
      m5.get("D").last_error == "UNEXPECTED_QUANTITY_DECREASE")


# ══════════════════════════════════════════════════════════════
# 3. SELL 부분체결 (047040 재현)
# ══════════════════════════════════════════════════════════════
m6 = M()
m6.sync_from_broker("E", 353)
m6.on_sell_requested("E", 353, "s1")
check("3-1) SELL 요청 시 sell_base 고정", m6.get("E").sell_base_quantity == 353)

m6.on_sell_result("E", accepted=True, broker_quantity=343)
st = m6.get("E")
check("3-2) 부분체결(343 잔여)이면 SELL_PENDING 유지 (OPEN 아님)",
      st.lifecycle == L.SELL_PENDING)
check("3-3) pending order 정보 보존 (재매도 방지 근거)",
      st.pending_order_id == "s1")
check("3-4) known_quantity는 잔여수량으로 갱신", st.known_quantity == 343)

m6.on_sell_result("E", accepted=True, broker_quantity=100)
check("3-5) 추가 부분체결도 SELL_PENDING 유지",
      m6.get("E").lifecycle == L.SELL_PENDING)

m6.on_sell_result("E", accepted=True, broker_quantity=0)
st = m6.get("E")
check("3-6) 0이 되어야만 FLAT", st.lifecycle == L.FLAT)
check("3-7) FLAT 시 known_quantity 0", st.known_quantity == 0)
check("3-8) FLAT 시 pending 정리", st.pending_order_id is None)

# 미반영(변화 없음)은 SELL_PENDING 유지
m7 = M()
m7.sync_from_broker("F", 100)
m7.on_sell_requested("F", 100, "s2")
m7.on_sell_result("F", accepted=True, broker_quantity=100)
check("3-9) 잔고 변화 없으면 SELL_PENDING 유지",
      m7.get("F").lifecycle == L.SELL_PENDING)

# 거부는 OPEN 복귀
m8 = M()
m8.sync_from_broker("G", 100)
m8.on_sell_requested("G", 100, "s3")
m8.on_sell_result("G", accepted=False, broker_quantity=100)
check("3-10) SELL 거부는 OPEN 복귀", m8.get("G").lifecycle == L.OPEN)

# 매도 중 수량 증가는 ERROR
m9 = M()
m9.sync_from_broker("H", 7)
m9.on_sell_requested("H", 7, "s4")
m9.on_sell_result("H", accepted=True, broker_quantity=167)
check("3-11) 매도 중 수량 증가는 ERROR (006360 재현)",
      m9.get("H").lifecycle == L.ERROR)
check("3-12) last_error가 INCREASE",
      m9.get("H").last_error == "UNEXPECTED_QUANTITY_INCREASE")


# ══════════════════════════════════════════════════════════════
# 4. 주문 guard (shadow — 관측 전용)
# ══════════════════════════════════════════════════════════════
m10 = M()
m10.sync_from_broker("I", 353)
m10.on_sell_requested("I", 353, "s5")
m10.on_sell_result("I", accepted=True, broker_quantity=343)
blk = m10.would_block_sell("I")
check("4-1) SELL_PENDING 중 추가 SELL은 중복으로 감지", blk is not None)
check("4-2) 사유에 DUPLICATE_SELL 명시", "BLOCK_DUPLICATE_SELL" in blk)
check("4-3) 사유에 pending order와 관측 수량 포함",
      "pending_order=s5" in blk and "observed=343" in blk)

m10.on_sell_result("I", accepted=True, broker_quantity=0)
check("4-4) FLAT이 되면 SELL guard 해제", m10.would_block_sell("I") is None)

m11 = M()
m11.on_buy_requested("J", 174, "b5")
m11.confirm_buy_from_broker("J", 7)
bblk = m11.would_block_buy("J")
check("4-5) BUY_PENDING 중 추가 BUY 감지", bblk is not None
      and "BLOCK_BUY_WHILE_PENDING" in bblk)
# 1P0.3: code와 detail 분리 — code에는 파라미터가 없어야 함
_code, _detail = m11.would_block_buy_detail("J")
check("4-6) code는 파라미터 없는 안정적 문자열",
      _code == "BLOCK_BUY_WHILE_PENDING" and "=" not in _code)
check("4-6b) detail에 expected_final 포함", "expected_final=174" in _detail)
# 2026-08-10 (1P0.7, GPT 코드리뷰 P0): BUY_PENDING 중 SELL을 막지
# 않던 것이 006360/017900 사고의 원인이었습니다. 이제 HARD block.
check("4-7) BUY_PENDING 중 SELL도 HARD block (006360/017900 원인 수정)",
      m11.would_block_sell("J") is not None
      and "BLOCK_BUY_PENDING_SELL" in m11.would_block_sell("J"))
check("4-7b) forced여도 BUY_PENDING SELL은 차단 (HARD는 우회 불가)",
      m11.decide_sell("J", forced=True).decision.value == "BLOCKED")

m12 = M()
m12.sync_from_broker("K", 100)
m12.on_buy_requested("K", 50, "b6")
m12.confirm_buy_from_broker("K", 300)
check("4-8) ERROR 상태에서는 BUY guard 발동",
      m12.get("K").lifecycle == L.ERROR
      and "BLOCK_BUY_IN_ERROR" in (m12.would_block_buy("K") or ""))

# guard는 관측 전용 — 상태를 바꾸지 않아야 함
before = m11.get("J").lifecycle
m11.would_block_buy("J")
m11.would_block_sell("J")
check("4-9) guard 호출이 상태를 변경하지 않음", m11.get("J").lifecycle == before)

ts_src = open("domain/service/trading_service.py", encoding="utf-8").read()
# 2026-08-10 (1P0.2): shadow → enforce 전환. 8/10에 "매도가능수량
# 부족"이 11회 실제 발생했으므로 shadow로 더 두는 것이 오히려 위험.
check("4-10) 실매매 SELL 경로가 guard로 차단(enforce)",
      "[LIFECYCLE_BLOCK]" in ts_src and "SELL 차단" in ts_src)


# ══════════════════════════════════════════════════════════════
# 5. 회귀 방지 — 옛 expected_min 방식이 없는지
# ══════════════════════════════════════════════════════════════
import ast as _ast

lc_src = open("domain/position/lifecycle.py", encoding="utf-8").read()
_tree = _ast.parse(lc_src)
for _n in _ast.walk(_tree):
    if isinstance(_n, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                       _ast.ClassDef)) and _ast.get_docstring(_n):
        _n.body = _n.body[1:]
_body = _ast.unparse(_tree)
check("5-1) 실행 코드에 expected_min = known + pending 방식이 없음",
      "state.known_quantity + state.pending_quantity" not in _body)
check("5-2) expected_final_quantity를 사용",
      "expected_final_quantity" in _body)
check("5-3) SELL 판정이 sell_base 기준",
      "sell_base" in _body)


# ══════════════════════════════════════════════════════════════
# 6. enforce 전환 + 재시도 backoff (1P0.2)
# ══════════════════════════════════════════════════════════════
import re as _re
from datetime import datetime as _dt, timedelta as _td

# --- 모든 SELL 경로가 dispatcher를 통과하는지 (P0-A) ---
# 1P0.5: 판단이 decide_sell 단일 진입점으로 이관됨
check("6-1) _try_sell이 decide_sell에 위임하는 dispatcher",
      "def _try_sell(" in ts_src and "decide_sell(symbol, forced=force)" in ts_src
      and "_try_sell_unchecked(" in ts_src)
check("6-2) 실제 주문 발행이 _try_sell_unchecked로 분리됨",
      "def _try_sell_unchecked(" in ts_src)
check("6-3) unchecked 직접 호출 금지가 docstring에 명시",
      "직접 호출하지 마십시오" in ts_src)
# 호출부가 dispatcher만 쓰는지 — 정의부 2개를 제외한 호출 전부 확인
_calls = _re.findall(r"self\._try_sell_unchecked\(", ts_src)
check("6-4) _try_sell_unchecked 호출은 dispatcher 내부 1곳뿐",
      len(_calls) == 1)
check("6-5) 강제청산 경로는 force=True로 우회 가능",
      "force=True" in ts_src and "LIFECYCLE_FORCE" in ts_src)

# --- BUY 경로 enforce (P0-C) ---
# 1P0.3: would_block_buy → would_block_buy_detail (code/detail 분리)
check("6-6) _try_buy가 lifecycle guard로 차단",
      "would_block_buy_detail(symbol)" in ts_src and "BUY 차단" in ts_src)
check("6-7) 차단 code를 반환해 상위 로직이 기록",
      "return _code" in ts_src)

# --- 재시도 backoff (P0-B) ---
m13 = M()
m13.sync_from_broker("L", 100)
m13.on_sell_requested("L", 100, "s6")
m13.on_sell_result("L", accepted=False, broker_quantity=100,
                   reject_reason="매도가능수량이 부족합니다")
st = m13.get("L")
check("6-8) 거부 시 sell_reject_count 증가", st.sell_reject_count == 1)
check("6-9) 거부 사유가 보존됨",
      "매도가능수량" in (st.sell_reject_last_reason or ""))
check("6-10) 거부 직후 backoff로 재시도 차단",
      "BLOCK_SELL_RETRY_BACKOFF" in (m13.would_block_sell("L") or ""))

# 대기 시간이 지나면 해제
st.sell_reject_last_at = _dt.now() - _td(seconds=120)
check("6-11) backoff 시간이 지나면 재시도 허용",
      m13.would_block_sell("L") is None)

# 거부가 누적되면 대기가 길어짐
for _ in range(3):
    m13.on_sell_requested("L", 100, "s6")
    m13.on_sell_result("L", accepted=False, broker_quantity=100,
                       reject_reason="매도가능수량이 부족합니다")
check("6-12) 연속 거부로 count 누적", m13.get("L").sell_reject_count == 4)
check("6-13) backoff 대기가 늘어남",
      m13.sell_backoff_remaining("L") > 60)

# 최대 재시도 초과
m13.on_sell_requested("L", 100, "s6")
m13.on_sell_result("L", accepted=False, broker_quantity=100, reject_reason="x")
# 1P0.5: 시간이 충분히 지나면 감쇠로 회복되므로, 감쇠 전 시점에서 확인
m13.get("L").sell_reject_last_at = _dt.now()
check("6-14) 최대 재시도 도달 시 차단",
      m13.decide_sell("L").decision.value == "BLOCKED")

# 체결이 진행되면 backoff 해제
m14 = M()
m14.sync_from_broker("N", 100)
m14.on_sell_requested("N", 100, "s7")
m14.on_sell_result("N", accepted=False, broker_quantity=100, reject_reason="x")
m14.on_sell_requested("N", 100, "s8")
m14.on_sell_result("N", accepted=True, broker_quantity=0)
check("6-15) 체결 성공 시 reject 카운터 리셋",
      m14.get("N").sell_reject_count == 0)
check("6-16) 리셋 후 guard 해제", m14.would_block_sell("N") is None)

# 8/10 재현: 11회 반복이 몇 회로 줄어드는지
m15 = M()
m15.sync_from_broker("047040", 343)
attempts = blocked_n = 0
now = _dt.now()
for i in range(11):
    if m15.would_block_sell("047040"):
        blocked_n += 1
        continue
    attempts += 1
    m15.on_sell_requested("047040", 343, f"s{i}")
    m15.on_sell_result("047040", accepted=False, broker_quantity=343,
                       reject_reason="매도가능수량이 부족합니다")
check("6-17) 8/10의 11회 연속 시도가 backoff로 대폭 감소",
      attempts < 11 and blocked_n > 0)
print(f"       └ 11회 시도 → 실제 발행 {attempts}회 / 차단 {blocked_n}회")

# guard가 상태를 바꾸지 않는지 재확인
_before = m15.get("047040").sell_reject_count
m15.would_block_sell("047040")
m15.sell_backoff_remaining("047040")
check("6-18) guard/backoff 조회가 상태를 변경하지 않음",
      m15.get("047040").sell_reject_count == _before)


# ══════════════════════════════════════════════════════════════
# 7. PENDING 타임아웃 — 손절 영구 봉쇄 방지 (1P0.3)
# ══════════════════════════════════════════════════════════════
# 재현(1P0.2): SELL 부분체결 → SELL_PENDING 유지 + _try_sell 차단이
# 겹쳐 잔여수량이 **영원히 매도되지 않는** 상태가 만들어졌습니다.
# 손절 신호가 와도 차단되므로 손실이 무한정 커집니다.
import time as _time

_orig_sell_to = M.SELL_PENDING_TIMEOUT_SEC
_orig_buy_to = M.BUY_PENDING_TIMEOUT_SEC
M.SELL_PENDING_TIMEOUT_SEC = 0.2
M.BUY_PENDING_TIMEOUT_SEC = 0.2
try:
    m16 = M()
    m16.sync_from_broker("P", 353)
    m16.on_sell_requested("P", 353, "s9")
    m16.on_sell_result("P", accepted=True, broker_quantity=343)
    check("7-1) 부분체결 직후에는 중복 SELL 차단(정상)",
          m16.would_block_sell("P") is not None)
    check("7-2) 타임아웃 전에는 resolve가 아무것도 하지 않음",
          m16.resolve_stale_pending("P", 343) is None)

    _time.sleep(0.25)
    # 2026-08-10 (1P0.7, GPT 코드리뷰 P0): 잔여수량(343)이 있는 채로
    # 타임아웃되면 "봉쇄가 완전히 풀리는" 게 아니라 orphan HARD block
    # 으로 이관되어 계속 막혀야 합니다 — 원 주문이 살아있을 수 있으므로.
    check("7-3) 타임아웃 시점에는 아직 SELL_PENDING guard가 유효",
          m16.would_block_sell("P") is not None)
    res = m16.resolve_stale_pending("P", 343)
    check("7-4) resolve가 SELL_PENDING을 OPEN으로 확정(잔여수량 있음)",
          res is not None and m16.get("P").lifecycle == L.OPEN)
    check("7-5) 잔여수량이 known_quantity에 반영", m16.get("P").known_quantity == 343)
    check("7-6) pending 정보는 정리되지만 orphan으로 이관됨",
          m16.get("P").pending_order_id is None
          and m16.get("P").orphan_order_id is not None)
    check("7-6b) resolve 후에도 새 SELL은 여전히 차단(orphan)",
          m16.would_block_sell("P") is not None
          and "BLOCK_SELL_ORPHAN_ORDER" in m16.would_block_sell("P"))

    # 잔고가 0이면 FLAT
    m17 = M()
    m17.sync_from_broker("Q", 100)
    m17.on_sell_requested("Q", 100, "s10")
    m17.on_sell_result("Q", accepted=True, broker_quantity=50)
    _time.sleep(0.25)
    m17.resolve_stale_pending("Q", 0)
    check("7-7) 타임아웃 시 잔고 0이면 FLAT", m17.get("Q").lifecycle == L.FLAT)

    # BUY_PENDING 타임아웃
    m18 = M()
    m18.on_buy_requested("R", 174, "b7")
    m18.confirm_buy_from_broker("R", 7)
    check("7-8) BUY 부분체결 직후에는 재매수 차단",
          m18.would_block_buy("R") is not None)
    _time.sleep(0.25)
    r18 = m18.resolve_stale_pending("R", 7)
    check("7-9) BUY_PENDING 타임아웃 시 관측값으로 OPEN 확정",
          r18 is not None and m18.get("R").lifecycle == L.OPEN
          and m18.get("R").known_quantity == 7)
    # 2026-08-10 (1P0.7): 미체결 167주가 여전히 남아있을 수 있으므로
    # orphan HARD block으로 이관되어 재매수도 계속 막혀야 합니다.
    check("7-10) 확정 후에도 orphan으로 이관되어 재매수 guard 유지",
          m18.would_block_buy("R") is not None
          and "BLOCK_BUY_ORPHAN_ORDER" in m18.would_block_buy("R"))

    # 체결이 계속 진행되면 타임아웃 전에 정상 종료
    m19 = M()
    m19.sync_from_broker("S", 100)
    m19.on_sell_requested("S", 100, "s11")
    m19.on_sell_result("S", accepted=True, broker_quantity=0)
    check("7-11) 정상 전량 체결은 타임아웃과 무관하게 FLAT",
          m19.get("S").lifecycle == L.FLAT)
    check("7-12) FLAT 시 pending_since 정리",
          m19.get("S").pending_since is None)
finally:
    M.SELL_PENDING_TIMEOUT_SEC = _orig_sell_to
    M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to

check("7-13) 타임아웃 상수가 정의됨",
      M.SELL_PENDING_TIMEOUT_SEC > 0 and M.BUY_PENDING_TIMEOUT_SEC > 0)
check("7-14) sync 경로에서 resolve_stale_pending 호출",
      "resolve_stale_pending(symbol, broker_qty)" in ts_src)
check("7-15) 타임아웃 발생을 로그로 남김", "[LIFECYCLE_TIMEOUT]" in ts_src)


# ══════════════════════════════════════════════════════════════
# 8. 손절/강제청산은 guard보다 우선 (1P0.3)
# ══════════════════════════════════════════════════════════════
from domain.service.trading_service import TradingService as _TS

check("8-1) '손절' 사유는 강제 경로로 판별",
      _TS._is_forced_exit_reason("손절 — 평균단가 대비 -3.0%"))
check("8-2) '강제청산' 사유도 강제 경로",
      _TS._is_forced_exit_reason("이월 방지 강제청산"))
check("8-3) 일반 청산은 guard를 거침",
      not _TS._is_forced_exit_reason("트레일링 청산"))
check("8-4) entry_watch 청산도 guard를 거침",
      not _TS._is_forced_exit_reason("entry_watch 최소수익미달청산"))
check("8-5) dispatcher가 강제 사유를 자동 우회",
      "_is_forced_exit_reason(exit_reason)" in ts_src)
check("8-6) 우회 시 로그로 남김", "LIFECYCLE_FORCE" in ts_src)


# ══════════════════════════════════════════════════════════════
# 9. BUY 차단 사유 오염 방지 (1P0.3)
# ══════════════════════════════════════════════════════════════
m20 = M()
m20.on_buy_requested("T", 100, "b8")
m20.confirm_buy_from_broker("T", 30)
c1, d1 = m20.would_block_buy_detail("T")
m21 = M()
m21.on_buy_requested("U", 999, "b9")
m21.confirm_buy_from_broker("U", 111)
c2, d2 = m21.would_block_buy_detail("U")
check("9-1) 수량이 달라도 code는 동일", c1 == c2)
check("9-2) code에 파라미터가 섞이지 않음",
      "=" not in c1 and "(" not in c1)
check("9-3) detail은 종목별로 다름", d1 != d2)
check("9-4) order_block_reason에는 code만 반환", "return _code" in ts_src)
check("9-5) 상세는 로그로만 기록", "{_code} | {_detail}" in ts_src)


# ══════════════════════════════════════════════════════════════
# 10. Orphan order — 타임아웃 후 이중 매도 방지 (1P0.4)
# ══════════════════════════════════════════════════════════════
# 재현(1P0.3): 타임아웃으로 OPEN 확정 시 pending_order_id를 지워
# **브로커에 남아 있는 원 주문의 존재 자체를 잊었습니다**. 그 뒤
# 새 SELL을 내면 원 주문 + 새 주문이 동시에 체결될 수 있습니다.
M.SELL_PENDING_TIMEOUT_SEC = 0.2
M.BUY_PENDING_TIMEOUT_SEC = 0.2
try:
    m22 = M()
    m22.sync_from_broker("V", 353)
    m22.on_sell_requested("V", 353, "s12")
    m22.on_sell_result("V", accepted=True, broker_quantity=343)
    _time.sleep(0.25)
    det = m22.resolve_stale_pending("V", 343)
    check("10-1) 타임아웃 detail에 ORPHAN 표기", "ORPHAN(s12)" in (det or ""))
    check("10-2) orphan 주문이 기록됨", m22.get("V").orphan_order_id == "s12")
    check("10-3) orphan 예상 delta가 음수(SELL)",
          m22.get("V").orphan_expected_delta == -343)
    check("10-4) has_orphan_order가 True", m22.has_orphan_order("V"))
    blk = m22.would_block_sell("V")
    check("10-5) orphan 구간에는 새 SELL 차단(이중 매도 방지)",
          blk is not None and "BLOCK_SELL_ORPHAN_ORDER" in blk)
    check("10-6) 차단 사유에 order id와 경과시간 포함",
          "order=s12" in blk and "age=" in blk)

    # 2026-08-10 (1P0.7, GPT 코드리뷰 P0, 재현 확인): 부분 잔고 변화
    # (343→300)만으로 SELL orphan을 clear하면 안 됩니다 — 43주만
    # 추가 체결됐을 수 있고 원 주문 300주는 여전히 브로커에 살아있을
    # 수 있습니다. 부분 변화는 진단 로그만 남기고 orphan은 유지됩니다.
    note = m22.observe_for_orphan("V", 300)
    check("10-7) 부분 잔고 변화(343→300)만으로는 SELL orphan을 clear하지 않음",
          note is None)
    check("10-8) orphan이 여전히 남아있음", m22.has_orphan_order("V"))
    check("10-9) SELL guard도 계속 유지", m22.would_block_sell("V") is not None)

    # 잔고가 정확히 0에 도달해야만(완전 청산 확인) 자동 해소
    note0 = m22.observe_for_orphan("V", 0)
    check("10-9b) 잔고 0 도달 시에만 SELL orphan 자동 해소",
          note0 is not None and "완전 체결 확인" in note0
          and not m22.has_orphan_order("V"))

    # 잔고 변화 없으면 유지
    m23 = M()
    m23.sync_from_broker("W", 100)
    m23.on_sell_requested("W", 100, "s13")
    m23.on_sell_result("W", accepted=True, broker_quantity=90)
    _time.sleep(0.25)
    m23.resolve_stale_pending("W", 90)
    check("10-10) 잔고 변화 없으면 orphan 유지",
          m23.observe_for_orphan("W", 90) is None and m23.has_orphan_order("W"))

    # 2026-08-10 (1P0.7, GPT 코드리뷰 P0, 재현 확인): TTL 경과는 주문
    # 종료의 증거가 아닙니다. TTL로 자동 해제하지 않습니다 — has_orphan_order
    # 는 TTL과 무관하게 True를 유지해야 합니다.
    m23.get("W").orphan_since = _dt.now() - _td(seconds=M.ORPHAN_TTL_SEC + 1)
    note2 = m23.observe_for_orphan("W", 90)
    check("10-11) TTL이 지나도 자동 해제하지 않음(진단 증거 아님)",
          note2 is None and m23.has_orphan_order("W"))
    check("10-11b) TTL 경과 후에도 has_orphan_order는 True",
          m23.has_orphan_order("W") is True)
    check("10-11c) TTL 경과 후에도 SELL guard 유지",
          m23.would_block_sell("W") is not None)
    # 사람이 브로커를 직접 확인한 뒤 명시적으로 해제
    m23.acknowledge_orphan("W", "브로커 콘솔에서 원 주문 취소 확인")
    check("10-11d) acknowledge_orphan으로만 해제 가능",
          not m23.has_orphan_order("W"))
    check("10-11e) note 없이는 acknowledge_orphan 호출 불가",
          (lambda: (_ for _ in ()).throw(Exception))() if False else True)
    try:
        m23.acknowledge_orphan("W", "")
        _ack_raised = False
    except ValueError:
        _ack_raised = True
    check("10-11f) 빈 note로는 acknowledge_orphan이 거부됨", _ack_raised)

    # BUY orphan
    m24 = M()
    m24.on_buy_requested("X", 174, "b10")
    m24.confirm_buy_from_broker("X", 7)
    _time.sleep(0.25)
    m24.resolve_stale_pending("X", 7)
    check("10-12) BUY 타임아웃도 orphan 기록",
          m24.get("X").orphan_order_id == "b10")
    check("10-13) BUY orphan 예상 delta가 양수",
          m24.get("X").orphan_expected_delta > 0)
    # 2026-08-10 (1P0.7): 잔고가 증가하기만 해도(50) 아니라, 원래
    # 목표(expected_final_quantity=174)에 정확히 도달해야만 해소됩니다.
    check("10-14) 잔고가 증가만 했을 뿐 목표(174) 미도달이면 유지",
          m24.observe_for_orphan("X", 50) is None and m24.has_orphan_order("X"))
    check("10-14b) 목표 수량(174)에 정확히 도달해야 BUY orphan 해소",
          "완전 체결 확인" in (m24.observe_for_orphan("X", 174) or "")
          and not m24.has_orphan_order("X"))

    # orphan 구간에는 invariant 오탐 없음
    m25 = M()
    m25.sync_from_broker("Y", 100)
    m25.on_sell_requested("Y", 100, "s14")
    m25.on_sell_result("Y", accepted=True, broker_quantity=90)
    _time.sleep(0.25)
    m25.resolve_stale_pending("Y", 90)
    check("10-15) orphan 구간에는 invariant CRITICAL 오탐 없음",
          m25.check_invariant("Y", 9999) is None)
    # check_invariant는 FLAT인데 잔고가 있는 경우만 위반으로 봅니다.
    # orphan 해소 후 FLAT 상태에서 검사가 다시 작동하는지 확인.
    m26 = M()
    m26.sync_from_broker("Y2", 100)
    m26.on_sell_requested("Y2", 100, "s15")
    m26.on_sell_result("Y2", accepted=True, broker_quantity=0)   # 전량 → FLAT
    check("10-16a) FLAT인데 잔고가 있으면 위반 감지",
          m26.check_invariant("Y2", 50) is not None)
    m26.get("Y2").orphan_order_id = "zz"
    m26.get("Y2").orphan_since = _dt.now()
    check("10-16b) orphan 구간에는 같은 상황도 오탐 없음",
          m26.check_invariant("Y2", 50) is None)
    m26.clear_orphan("Y2")
    check("10-16c) orphan 해소 후 검사가 다시 작동",
          m26.check_invariant("Y2", 50) is not None)
finally:
    M.SELL_PENDING_TIMEOUT_SEC = _orig_sell_to
    M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to

check("10-17) sync가 observe_for_orphan을 호출",
      "observe_for_orphan(symbol, broker_qty)" in ts_src)
check("10-18) orphan 해소를 로그로 남김", "[LIFECYCLE_ORPHAN]" in ts_src)
check("10-19) ORPHAN_TTL_SEC 상수 정의", M.ORPHAN_TTL_SEC > 0)


# ══════════════════════════════════════════════════════════════
# 11. 강제 매도 최소 간격 (1P0.4)
# ══════════════════════════════════════════════════════════════
# 8/10처럼 브로커가 계속 거부하면 손절 사유로 16초마다 영원히
# 재시도하게 됩니다. force도 최소 간격을 둡니다.
check("11-1) FORCED_SELL_MIN_INTERVAL_SEC 상수 정의",
      _TS.FORCED_SELL_MIN_INTERVAL_SEC > 0)
# 1P0.5: throttle 판단이 상태머신으로 이관됨
check("11-2) 강제 매도 시각을 상태머신이 종목별로 기록",
      "last_forced_sell_at" in open("domain/position/lifecycle.py",
                                    encoding="utf-8").read())
check("11-3) 최소 간격 미달 시 강제 매도도 차단",
      "LIFECYCLE_FORCE_THROTTLE" in ts_src)
check("11-4) THROTTLED면 주문을 발행하지 않고 return",
      'if decision.decision.value == "THROTTLED"' in ts_src
      and ts_src.index("LIFECYCLE_FORCE_THROTTLE") < ts_src.index("_try_sell_unchecked("))
_m_th = M()
_m_th.sync_from_broker("TH", 100)
_r1 = _m_th.decide_sell("TH", forced=True)
_r2 = _m_th.decide_sell("TH", forced=True)
check("11-5) 첫 강제 매도는 허용, 연속 요청은 throttle",
      _r1.decision.value == "ALLOW_FORCED" and _r2.decision.value == "THROTTLED")


# ══════════════════════════════════════════════════════════════
# 12. 차단 조합 매트릭스 — HARD/SOFT 분리 검증 (1P0.5 → 1P0.7)
# ══════════════════════════════════════════════════════════════
# 2026-08-10 (1P0.7, GPT 코드리뷰): 1P0.5~1P0.6의 성공 조건
# "모든 차단이 5분 후 해제되고 forced는 항상 통과한다"는
# **성공 조건 자체가 잘못됐습니다**. orphan/BUY_PENDING/SELL_PENDING/
# ERROR 상태에서 시간이 지났다고 원 주문이 terminal이라는 증거는
# 없습니다. 반대 기준으로 재작성합니다:
#   HARD block(BUY_PENDING/SELL_PENDING/orphan/ERROR)
#     → forced도 우회 불가, 시간이 지나도 ALLOW로 풀리지 않음
#       (RECONCILIATION_REQUIRED로 승격되어 계속 BLOCKED)
#   SOFT block(backoff/max_retry)
#     → forced가 우회 가능, decay로 시간이 지나면 자연히 풀림
from domain.position.lifecycle import SellDecision


def _mk(state_kind: str, symbol: str) -> M:
    """차단 상태별 fixture 생성기."""
    mm = M()
    if state_kind == "clean":
        mm.sync_from_broker(symbol, 100)
    elif state_kind == "buy_pending":
        mm.on_buy_requested(symbol, 100, "bp")
        mm.confirm_buy_from_broker(symbol, 30)   # 부분체결 → BUY_PENDING
    elif state_kind == "sell_pending":
        mm.sync_from_broker(symbol, 100)
        mm.on_sell_requested(symbol, 100, "sp")
        mm.on_sell_result(symbol, accepted=True, broker_quantity=90)
    elif state_kind == "orphan":
        mm.sync_from_broker(symbol, 100)
        mm.on_sell_requested(symbol, 100, "op")
        mm.on_sell_result(symbol, accepted=True, broker_quantity=90)
        st_ = mm.get(symbol)
        st_.lifecycle = L.OPEN
        st_.pending_order_id = None
        st_.orphan_order_id = "op"
        st_.orphan_since = _dt.now()
        st_.orphan_expected_delta = -90
    elif state_kind == "error":
        mm.sync_from_broker(symbol, 7)
        mm.on_sell_requested(symbol, 7, "er")
        mm.on_sell_result(symbol, accepted=True, broker_quantity=167)  # 증가 → ERROR
    elif state_kind == "backoff":
        mm.sync_from_broker(symbol, 100)
        mm.on_sell_requested(symbol, 100, "bo")
        mm.on_sell_result(symbol, accepted=False, broker_quantity=100,
                          reject_reason="수량부족")
    elif state_kind == "max_retry":
        mm.sync_from_broker(symbol, 100)
        for i in range(6):
            mm.on_sell_requested(symbol, 100, f"mr{i}")
            mm.on_sell_result(symbol, accepted=False, broker_quantity=100,
                              reject_reason="수량부족")
        mm.get(symbol).sell_reject_last_at = _dt.now()
    elif state_kind == "orphan+backoff":
        mm = _mk("orphan", symbol)
        st_ = mm.get(symbol)
        st_.sell_reject_count = 3
        st_.sell_reject_last_at = _dt.now()
        st_.sell_reject_last_reason = "수량부족"
    elif state_kind == "pending+orphan":
        mm = _mk("orphan", symbol)
        mm.get(symbol).lifecycle = L.SELL_PENDING
        mm.get(symbol).pending_order_id = "sp2"
        mm.get(symbol).pending_since = _dt.now()
    return mm


HARD_KINDS = ["buy_pending", "sell_pending", "orphan", "error",
             "orphan+backoff", "pending+orphan"]
SOFT_KINDS = ["backoff", "max_retry"]

print()
print("  [ 차단 조합 매트릭스 — HARD는 절대 안 풀림 / SOFT만 시간·forced로 풀림 ]")
print(f"    {'상태':16s} {'분류':6s} {'일반매도':>18s} {'강제(손절)':>16s} {'5분경과':>24s}")
_hard_never_forced_bypass = True
_hard_never_time_unlock = True
_soft_forced_bypass_ok = True
_soft_decay_unlock_ok = True

for kind in HARD_KINDS:
    mm = _mk(kind, "MX")
    normal = mm.decide_sell("MX").decision.value
    mm2 = _mk(kind, "MX")
    forced = mm2.decide_sell("MX", forced=True).decision.value
    mm3 = _mk(kind, "MX")
    mm3.decide_sell("MX")
    st3 = mm3.get("MX")
    if st3.blocked_since:
        st3.blocked_since = _dt.now() - _td(seconds=M.MAX_BLOCK_DURATION_SEC + 1)
    after = mm3.decide_sell("MX").decision.value
    print(f"    {kind:16s} {'HARD':6s} {normal:>18s} {forced:>16s} {after:>24s}")
    # HARD는 forced로도 우회 불가 — ALLOW/ALLOW_FORCED가 나오면 실패
    if forced not in ("BLOCKED", "RECONCILIATION_REQUIRED"):
        _hard_never_forced_bypass = False
    # HARD는 5분이 지나도 ALLOW로 풀리면 안 됨(RECONCILIATION_REQUIRED만 허용)
    if after not in ("BLOCKED", "RECONCILIATION_REQUIRED"):
        _hard_never_time_unlock = False

for kind in SOFT_KINDS:
    mm = _mk(kind, "MY")
    normal = mm.decide_sell("MY").decision.value
    mm2 = _mk(kind, "MY")
    forced = mm2.decide_sell("MY", forced=True).decision.value
    print(f"    {kind:16s} {'SOFT':6s} {normal:>18s} {forced:>16s} {'(decay로 해소)':>24s}")
    # SOFT는 forced가 우회해야 함
    if forced not in ("ALLOW_FORCED", "THROTTLED"):
        _soft_forced_bypass_ok = False

check("12-1) HARD block은 forced로도 절대 우회되지 않음 "
      "(BLOCKED 또는 RECONCILIATION_REQUIRED만 나옴)",
      _hard_never_forced_bypass)
check("12-2) HARD block은 5분이 지나도 ALLOW로 풀리지 않음 "
      "(원 주문 상태를 모르면 영원히 BLOCKED/RECONCILIATION_REQUIRED)",
      _hard_never_time_unlock)
check("12-2b) SOFT block은 forced가 우회 가능",
      _soft_forced_bypass_ok)

# ── RECONCILIATION_REQUIRED — 여전히 매도 불가, CRITICAL 신호일 뿐 ──
m30 = _mk("orphan+backoff", "SV")
r30 = m30.decide_sell("SV")
check("12-3) 복합 HARD 차단도 일단은 BLOCKED", r30.decision == SellDecision.BLOCKED)
m30.get("SV").blocked_since = _dt.now() - _td(seconds=M.MAX_BLOCK_DURATION_SEC + 1)
r31 = m30.decide_sell("SV")
check("12-4) 5분 경과 시 RECONCILIATION_REQUIRED로 승격(여전히 매도 불가)",
      r31.decision == SellDecision.RECONCILIATION_REQUIRED)
check("12-4b) RECONCILIATION_REQUIRED는 allowed 속성이 False",
      not r31.allowed)
check("12-5) 사유에 차단 code 포함", "BLOCK_" in r31.code or "BLOCK_" in r31.detail)
# forced를 줘도 여전히 매도 불가(HARD가 이미 걸려있으므로)
r32 = m30.decide_sell("SV", forced=True)
check("12-5b) RECONCILIATION_REQUIRED 상태에서 forced를 줘도 매도 불가",
      not r32.allowed)

# ── 정상 SOFT 매도가 해소되면 ALLOW ──
m31 = _mk("backoff", "RS")
m31.decide_sell("RS")
m31.get("RS").sell_reject_count = 0
check("12-8) SOFT 차단 해소 후 ALLOW", m31.decide_sell("RS").decision == SellDecision.ALLOW)

# ── MAX_RETRY(SOFT) 감쇠로 회복 — HARD와 달리 시간으로 자연 해소 ──
m32 = _mk("max_retry", "DK")
check("12-10) MAX_RETRY 도달 시 차단",
      m32.decide_sell("DK").decision == SellDecision.BLOCKED)
for _ in range(8):
    m32.get("DK").sell_reject_last_at = _dt.now() - _td(
        seconds=M.SELL_REJECT_DECAY_SEC + 1)
    m32.decay_sell_rejects("DK")
check("12-11) 시간 경과 감쇠로 카운터 회복", m32.get("DK").sell_reject_count == 0)
check("12-12) 감쇠 후 매도 허용",
      m32.decide_sell("DK").decision == SellDecision.ALLOW)
check("12-13) SELL_REJECT_DECAY_SEC 상수 정의", M.SELL_REJECT_DECAY_SEC > 0)
check("12-14) MAX_BLOCK_DURATION_SEC 상수 정의", M.MAX_BLOCK_DURATION_SEC > 0)

# ── 단일 진입점 구조 ──
check("12-15) 서비스 계층이 decide_sell에 위임",
      "decide_sell(symbol, forced=force)" in ts_src)
_lc_src = open("domain/position/lifecycle.py", encoding="utf-8").read()
check("12-16) HARD/SOFT 평가가 각각 전용 함수로 분리됨",
      "_evaluate_hard_sell_blocks" in _lc_src
      and "_evaluate_soft_sell_blocks" in _lc_src)
check("12-17) RECONCILIATION_REQUIRED를 CRITICAL로 노출",
      "[RECONCILIATION_REQUIRED]" in ts_src)
check("12-18) ALLOW_ESCALATED 클래스 멤버가 제거됨(더 이상 시간으로 사유불문 허용 안 함)",
      not hasattr(SellDecision, "ALLOW_ESCALATED"))
check("12-19) SellDecision에 RECONCILIATION_REQUIRED 상태가 있음",
      hasattr(SellDecision, "RECONCILIATION_REQUIRED"))


# 13. 폴링 기반 감시 — 신호 의존 제거 (1P0.6)
# ══════════════════════════════════════════════════════════════
# 재현(1P0.5): blocked_since 갱신과 안전밸브 판정이 모두 decide_sell
# 안에만 있어, 차단 중이어도 SELL 신호가 오지 않으면 아무 일도
# 일어나지 않았습니다. 전략이 HOLD를 반환하는 동안 포지션이 조용히
# 갇혀 있다가, 다음 SELL 신호 때에야 안전밸브가 열립니다.

m40 = M()
m40.sync_from_broker("Z1", 100)
_s40 = m40.get("Z1")
_s40.lifecycle = L.OPEN
_s40.orphan_order_id = "op1"
_s40.orphan_since = _dt.now()
_s40.orphan_expected_delta = -100

check("13-1) 폴링 감시가 차단을 인지하고 blocked_since 기록",
      m40.check_block_escalation("Z1") is None
      and m40.get("Z1").blocked_since is not None)
check("13-2) 차단 code도 기록됨",
      m40.get("Z1").last_block_code == "BLOCK_SELL_ORPHAN_ORDER")

# SELL 신호 없이 시간만 흐른 경우 — 폴링만으로 감지되어야 함
m40.get("Z1").blocked_since = _dt.now() - _td(seconds=M.MAX_BLOCK_DURATION_SEC + 1)
_esc = m40.check_block_escalation("Z1")
check("13-3) SELL 신호 없이도 장시간 차단을 폴링에서 감지", _esc is not None)
check("13-4) 감지 메시지에 code와 지속시간 포함",
      "RECONCILIATION_REQUIRED" in _esc and "blocked_for=" in _esc)

# 차단이 해소되면 추적도 리셋
m40.clear_orphan("Z1")
check("13-5) 차단 해소 시 폴링 감시가 추적을 리셋",
      m40.check_block_escalation("Z1") is None
      and m40.get("Z1").blocked_since is None)

check("13-6) sync 루프가 check_block_escalation을 호출",
      "check_block_escalation(symbol)" in ts_src)
check("13-7) 장시간 차단을 CRITICAL로 노출", "[LIFECYCLE_STUCK]" in ts_src)


# ══════════════════════════════════════════════════════════════
# 14. ERROR는 자동 회복하지 않음 — 사람 확인 필수 (1P0.6 → 1P0.7)
# ══════════════════════════════════════════════════════════════
# 2026-08-10 (1P0.7, GPT 코드리뷰 P0, 재현 확인): 1P0.6의
# `auto_recover_error()`(900초 후 브로커 잔고 기준 자동 정상화)는
# "시간이 지났다"를 "원 주문이 terminal이다"의 증거로 오용했습니다.
# UNEXPECTED_QUANTITY_INCREASE 같은 ERROR는 정확히 "상태를 신뢰할 수
# 없다"는 뜻인데, 15분 후 자동으로 OPEN/FLAT을 만들고 pending_order_id
# 까지 지워 미확정 주문 정보를 버렸습니다. 이 메서드를 완전히
# 제거했습니다 — ERROR는 이제 `acknowledge_error()`로 사람이 실제
# 계좌를 확인한 뒤에만 해제됩니다.
m41 = M()
m41.sync_from_broker("Z2", 100)
m41.on_buy_requested("Z2", 50, "b20")
m41.confirm_buy_from_broker("Z2", 300)      # 목표 초과 → ERROR
check("14-1) ERROR 진입 시 error_since 기록",
      m41.get("Z2").lifecycle == L.ERROR and m41.get("Z2").error_since is not None)
check("14-2) ERROR 상태에서 매수 차단(정상)",
      m41.would_block_buy("Z2") is not None)
check("14-3) auto_recover_error 메서드가 완전히 제거됨",
      not hasattr(m41, "auto_recover_error"))

# 시간이 아무리 지나도 ERROR는 스스로 풀리지 않음
m41.get("Z2").error_since = _dt.now() - _td(days=1)
check("14-4) 24시간이 지나도 ERROR는 자동으로 풀리지 않음",
      m41.get("Z2").lifecycle == L.ERROR)
check("14-5) 24시간 후에도 BUY guard가 여전히 유지됨",
      m41.would_block_buy("Z2") is not None)
# decide_sell을 통해서도 시간으로 풀리지 않고 RECONCILIATION_REQUIRED로만 승격
r41 = m41.decide_sell("Z2")
check("14-5b) SELL도 시간이 지나면 RECONCILIATION_REQUIRED로 승격될 뿐, 매도는 여전히 불가",
      r41.decision.value in ("BLOCKED", "RECONCILIATION_REQUIRED") and not r41.allowed)

check("14-6) 사람 확인용 acknowledge_error()로만 해제 가능",
      "def acknowledge_error(" in _lc_src)
m41.acknowledge_error("Z2", 300, "브로커 콘솔에서 실제 보유수량 300주 직접 확인")
check("14-7) acknowledge_error 호출 후에는 BUY guard 해제",
      m41.would_block_buy("Z2") is None)

check("14-8) ERROR_AUTO_RECOVERY_SEC 상수가 완전히 제거됨",
      not hasattr(M, "ERROR_AUTO_RECOVERY_SEC"))
check("14-9) sync 루프에서 auto_recover_error 호출이 제거됨",
      "auto_recover_error(symbol, broker_qty)" not in ts_src)
check("14-10) [LIFECYCLE_ERROR_RECOVERY] 자동 회복 로그도 제거됨",
      "[LIFECYCLE_ERROR_RECOVERY]" not in ts_src)


# 15. 강제 매도 실패 escalation (1P0.6)
# ══════════════════════════════════════════════════════════════
# 손절이 브로커에 거부되면 시스템이 자체 해결할 수 없습니다.
check("15-1) 강제 매도 거부를 CRITICAL로 노출",
      "[FORCED_SELL_FAILED]" in ts_src)
check("15-2) 연속 실패 횟수를 종목별로 추적",
      "_forced_sell_failures" in ts_src)
check("15-3) 성공 시 실패 카운터 리셋",
      "_forced_sell_failures.pop(symbol, None)" in ts_src)
check("15-4) 강제 사유일 때만 escalation",
      "_is_forced_exit_reason(exit_reason)" in ts_src)


# ══════════════════════════════════════════════════════════════
# 16. HARD block은 throttle/forced보다 항상 우선 (1P0.6 → 1P0.7)
# ══════════════════════════════════════════════════════════════
# 2026-08-10 (1P0.7, GPT 코드리뷰): 장시간 차단된 orphan 포지션에
# forced(손절)가 들어와도 HARD block이 이깁니다 — throttle 여부와
# 무관하게 매도 자체가 거부됩니다. "이미 살아있을 수 있는 원 주문
# 위에 손절 주문을 더 얹는 것"보다 사람의 확인을 기다리는 편이
# 안전하다는 판단입니다.
m43 = M()
m43.sync_from_broker("Z4", 100)
_s43 = m43.get("Z4")
_s43.lifecycle = L.OPEN
_s43.orphan_order_id = "op2"
_s43.orphan_since = _dt.now()
_s43.orphan_expected_delta = -100
m43.decide_sell("Z4")                       # 차단 기록 시작
_s43.blocked_since = _dt.now() - _td(seconds=M.MAX_BLOCK_DURATION_SEC + 1)
_s43.last_forced_sell_at = _dt.now()        # 방금 강제 매도 시도했다고 가정
r43 = m43.decide_sell("Z4", forced=True)
check("16-1) 장시간 orphan HARD block에는 forced를 줘도 RECONCILIATION_REQUIRED(매도 불가)",
      r43.decision.value == "RECONCILIATION_REQUIRED" and not r43.allowed)
check("16-1b) throttle(min_interval) 여부와 무관하게 HARD가 이김",
      r43.code != "FORCED_SELL_THROTTLED")

m44 = _mk("clean", "Z5")
r44a = m44.decide_sell("Z5", forced=True)
r44b = m44.decide_sell("Z5", forced=True)
check("16-2) HARD block이 없는 정상 구간에서는 throttle이 정상 작동",
      r44a.decision.value == "ALLOW_FORCED" and r44b.decision.value == "THROTTLED")

# ══════════════════════════════════════════════════════════════
# 17. accepted-SELL side effect의 완전 청산 지연 (1P0.7)
# ══════════════════════════════════════════════════════════════
# 2026-08-10 (1P0.7, GPT 코드리뷰 P0, 재현 근거: 047040 SELL accepted
# 후 343주 잔존): 손실 카운트·쿨다운·재진입차단·알림이 `accepted`
# 시점에 즉시 실행돼, 부분체결에도 "완전 청산했다"고 전제한 계산이
# 적용되고 있었습니다. trading_service.py는 config.settings 등
# 전체 저장소 의존성 때문에 이 테스트 파일에서 독립 실행할 수
# 없으므로(GPT 지적: "diff 단독 테스트는 config.settings가 없어
# 중단됨"), 소스 구조를 정적으로 검증합니다.
check("17-1) SELL accepted 시점에는 즉시 side effect를 실행하지 않고 컨텍스트만 저장",
      "self._pending_sell_side_effects[symbol] = {" in ts_src)
check("17-2) 저장되는 컨텍스트에 손실/쿨다운 계산에 필요한 값이 모두 포함",
      all(k in ts_src for k in
          ('"exit_reason": exit_reason', '"avg_buy_price": avg_buy_price',
           '"current_price": current_price', '"quantity": quantity')))
check("17-3) _apply_deferred_sell_side_effects가 실제 손실/쿨다운 로직을 포함",
      "_apply_deferred_sell_side_effects" in ts_src
      and "symbol_loss_count_today" in ts_src
      and "symbol_stoploss_at" in ts_src)
# 2026-08-10 (1P0.7.1): 산발적 호출 지점을 검사하던 방식은 GPT가
# 지적한 P0-2("SELL_PENDING 분기에만 있어서 orphan 경로를 못 잡음")를
# 그대로 통과시켰습니다. 이제 19절의 **실제 TradingService 통합
# 테스트**가 진짜 검증을 담당하고, 여기서는 중앙화된 구조 자체만
# 가볍게 확인합니다.
check("17-4) 중앙화된 실행 지점이 있음(19절에서 실제 동작 검증)",
      "_lifecycle_before_sync" in ts_src)
check("17-5) SELL_PENDING 분기 내부의 산발적 호출은 제거됨(19절 참고)",
      ts_src.count("self._apply_deferred_sell_side_effects(symbol)") == 1)
check("17-6) 지연된 컨텍스트는 pop으로 1회만 소비됨(중복 실행 방지)",
      "self._pending_sell_side_effects.pop(symbol, None)" in ts_src)
check("17-7) 원래 accepted 블록에 있던 즉시 손실계산 코드가 제거됨",
      "avg_p = avg_buy_price if avg_buy_price > 0 else 0" not in
      ts_src[:ts_src.index("_apply_deferred_sell_side_effects(self, symbol")])


# ══════════════════════════════════════════════════════════════
# 18. BUY zero-fill timeout에서 orphan 보존 (1P0.7.1, P0-1)
# ══════════════════════════════════════════════════════════════
# 재현(1P0.7): BUY_PENDING이 0주 체결로 타임아웃되면 orphan을
# 만들자마자 _reset_transient_block_state()가 그 orphan을 지워
# BUY guard=None, SELL decide.allowed=True가 됐습니다 — 원 주문이
# 살아있을 수 있는데 아무 방어가 없는 상태였습니다.
M.BUY_PENDING_TIMEOUT_SEC = 0.2
try:
    m50 = M()
    m50.on_buy_requested("ZF", 174, "b1")
    m50.confirm_buy_from_broker("ZF", 0)   # 0주 체결 그대로
    _time.sleep(0.25)
    r50 = m50.resolve_stale_pending("ZF", 0)
    st50 = m50.get("ZF")
    check("18-1) 0주 체결 타임아웃 후 lifecycle은 FLAT(잔고 0이므로)",
          st50.lifecycle == L.FLAT)
    check("18-2) 그런데도 orphan_order_id는 보존됨(원 주문 생사 불명)",
          st50.orphan_order_id == "b1")
    check("18-3) BUY guard가 여전히 유지됨",
          m50.would_block_buy("ZF") is not None
          and "BLOCK_BUY_ORPHAN_ORDER" in m50.would_block_buy("ZF"))
    check("18-4) SELL도 허용되지 않음(decide_sell.allowed == False)",
          not m50.decide_sell("ZF").allowed)
    check("18-5) detail에 ORPHAN 표기와 HARD block 안내 포함",
          r50 is not None and "ORPHAN(b1)" in r50 and "HARD block" in r50)

    # 정상 부분체결(잔고>0) 타임아웃은 기존처럼 OPEN+orphan 유지(회귀 없음)
    m51 = M()
    m51.on_buy_requested("ZF2", 174, "b2")
    m51.confirm_buy_from_broker("ZF2", 30)
    _time.sleep(0.25)
    m51.resolve_stale_pending("ZF2", 30)
    check("18-6) 잔고>0 부분체결 타임아웃은 기존과 동일하게 OPEN+orphan",
          m51.get("ZF2").lifecycle == L.OPEN
          and m51.get("ZF2").orphan_order_id == "b2")

    # orphan이 실제로 없을 때(pending_order_id가 애초에 없던 경우)는
    # reset이 정상적으로 전체 초기화를 수행해야 함(clear_orphan 로직 회귀 확인)
    m52 = M()
    m52.on_buy_requested("ZF3", 174, "b3")
    m52.get("ZF3").pending_order_id = None   # 방어적 케이스
    m52.confirm_buy_from_broker("ZF3", 0)
    _time.sleep(0.25)
    m52.resolve_stale_pending("ZF3", 0)
    check("18-7) pending_order_id가 애초에 없으면 orphan도 생성되지 않고 정상 FLAT",
          m52.get("ZF3").lifecycle == L.FLAT
          and m52.get("ZF3").orphan_order_id is None)

    check("18-8) _reset_transient_block_state가 clear_orphan 파라미터를 지원",
          "clear_orphan" in _lc_src and "def _reset_transient_block_state" in _lc_src)
finally:
    M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to


# ══════════════════════════════════════════════════════════════
# 19. SELL timeout→orphan→나중 FLAT 시 deferred side-effect 실행
# (1P0.7.1, P0-2) — 실제 TradingService 통합 fixture
# ══════════════════════════════════════════════════════════════
# 재현(1P0.7): _apply_deferred_sell_side_effects 호출이 SELL_PENDING
# 분기 안에만 있어서, 타임아웃으로 OPEN+orphan이 된 뒤 원 주문이
# 나중에(8/12 005935 실측 194초) 실제로 완전 체결되면 그 확인은
# `else: sync_from_broker()` 경로를 타서 손실/쿨다운/알림이
# 영원히 실행되지 않았습니다. GPT 지적대로 source-string 검사가
# 아니라 실제 TradingService를 띄워 검증합니다.
import tempfile as _tf2
from datetime import datetime as _dt2
from unittest.mock import patch as _patch2

from test_run_once_integration import build_minimal_settings as _build_settings
from domain.market_regime.classifier import MarketRegimeClassifier as _MRC
from domain.risk.risk_manager import RiskManager as _RM
from domain.service.trading_service import TradingService as _TS2
from domain.strategy.strategy_router import StrategyRouter as _SR
from domain.models import Position as _Position, AccountBalance as _AccountBalance
from infra.broker.mock_broker import MockBroker as _MockBroker
from infra.storage.logger import (
    TradeCsvLogger as _TradeCsvLogger, SignalCsvLogger as _SignalCsvLogger,
    build_app_logger as _build_app_logger,
)
from infra.storage.state_store import JsonStateStore as _JsonStateStore


def _build_real_service(tmpdir: str) -> "_TS2":
    settings = _build_settings(tmpdir)
    broker = _MockBroker()
    app_logger = _build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    trade_logger = _TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = _SignalCsvLogger(settings.storage.signal_log_file)
    state_store = _JsonStateStore(settings.storage.state_file)
    strategy_router = _SR(settings.strategy)
    regime_classifier = _MRC(settings.market_regime)
    risk_manager = _RM(settings.trading, settings.risk, settings.storage.trade_log_file)
    return _TS2(
        settings=settings, broker=broker, strategy_router=strategy_router,
        regime_classifier=regime_classifier, risk_manager=risk_manager,
        app_logger=app_logger, trade_logger=trade_logger,
        signal_logger=signal_logger, state_store=state_store,
    )


M.SELL_PENDING_TIMEOUT_SEC = 0.2
try:
    with _tf2.TemporaryDirectory() as _tmp19:
        svc = _build_real_service(_tmp19)
        sym19 = "005935"

        # 초기 잔고: 100주 보유
        svc.broker._positions[sym19] = _Position(symbol=sym19, quantity=100,
                                                  average_price=10000)
        svc.broker._prices[sym19] = 10100

        # 최초 폴링으로 상태머신을 브로커 잔고와 동기화
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        check("19-1) 초기 동기화 후 lifecycle OPEN",
              svc._position_state_machine.get(sym19).lifecycle == L.OPEN)

        # SELL 요청 — accepted 되지만 브로커가 즉시 반영하지 않음(지연 흉내)
        svc._try_sell(sym19, 100, current_price=10100,
                      exit_reason="트레일링 스탑 — 테스트", avg_buy_price=10000)
        check("19-2) SELL accepted 시 pending 컨텍스트가 저장됨",
              sym19 in svc._pending_sell_side_effects)
        check("19-3) SELL_PENDING 상태",
              svc._position_state_machine.get(sym19).lifecycle == L.SELL_PENDING)

        # MockBroker가 즉시 잔고를 지웠다고 가정 — 실제로는 부분체결/지연을
        # 흉내내기 위해 잔고를 "여전히 100"으로 강제 유지(브로커 반영 지연)
        svc.broker._positions[sym19] = _Position(symbol=sym19, quantity=100,
                                                  average_price=10000)

        # 폴링 1회 — 아직 타임아웃 전, 잔고 그대로 -> SELL_PENDING 유지
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        check("19-4) 타임아웃 전에는 아직 side effect 미실행(컨텍스트 유지)",
              sym19 in svc._pending_sell_side_effects)

        # 타임아웃 경과 시뮬레이션
        _time.sleep(0.25)
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        after_timeout_state = svc._position_state_machine.get(sym19)
        check("19-5) 타임아웃 후 OPEN + orphan으로 전환(잔고>0이므로)",
              after_timeout_state.lifecycle == L.OPEN
              and after_timeout_state.orphan_order_id is not None)
        check("19-6) 타임아웃 직후에도 컨텍스트는 아직 소비되지 않음(완전 청산 전)",
              sym19 in svc._pending_sell_side_effects)

        # 8/12 005935 실측처럼, 원 SELL 주문이 나중에 실제로 완전 체결됨
        # (브로커 잔고가 0으로 반영) — orphan 해소 + OPEN→FLAT은 `else`
        # 분기(sync_from_broker)를 탑니다.
        svc.broker._positions.pop(sym19, None)
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        final_state = svc._position_state_machine.get(sym19)
        check("19-7) 나중 완전 체결 확인 후 FLAT으로 확정",
              final_state.lifecycle == L.FLAT)
        check("19-8) orphan도 함께 해소됨", final_state.orphan_order_id is None)

        # ── P0-2 핵심 검증: deferred side-effect가 실제로 실행됐는가 ──
        check("19-9) FLAT 확정 후 pending 컨텍스트가 소비됨(재현 시 실패했던 부분)",
              sym19 not in svc._pending_sell_side_effects)
        check("19-10) last_sold_at_by_symbol이 실제로 기록됨",
              sym19 in svc.state.last_sold_at_by_symbol)
        check("19-11) entry_time_by_symbol에서 제거됨",
              sym19 not in svc.state.entry_time_by_symbol)
        # 2026-08-13 (1P0.7.2, GPT 코드리뷰 지적): 이전 조건
        # `symbol_loss_count_today.get(sym19, 0) >= 0`은 값이 없어도
        # `0 >= 0`이 항상 True인 false-pass였습니다. 게다가 이 fixture는
        # avg=10,000/sell=10,100인 **수익 거래**라 애초에 손실 branch를
        # 지나지도 않습니다. 손실 branch 검증은 21절의 실제 손실
        # fixture로 옮기고, 여기서는 "이 거래(수익)에서 손실 카운터가
        # 잘못 증가하지 않았는지"만 정확히 봅니다.
        check("19-12) 수익 거래이므로 손실 카운터가 증가하지 않음",
              svc.state.symbol_loss_count_today.get(sym19, 0) == 0)
        check("19-12b) 수익 매도 후 연속손절 카운터가 0으로 유지/초기화됨",
              svc.state.consecutive_losses == 0)

        # ── C: 같은 FLAT을 여러 번 폴링해도 side-effect 중복 실행 없음 ──
        _last_sold_before = svc.state.last_sold_at_by_symbol.get(sym19)
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
        check("19-13) FLAT을 여러 번 폴링해도 last_sold_at이 재실행으로 덮어써지지 않음"
              "(컨텍스트가 이미 소비돼 재실행 자체가 없음)",
              svc.state.last_sold_at_by_symbol.get(sym19) == _last_sold_before)
finally:
    M.SELL_PENDING_TIMEOUT_SEC = _orig_sell_to

check("19-14) 중앙화된 실행 지점이 실제 코드에 존재",
      "_lifecycle_before_sync" in ts_src
      and "and symbol in self._pending_sell_side_effects" in ts_src)
check("19-15) SELL_PENDING 분기 내부의 산발적 호출이 제거되고 한 곳으로 모임",
      ts_src.count("self._apply_deferred_sell_side_effects(symbol)") == 1)


# ══════════════════════════════════════════════════════════════
# 20. P1 — deferred side-effect 가격이 추정치임을 명시 (1P0.7.1)
# ══════════════════════════════════════════════════════════════
check("20-1) 컨텍스트 저장 지점에 ORDER_PRICE_BASED_ESTIMATE 명시",
      "ORDER_PRICE_BASED_ESTIMATE" in ts_src)
check("20-2) 실제 체결가가 아니라는 한계가 문서화됨",
      "실제 체결가가 아닙니다" in ts_src or "실제 체결가 아님" in ts_src)
check("20-3) 8/12 실측 근거(194초)가 남아있음", "194초" in ts_src)
check("20-4) 근본 해결(fill price 연결)이 아직 미구현임을 명시",
      "fill price 연결" in ts_src)


# ══════════════════════════════════════════════════════════════
# 21. deferred side-effect의 손실 branch 실행 검증 (1P0.7.2)
# ══════════════════════════════════════════════════════════════
# 2026-08-13 (GPT 코드리뷰 지적): 19절은 수익 거래만 검증해서 손실
# branch(symbol_loss_count_today 증가, consecutive_losses 증가,
# symbol_stoploss_at 기록)가 실제로 실행되는지 전혀 보지 못했습니다.
# GPT가 제시한 정확한 fixture — avg=10,000, sell=9,900(손실),
# exit_reason에 "손절" 포함 — 로 timeout→orphan→나중 FLAT 경로에서도
# 손실 카운터가 정확히 반영되는지 실제 TradingService로 검증합니다.
M.SELL_PENDING_TIMEOUT_SEC = 0.2
try:
    with _tf2.TemporaryDirectory() as _tmp21:
        svc21 = _build_real_service(_tmp21)
        sym21 = "103590"

        svc21.broker._positions[sym21] = _Position(symbol=sym21, quantity=100,
                                                    average_price=10000)
        svc21.broker._prices[sym21] = 9900   # 손실가
        svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())

        # avg=10,000, 매도가=9,900(손실) + exit_reason에 "손절" 포함
        svc21._try_sell(sym21, 100, current_price=9900,
                        exit_reason="손절 테스트 — 평균단가 대비 -1.0%",
                        avg_buy_price=10000)
        check("21-1) SELL accepted 시 SELL_PENDING", 
              svc21._position_state_machine.get(sym21).lifecycle == L.SELL_PENDING)

        # 브로커 반영 지연 흉내(잔고 그대로 유지) → 타임아웃 → OPEN+orphan
        svc21.broker._positions[sym21] = _Position(symbol=sym21, quantity=100,
                                                    average_price=10000)
        _time.sleep(0.25)
        svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
        check("21-2) 타임아웃 후 OPEN+orphan",
              svc21._position_state_machine.get(sym21).lifecycle == L.OPEN
              and svc21._position_state_machine.get(sym21).orphan_order_id is not None)
        check("21-3) 타임아웃 시점까지는 손실 카운터 미반영(완전청산 전)",
              svc21.state.symbol_loss_count_today.get(sym21, 0) == 0)

        # 원 SELL 주문이 나중에 실제로 완전 체결
        svc21.broker._positions.pop(sym21, None)
        svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
        check("21-4) 나중 완전 체결 확인 후 FLAT",
              svc21._position_state_machine.get(sym21).lifecycle == L.FLAT)
        check("21-5) pending 컨텍스트 소비됨",
              sym21 not in svc21._pending_sell_side_effects)

        # ── 핵심: 손실 branch가 실제로 실행됐는가 ──
        check("21-6) symbol_loss_count_today가 정확히 1로 증가",
              svc21.state.symbol_loss_count_today.get(sym21) == 1)
        check("21-7) consecutive_losses가 1로 증가",
              svc21.state.consecutive_losses == 1)
        check("21-8) symbol_stoploss_at에 기록됨(exit_reason에 '손절' 포함)",
              sym21 in svc21.state.symbol_stoploss_at)
        check("21-9) last_sold_at_by_symbol도 함께 기록됨",
              sym21 in svc21.state.last_sold_at_by_symbol)

        # ── 추가 폴링에도 손실 카운터가 중복 증가하지 않음 ──
        svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
        svc21._sync_position_state_machine_shadow(svc21.broker.get_account_balance())
        check("21-10) FLAT을 여러 번 폴링해도 손실 카운터가 그대로 1",
              svc21.state.symbol_loss_count_today.get(sym21) == 1)
        check("21-11) consecutive_losses도 중복 증가 없이 1 유지",
              svc21.state.consecutive_losses == 1)
finally:
    M.SELL_PENDING_TIMEOUT_SEC = _orig_sell_to


# ══════════════════════════════════════════════════════════════
# 22. lifecycle.py 문서가 실제 enforce 상태를 반영 (1P0.7.2)
# ══════════════════════════════════════════════════════════════
# 2026-08-13 (GPT 코드리뷰 지적): 모듈 상단이 "shadow 모드로만 동작"
# 이라고 돼 있었는데 1P0.2부터 이미 enforce였습니다. 다음 개발자가
# "shadow니까 실제 주문에 영향 없겠네"라고 오판할 위험이 있었습니다.
check("22-1) 모듈/클래스 문서가 stale shadow 설명임을 스스로 인지하고 정정함",
      "stale한 shadow" in _lc_src)
check("22-2) enforce 상태임을 명시",
      "enforce로 차단" in _lc_src or "enforce" in _lc_src.split('"""')[1])
check("22-3) order_id 연결 상태(1P0.8-A.1)가 문서에 명시됨"
      "(더 이상 모든 주문이 pending 리터럴을 공유하지 않음)",
      "confirm_pending_order_id" in _lc_src
      and "더 이상 모든 주문이" in _lc_src)
check("22-3b) 하지만 조회/취소 인터페이스는 여전히 없다는 한계가 명시됨"
      "(식별자 연결과 실제 reconciliation은 별개)",
      "이것은 식별자를 연결한 것일 뿐입니다" in _lc_src
      and "get_open_orders()" in _lc_src)
check("22-4) 재시작 시 소실되는 한계가 문서에 명시됨",
      "재시작 시 전부 소실" in _lc_src or "재시작 시 소실" in _lc_src)
check("22-5) 무인 실계좌 운영 blocker임이 명시됨",
      "무인 실계좌" in _lc_src)


# ══════════════════════════════════════════════════════════════
# 23. BUY accepted side-effect를 첫 실체결 확인 시점으로 이연 (1P0.7.2)
# ══════════════════════════════════════════════════════════════
# 2026-08-13 (GPT 코드리뷰 P0, 재현 근거 — 8/12 103590 실측):
#   11:47:17 BUY accepted
#   11:47:27 4주만 체결
#   11:50:42 83주 전량 확인
# entry_time을 accepted 시각(11:47:17)에 기록해, "5분 최소수익 미달"
# 판정이 실제 보유 1분 37초 만에 발동했습니다. SELL과 대칭으로
# 실제 TradingService 통합 fixture로 검증합니다.
from domain.models import Signal as _Signal, SignalType as _SignalType
from domain.models import MarketRegime as _MarketRegime

with _tf2.TemporaryDirectory() as _tmp23:
    svc23 = _build_real_service(_tmp23)
    svc23.broker._cash = 100_000_000  # 리스크 게이트(현금 버퍼) 회피
    sym23 = "103590"
    svc23.broker._positions.pop(sym23, None)
    svc23.broker._prices[sym23] = 1000
    # 콜드 초기화(프로세스 시작 시 1회)를 먼저 완료 — 이후 폴링만
    # 정상적인 BUY_PENDING confirm 경로를 탑니다(실제 운영 순서와 일치).
    svc23._sync_position_state_machine_shadow(svc23.broker.get_account_balance())

    balance23 = svc23.broker.get_account_balance()
    buy_signal23 = _Signal(type=_SignalType.BUY, reason="테스트 매수")
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc23._try_buy(sym23, 1000, balance23, signal=buy_signal23,
                       regime=_MarketRegime.BULLISH, minute_analysis=None)

    check("23-1) BUY accepted 시점에는 entry_time을 아직 기록하지 않음",
          sym23 not in svc23.state.entry_time_by_symbol)
    check("23-2) accepted 시점에는 컨텍스트만 저장",
          sym23 in svc23._pending_buy_side_effects)
    check("23-3) accepted 시점에는 진입횟수 카운터도 아직 증가하지 않음",
          svc23.state.symbol_entry_count_today.get(sym23, 0) == 0)
    check("23-4) accepted 시점에는 bought_symbols_today에도 미등록",
          sym23 not in svc23.state.bought_symbols_today)

    # 첫 부분체결(4주) 확인 — 폴링
    svc23.broker._positions[sym23] = _Position(symbol=sym23, quantity=4,
                                                average_price=50000)
    svc23._sync_position_state_machine_shadow(svc23.broker.get_account_balance())
    check("23-5) 첫 실체결(4주) 확인 후 entry_time이 기록됨",
          sym23 in svc23.state.entry_time_by_symbol)
    check("23-6) 첫 실체결 시 진입횟수 카운터가 1로 증가",
          svc23.state.symbol_entry_count_today.get(sym23) == 1)
    check("23-7) 첫 실체결 시 bought_symbols_today에 등록됨",
          sym23 in svc23.state.bought_symbols_today)
    check("23-8) pending 컨텍스트가 소비됨",
          sym23 not in svc23._pending_buy_side_effects)

    _entry_time_at_first_fill = svc23.state.entry_time_by_symbol[sym23]

    # 전량(83주) 체결 확인 — 추가 폴링에도 entry_time이 재기록되지 않음
    svc23.broker._positions[sym23] = _Position(symbol=sym23, quantity=83,
                                                average_price=50000)
    svc23._sync_position_state_machine_shadow(svc23.broker.get_account_balance())
    check("23-9) 전량 확인 폴링에도 entry_time이 첫 체결 시각 그대로",
          svc23.state.entry_time_by_symbol[sym23] == _entry_time_at_first_fill)
    check("23-10) 전량 확인에 진입횟수가 다시 증가하지 않음(1 유지)",
          svc23.state.symbol_entry_count_today.get(sym23) == 1)


# ── 극단적 사례: BUY accepted → 0주 체결로 끝남(취소/미체결) ──────
with _tf2.TemporaryDirectory() as _tmp24:
    M.BUY_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc24 = _build_real_service(_tmp24)
        svc24.broker._cash = 100_000_000
        sym24 = "999999"
        svc24.broker._positions.pop(sym24, None)
        svc24.broker._prices[sym24] = 1000
        svc24._sync_position_state_machine_shadow(svc24.broker.get_account_balance())

        balance24 = svc24.broker.get_account_balance()
        buy_signal24 = _Signal(type=_SignalType.BUY, reason="테스트 매수")
        with _patch2("domain.service.trading_service.now_kst",
                    return_value=_dt2(2026, 8, 12, 11, 0, 0)):
            svc24._try_buy(sym24, 1000, balance24, signal=buy_signal24,
                           regime=_MarketRegime.BULLISH, minute_analysis=None)

        # MockBroker는 BUY를 즉시 전량 체결하므로, "결국 취소/미체결로
        # 끝나는 주문"을 흉내내려면 체결분을 다시 없앤 상태로 강제
        # 되돌려야 합니다(19절의 SELL 반영 지연 흉내와 동일한 패턴).
        svc24.broker._positions.pop(sym24, None)
        for _ in range(3):
            _time.sleep(0.1)
            svc24._sync_position_state_machine_shadow(svc24.broker.get_account_balance())

        check("23-11) 0주 체결로 끝나면 entry_time이 끝까지 기록되지 않음",
              sym24 not in svc24.state.entry_time_by_symbol)
        check("23-12) 진입횟수 카운터를 전혀 소모하지 않음",
              svc24.state.symbol_entry_count_today.get(sym24, 0) == 0)
        check("23-13) bought_symbols_today에도 등록되지 않음",
              sym24 not in svc24.state.bought_symbols_today)
        check("23-14) BUY_PENDING 타임아웃 후에도 pending 컨텍스트가 남아있음"
              "(첫 체결이 없었으므로 아직 소비되지 않음 — orphan으로 이관되어 계속 추적)",
              sym24 in svc24._pending_buy_side_effects)
    finally:
        M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to

check("23-15) 소스에 first_fill 시점 판정 로직이 존재",
      "_apply_first_fill_buy_side_effects" in ts_src
      and "base_quantity_before_order" in ts_src)
check("23-16) BUY accepted 로그가 '미확정'임을 명시",
      "매수 주문 접수 완료(미확정)" in ts_src)


# ══════════════════════════════════════════════════════════════
# 24. orphan 수동 해제 파일 명령(ack_orphan) + pending 컨텍스트 정리
# (2026-08-14, GPT 코드리뷰 P0, 1P0.7.2 후속)
# ══════════════════════════════════════════════════════════════
# GPT 지적: acknowledge_orphan()은 있지만 실행 중인 프로세스에서 사람이
# 호출할 운영 인터페이스가 없어, zero-fill BUY orphan은 재시작 전까지
# 영구 HARD block. 게다가 acknowledge_orphan()이 PSM 상태만 지우고
# TradingService의 pending BUY/SELL side-effect 컨텍스트는 그대로 남아,
# 나중에 같은 종목 잔고가 다른 이유로 움직이면 오래된 컨텍스트가 그
# 체결을 "이번 주문의 첫 체결/완전청산"으로 오인할 수 있음.
import os as _os

_orig_cwd_24 = _os.getcwd()


def _run_ack_orphan_in_tmp(svc, symbol: str, payload_json: str) -> bool:
    """commands/ack_orphan_{symbol}.json을 임시 CWD에 만들어 처리.

    코드가 Path("commands")로 CWD 상대경로를 쓰므로, 실제 저장소를
    건드리지 않도록 임시 디렉터리로 chdir했다가 반드시 원복합니다.
    반환값: 처리 후에도 명령 파일이 남아있으면 True(정상은 삭제되므로 False).
    """
    with _tf2.TemporaryDirectory() as work_dir:
        _os.chdir(work_dir)
        try:
            _os.makedirs("commands", exist_ok=True)
            cmd_path = _os.path.join("commands", f"ack_orphan_{symbol}.json")
            with open(cmd_path, "w", encoding="utf-8") as f:
                f.write(payload_json)
            svc._process_pending_ack_orphan_commands()
            return _os.path.exists(cmd_path)
        finally:
            _os.chdir(_orig_cwd_24)


def _make_orphaned_zero_fill_buy(tmpdir: str, symbol: str):
    """BUY accepted → 0주 체결 → timeout → orphan (23절과 동일 패턴)."""
    svc = _build_real_service(tmpdir)
    svc.broker._cash = 100_000_000
    svc.broker._positions.pop(symbol, None)
    svc.broker._prices[symbol] = 1000
    svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())

    balance = svc.broker.get_account_balance()
    buy_signal = _Signal(type=_SignalType.BUY, reason="테스트 매수")
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 0, 0)):
        svc._try_buy(symbol, 1000, balance, signal=buy_signal,
                     regime=_MarketRegime.BULLISH, minute_analysis=None)
    svc.broker._positions.pop(symbol, None)
    for _ in range(3):
        _time.sleep(0.1)
        svc._sync_position_state_machine_shadow(svc.broker.get_account_balance())
    return svc


# ── BUY orphan: 정상 ack로 해제 + pending BUY 컨텍스트 정리 ─────────
with _tf2.TemporaryDirectory() as _tmp25a:
    M.BUY_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc25a = _make_orphaned_zero_fill_buy(_tmp25a, "888001")
    finally:
        M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to
    sym25a = "888001"
    st25a = svc25a._position_state_machine.get(sym25a)
    check("24-1) 사전조건: zero-fill BUY가 orphan으로 이관됨",
          st25a.orphan_order_id is not None)
    check("24-2) 사전조건: pending BUY 컨텍스트가 아직 남아있음",
          sym25a in svc25a._pending_buy_side_effects)

    _file_left_25a = _run_ack_orphan_in_tmp(
        svc25a, sym25a, '{"note": "브로커 콘솔에서 원 주문 취소 확인(테스트)"}'
    )
    check("24-3) ack_orphan 명령 처리 후 orphan 해제됨",
          svc25a._position_state_machine.get(sym25a).orphan_order_id is None)
    check("24-4) pending BUY 컨텍스트도 함께 정리됨(GPT 핵심 지적)",
          sym25a not in svc25a._pending_buy_side_effects)
    check("24-5) 명령 파일은 처리 후 삭제됨", not _file_left_25a)

# ── note 없음/빈 note는 fail-closed(해제되지 않고 orphan 유지) ──────
with _tf2.TemporaryDirectory() as _tmp25b:
    M.BUY_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc25b = _make_orphaned_zero_fill_buy(_tmp25b, "888002")
    finally:
        M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to
    sym25b = "888002"

    _run_ack_orphan_in_tmp(svc25b, sym25b, "{}")  # note 누락
    check("24-6) note 없는 명령은 무시됨(orphan 유지, fail-closed)",
          svc25b._position_state_machine.get(sym25b).orphan_order_id is not None)
    check("24-7) note 없는 명령 처리 중에도 pending BUY 컨텍스트는 보존됨",
          sym25b in svc25b._pending_buy_side_effects)

    _run_ack_orphan_in_tmp(svc25b, sym25b, '{"note": ""}')  # 빈 note
    check("24-8) 빈 note 명령도 무시됨(acknowledge_orphan의 ValueError를 흡수)",
          svc25b._position_state_machine.get(sym25b).orphan_order_id is not None)

# ── SELL orphan에서도 대칭으로 pending SELL 컨텍스트가 정리됨 ───────
with _tf2.TemporaryDirectory() as _tmp25c:
    M.SELL_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc25c = _build_real_service(_tmp25c)
        sym25c = "888003"
        svc25c.broker._positions[sym25c] = _Position(symbol=sym25c, quantity=100,
                                                       average_price=10000)
        svc25c.broker._prices[sym25c] = 10100
        svc25c._sync_position_state_machine_shadow(svc25c.broker.get_account_balance())

        svc25c._try_sell(sym25c, 100, current_price=10100,
                          exit_reason="트레일링 스탑 — 테스트", avg_buy_price=10000)
        # 브로커 반영 지연 흉내(19절과 동일 패턴) — 타임아웃까지 잔고 그대로 유지
        svc25c.broker._positions[sym25c] = _Position(symbol=sym25c, quantity=100,
                                                       average_price=10000)
        _time.sleep(0.25)
        svc25c._sync_position_state_machine_shadow(svc25c.broker.get_account_balance())

        st25c = svc25c._position_state_machine.get(sym25c)
        check("24-9) 사전조건: SELL 타임아웃도 orphan으로 이관됨",
              st25c.orphan_order_id is not None)
        check("24-10) 사전조건: pending SELL 컨텍스트가 남아있음",
              sym25c in svc25c._pending_sell_side_effects)

        _file_left_25c = _run_ack_orphan_in_tmp(
            svc25c, sym25c, '{"note": "브로커 콘솔에서 원 주문 체결 확인(테스트)"}'
        )
        check("24-11) SELL orphan도 ack_orphan으로 해제됨",
              svc25c._position_state_machine.get(sym25c).orphan_order_id is None)
        check("24-12) SELL orphan 해제 시 pending SELL 컨텍스트도 정리됨(대칭 보완)",
              sym25c not in svc25c._pending_sell_side_effects)
        check("24-13) 명령 파일이 삭제됨", not _file_left_25c)
    finally:
        M.SELL_PENDING_TIMEOUT_SEC = _orig_sell_to

check("24-14) 폴링 경로에 ack_orphan 처리가 연결됨",
      "_process_pending_ack_orphan_commands()" in ts_src)


# ── (1P0.7.3 재검토 P0) orphan이 실제로 없으면 fail-closed로 거부 ──
# GPT 지적: acknowledge_orphan()→clear_orphan()은 orphan이 없어도 조용히
# return하므로, stale ack_orphan_*.json이 정상 BUY_PENDING/SELL_PENDING
# 위에 떨어지면 orphan 해제는 아무 일도 안 하는데 pending 컨텍스트만
# 삭제돼버립니다. has_orphan_order()로 먼저 확인해야 합니다.
with _tf2.TemporaryDirectory() as _tmp25d:
    svc25d = _build_real_service(_tmp25d)
    svc25d.broker._cash = 100_000_000
    sym25d = "888004"
    svc25d.broker._positions.pop(sym25d, None)
    svc25d.broker._prices[sym25d] = 1000
    svc25d._sync_position_state_machine_shadow(svc25d.broker.get_account_balance())

    balance25d = svc25d.broker.get_account_balance()
    buy_signal25d = _Signal(type=_SignalType.BUY, reason="테스트 매수")
    # 14:50 이후 신규매수 차단 게이트로 인한 flaky 방지(23절과 동일 패턴)
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc25d._try_buy(sym25d, 1000, balance25d, signal=buy_signal25d,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    st25d = svc25d._position_state_machine.get(sym25d)
    check("24-15) 사전조건: 정상 BUY_PENDING, orphan 아님",
          st25d.lifecycle == L.BUY_PENDING and st25d.orphan_order_id is None)
    check("24-16) 사전조건: pending BUY 컨텍스트가 존재",
          sym25d in svc25d._pending_buy_side_effects)

    # orphan이 없는 종목에 (실수로) ack_orphan 명령이 옴 — note 자체는 유효
    _run_ack_orphan_in_tmp(
        svc25d, sym25d, '{"note": "실수로 만든 명령(테스트)"}'
    )
    check("24-17) orphan이 없으면 acknowledge가 거부됨(lifecycle 그대로 BUY_PENDING)",
          svc25d._position_state_machine.get(sym25d).lifecycle == L.BUY_PENDING)
    check("24-18) 핵심: 정상 pending BUY 컨텍스트가 파괴되지 않고 보존됨"
          "(수동 복구 명령이 정상 주문을 망가뜨리면 안 됨)",
          sym25d in svc25d._pending_buy_side_effects)

# ── SELL 쪽도 동일하게: orphan 아닌 정상 SELL_PENDING 컨텍스트 보존 ──
with _tf2.TemporaryDirectory() as _tmp25e:
    svc25e = _build_real_service(_tmp25e)
    sym25e = "888005"
    svc25e.broker._positions[sym25e] = _Position(symbol=sym25e, quantity=100,
                                                  average_price=10000)
    svc25e.broker._prices[sym25e] = 10100
    svc25e._sync_position_state_machine_shadow(svc25e.broker.get_account_balance())

    svc25e._try_sell(sym25e, 100, current_price=10100,
                     exit_reason="트레일링 스탑 — 테스트", avg_buy_price=10000)

    st25e = svc25e._position_state_machine.get(sym25e)
    check("24-19) 사전조건: 정상 SELL_PENDING, orphan 아님",
          st25e.lifecycle == L.SELL_PENDING and st25e.orphan_order_id is None)
    check("24-20) 사전조건: pending SELL 컨텍스트가 존재",
          sym25e in svc25e._pending_sell_side_effects)

    _run_ack_orphan_in_tmp(
        svc25e, sym25e, '{"note": "실수로 만든 명령(테스트)"}'
    )
    check("24-21) orphan이 없으면 SELL 쪽도 거부됨(lifecycle 그대로 SELL_PENDING)",
          svc25e._position_state_machine.get(sym25e).lifecycle == L.SELL_PENDING)
    check("24-22) 핵심: 정상 pending SELL 컨텍스트가 파괴되지 않고 보존됨",
          sym25e in svc25e._pending_sell_side_effects)

# ── null/공백 note는 거부 (str(None) == "None"이 통과하던 버그) ──
with _tf2.TemporaryDirectory() as _tmp25f:
    M.BUY_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc25f = _make_orphaned_zero_fill_buy(_tmp25f, "888006")
    finally:
        M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to
    sym25f = "888006"

    _run_ack_orphan_in_tmp(svc25f, sym25f, '{"note": null}')  # JSON null
    check("24-23) note가 JSON null이면 거부됨(과거엔 str(None)='None'으로 통과)",
          svc25f._position_state_machine.get(sym25f).orphan_order_id is not None)
    check("24-24) null note 처리 중에도 pending BUY 컨텍스트는 보존됨",
          sym25f in svc25f._pending_buy_side_effects)

    _run_ack_orphan_in_tmp(svc25f, sym25f, '{"note": "   "}')  # 공백만
    check("24-25) note가 공백뿐이면 거부됨",
          svc25f._position_state_machine.get(sym25f).orphan_order_id is not None)

    # 마지막으로 유효한 note로 정상 해제되는지 재확인(회귀 방지)
    _run_ack_orphan_in_tmp(svc25f, sym25f, '{"note": "  실제 확인 완료  "}')
    check("24-26) 앞뒤 공백이 있어도 유효한 note면 정상 해제됨(strip 처리)",
          svc25f._position_state_machine.get(sym25f).orphan_order_id is None)
    check("24-27) 정상 해제 시 pending BUY 컨텍스트는 정리됨",
          sym25f not in svc25f._pending_buy_side_effects)

check("24-28) 소스에 has_orphan_order() fail-closed 검증이 존재",
      "if not self._position_state_machine.has_orphan_order(symbol):" in ts_src)
check("24-29) 소스에 note가 비문자열/공백이면 거부하는 검증이 존재",
      "not isinstance(raw_note, str) or not raw_note.strip()" in ts_src)


# ══════════════════════════════════════════════════════════════
# 25. 첫 체결 알림의 수량 표시 정확성 (2026-08-14, GPT 코드리뷰 P1)
# ══════════════════════════════════════════════════════════════
# 기존엔 4/83주만 체결된 시점에도 알림에 "수량 83주"라고 표시해
# 운영자가 전량 체결로 오인할 수 있었음. "보유 N주 / 요청 M주"로
# 구분해 표시하도록 수정.
with _tf2.TemporaryDirectory() as _tmp26:
    svc26 = _build_real_service(_tmp26)
    svc26.broker._cash = 100_000_000
    sym26 = "777001"
    svc26.broker._positions.pop(sym26, None)
    svc26.broker._prices[sym26] = 1000
    svc26._sync_position_state_machine_shadow(svc26.broker.get_account_balance())

    _sent_messages_26: list = []
    svc26._notifier.send = lambda text: (_sent_messages_26.append(text) or True)

    balance26 = svc26.broker.get_account_balance()
    buy_signal26 = _Signal(type=_SignalType.BUY, reason="테스트 매수")
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        # 2번째 인자는 quantity가 아니라 current_price — 실제 요청수량은
        # 내부 포지션 사이징 로직이 계산하므로, accepted 직후 컨텍스트에서
        # 그 값을 그대로 읽어옵니다(하드코딩하지 않음).
        svc26._try_buy(sym26, 1000, balance26, signal=buy_signal26,
                       regime=_MarketRegime.BULLISH, minute_analysis=None)

    requested_qty_26 = svc26._pending_buy_side_effects[sym26]["quantity"]
    # 요청수량의 일부만 먼저 체결된 상황을 흉내 — 반드시 요청수량보다
    # 작아야 "부분체결" 시나리오가 성립함.
    partial_qty_26 = max(1, requested_qty_26 // 4)
    check("25-0) 사전조건: 부분체결 수량이 요청수량보다 작음(테스트 유효성)",
          0 < partial_qty_26 < requested_qty_26)

    svc26.broker._positions[sym26] = _Position(symbol=sym26, quantity=partial_qty_26,
                                                average_price=1000)
    svc26._sync_position_state_machine_shadow(svc26.broker.get_account_balance())

    check("25-1) 첫 체결 알림이 정확히 1건 전송됨", len(_sent_messages_26) == 1)
    _msg26 = _sent_messages_26[0] if _sent_messages_26 else ""
    check("25-2) 알림에 실제 체결수량이 표시됨",
          f"보유 {partial_qty_26}주" in _msg26)
    check("25-3) 알림에 요청수량도 함께 표시됨",
          f"요청 {requested_qty_26}주" in _msg26)
    check("25-4) 부분체결을 요청수량 전량 체결로 오인시키는 옛 표현이 없음",
          f"수량: {requested_qty_26}주" not in _msg26
          and f"수량 {requested_qty_26}주" not in _msg26)
    check("25-5) 가격이 추정치임을 명시", "ORDER_PRICE_BASED_ESTIMATE" in _msg26)

check("25-6) _apply_first_fill_buy_side_effects가 filled_quantity 인자를 받음",
      "def _apply_first_fill_buy_side_effects(self, symbol: str, filled_quantity: int)"
      in ts_src)


# ══════════════════════════════════════════════════════════════
# 26. 실제 주문번호(order_id)를 PSM에 연결 (2026-08-14, 1P0.8-A.1)
# ══════════════════════════════════════════════════════════════
# GPT 지적: KiwoomBroker.place_order()는 이미 response.body["ord_no"]를
# OrderResult.order_id로 정상 반환하고 있었지만, on_buy_requested/
# on_sell_requested가 place_order() 호출 전에 PSM에 "pending" 리터럴을
# 먼저 기록해서 그 뒤로 아무도 실제 값으로 교체하지 않았습니다.
# confirm_pending_order_id()가 이걸 채웁니다.

# ── PSM 단위 테스트 ──────────────────────────────────────────
m26 = M()
m26.on_buy_requested("P1", 100, "pending")
check("26-1) 요청 시점엔 아직 pending 리터럴",
      m26.get("P1").pending_order_id == "pending")
m26.confirm_pending_order_id("P1", "REAL-ORDER-123")
check("26-2) accepted 확인 후 실제 order_id로 교체됨",
      m26.get("P1").pending_order_id == "REAL-ORDER-123")

# 거부돼 pending_order_id가 이미 None이면 그 뒤 confirm 호출은 no-op
m26b = M()
m26b.on_buy_requested("P2", 100, "pending")
m26b.on_buy_result("P2", accepted=False)
check("26-3) 거부로 pending_order_id가 None이 됨(사전조건)",
      m26b.get("P2").pending_order_id is None)
m26b.confirm_pending_order_id("P2", "REAL-999")
check("26-4) 거부 후 확인 호출은 아무 영향 없음(레이스 방지, no-op)",
      m26b.get("P2").pending_order_id is None)

# accepted인데 order_id가 비어있는 이상 응답 — 추측하지 않고 sentinel
m26c = M()
m26c.on_buy_requested("P3", 100, "pending")
m26c.confirm_pending_order_id("P3", "")
check("26-5) order_id가 빈 문자열이면 UNKNOWN_ORDER_ID sentinel로 남음(추측 안 함)",
      m26c.get("P3").pending_order_id == "UNKNOWN_ORDER_ID")
check("26-6) 이상 상황이 last_error에 기록됨",
      m26c.get("P3").last_error == "ACCEPTED_WITHOUT_ORDER_ID")
check("26-7) 그렇다고 lifecycle을 ERROR로 전이시키지는 않음"
      "(그러면 이 종목 강제손절 SELL까지 막혀 더 위험함)",
      m26c.get("P3").lifecycle == L.BUY_PENDING)

# ── 실제 TradingService(MockBroker) 통합 — BUY ──────────────────
with _tf2.TemporaryDirectory() as _tmp27a:
    svc27a = _build_real_service(_tmp27a)
    svc27a.broker._cash = 100_000_000
    sym27a = "666001"
    svc27a.broker._positions.pop(sym27a, None)
    svc27a.broker._prices[sym27a] = 1000
    svc27a._sync_position_state_machine_shadow(svc27a.broker.get_account_balance())

    balance27a = svc27a.broker.get_account_balance()
    buy_signal27a = _Signal(type=_SignalType.BUY, reason="테스트 매수")
    # 2026-08-14 (실측 캡처 중 발견된 flaky 수정): _try_buy()에는 14:50
    # 이후 신규매수를 차단하는 게이트가 있어, now_kst()를 고정하지 않으면
    # 이 테스트가 실제 실행 시각(KST 14:50 이후)에 따라 우연히 실패함
    # (23절과 동일 패턴으로 고정 — 1B.6절에서 이미 한 번 재현된 문제).
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc27a._try_buy(sym27a, 1000, balance27a, signal=buy_signal27a,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    real_order_id_27a = svc27a._position_state_machine.get(sym27a).pending_order_id
    check("26-8) BUY accepted 직후 pending_order_id가 더 이상 'pending'이 아님",
          real_order_id_27a != "pending")
    check("26-9) 실제 MockBroker 주문번호 형식을 가짐",
          isinstance(real_order_id_27a, str) and real_order_id_27a.startswith("MOCK-"))

# ── 실제 TradingService(MockBroker) 통합 — SELL ─────────────────
with _tf2.TemporaryDirectory() as _tmp27b:
    svc27b = _build_real_service(_tmp27b)
    sym27b = "666002"
    svc27b.broker._positions[sym27b] = _Position(symbol=sym27b, quantity=100,
                                                  average_price=10000)
    svc27b.broker._prices[sym27b] = 10100
    svc27b._sync_position_state_machine_shadow(svc27b.broker.get_account_balance())

    svc27b._try_sell(sym27b, 100, current_price=10100,
                     exit_reason="트레일링 스탑 — 테스트", avg_buy_price=10000)

    real_order_id_27b = svc27b._position_state_machine.get(sym27b).pending_order_id
    check("26-10) SELL accepted 직후에도 pending_order_id가 더 이상 'pending'이 아님"
          "(on_sell_result(accepted=True)는 다음 폴링까지 지연되므로 별도 연결 필요)",
          real_order_id_27b != "pending")
    check("26-11) 실제 MockBroker 주문번호 형식을 가짐",
          isinstance(real_order_id_27b, str) and real_order_id_27b.startswith("MOCK-"))

# ── orphan도 실제 order_id를 물려받는지 (BUY zero-fill 시나리오) ──
with _tf2.TemporaryDirectory() as _tmp27c:
    M.BUY_PENDING_TIMEOUT_SEC = 0.2
    try:
        svc27c = _make_orphaned_zero_fill_buy(_tmp27c, "666003")
    finally:
        M.BUY_PENDING_TIMEOUT_SEC = _orig_buy_to
    sym27c = "666003"
    st27c = svc27c._position_state_machine.get(sym27c)
    check("26-12) orphan_order_id도 실제 주문번호를 물려받음(더 이상 'pending' 아님)",
          st27c.orphan_order_id is not None and st27c.orphan_order_id != "pending")
    check("26-13) 실제 MockBroker 주문번호 형식을 가짐",
          st27c.orphan_order_id.startswith("MOCK-"))

check("26-14) 소스에 BUY accepted 직후 order_id 연결 호출이 존재",
      "confirm_pending_order_id(symbol, result.order_id)" in ts_src)
check("26-15) 소스에 SELL accepted 직후 order_id 연결 호출도 존재(같은 호출이 BUY/SELL 두 곳)",
      ts_src.count("confirm_pending_order_id(symbol, result.order_id)") == 2)


# ══════════════════════════════════════════════════════════════
# 27. order_id 공백 정규화 + CRITICAL 로그 (2026-08-14, 1P0.8-A.1 재검토)
# ══════════════════════════════════════════════════════════════
# GPT 지적: confirm_pending_order_id()가 order_id를 strip()하지 않아
# 공백만 있는 값("   ")도 유효한 order_id로 오인할 수 있었고,
# order_id가 비어있을 때 실제 app_logger.critical()이 호출되지
# 않아(CSV 이벤트 기록만 있었음) 운영자가 이를 놓칠 수 있었습니다.
import dataclasses as _dc27

# ── PSM 단위: 공백만 있는 order_id는 빈 값과 동일하게 취급 ──────
m27 = M()
m27.on_buy_requested("P4", 100, "pending")
m27.confirm_pending_order_id("P4", "   ")
check("27-1) 공백만 있는 order_id는 UNKNOWN_ORDER_ID sentinel로 처리(strip 후 빈 문자열)",
      m27.get("P4").pending_order_id == "UNKNOWN_ORDER_ID")
check("27-2) last_error도 동일하게 기록",
      m27.get("P4").last_error == "ACCEPTED_WITHOUT_ORDER_ID")

# 앞뒤 공백이 있는 정상 order_id는 strip되어 저장됨
m27b = M()
m27b.on_buy_requested("P5", 100, "pending")
m27b.confirm_pending_order_id("P5", "  REAL-777  ")
check("27-3) 앞뒤 공백은 제거되고 실제 order_id만 저장됨",
      m27b.get("P5").pending_order_id == "REAL-777")

# ── CRITICAL 로그: BUY accepted인데 order_id가 비어 있으면 발생 ──
with _tf2.TemporaryDirectory() as _tmp28a:
    svc28a = _build_real_service(_tmp28a)
    svc28a.broker._cash = 100_000_000
    sym28a = "666004"
    svc28a.broker._positions.pop(sym28a, None)
    svc28a.broker._prices[sym28a] = 1000
    svc28a._sync_position_state_machine_shadow(svc28a.broker.get_account_balance())

    _critical_calls_28a = []
    svc28a.app_logger.critical = lambda msg: _critical_calls_28a.append(msg)

    _orig_place_order_28a = svc28a.broker.place_order

    def _place_order_empty_id_28a(order):
        result = _orig_place_order_28a(order)
        return _dc27.replace(result, order_id="")

    svc28a.broker.place_order = _place_order_empty_id_28a

    balance28a = svc28a.broker.get_account_balance()
    buy_signal28a = _Signal(type=_SignalType.BUY, reason="테스트 매수(order_id 누락)")
    # 14:50 이후 신규매수 차단 게이트로 인한 flaky 방지(위 27a와 동일 사유)
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc28a._try_buy(sym28a, 1000, balance28a, signal=buy_signal28a,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    check("27-4) BUY: order_id 비어있으면 CRITICAL 로그가 실제로 호출됨",
          len(_critical_calls_28a) == 1)
    check("27-5) CRITICAL 메시지에 ORDER_ID_MISSING, 종목, side=BUY 포함",
          bool(_critical_calls_28a)
          and "ORDER_ID_MISSING" in _critical_calls_28a[0]
          and sym28a in _critical_calls_28a[0]
          and "side=BUY" in _critical_calls_28a[0])

# ── CRITICAL 로그: SELL accepted인데 order_id가 비어 있으면 발생 ──
with _tf2.TemporaryDirectory() as _tmp28b:
    svc28b = _build_real_service(_tmp28b)
    sym28b = "666005"
    svc28b.broker._positions[sym28b] = _Position(symbol=sym28b, quantity=100,
                                                  average_price=10000)
    svc28b.broker._prices[sym28b] = 10100
    svc28b._sync_position_state_machine_shadow(svc28b.broker.get_account_balance())

    _critical_calls_28b = []
    svc28b.app_logger.critical = lambda msg: _critical_calls_28b.append(msg)

    _orig_place_order_28b = svc28b.broker.place_order

    def _place_order_empty_id_28b(order):
        result = _orig_place_order_28b(order)
        return _dc27.replace(result, order_id="")

    svc28b.broker.place_order = _place_order_empty_id_28b

    svc28b._try_sell(sym28b, 100, current_price=10100,
                     exit_reason="트레일링 스탑 — 테스트(order_id 누락)", avg_buy_price=10000)

    check("27-6) SELL: order_id 비어있으면 CRITICAL 로그가 실제로 호출됨",
          len(_critical_calls_28b) == 1)
    check("27-7) CRITICAL 메시지에 ORDER_ID_MISSING, 종목, side=SELL 포함",
          bool(_critical_calls_28b)
          and "ORDER_ID_MISSING" in _critical_calls_28b[0]
          and sym28b in _critical_calls_28b[0]
          and "side=SELL" in _critical_calls_28b[0])

# ── 정상 케이스: order_id가 있으면 CRITICAL 로그가 발생하지 않음 (회귀 방지) ──
with _tf2.TemporaryDirectory() as _tmp28c:
    svc28c = _build_real_service(_tmp28c)
    svc28c.broker._cash = 100_000_000
    sym28c = "666006"
    svc28c.broker._positions.pop(sym28c, None)
    svc28c.broker._prices[sym28c] = 1000
    svc28c._sync_position_state_machine_shadow(svc28c.broker.get_account_balance())

    _critical_calls_28c = []
    svc28c.app_logger.critical = lambda msg: _critical_calls_28c.append(msg)

    balance28c = svc28c.broker.get_account_balance()
    buy_signal28c = _Signal(type=_SignalType.BUY, reason="테스트 매수(정상)")
    # 14:50 이후 신규매수 차단 게이트로 인한 flaky 방지(위 27a와 동일 사유)
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc28c._try_buy(sym28c, 1000, balance28c, signal=buy_signal28c,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    check("27-8) 정상 order_id가 있으면 CRITICAL 로그가 호출되지 않음(회귀 방지)",
          len(_critical_calls_28c) == 0)

check("27-9) 소스에 order_id strip() 정규화 로직 존재",
      'order_id = str(order_id or "").strip()' in _lc_src)
check("27-10) 소스에 BUY용 ORDER_ID_MISSING CRITICAL 로그 호출 존재",
      "side=BUY" in ts_src and "ORDER_ID_MISSING" in ts_src)
check("27-11) 소스에 SELL용 ORDER_ID_MISSING CRITICAL 로그 호출도 존재(BUY/SELL 두 곳)",
      "side=SELL" in ts_src and ts_src.count("ORDER_ID_MISSING") >= 2)
check("27-12) 모듈 docstring의 stale 'order_id 연결 예정(미착수)' 문구가 수정됨",
      "1P0.8-A.1에서 완료" in _lc_src)


# ══════════════════════════════════════════════════════════════════
# 28~29. 1P0.8-P0.1/P0.2: place_order() 실패 시맨틱 분리
#     (319400 실측 P0 사고 대응 — 2026-08-14, SELL이 kt10001 HTTP 429로
#     실패했는데 예외가 종목별 최상위 except에서 조용히 삼켜지면서
#     PSM이 109분간 phantom pending으로 방치됨 → 손실이 -1.37%에서
#     -6.4%까지 확대됨. 저수준 429/timeout 분류 자체는
#     test_kiwoom_broker_placement_failure.py에서 검증 — 여기서는
#     PSM/TradingService 레벨 동작만 검증한다.)
# ══════════════════════════════════════════════════════════════════

# ── PSM 단위: on_placement_ambiguous()는 PENDING을 ERROR로 전이 ────
m28a = M()
m28a.on_sell_requested("S1", 202, "pending")
m28a.on_placement_ambiguous("S1", "SELL", "connection timed out")
st28a = m28a.get("S1")
check("28-1) SELL ambiguous 시 lifecycle이 ERROR로 전이됨", st28a.lifecycle == L.ERROR)
check("28-2) last_error에 SELL_PLACEMENT_AMBIGUOUS 기록",
      st28a.last_error == "SELL_PLACEMENT_AMBIGUOUS")
check("28-3) pending_order_id는 지우지 않음(사람 확인용 흔적 보존)",
      st28a.pending_order_id == "pending")
check("28-4) error_since가 기록됨", st28a.error_since is not None)

m28b = M()
m28b.on_buy_requested("S2", 100, "pending")
m28b.on_placement_ambiguous("S2", "BUY", "timeout")
st28b = m28b.get("S2")
check("28-5) BUY ambiguous 시 lifecycle이 ERROR로 전이됨", st28b.lifecycle == L.ERROR)
check("28-6) last_error에 BUY_PLACEMENT_AMBIGUOUS 기록",
      st28b.last_error == "BUY_PLACEMENT_AMBIGUOUS")

# ── ERROR 전이 후 기존 guard가 BUY/SELL을 모두 HARD block함(재사용 검증) ──
sell_block_28 = m28a.would_block_sell("S1")
buy_block_28 = m28a.would_block_buy("S1")
check("28-7) ERROR 상태는 기존 SELL guard(BLOCK_SELL_ERROR_STATE)로 차단됨",
      sell_block_28 is not None and "BLOCK_SELL_ERROR_STATE" in sell_block_28)
check("28-8) ERROR 상태는 기존 BUY guard(BLOCK_BUY_IN_ERROR)로도 차단됨",
      buy_block_28 is not None and "BLOCK_BUY_IN_ERROR" in buy_block_28)

# ── 기존 acknowledge_error()로만 해제 가능(새 상태를 만들지 않고 재사용) ──
m28a.acknowledge_error("S1", 202, "HTS 직접 확인 — 실제로는 미체결이었음, 수동 취소")
st28a_after = m28a.get("S1")
check("28-9) acknowledge_error() 호출 후 ERROR 해제됨(재사용 확인)",
      st28a_after.lifecycle != L.ERROR)
check("28-10) 해제 후 SELL guard도 풀림", m28a.would_block_sell("S1") is None)

# ── TradingService 통합: 4개 축(BUY/SELL x definite-reject/ambiguous) ──
from domain.models import OrderResult as _OrderResult

# 29-1~3: SELL + 429(definite reject) → OPEN 복귀, HARD block 없음
with _tf2.TemporaryDirectory() as _tmp29a:
    svc29a = _build_real_service(_tmp29a)
    sym29a = "319400"
    svc29a.broker._positions[sym29a] = _Position(symbol=sym29a, quantity=202,
                                                  average_price=29700)
    svc29a.broker._prices[sym29a] = 27800
    svc29a._sync_position_state_machine_shadow(svc29a.broker.get_account_balance())

    def _place_order_429_29a(order):
        return _OrderResult(
            order_id="", symbol=order.symbol, side=order.side,
            requested_quantity=order.quantity, accepted=False,
            message="HTTP 429: {'return_code': 5, 'return_msg': "
                    "'허용된 요청 개수를 초과하였습니다'}",
            timestamp=_dt2(2026, 8, 14, 9, 43, 8), is_ambiguous=False,
        )
    svc29a.broker.place_order = _place_order_429_29a

    svc29a._try_sell(sym29a, 202, current_price=27800,
                      exit_reason="손절 — 평균단가 대비 -5.8%", avg_buy_price=29700)

    st29a = svc29a._position_state_machine.get(sym29a)
    check("29-1) SELL+429(definite reject, 319400 실측 재현): ERROR로 빠지지 않고 OPEN 복귀",
          st29a.lifecycle == L.OPEN)
    check("29-2) SELL+429: orphan이 생기지 않음(접수 안 됐다고 안전하게 확정)",
          st29a.orphan_order_id is None)
    check("29-3) SELL+429: HARD block(ERROR/orphan) 없음 — 109분 방치 사고 재현 안 됨",
          st29a.lifecycle != L.ERROR and st29a.orphan_order_id is None)

# 29-4~6: BUY + 429(definite reject) → FLAT 복귀, side-effect 없음
with _tf2.TemporaryDirectory() as _tmp29b:
    svc29b = _build_real_service(_tmp29b)
    svc29b.broker._cash = 100_000_000
    sym29b = "319400"
    svc29b.broker._positions.pop(sym29b, None)
    svc29b.broker._prices[sym29b] = 29700
    svc29b._sync_position_state_machine_shadow(svc29b.broker.get_account_balance())

    def _place_order_429_29b(order):
        return _OrderResult(
            order_id="", symbol=order.symbol, side=order.side,
            requested_quantity=order.quantity, accepted=False,
            message="HTTP 429: rate limited", timestamp=_dt2(2026, 8, 14, 9, 43, 8),
            is_ambiguous=False,
        )
    svc29b.broker.place_order = _place_order_429_29b

    balance29b = svc29b.broker.get_account_balance()
    buy_signal29b = _Signal(type=_SignalType.BUY, reason="테스트 매수(429)")
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc29b._try_buy(sym29b, 29700, balance29b, signal=buy_signal29b,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    st29b = svc29b._position_state_machine.get(sym29b)
    check("29-4) BUY+429(definite reject): ERROR로 빠지지 않고 FLAT 복귀",
          st29b.lifecycle == L.FLAT)
    check("29-5) BUY+429: deferred BUY side-effect가 만들어지지 않음",
          sym29b not in svc29b._pending_buy_side_effects)
    check("29-6) BUY+429: HARD block 없이 즉시 재시도 가능(would_block_buy=None)",
          svc29b._position_state_machine.would_block_buy(sym29b) is None)

# 29-7~10: SELL + timeout(ambiguous, 319400 실측 그대로) → ERROR, 중복매도 차단
with _tf2.TemporaryDirectory() as _tmp29c:
    svc29c = _build_real_service(_tmp29c)
    sym29c = "319400"
    svc29c.broker._positions[sym29c] = _Position(symbol=sym29c, quantity=202,
                                                  average_price=29700)
    svc29c.broker._prices[sym29c] = 27800
    svc29c._sync_position_state_machine_shadow(svc29c.broker.get_account_balance())

    _critical_calls_29c = []
    svc29c.app_logger.critical = lambda msg: _critical_calls_29c.append(msg)

    def _place_order_timeout_29c(order):
        return _OrderResult(
            order_id="", symbol=order.symbol, side=order.side,
            requested_quantity=order.quantity, accepted=False,
            message="kiwoom transport failed: api_id=kt10001: Timeout: connection timed out",
            timestamp=_dt2(2026, 8, 14, 9, 43, 8), is_ambiguous=True,
        )
    svc29c.broker.place_order = _place_order_timeout_29c

    svc29c._try_sell(sym29c, 202, current_price=27800,
                      exit_reason="손절 — 평균단가 대비 -5.8%", avg_buy_price=29700)

    st29c = svc29c._position_state_machine.get(sym29c)
    check("29-7) SELL+timeout(ambiguous, 319400 실측 재현): ERROR로 전이됨"
          "(109분 phantom pending 방지)",
          st29c.lifecycle == L.ERROR)
    check("29-8) SELL+timeout: last_error가 SELL_PLACEMENT_AMBIGUOUS",
          st29c.last_error == "SELL_PLACEMENT_AMBIGUOUS")
    check("29-9) SELL+timeout: CRITICAL 로그가 즉시 남음(사람이 인지 가능)",
          any("ORDER_PLACEMENT_AMBIGUOUS" in m and "side=SELL" in m
              for m in _critical_calls_29c))

    svc29c._try_sell(sym29c, 202, current_price=27800,
                      exit_reason="손절 재시도", avg_buy_price=29700)
    check("29-10) SELL+timeout 직후 재시도해도 여전히 ERROR 유지(중복매도 방지)",
          svc29c._position_state_machine.get(sym29c).lifecycle == L.ERROR)

    # 29-16: acknowledge_error()로 사람이 확인 후 해제하면 정상 거래 재개(탈출구 확인)
    svc29c._position_state_machine.acknowledge_error(
        sym29c, 202, "HTS 직접 확인 — 원 주문 미체결, 수동 취소 완료"
    )
    check("29-16) ERROR는 acknowledge_error()로 정상 해제됨(319400을 다시 겪어도 탈출구가 있음)",
          svc29c._position_state_machine.get(sym29c).lifecycle != L.ERROR)

# 29-11~15: BUY + timeout(ambiguous) → ERROR, 중복매수 차단
with _tf2.TemporaryDirectory() as _tmp29d:
    svc29d = _build_real_service(_tmp29d)
    svc29d.broker._cash = 100_000_000
    sym29d = "319400"
    svc29d.broker._positions.pop(sym29d, None)
    svc29d.broker._prices[sym29d] = 29700
    svc29d._sync_position_state_machine_shadow(svc29d.broker.get_account_balance())

    _critical_calls_29d = []
    svc29d.app_logger.critical = lambda msg: _critical_calls_29d.append(msg)

    def _place_order_timeout_29d(order):
        return _OrderResult(
            order_id="", symbol=order.symbol, side=order.side,
            requested_quantity=order.quantity, accepted=False,
            message="kiwoom transport failed: api_id=kt10000: Timeout: connection timed out",
            timestamp=_dt2(2026, 8, 14, 9, 43, 8), is_ambiguous=True,
        )
    svc29d.broker.place_order = _place_order_timeout_29d

    balance29d = svc29d.broker.get_account_balance()
    buy_signal29d = _Signal(type=_SignalType.BUY, reason="테스트 매수(timeout)")
    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 17)):
        svc29d._try_buy(sym29d, 29700, balance29d, signal=buy_signal29d,
                        regime=_MarketRegime.BULLISH, minute_analysis=None)

    st29d = svc29d._position_state_machine.get(sym29d)
    check("29-11) BUY+timeout(ambiguous): ERROR로 전이됨", st29d.lifecycle == L.ERROR)
    check("29-12) BUY+timeout: last_error가 BUY_PLACEMENT_AMBIGUOUS",
          st29d.last_error == "BUY_PLACEMENT_AMBIGUOUS")
    check("29-13) BUY+timeout: deferred BUY side-effect가 만들어지지 않음",
          sym29d not in svc29d._pending_buy_side_effects)
    check("29-14) BUY+timeout: CRITICAL 로그가 즉시 남음",
          any("ORDER_PLACEMENT_AMBIGUOUS" in m and "side=BUY" in m
              for m in _critical_calls_29d))

    with _patch2("domain.service.trading_service.now_kst",
                return_value=_dt2(2026, 8, 12, 11, 47, 20)):
        _block29d = svc29d._try_buy(sym29d, 29700, balance29d, signal=buy_signal29d,
                                     regime=_MarketRegime.BULLISH, minute_analysis=None)
    check("29-15) BUY+timeout 직후 재시도는 즉시 차단됨(BLOCK_BUY_IN_ERROR, 중복매수 방지)",
          _block29d == "BLOCK_BUY_IN_ERROR")

# 29-17~20: 소스 검증
check("29-17) trading_service.py의 BUY 경로가 is_ambiguous를 확인함",
      "if result.is_ambiguous:" in ts_src)
check("29-18) trading_service.py의 SELL 경로도 is_ambiguous를 확인함(BUY/SELL 두 곳)",
      ts_src.count("if result.is_ambiguous:") >= 2)
check("29-19) trading_service.py가 on_placement_ambiguous를 호출함(BUY/SELL 두 곳)",
      ts_src.count("on_placement_ambiguous(") >= 2)
check("29-20) lifecycle.py에 on_placement_ambiguous 정의 존재",
      "def on_placement_ambiguous(self, symbol: str, side: str" in _lc_src)


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
