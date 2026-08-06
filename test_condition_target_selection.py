# -*- coding: utf-8 -*-
"""조건검색 → 단타 targets 산출 검증 (2026-08-06, 1E.9단계)

이 테스트가 존재하는 이유:

1E.8에서 `app/main.py`의 `on_symbols_changed()` 콜백이 어떤 자동
테스트로도 실행되지 않는다는 사각지대가 드러났습니다(1E.7의 필드
개명을 콜백이 따라가지 못해 실서버에서 AttributeError 재연결 루프가
발생했는데도 회귀 스위트 30/30이 전부 통과했음). 1E.9에서 그 계산을
`app/target_selection.compute_day_targets()` 순수 함수로 분리하면서
이 테스트를 추가합니다.

검증 범위:
  1) 실서버 8/6 09:10:10 로그로 확인된 P0 결함(실시간 편입 종목이
     targets에서 통째로 누락)이 재현되지 않는지
  2) 8/5(구 코드)의 실제 편입 규모를 넣었을 때 targets가 정상 산출되는지
  3) 설정에 등록되지 않은 seq의 결과가 targets에 섞이지 않는지
  4) 자동 제외 종목 차단 / max_symbols 상한 / 수동 targets 우선순위
  5) `ConditionWatcher`의 public 접근자가 내부 상태와 일치하는지
     (1E.8식 필드명 불일치 재발 방지)
  6) CNSRREQ 900003(이미 등록된 seq) 회복 경로
"""
from __future__ import annotations

import asyncio
import re
import sys

sys.path.insert(0, ".")

from app.target_selection import compute_day_targets
from infra.websocket.condition_watcher import ConditionWatcher

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


MANUAL = ["010170", "006260", "005930", "080220"]


# ══════════════════════════════════════════════════════════════
# 1) 실서버 8/6 09:10:10 시나리오 정확 재현
# ══════════════════════════════════════════════════════════════
# 실제 로그:
#   09:10:05 [COND] [자동매매_V자_BV] 편입: 069540      (seq2 확정)
#   09:10:05 ERROR CNSRREQ return_code 900003 (seq=3)   (seq3 조회 실패)
#   09:10:10 [COND] [출처불명] 편입: 215790             (실시간)
#   09:10:10 [COND_STATUS] final=5종목: [...069540]     ← 215790 누락
sel = compute_day_targets(
    confirmed_symbols_by_seq={"1": set(), "2": {"069540"}, "3": set()},
    realtime_unresolved={"215790"},
    day_seqs=[1, 2, 3],
    manual_symbols=MANUAL,
    excluded_symbols=set(),
    max_symbols=10,
)
check("1-1) 8/6 재현: 실시간 편입 215790이 최종 targets에 포함됨(P0 결함 수정)",
      "215790" in sel.final_targets)
check("1-2) 8/6 재현: 확정 편입 069540도 그대로 유지됨",
      "069540" in sel.final_targets)
check("1-3) 8/6 재현: 수동 targets 4종목 전부 유지됨",
      all(s in sel.final_targets for s in MANUAL))
check("1-4) 8/6 재현: final이 6종목(수동4 + 069540 + 215790)",
      len(sel.final_targets) == 6)
check("1-5) 8/6 재현: 215790이 unresolved_used로 정확히 보고됨",
      sel.unresolved_used == ["215790"])
check("1-6) 8/6 재현: 확정 종목은 unresolved_used로 중복 계상되지 않음",
      "069540" not in sel.unresolved_used)

# 수정 전 동작(확정 버킷만 순회)이 실제로 215790을 잃었다는 점도 명시적으로 고정
legacy_day_symbols = sorted(
    s for seq, syms in {"1": set(), "2": {"069540"}, "3": set()}.items() for s in syms
)
check("1-7) (대조) 수정 전 로직은 215790을 실제로 누락했음 — 결함이 실재했음을 고정",
      "215790" not in legacy_day_symbols)


