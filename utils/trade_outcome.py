from __future__ import annotations

"""매매 손익의 승/무/패 분류 — daily_reporter.py와 analyze_trades.py가
공유하는 단일 정의 (1P0.8-OBS.2-C).

2026-08-21 (8/21 실측 재현): 두 리포트가 동률(pnl==0)을 서로 다르게
처리해왔습니다.

    daily_reporter.py:  sell_price >= buy_price  → 동률도 승
    analyze_trades.py:  pnl_pct > 0              → 동률은 승이 아님

017670(2026-08-21, 매수 100,300원 / 매도 100,300원, pnl=0)이 정확히
이 경계에 걸려 같은 5건의 매매를 두고 daily_report는 "3승 2패",
trade_analysis는 "2승 3패"를 동시에 출력한 것이 실제로 확인됐습니다.
이후 Low Upside/5-Min Exit 같은 수익성 연구에 이 리포트들을 근거
자료로 쓸 것이므로, 정의 자체를 이 모듈 하나로 고정하고 두 리포트가
모두 이것만 참조하게 합니다.

BUY/SELL 판단이나 lifecycle 정책과는 무관한 순수 리포트 집계
유틸리티입니다 — 여기 정의를 바꿔도 실제 매매는 전혀 영향받지
않습니다.
"""

WIN = "WIN"
LOSS = "LOSS"
BREAKEVEN = "BREAKEVEN"


def classify_outcome(pnl: float) -> str:
    """pnl(금액 또는 %, 부호만 의미 있음)을 WIN/LOSS/BREAKEVEN으로 분류합니다.

        pnl > 0  → WIN
        pnl < 0  → LOSS
        pnl == 0 → BREAKEVEN
    """
    if pnl > 0:
        return WIN
    if pnl < 0:
        return LOSS
    return BREAKEVEN


def format_win_rate(wins: int, losses: int, breakevens: int = 0) -> str:
    """N승 [M무 ]K패 (승률%) 형태의 표시 문자열을 만듭니다.

    승률 분모는 wins+losses만 씁니다 — breakeven은 분모에서 제외합니다
    (무승부를 분모에 넣으면 "이기지도 지지도 않은" 결과가 승률을 깎는
    셈이 되어 해석이 왜곡되므로, breakeven은 승패가 갈리지 않은
    결과로 취급하고 분모에서 뺍니다). breakeven이 0건이면 기존과
    동일하게 "N승 K패" 형태만 출력합니다(무변화 케이스에서 기존
    리포트 가독성을 그대로 유지).
    """
    decided = wins + losses
    pct = f"{wins/decided*100:.0f}%" if decided > 0 else None
    if wins + losses + breakevens == 0:
        return "해당없음"
    pct_str = pct if pct is not None else "해당없음"
    if breakevens > 0:
        return f"{wins}승 {breakevens}무 {losses}패 ({pct_str})"
    return f"{wins}승 {losses}패 ({pct_str})"
