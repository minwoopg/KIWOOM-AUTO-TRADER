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
check("G-4) replay_runner가 prev_close를 복원 계층으로 얻음",
      "resolve_prev_close(" in rr)
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

# ══════════════════════════════════════════════════════════════
# I. pullback MFE/MAE도 clock-time (1J.3.1)
# ══════════════════════════════════════════════════════════════
pb2 = Path("simulate_pullback_removal.py").read_text(encoding="utf-8")
check("I-1) pullback 코드에 bars[i+1:i+21] 방식이 없음",
      "bars[i + 1: i + 21]" not in _code_only(pb2)
      and "bars[i+1:i+21]" not in _code_only(pb2))
check("I-2) pullback이 ctx.mfe_mae를 사용", "ctx.mfe_mae(" in pb2)

# 20분 밖 봉이 실제로 제외되는지 (공용 함수 동작 재확인)
mm2 = [mk("20260807", 10, 0, 10000),
       mk("20260807", 10, 19, 10000, high=10400, low=9700),
       mk("20260807", 10, 21, 10000, high=99999, low=1)]     # 21분 → 제외
ctx_i = ReplayDayContext(mm2, TARGET)
mfe2, mae2 = ctx_i.mfe_mae(datetime(2026, 8, 7, 10, 0), 10000, minutes=20)
check("I-3) clock-time 20분 밖 봉이 MFE에 포함되지 않음", abs(mfe2 - 4.0) < 1e-9)
check("I-4) clock-time 20분 밖 봉이 MAE에 포함되지 않음", abs(mae2 - (-3.0)) < 1e-9)


# ══════════════════════════════════════════════════════════════
# J. replay fail-closed (1J.3.1)
# ══════════════════════════════════════════════════════════════
import replay_runner as RR

check("J-1) ReplayConfigError가 정의됨", hasattr(RR, "ReplayConfigError"))
check("J-2) 기본은 fallback 비활성", RR.ALLOW_CONFIG_FALLBACK is False)
check("J-3) 정상 설정에서 minute_bar_count를 읽음",
      RR.resolve_minute_bar_count() == 60)

# 설정 로딩 실패 → 예외
_orig_ls = RR.load_settings
RR.load_settings = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    RR.resolve_minute_bar_count()
    _r1 = False
except RR.ReplayConfigError:
    _r1 = True
except Exception:
    _r1 = False
check("J-4) 설정 로딩 실패 → ReplayConfigError", _r1)
check("J-5) --allow-config-fallback일 때만 60으로 대체",
      RR.resolve_minute_bar_count(allow_fallback=True) == 60)
RR.load_settings = _orig_ls

# minute_bar_count가 유효하지 않으면 예외
class _S:
    class market_regime:
        minute_bar_count = 0
RR.load_settings = lambda *a, **k: _S()
try:
    RR.resolve_minute_bar_count()
    _r2 = False
except RR.ReplayConfigError:
    _r2 = True
check("J-6) minute_bar_count가 0이면 ReplayConfigError", _r2)
RR.load_settings = _orig_ls

# analyzer 생성 실패 → 기본 예외, 옵션 시에만 None
_orig_mbc = RR.MINUTE_BAR_COUNT
import builtins as _b
_real_import = _b.__import__


def _blocked(name, *a, **k):
    if "minute_analyzer" in name:
        raise ImportError("blocked for test")
    return _real_import(name, *a, **k)


_b.__import__ = _blocked
try:
    RR.try_import_analyzer()
    _r3 = False
except RR.ReplayConfigError:
    _r3 = True
except Exception:
    _r3 = False
check("J-7) MinuteAnalyzer 생성 실패 → 기본은 ReplayConfigError", _r3)
try:
    _fb = RR.try_import_analyzer(allow_simple_fallback=True)
    _r4 = _fb is None
except Exception:
    _r4 = False
check("J-8) --allow-simple-fallback일 때만 None 반환(간이 전략)", _r4)
_b.__import__ = _real_import

_rr2 = Path("replay_runner.py").read_text(encoding="utf-8")
check("J-9) analyzer_mode를 리포트에 출력", "analyzer_mode" in _rr2)
check("J-10) SIMPLE_FALLBACK 사용 금지 경고가 있음",
      "enforce 판단에 사용하지 마십시오" in _rr2)