# ══════════════════════════════════════════════════════════════
# 2) 장전 초기조회 0종목 + 장중 실시간 편입만 있는 실제 패턴
# ══════════════════════════════════════════════════════════════
# 8/5 로그: 08:44 초기결과 seq1/2/3 전부 0종목, 장중 실시간 편입
# 3,318건으로 종목이 쌓임. CNSRREQ는 연결당 1회뿐이므로 재연결이
# 없으면 확정 버킷은 하루 종일 비어 있음 — 이때 targets가 수동
# 종목만 남으면 조건검색 자동매매가 사실상 마비됨.
intraday = {f"{i:06d}" for i in range(100000, 100078)}  # 78종목(8/5 15:30 실측 규모)
sel2 = compute_day_targets(
    confirmed_symbols_by_seq={"1": set(), "2": set(), "3": set()},
    realtime_unresolved=intraday,
    day_seqs=[1, 2, 3],
    manual_symbols=MANUAL,
    excluded_symbols=set(),
    max_symbols=10,
)
check("2-1) 확정 버킷이 전부 비어도 실시간 편입 종목이 day_symbols에 들어감",
      len(sel2.day_symbols) == 78)
check("2-2) max_symbols=10 상한이 정확히 적용됨",
      len(sel2.final_targets) == 10)
check("2-3) 수동 targets가 상한 안에서 우선 배치됨",
      sel2.final_targets[:4] == MANUAL)
check("2-4) 나머지 6칸이 실시간 편입 종목으로 채워짐",
      all(s in intraday for s in sel2.final_targets[4:]))


# ══════════════════════════════════════════════════════════════
# 3) 설정에 등록되지 않은 seq의 결과는 targets에 섞이지 않음
# ══════════════════════════════════════════════════════════════
# 2026-08-06 (1F): 스윙 전략 폐기로 "스윙 seq 제외" 시나리오는
# 사라졌지만, day_seqs 필터링 자체는 방어선으로 남겨둠 — 설정에
# 없는 seq의 확정 결과가 흘러들어와도 조용히 감시 대상이 되지
# 않아야 함.
sel3 = compute_day_targets(
    confirmed_symbols_by_seq={"1": {"111111"}, "2": set(), "3": set(), "9": {"999999"}},
    realtime_unresolved={"215790"},
    day_seqs=[1, 2, 3],
    manual_symbols=[],
    excluded_symbols=set(),
    max_symbols=10,
)
check("3-1) 미등록 seq(9)의 확정 종목은 targets에 포함되지 않음",
      "999999" not in sel3.final_targets)
check("3-2) 등록된 seq의 확정 종목은 정상 포함됨",
      "111111" in sel3.final_targets)
check("3-3) 출처 미확정 종목은 항상 포함됨(스윙 폐기로 제외 분기 없음)",
      "215790" in sel3.final_targets and sel3.unresolved_used == ["215790"])
check("3-4) 최종 targets가 정확히 두 종목",
      sel3.final_targets == ["111111", "215790"])


# ══════════════════════════════════════════════════════════════
# 4) 자동 제외 종목 재편입 차단
# ══════════════════════════════════════════════════════════════
sel4 = compute_day_targets(
    confirmed_symbols_by_seq={"1": {"111111", "222222"}, "2": set(), "3": set()},
    realtime_unresolved={"333333"},
    day_seqs=[1, 2, 3],
    manual_symbols=[],
    excluded_symbols={"222222", "333333"},
    max_symbols=10,
)
check("4-1) 제외 종목은 확정/미확정 관계없이 최종 targets에서 빠짐",
      sel4.final_targets == ["111111"])
check("4-2) 차단된 종목이 blocked로 정확히 보고됨",
      sel4.blocked == {"222222", "333333"})
check("4-3) day_symbols에는 차단 전 후보가 전부 남아있음(집계용)",
      set(sel4.day_symbols) == {"111111", "222222", "333333"})


