from __future__ import annotations

"""일일 매매 리포트 생성기.

trades.csv를 읽어서 당일 매매 내역을 분석하고
사람이 읽기 쉬운 상세 리포트를 파일로 저장합니다.

추가 API 호출 없이 로컬 파일만 사용합니다.
"""

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path


DAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


class DailyReporter:
    """당일 trades.csv를 분석해 일일 리포트를 생성합니다."""

    def __init__(self, trade_log_file: str, report_dir: str = "logs") -> None:
        self.trade_log_file = Path(trade_log_file)
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        target_date: date | None = None,
        regime_summary: dict[str, str] | None = None,
    ) -> str:
        target_date = target_date or date.today()
        trades = self._load_trades(target_date)
        report = self._build_report(target_date, trades, regime_summary or {})

        report_file = self.report_dir / f"daily_report_{target_date.strftime('%Y%m%d')}.txt"
        report_file.write_text(report, encoding="utf-8")
        return report

    # ── 내부 메서드 ──────────────────────────────────────────────

    def _load_trades(self, target_date: date) -> list[dict]:
        if not self.trade_log_file.exists():
            return []
        trades = []
        with self.trade_log_file.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts.date() == target_date:
                        trades.append(row)
                except (ValueError, KeyError):
                    continue
        return trades

    def _build_report(
        self,
        target_date: date,
        trades: list[dict],
        regime_summary: dict[str, str],
    ) -> str:
        day_str = DAYS_KO[target_date.weekday()]
        date_str = target_date.strftime(f"%Y-%m-%d ({day_str})")
        sep = "═" * 50

        lines = []
        lines.append(sep)
        lines.append(f"  📊 일일 매매 리포트  {date_str}")
        lines.append(sep)

        if not trades:
            lines.append("")
            lines.append("  당일 거래 내역이 없습니다.")
            lines.append(sep)
            return "\n".join(lines)

        accepted = [t for t in trades if t.get("accepted", "").lower() == "true"]
        failed   = [t for t in trades if t.get("accepted", "").lower() != "true"]
        buys     = [t for t in accepted if t["side"] == "BUY"]
        sells    = [t for t in accepted if t["side"] == "SELL"]

        # ── 종목별 매수/매도 매칭 ─────────────────────────────────
        symbol_buys:  dict[str, list] = defaultdict(list)
        symbol_sells: dict[str, list] = defaultdict(list)

        for t in accepted:
            price = int(t.get("price", 0))
            qty   = int(t.get("quantity", 0))
            if t["side"] == "BUY":
                symbol_buys[t["symbol"]].append({"price": price, "qty": qty, "ts": t["timestamp"]})
            else:
                symbol_sells[t["symbol"]].append({"price": price, "qty": qty, "ts": t["timestamp"]})

        # 손익 계산
        total_buy_amount  = sum(int(t.get("price", 0)) * int(t.get("quantity", 0)) for t in buys)
        total_sell_amount = sum(int(t.get("price", 0)) * int(t.get("quantity", 0)) for t in sells)
        realized_pnl = total_sell_amount - total_buy_amount

        # 승/패 계산 (매도한 종목 기준)
        wins = losses = 0
        for sym, sell_list in symbol_sells.items():
            buy_list = symbol_buys.get(sym, [])
            if not buy_list:
                continue
            avg_buy  = sum(b["price"] * b["qty"] for b in buy_list) / sum(b["qty"] for b in buy_list)
            avg_sell = sum(s["price"] * s["qty"] for s in sell_list) / sum(s["qty"] for s in sell_list)
            if avg_sell >= avg_buy:
                wins += 1
            else:
                losses += 1

        total_match = wins + losses
        win_rate = f"{wins}승 {losses}패 ({wins/total_match*100:.0f}%)" if total_match > 0 else "해당없음"

        # ── 손익 요약 ──────────────────────────────────────────────
        lines.append("")
        lines.append("[ 💰 손익 요약 ]")
        pnl_sign = "+" if realized_pnl >= 0 else ""
        lines.append(f"  실현 손익   : {pnl_sign}{realized_pnl:>10,}원")
        lines.append(f"  매수 총액   : {total_buy_amount:>10,}원  ({len(buys)}건)")
        lines.append(f"  매도 총액   : {total_sell_amount:>10,}원  ({len(sells)}건)")
        lines.append(f"  승률        : {win_rate}")

        # ── 종목별 상세 ────────────────────────────────────────────
        all_symbols = sorted(set(list(symbol_buys.keys()) + list(symbol_sells.keys())))

        if all_symbols:
            lines.append("")
            lines.append("[ 📋 종목별 상세 ]")

            for sym in all_symbols:
                buy_list  = symbol_buys.get(sym, [])
                sell_list = symbol_sells.get(sym, [])

                if buy_list:
                    avg_buy = sum(b["price"]*b["qty"] for b in buy_list) / sum(b["qty"] for b in buy_list)
                    total_buy_qty = sum(b["qty"] for b in buy_list)
                else:
                    avg_buy = 0
                    total_buy_qty = 0

                if sell_list:
                    avg_sell = sum(s["price"]*s["qty"] for s in sell_list) / sum(s["qty"] for s in sell_list)
                    total_sell_qty = sum(s["qty"] for s in sell_list)
                    pnl = (avg_sell - avg_buy) * total_sell_qty if avg_buy > 0 else 0
                    pnl_pct = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0
                    result_tag = "✅" if pnl >= 0 else "❌"
                    sell_str = f"매도 {avg_sell:,.0f}원  {'+' if pnl>=0 else ''}{pnl:,.0f}원 ({pnl_pct:+.1f}%)  {result_tag}"
                else:
                    sell_str = "홀딩 중 🔄"
                    pnl = 0

                buy_str = f"매수 {avg_buy:,.0f}원 x{total_buy_qty}주" if buy_list else "매수없음"
                lines.append(f"  {sym}  {buy_str}  →  {sell_str}")

        # ── 매매 통계 ──────────────────────────────────────────────
        lines.append("")
        lines.append("[ 📈 매매 통계 ]")
        lines.append(f"  총 주문     : {len(trades)}건  (성공 {len(accepted)} / 실패 {len(failed)})")
        lines.append(f"  매수        : {len(buys)}건  ({sum(int(t.get('quantity',0)) for t in buys)}주)")
        lines.append(f"  매도        : {len(sells)}건  ({sum(int(t.get('quantity',0)) for t in sells)}주)")

        # 평균 보유 시간 계산
        hold_times = []
        for sym in all_symbols:
            buy_list  = symbol_buys.get(sym, [])
            sell_list = symbol_sells.get(sym, [])
            if buy_list and sell_list:
                buy_dt  = datetime.fromisoformat(buy_list[0]["ts"])
                sell_dt = datetime.fromisoformat(sell_list[-1]["ts"])
                hold_times.append((sell_dt - buy_dt).total_seconds() / 60)

        if hold_times:
            avg_hold = sum(hold_times) / len(hold_times)
            lines.append(f"  평균 보유   : 약 {avg_hold:.0f}분")

        # ── 장세 판단 요약 ─────────────────────────────────────────
        if regime_summary:
            lines.append("")
            lines.append("[ 🌐 장세 판단 ]")
            for sym in all_symbols:
                if sym in regime_summary:
                    lines.append(f"  {sym}  {regime_summary[sym]}")

        # ── 참고 ──────────────────────────────────────────────────
        lines.append("")
        lines.append("[ ℹ️  참고 ]")
        lines.append("  표시 손익은 주문가 기준 예상값입니다. 실제 체결가와 다를 수 있습니다.")
        lines.append("  정확한 손익은 증권사 앱에서 확인하세요.")
        lines.append(sep)

        return "\n".join(lines)
