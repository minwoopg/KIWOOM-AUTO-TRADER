# -*- coding: utf-8 -*-
"""
entry_watch 기능 단위테스트 (2026-07-20)

TradingService._check_entry_watch()가 CHANGELOG_v1.5.md 7.10절에 명시한
6개 시나리오대로 동작하는지 확인합니다. 실제 브로커/전략 라우터 등은
필요 없으므로 TradingService를 생성자 없이 만들고 _check_entry_watch가
참조하는 속성(settings, state)만 채워서 메서드를 직접 호출합니다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from config.settings import EntryWatchConfig
from domain.models import Position, RuntimeState, SignalType
from domain.market_regime.minute_analyzer import MinuteAnalysis
from domain.service.trading_service import TradingService


def make_service(entry_watch: EntryWatchConfig | None) -> TradingService:
    svc = TradingService.__new__(TradingService)  # __init__ 건너뛰고 속성만 채움

    class _Settings:
        pass

    svc.settings = _Settings()
    svc.settings.entry_watch = entry_watch
    svc.state = RuntimeState()
    return svc


def make_minute_analysis(price_above_vwap: bool, vwap: float = 10000.0) -> MinuteAnalysis:
    """MinuteAnalysis는 필드가 많으므로 필요한 값만 채우고 나머지는 기본값으로."""
    import dataclasses
    fields = {f.name: None for f in dataclasses.fields(MinuteAnalysis)}
    # 흔히 쓰이는 bool/숫자 필드 기본값
    for f in dataclasses.fields(MinuteAnalysis):
        if f.type in ("bool", bool):
            fields[f.name] = False
        elif f.type in ("float", float):
            fields[f.name] = 0.0
        elif f.type in ("int", int):
            fields[f.name] = 0
    fields["price_above_vwap"] = price_above_vwap
    fields["vwap"] = vwap
    return MinuteAnalysis(**fields)


EW = EntryWatchConfig(
    enabled=True,
    watch_minutes=5,
    min_profit_pct=0.5,
    fail_cut_pct=-1.0,
    fail_on_vwap_break=True,
)

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


# ── 시나리오 1: 급락 즉시청산 ──────────────────────────────
svc = make_service(EW)
symbol = "005930"
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)  # -1.2%
check("1) 급락(-1.2%, 기준-1.0%) 즉시 SELL", sig is not None and sig.type == SignalType.SELL)
check("   사유에 급락청산 문구 포함", sig is not None and "급락청산" in sig.reason)

# ── 시나리오 2: VWAP 이탈 청산 ──────────────────────────────
svc = make_service(EW)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
ma = make_minute_analysis(price_above_vwap=False, vwap=10050.0)
sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=ma)  # +0.2%, VWAP 아래
check("2) VWAP 이탈 시 SELL", sig is not None and sig.type == SignalType.SELL)
check("   사유에 VWAP이탈청산 문구 포함", sig is not None and "VWAP이탈청산" in sig.reason)

# ── 시나리오 3: 5분 시점 최소수익 미달 청산 ──────────────────
svc = make_service(EW)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=5, seconds=10)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
ma = make_minute_analysis(price_above_vwap=True)
sig = svc._check_entry_watch(symbol, pos, current_price=10020, minute_analysis=ma)  # +0.2% < 0.5%
check("3) 5분 경과, 수익 +0.2%<0.5% → SELL", sig is not None and sig.type == SignalType.SELL)
check("   사유에 최소수익미달청산 문구 포함", sig is not None and "최소수익미달청산" in sig.reason)

# ── 시나리오 4: 양호한 경우 → 정규 전략에 위임(None) ──────────
svc = make_service(EW)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=3)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
ma = make_minute_analysis(price_above_vwap=True)
sig = svc._check_entry_watch(symbol, pos, current_price=10080, minute_analysis=ma)  # +0.8%, VWAP 위
check("4) 양호(+0.8%, VWAP위, 3분경과) → None(정규전략 위임)", sig is None)

# ── 시나리오 5: 관찰윈도우(watch_minutes+1분) 초과 시 손실이어도 관여 안 함
svc = make_service(EW)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=7)).isoformat()  # 5+1=6분 초과
pos = Position(symbol=symbol, quantity=10, average_price=10000)
sig = svc._check_entry_watch(symbol, pos, current_price=9700, minute_analysis=None)  # -3.0% 손실
check("5) 윈도우(6분) 초과, 손실 -3.0%여도 None(정규전략 위임)", sig is None)

# ── 시나리오 6: enabled=False 시 항상 비활성 ──────────────────
ew_disabled = EntryWatchConfig(
    enabled=False, watch_minutes=5, min_profit_pct=0.5,
    fail_cut_pct=-1.0, fail_on_vwap_break=True,
)
svc = make_service(ew_disabled)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)  # -1.2% 급락이어도
check("6) enabled=False → 급락(-1.2%)이어도 None", sig is None)

# ── 부가 체크: entry_watch=None (Settings 미설정) ─────────────
svc = make_service(None)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
pos = Position(symbol=symbol, quantity=10, average_price=10000)
sig = svc._check_entry_watch(symbol, pos, current_price=9880, minute_analysis=None)
check("7) entry_watch=None → None (안전 기본값)", sig is None)

# ── 부가 체크: position=None (미보유) ─────────────────────────
svc = make_service(EW)
svc.state.entry_time_by_symbol[symbol] = (datetime.now() - timedelta(minutes=2)).isoformat()
sig = svc._check_entry_watch(symbol, None, current_price=9880, minute_analysis=None)
check("8) position=None → None", sig is None)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