# ══════════════════════════════════════════════════════════════
# 5) 중복 제거 / 결정성
# ══════════════════════════════════════════════════════════════
sel5 = compute_day_targets(
    confirmed_symbols_by_seq={"1": {"111111"}, "2": {"111111"}, "3": set()},
    realtime_unresolved={"111111", "222222"},
    day_seqs=[1, 2, 3],
    manual_symbols=["111111"],
    excluded_symbols=set(),
    max_symbols=10,
)
check("5-1) 복수 조건식 동시 편입 + 미확정 + 수동 중복이 한 번만 나타남",
      sel5.final_targets.count("111111") == 1)
check("5-2) 이미 확정된 종목은 unresolved_used로 중복 계상되지 않음",
      sel5.unresolved_used == ["222222"])
sel5b = compute_day_targets(
    confirmed_symbols_by_seq={"3": set(), "2": {"111111"}, "1": set()},
    realtime_unresolved={"222222", "215790"},
    day_seqs=[1, 2, 3],
    manual_symbols=[],
    excluded_symbols=set(),
    max_symbols=10,
)
check("5-3) dict 순회 순서가 달라도 결과가 결정적(정렬 보장)",
      sel5b.final_targets == ["111111", "215790", "222222"])


# ══════════════════════════════════════════════════════════════
# 6) ConditionWatcher public 접근자가 내부 상태와 일치 (1E.8 재발 방지)
# ══════════════════════════════════════════════════════════════
w = ConditionWatcher.__new__(ConditionWatcher)
w._confirmed_symbols_by_seq = {"1": {"005930"}, "2": set(), "3": {"047040", "058610"}}
w._realtime_unresolved = {"215790"}

check("6-1) confirmed_symbols_by_seq property가 내부 상태와 동일한 값을 돌려줌",
      w.confirmed_symbols_by_seq == {"1": {"005930"}, "2": set(), "3": {"047040", "058610"}})
check("6-2) realtime_unresolved_symbols property가 내부 상태와 동일한 값을 돌려줌",
      w.realtime_unresolved_symbols == {"215790"})

# 복사본이어야 함 — 호출부(app/main.py)가 실수로 watcher 내부 상태를
# 오염시키지 못하도록.
snapshot = w.confirmed_symbols_by_seq
snapshot["1"].add("999999")
snapshot["9"] = {"888888"}
unresolved_snapshot = w.realtime_unresolved_symbols
unresolved_snapshot.add("777777")
check("6-3) confirmed_symbols_by_seq 반환값 수정이 내부 상태를 오염시키지 않음",
      w._confirmed_symbols_by_seq == {"1": {"005930"}, "2": set(), "3": {"047040", "058610"}})
check("6-4) realtime_unresolved_symbols 반환값 수정이 내부 상태를 오염시키지 않음",
      w._realtime_unresolved == {"215790"})

# app/main.py가 실제로 이 두 property만 쓰고 private 필드를 직접
# 참조하지 않는지 — 1E.8과 똑같은 사고를 소스 수준에서 차단.
main_src = open("app/main.py", encoding="utf-8").read()
# 주의: "confirmed_symbols_by_seq"는 "_symbols_by_seq"를 부분문자열로
# 포함하므로 단순 substring 검사는 오탐이 납니다(이 테스트 작성 중
# 실제로 오탐 확인). watcher 객체에 대한 private 접근(`watcher._...`)
# 만 정확히 잡습니다.
private_access = re.findall(r"watcher\._\w+", main_src)
check("6-5) app/main.py가 watcher의 private 필드를 직접 참조하지 않음",
      private_access == [])
if private_access:
    print(f"       └ 발견된 private 접근: {sorted(set(private_access))}")
check("6-6) app/main.py가 compute_day_targets를 실제로 사용함",
      "compute_day_targets(" in main_src)


# ══════════════════════════════════════════════════════════════
# 7) CNSRREQ 900003(이미 등록된 seq) 회복 경로
# ══════════════════════════════════════════════════════════════
class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