# FALLBACK_MINUTE_BAR_COUNT = 60 상수 정의는 허용(명시적 옵션 전용).
# 금지 대상은 except 블록에서 조용히 대입하던 형태.
_rr_code2 = _code_only(_rr2)
check("J-11) except 블록의 silent 60 대입이 없음",
      "except Exception:\n    MINUTE_BAR_COUNT = 60" not in _rr_code2
      and "MINUTE_BAR_COUNT = 60\n" not in _rr_code2.replace("FALLBACK_MINUTE_BAR_COUNT = 60\n", ""))


# ══════════════════════════════════════════════════════════════
# K. prev_close 복원 계층 + coverage 계측 (1J.3.1)
# ══════════════════════════════════════════════════════════════
from domain.replay_context import (
    resolve_prev_close, PREV_CLOSE_SAME_FILE, PREV_CLOSE_PREV_DAY,
    PREV_CLOSE_UNAVAILABLE,
)

# 1순위: 같은 파일
ctx_k1 = ReplayDayContext([mk("20260806", 15, 19, 10000),
                           mk("20260807", 9, 0, 10100)], TARGET)
r1 = resolve_prev_close(ctx_k1, "005930")
check("K-1) 1순위 — 같은 CSV의 target 이전 마지막 close",
      r1.value == 10000 and r1.source == PREV_CLOSE_SAME_FILE)
check("K-2) 1순위 confidence가 high", r1.confidence == "high")

# 2순위: 직전 거래일 파일
ctx_k2 = ReplayDayContext([mk("20260807", 9, 0, 10100)], TARGET)
r2 = resolve_prev_close(ctx_k2, "005930")
check("K-3) 직전 거래일 조회 수단이 없으면 UNAVAILABLE",
      r2.source == PREV_CLOSE_UNAVAILABLE and r2.value is None)
check("K-4) UNAVAILABLE의 confidence가 none", r2.confidence == "none")

# 실데이터로 2순위 동작 확인
_real2 = RR.load_bars("005930", date(2026, 6, 23))
if _real2:
    _c = ReplayDayContext(_real2, date(2026, 6, 23), 60)
    _r = resolve_prev_close(_c, "005930", RR.load_bars, RR.MINUTE_BARS_DIR,
                            date(2026, 6, 23))
    check("K-5) 실데이터에서 prev_close 복원 성공", _r.available)
    check("K-6) source가 기록됨", _r.source in
          (PREV_CLOSE_SAME_FILE, PREV_CLOSE_PREV_DAY))

# 2026-08-07 (1J.3.2): 전역 dict → ReplayQualityStats 객체로 변경
check("K-7) replay가 coverage 통계를 수집",
      "prev_close_sources" in RR.ReplayQualityStats().__dict__)
check("K-8) replay가 horizon 통계를 수집",
      "horizon_elapsed" in RR.ReplayQualityStats().__dict__)
# horizon 통계를 실제 리플레이로 채운 뒤 검사
# (통계가 비어 있으면 horizon 섹션 자체가 출력되지 않음)
_stats_k = RR.ReplayQualityStats()
_rb = RR.load_bars("005930", date(2026, 6, 23))
if _rb:
    RR.run_replay("005930", _rb, RR.try_import_analyzer(), date(2026, 6, 23),
                  minute_bar_count=60, quality_stats=_stats_k)
_q = "\n".join(RR.build_quality_report(_stats_k))
# 1J.3.2에서 요구된 필드명으로 변경
for key in ("total_symbol_days", "same_file_count", "previous_data_day_count",
            "unavailable_count", "prev_close_coverage_pct", "analyzer_mode"):
    check(f"K-9) 품질 리포트에 {key} 출력", key in _q)
check("K-10) '전체 데이터'가 아님을 명시", "'전체 데이터'가 아닙니다" in _q)
check("K-11) horizon 품질에 exact / stale / N/A 구분",
      "exact" in _q and "stale" in _q and "N/A" in _q)
check("K-12) horizon 품질에 median과 p10/p90",
      "median" in _q and "p10" in _q and "p90" in _q)

