# -*- coding: utf-8 -*-
"""리플레이 시간축 무결성 검증 (2026-08-07, 1J.3단계)

배경: 리플레이가 실제 봇과 다른 시간축을 쓰고 있었음.
실측(분봉 CSV 1,781개):
    대상일 외 날짜 봉 포함  920개 (51.7%)
    1분 초과 gap 존재       821개 (46.1%)
    5분 이상 gap 존재       272개 (15.3%)

이 때문에 (a) 전일 봉이 candidate가 되고, (b) analyzer에 60봉이
아니라 200~400봉이 들어가고, (c) prev_close가 전일 첫 봉이 되고,
(d) "5분 후"가 실제로 "5개 봉 후"였음.

실측 영향: crash 3분 지연 Gross 평균 +5.30% → +3.84% (1.46%p).
"""
from __future__ import annotations

import sys
from datetime import date, datetime

sys.path.insert(0, ".")

from domain.replay_context import (
    ReplayDayContext, MAX_STALENESS_MINUTES, parse_bar_dt, floor_to_minute,
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


class Bar:
    """replay_runner.MinuteBarRow 호환 최소 fixture."""

    def __init__(self, cntr_tm: str, close: int, high: int | None = None,
                 low: int | None = None):
        self.cntr_tm = cntr_tm
        self.open_price = close
        self.close_price = close
        self.high_price = high if high is not None else close
        self.low_price = low if low is not None else close
        self.volume = 1000
        self.acc_volume = 1000


def mk(day: str, hh: int, mm: int, close: int, **kw) -> Bar:
    return Bar(f"{day}{hh:02d}{mm:02d}00", close, **kw)


TARGET = date(2026, 8, 7)

# ══════════════════════════════════════════════════════════════
# A. 전일 봉이 candidate가 되지 않음 (P0-1)
# ══════════════════════════════════════════════════════════════
# 8/6 14:20~15:19 + 8/7 09:00~ 를 한 파일에 담은 실제 구조
prev_day = [mk("20260806", 14, m, 10000 + m) for m in range(20, 60)]
prev_day += [mk("20260806", 15, m, 10040 + m) for m in range(0, 20)]
today = [mk("20260807", 9, m, 10100 + m) for m in range(0, 60)]
bars = prev_day + today
ctx = ReplayDayContext(bars, TARGET, minute_bar_count=60)

check("A-1) 전일 봉이 파일에 실제로 섞여 있음(재현 조건)",
      len(prev_day) == 60 and bars[0].cntr_tm.startswith("20260806"))
check("A-2) target_indices가 당일 봉만 포함",
      len(ctx.target_indices) == len(today))
check("A-3) 전일 봉은 candidate 자격 없음",
      all(not ctx.is_target_bar(i) for i in range(len(prev_day))))
check("A-4) 당일 봉은 candidate 자격 있음",
      all(ctx.is_target_bar(i) for i in range(len(prev_day), len(bars))))
check("A-5) target_bars의 모든 봉이 target_date 소속",
      all(b.cntr_tm.startswith("20260807") for b in ctx.target_bars))
check("A-6) 전일 봉도 history로는 보존됨(analysis_window에서 사용 가능)",
      len(ctx.all_bars) == len(bars))

# 첫 당일 봉의 window에 전일 봉이 history로 들어가는지
first_today = ctx.target_indices[0]
w0 = ctx.analysis_window(first_today)
check("A-7) 당일 첫 봉의 window에 전일 봉이 history로 포함됨",
      any(b.cntr_tm.startswith("20260806") for b in w0))


# ══════════════════════════════════════════════════════════════
# B. live와 동일한 60봉 window + 현재봉 포함 (P0-2)
# ══════════════════════════════════════════════════════════════
long_bars = [mk("20260807", 9 + m // 60, m % 60, 10000 + m) for m in range(0, 300)]
ctx_b = ReplayDayContext(long_bars, TARGET, minute_bar_count=60)

for idx in (0, 30, 59, 60, 61, 150, 299):
    w = ctx_b.analysis_window(idx)
    check(f"B-1) index={idx}: window 길이가 minute_bar_count 이하", len(w) <= 60)
    check(f"B-2) index={idx}: 현재봉이 window의 마지막",
          w[-1].cntr_tm == long_bars[idx].cntr_tm)

check("B-3) 61번째 이후에도 항상 최근 60개",
      len(ctx_b.analysis_window(150)) == 60 and len(ctx_b.analysis_window(299)) == 60)
check("B-4) current.close가 analyzer가 보는 최신 close와 일치",
      ctx_b.analysis_window(150)[-1].close_price == long_bars[150].close_price)
check("B-5) 초반(60봉 미만)에는 있는 만큼만",
      len(ctx_b.analysis_window(10)) == 11)


# ══════════════════════════════════════════════════════════════
# C. prev_close 복원 (P0-3)
# ══════════════════════════════════════════════════════════════
c_bars = [mk("20260806", 15, 19, 10000), mk("20260807", 9, 0, 10100),
          mk("20260807", 9, 1, 10150)]
# 파일 첫 봉을 일부러 다른 값으로 둬서 bars[0] 방식과 구분
c_bars.insert(0, mk("20260806", 14, 20, 9500))
ctx_c = ReplayDayContext(c_bars, TARGET)
check("C-1) prev_close가 전일 마지막 봉의 close", ctx_c.previous_close == 10000)
check("C-2) prev_close가 파일 첫 봉(9500)이 아님", ctx_c.previous_close != 9500)
check("C-3) previous_close_available이 True", ctx_c.previous_close_available)

# 이전 날짜 봉이 아예 없으면 None (임의 추정 금지)
ctx_c2 = ReplayDayContext([mk("20260807", 9, 0, 10100)], TARGET)
check("C-4) 이전 날짜 봉이 없으면 None(임의 추정하지 않음)",
      ctx_c2.previous_close is None)
check("C-5) previous_close_available이 False", not ctx_c2.previous_close_available)


# ══════════════════════════════════════════════════════════════
# D. 5/10/20분이 timestamp 기준 (P0-4)
# ══════════════════════════════════════════════════════════════
# gap이 있는 실제 패턴: 09:00, 09:01, 09:07, 09:08, 09:12 ...
gap_bars = [mk("20260807", 9, 0, 10000), mk("20260807", 9, 1, 10010),
            mk("20260807", 9, 7, 10070), mk("20260807", 9, 8, 10080),
            mk("20260807", 9, 12, 10120), mk("20260807", 9, 20, 10200)]
ctx_d = ReplayDayContext(gap_bars, TARGET)
entry_dt = datetime(2026, 8, 7, 9, 0, 0)

hp5 = ctx_d.price_at_horizon(entry_dt, 5)
check("D-1) 09:00+5분 → 5분 이내 봉이 없으면 N/A (09:01은 4분 전이라 stale)",
      not hp5.available)

hp10 = ctx_d.price_at_horizon(entry_dt, 10)
check("D-2) 09:00+10분 → 09:08 봉 선택(target 이하 최신)",
      hp10.available and hp10.bar_dt == datetime(2026, 8, 7, 9, 8))
check("D-3) actual_elapsed_minutes가 8.0으로 기록됨",
      hp10.actual_elapsed_minutes == 8.0)
check("D-4) 봉 개수 방식(i+10)과 다름 — gap 때문에 인덱스로는 도달 불가",
      len(gap_bars) < 11)

hp20 = ctx_d.price_at_horizon(entry_dt, 20)
check("D-5) 09:00+20분 → 09:20 봉 정확 매칭",
      hp20.available and hp20.actual_elapsed_minutes == 20.0)

check("D-6) MAX_STALENESS_MINUTES가 명시적 상수", MAX_STALENESS_MINUTES == 3)

# 날짜 경계를 넘지 않음
cross = [mk("20260807", 15, 19, 10000), mk("20260808", 9, 0, 11000)]
ctx_x = ReplayDayContext(cross, TARGET)
hx = ctx_x.price_at_horizon(datetime(2026, 8, 7, 15, 19), 5)
check("D-7) 다음 거래일 봉을 horizon 가격으로 쓰지 않음", not hx.available)


# ══════════════════════════════════════════════════════════════
# E. MFE/MAE도 clock-time 기준 (P0-4)
# ══════════════════════════════════════════════════════════════
mm_bars = [mk("20260807", 9, 0, 10000),
           mk("20260807", 9, 5, 10000, high=10300, low=9900),
           mk("20260807", 9, 19, 10000, high=10500, low=9800),
           mk("20260807", 9, 25, 10000, high=11000, low=9000)]   # 20분 밖
ctx_e = ReplayDayContext(mm_bars, TARGET)
mfe, mae = ctx_e.mfe_mae(datetime(2026, 8, 7, 9, 0), 10000, minutes=20)
check("E-1) MFE가 20분 이내 봉만 반영(+5.0%)", abs(mfe - 5.0) < 1e-9)
check("E-2) MAE가 20분 이내 봉만 반영(-2.0%)", abs(mae - (-2.0)) < 1e-9)
check("E-3) 20분 밖 봉(09:25)이 제외됨 — 봉 개수 방식이면 포함됐을 것",
      mfe < 10.0 and mae > -10.0)

between = ctx_e.bars_between(datetime(2026, 8, 7, 9, 0), datetime(2026, 8, 7, 9, 20))
check("E-4) bars_between이 (entry, entry+N] 범위", len(between) == 2)


# ══════════════════════════════════════════════════════════════
# F. V-drop 날짜/초 매칭 (P0-5)
# ══════════════════════════════════════════════════════════════
# 전일 14:22 + 당일 09:23 — 시각만 비교하면 전일 봉이 선택됨
vd_bars = [mk("20260806", 14, 22, 9000), mk("20260807", 9, 23, 10000),
           mk("20260807", 9, 24, 10010)]
ctx_f = ReplayDayContext(vd_bars, TARGET)
idx = ctx_f.index_at_or_after(datetime(2026, 8, 7, 9, 23, 0, 608000))
check("F-1) 09:23:00.608 이벤트가 당일 09:23 봉과 매칭(09:24로 밀리지 않음)",
      idx is not None and vd_bars[idx].cntr_tm == "20260807092300")
check("F-2) 전일 14:22 봉이 절대 선택되지 않음",
      idx is not None and not vd_bars[idx].cntr_tm.startswith("20260806"))
check("F-3) floor_to_minute이 초·마이크로초를 버림",
      floor_to_minute(datetime(2026, 8, 7, 9, 23, 0, 608000))
      == datetime(2026, 8, 7, 9, 23))
check("F-4) parse_bar_dt가 14자리 형식을 해석",
      parse_bar_dt("20260807092300") == datetime(2026, 8, 7, 9, 23))
check("F-5) parse_bar_dt가 잘못된 값에 None", parse_bar_dt("bad") is None)


# ══════════════════════════════════════════════════════════════
# G. 분석기들이 공용 컨텍스트를 실제로 사용 (P0-6)
# ══════════════════════════════════════════════════════════════
from pathlib import Path

def _code_only(src: str) -> str:
    """주석·docstring을 제거한 실행 코드만 남깁니다.

    문자열 검사는 설명 문구까지 잡아 오탐이 납니다(1J.3 작성 중
    실제 발생). AST로 docstring을 걷어내고 # 주석도 제거합니다.
    """
    import ast as _ast
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)) and _ast.get_docstring(node):
            node.body = node.body[1:]
    out = _ast.unparse(tree)
    return "\n".join(l for l in out.splitlines() if not l.strip().startswith("#"))


for name in ("replay_runner.py", "analyze_v_drop_backtest.py",
             "analyze_crash_rebound_days.py", "simulate_pullback_removal.py"):
    src = Path(name).read_text(encoding="utf-8")
    check(f"G-1) {name}이 ReplayDayContext를 import",
          "from domain.replay_context import" in src)

rr = Path("replay_runner.py").read_text(encoding="utf-8")
check("G-2) replay_runner가 target_indices로 candidate를 한정",
      "ctx.target_indices" in rr)
check("G-3) replay_runner가 analysis_window를 사용", "ctx.analysis_window(" in rr)
check("G-4) replay_runner가 ctx.previous_close를 사용", "ctx.previous_close" in rr)
check("G-5) replay_runner 코드에 prev_close = bars[0].close_price가 없음",
      "prev_close = bars[0].close_price" not in _code_only(rr))
check("G-6) replay_runner 코드에 idx = i + m 방식이 없음",
      "idx = i + m" not in _code_only(rr))
check("G-7) replay_runner가 price_at_horizon을 사용", "price_at_horizon(" in rr)
check("G-8) replay_runner가 actual_elapsed를 기록", "elapsed_5m" in rr)

pb = Path("simulate_pullback_removal.py").read_text(encoding="utf-8")
check("G-9) pullback이 공용 컨텍스트로 window를 만듦", "ctx.analysis_window(" in pb)
check("G-10) pullback 코드에 idx = i + m 방식이 없음",
      "idx = i + m" not in _code_only(pb))

cr = Path("analyze_crash_rebound_days.py").read_text(encoding="utf-8")
check("G-11) crash가 target_bars만으로 급락을 계산", "ctx.target_bars" in cr)
check("G-12) crash가 저점+N분을 clock-time으로 계산",
      "price_at_horizon(_low_dt" in cr)

vd = Path("analyze_v_drop_backtest.py").read_text(encoding="utf-8")
check("G-13) v_drop이 index_at_or_after로 full datetime 비교",
      "index_at_or_after(" in vd)
check("G-14) v_drop 코드에 시각만 비교하는 방식이 없음",
      "bar_time >= entry_ts.time()" not in _code_only(vd))


# ══════════════════════════════════════════════════════════════
# H. 실데이터 회귀 — 전일 봉 제외가 실제로 동작
# ══════════════════════════════════════════════════════════════
import replay_runner as R

real = R.load_bars("005930", date(2026, 6, 23))
if real:
    ctx_h = ReplayDayContext(real, date(2026, 6, 23), 60)
    check("H-1) 실데이터에 전일 봉이 존재(재현 조건)",
          real[0].cntr_tm.startswith("20260622"))
    check("H-2) target 봉 수가 전체보다 적음",
          len(ctx_h.target_indices) < len(real))
    check("H-3) prev_close가 파일 첫 봉과 다름",
          ctx_h.previous_close != real[0].close_price)
    check("H-4) 마지막 window가 정확히 60봉",
          len(ctx_h.analysis_window(ctx_h.target_indices[-1])) == 60)
    res = R.run_replay("005930", real, None, date(2026, 6, 23))
    check("H-5) replay 결과의 entry_time이 전부 target_date",
          all(str(r["entry_time"]).startswith("20260623") for r in res))
else:
    print("[SKIP] H절 — 실데이터 없음")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