w2 = ConditionWatcher.__new__(ConditionWatcher)
w2._confirmed_symbols_by_seq = {"1": set(), "2": set(), "3": set()}
w2._realtime_unresolved = set()
w2._condition_names = {}
w2._resubscribe_attempted = set()
w2._ws_client = _FakeWs()
w2.on_symbols_changed = lambda symbols: None

# 실서버 8/6 09:10:05에 실제로 받은 응답 형태 그대로 — 최상위 seq 키가
# 없고 return_msg 문구 안에만 seq가 들어있음.
err_msg = {
    "trnm": "CNSRREQ",
    "return_code": 900003,
    "return_msg": "이미 등록된 조건검색 일련번호입니다.(seq=3)",
}
check("7-1) return_msg 문구에서 seq를 정확히 추출함",
      ConditionWatcher._extract_seq_from_error(err_msg) == "3")

asyncio.run(w2._on_initial_result(err_msg))
check("7-2) 900003 수신 시 CNSRCLR로 기존 등록을 해제함",
      any(m.get("trnm") == "CNSRCLR" and m.get("seq") == "3" for m in w2._ws_client.sent))
check("7-3) 해제 직후 같은 seq로 CNSRREQ를 재발송함",
      any(m.get("trnm") == "CNSRREQ" and m.get("seq") == "3" for m in w2._ws_client.sent))
check("7-4) 전송 순서가 CNSRCLR → CNSRREQ 임",
      [m["trnm"] for m in w2._ws_client.sent] == ["CNSRCLR", "CNSRREQ"])

# 재시도는 연결당 seq별 1회 — 무한 루프 방지
before = len(w2._ws_client.sent)
asyncio.run(w2._on_initial_result(err_msg))
check("7-5) 같은 연결에서 900003이 또 와도 재시도하지 않음(무한 루프 방지)",
      len(w2._ws_client.sent) == before)

# 재로그인하면 다시 한 번 시도할 수 있어야 함
w2._resubscribe_attempted.clear()
asyncio.run(w2._on_initial_result(err_msg))
check("7-6) 재연결(로그인) 후에는 회복을 다시 시도함",
      len(w2._ws_client.sent) == before + 2)

# 정상 응답 경로가 그대로 동작하는지 (async 전환으로 깨지지 않았는지)
w3 = ConditionWatcher.__new__(ConditionWatcher)
w3._confirmed_symbols_by_seq = {"1": set(), "2": set(), "3": set()}
w3._realtime_unresolved = {"047040"}
w3._condition_names = {"3": "자동매매_돌파형A"}
w3._resubscribe_attempted = set()
w3._ws_client = _FakeWs()
notified: list[list[str]] = []
w3.on_symbols_changed = lambda symbols: notified.append(list(symbols))

asyncio.run(w3._on_initial_result({
    "trnm": "CNSRREQ",
    "return_code": 0,
    "seq": "3",
    "data": [{"jmcode": "A047040"}, {"jmcode": "005930"}],
}))
check("7-7) 정상 응답이 확정 버킷에 정확히 반영됨",
      w3._confirmed_symbols_by_seq["3"] == {"047040", "005930"})
check("7-8) 재조회로 확정된 종목은 unresolved에서 제거됨",
      w3._realtime_unresolved == set())
check("7-9) 정상 응답 후 on_symbols_changed가 호출됨",
      len(notified) == 1 and set(notified[0]) == {"047040", "005930"})

# 900003 외의 오류는 기존처럼 조용히 무시(재구독 시도 안 함)
w4 = ConditionWatcher.__new__(ConditionWatcher)
w4._confirmed_symbols_by_seq = {"1": set()}
w4._realtime_unresolved = set()
w4._condition_names = {}
w4._resubscribe_attempted = set()
w4._ws_client = _FakeWs()
w4.on_symbols_changed = lambda symbols: None
asyncio.run(w4._on_initial_result({"trnm": "CNSRREQ", "return_code": 900001, "return_msg": "기타 오류"}))
check("7-10) 900003이 아닌 오류에는 재구독을 시도하지 않음",
      w4._ws_client.sent == [])


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