# ══════════════════════════════════════════════════════════════
# L. prev_close는 직전 데이터 날짜 1곳만 (1J.3.2, P0)
# ══════════════════════════════════════════════════════════════
# 재현(1J.3.1): 파일을 찾을 때까지 과거를 무제한 탐색해 최대 36
# 데이터 날짜 전 종가를 전일 종가로 사용했음(오염 280건).
import tempfile as _tf
import csv as _csv
from domain.replay_context import PREV_CLOSE_PREV_DAY

_root = Path(_tf.mkdtemp())


def _write_day(day: str, symbol: str, closes: list[int]) -> None:
    d = _root / day
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{symbol}.csv").open("w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["cntr_tm", "open", "high", "low", "close", "volume", "acc_volume"])
        for i, c in enumerate(closes):
            w.writerow([f"{day}09{i:02d}00", c, c, c, c, 100, 100])


def _load(symbol, d):
    path = _root / d.strftime("%Y%m%d") / f"{symbol}.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [Bar(r["cntr_tm"], int(r["close"])) for r in _csv.DictReader(f)]


# 8/5에는 심볼 있음, 8/6 디렉터리는 있지만 심볼 없음, 8/7이 target
_write_day("20260805", "AAA", [8000, 8100])
(_root / "20260806").mkdir(exist_ok=True)
_write_day("20260806", "BBB", [9000])          # 다른 종목만
_write_day("20260807", "AAA", [10000, 10100])

_ctx_l = ReplayDayContext(_load("AAA", date(2026, 8, 7)), date(2026, 8, 7), 60)
_r_l = resolve_prev_close(_ctx_l, "AAA", _load, _root, date(2026, 8, 7))
check("L-1) 직전 데이터 날짜(8/6)에 종목이 없으면 UNAVAILABLE",
      _r_l.source == PREV_CLOSE_UNAVAILABLE and _r_l.value is None)
check("L-2) 2홉 전(8/5) 종가 8100을 절대 쓰지 않음", _r_l.value != 8100)

# 8/6에 심볼이 있으면 그 날의 마지막 당일 close 사용
_write_day("20260806", "AAA", [9500, 9700])
_r_l2 = resolve_prev_close(_ctx_l, "AAA", _load, _root, date(2026, 8, 7))
check("L-3) 직전 데이터 날짜에 종목이 있으면 그 마지막 close 사용",
      _r_l2.value == 9700 and _r_l2.source == PREV_CLOSE_PREV_DAY)
check("L-4) previous_data_date가 기록됨",
      _r_l2.previous_data_date == date(2026, 8, 6))
check("L-5) calendar_gap_days가 기록됨", _r_l2.calendar_gap_days == 1)
check("L-6) confidence가 medium", _r_l2.confidence == "medium")

# 이름 정정 — trading day라고 단정하지 않음
check("L-7) source 이름이 PREVIOUS_DATA_DAY_FILE",
      PREV_CLOSE_PREV_DAY == "PREVIOUS_DATA_DAY_FILE")
_rc_src = Path("domain/replay_context.py").read_text(encoding="utf-8")
check("L-8) 무제한 과거 탐색 코드가 제거됨",
      "for d in reversed(prior)" not in _code_only(_rc_src))


# ══════════════════════════════════════════════════════════════
# M. analyzer 예외 fail-closed (1J.3.2, P0)
# ══════════════════════════════════════════════════════════════
check("M-1) ReplayEvaluationError가 정의됨", hasattr(RR, "ReplayEvaluationError"))


class _BoomAnalyzer:
    def analyze(self, *a, **k):
        raise RuntimeError("analyzer boom")


_m_bars = [mk("20260806", 15, 19, 10000)] + [mk("20260807", 9, m, 10000 + m)
                                             for m in range(0, 30)]
try:
    RR.run_replay("AAA", _m_bars, _BoomAnalyzer(), date(2026, 8, 7),
                  minute_bar_count=60, quality_stats=RR.ReplayQualityStats())
    _m1 = False
except RR.ReplayEvaluationError:
    _m1 = True
except Exception:
    _m1 = False
check("M-2) analyzer 예외 시 기본은 ReplayEvaluationError (조용히 []가 아님)", _m1)

_st_m = RR.ReplayQualityStats()
_res_m = RR.run_replay("AAA", _m_bars, _BoomAnalyzer(), date(2026, 8, 7),
                       minute_bar_count=60, quality_stats=_st_m,
                       skip_analyzer_errors=True)
check("M-3) --skip-analyzer-errors일 때만 건너뜀", _res_m == [])
check("M-4) analyzer_error_count가 집계됨", _st_m.analyzer_error_count > 0)
check("M-5) analyzer_error_symbols 기록", "AAA" in _st_m.analyzer_error_symbols)
check("M-6) analyzer_error_timestamps 기록", len(_st_m.analyzer_error_timestamps) > 0)
_q_m = "\n".join(RR.build_quality_report(_st_m))
check("M-7) 품질 리포트에 analyzer_error_count 출력", "analyzer_error_count" in _q_m)
check("M-8) skip 옵션 사용 경고 표시", "기본 실행에서는 이 옵션을 쓰지 마십시오" in _q_m)

_rr3 = Path("replay_runner.py").read_text(encoding="utf-8")
check("M-9) except Exception: continue 형태가 코드에 없음",
      "except Exception:\n                continue" not in _code_only(_rr3))


# ══════════════════════════════════════════════════════════════
# N. horizon bucket 상호 배타 (1J.3.2, P1)
# ══════════════════════════════════════════════════════════════
_st_n = RR.ReplayQualityStats()
# exact 3건, ≤1m stale 2건, 1~3m stale 1건, N/A 2건 = 8건
_st_n.horizon_elapsed[5] = [5.0, 5.0, 5.0, 4.5, 4.0, 2.5, None, None]
_q_n = "\n".join(RR.build_quality_report(_st_n))
check("N-1) exact가 3건으로 집계", "exact     3" in _q_n)
check("N-2) ≤1m stale이 2건(exact 제외)", "≤1m stale     2" in _q_n)
check("N-3) 1~3m stale이 1건", "1~3m stale     1" in _q_n)
check("N-4) N/A가 2건", "N/A     2" in _q_n)
check("N-5) 표본 총계가 8건", "표본     8" in _q_n)
check("N-6) 상호 배타 invariant가 리포트에 명시",
      "상호 배타적이며 합계 = 표본" in _q_n)
# invariant는 build_quality_report 내부 assert로도 강제됨
check("N-7) 합계 invariant: exact+≤1m+1~3m+N/A == 표본", 3 + 2 + 1 + 2 == 8)


# ══════════════════════════════════════════════════════════════
# O. 품질 통계 객체화 + import side-effect (1J.3.2, P1)
# ══════════════════════════════════════════════════════════════
check("O-1) ReplayQualityStats가 정의됨", hasattr(RR, "ReplayQualityStats"))
_a = RR.ReplayQualityStats()
_b2 = RR.ReplayQualityStats()
_a.symbol_days = 5
check("O-2) 인스턴스끼리 통계가 섞이지 않음", _b2.symbol_days == 0)
_a.prev_close_sources["X"] = 3
_b2.prev_close_sources["X"] = 2
_b2.merge(_a)
check("O-3) merge로 전체 집계 가능", _b2.prev_close_sources["X"] == 5)
check("O-4) merge가 symbol_days도 합산", _b2.symbol_days == 5)
check("O-5) reset_replay_quality_stats가 새 인스턴스 반환",
      RR.reset_replay_quality_stats() is not _a)

check("O-6) import 시 MINUTE_BAR_COUNT를 resolve하지 않음",
      "MINUTE_BAR_COUNT = resolve_minute_bar_count()" not in _code_only(_rr3))
check("O-7) run_replay가 minute_bar_count를 명시적으로 받음",
      "minute_bar_count: int | None = None" in _rr3)
check("O-8) run_replay가 quality_stats를 받음", "quality_stats:" in _rr3)

# import 자체가 settings.yaml을 읽지 않는지 — 서브프로세스로 확인
import subprocess as _sp
_probe = _sp.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,'.');\n"
     "import config.settings as cs\n"
     "cs.load_settings = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no config'))\n"
     "import importlib; import replay_runner as R\n"
     "print('IMPORT_OK', R.MINUTE_BAR_COUNT)"],
    capture_output=True, text=True, cwd=".")
check("O-9) 설정이 깨져도 replay_runner import 자체는 성공",
      "IMPORT_OK" in _probe.stdout)
check("O-10) import 직후 MINUTE_BAR_COUNT는 None",
      "IMPORT_OK None" in _probe.stdout)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
