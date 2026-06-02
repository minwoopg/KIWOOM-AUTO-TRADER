from __future__ import annotations

"""일일 매매 리포트 생성기.

trades.csv를 읽어서 당일 매매 내역을 분석하고
사람이 읽기 쉬운 상세 리포트를 파일로 저장합니다.

추가 API 호출 없이 로컬 파일만 사용합니다.
"""

import csv
import sys
from collections import defaultdict, Counter
from datetime import date, datetime
from pathlib import Path


DAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


class DailyReporter:
    """당일 trades.csv를 분석해 일일 리포트를 생성합니다."""

    def __init__(
        self,
        trade_log_file: str,
        report_dir: str = "logs",
        signal_log_file: str = "logs/signal_log.csv",
    ) -> None:
        self.trade_log_file  = Path(trade_log_file)
        self.signal_log_file = Path(signal_log_file)
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

        # ── 분석 섹션 추가 ─────────────────────────────────
        signal_section = self._build_signal_analysis(target_date)
        trade_section  = self._build_trade_analysis(target_date)
        full_report = report + "\n\n" + signal_section + "\n\n" + trade_section

        report_file = self.report_dir / f"daily_report_{target_date.strftime('%Y%m%d')}.txt"
        report_file.write_text(full_report, encoding="utf-8")
        return full_report

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

        # ── 전일 이월 포지션 분리 ─────────────────────────────────
        # 오늘 매수 없이 매도만 있는 종목 = 전일 이월 포지션
        carryover_sells = {
            sym: sell_list
            for sym, sell_list in symbol_sells.items()
            if sym not in symbol_buys
        }
        today_sells = {
            sym: sell_list
            for sym, sell_list in symbol_sells.items()
            if sym in symbol_buys
        }

        # 손익 계산 — 당일 매수/매도 쌍이 완성된 종목만
        # today_sells: 당일 매수+매도 모두 있는 종목
        completed_buys = [t for t in buys if t["symbol"] in today_sells]
        holding_buys   = [t for t in buys if t["symbol"] not in today_sells]
        total_buy_amount  = sum(
            int(t.get("price", 0)) * int(t.get("quantity", 0))
            for t in completed_buys
        )
        total_sell_amount = sum(
            int(t.get("price", 0)) * int(t.get("quantity", 0))
            for t in sells if t["symbol"] in today_sells
        )
        realized_pnl = total_sell_amount - total_buy_amount

        # 승/패 계산 (당일 매수/매도 쌍만)
        wins = losses = 0
        for sym, sell_list in today_sells.items():
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
        lines.append(f"  실현 손익   : {pnl_sign}{realized_pnl:>10,}원  (당일 매수/매도 기준)")
        lines.append(f"  매수 총액   : {total_buy_amount:>10,}원  ({len(completed_buys)}건)")
        lines.append(f"  매도 총액   : {total_sell_amount:>10,}원  ({len([t for t in sells if t['symbol'] in today_sells])}건)")
        if holding_buys:
            holding_syms = sorted(set(t['symbol'] for t in holding_buys))
            holding_amt  = sum(int(t.get('price',0))*int(t.get('quantity',0)) for t in holding_buys)
            lines.append(f"  홀딩 매수   : {holding_amt:>10,}원  ({', '.join(holding_syms)})  (미청산 — 손익 미집계)")
        lines.append(f"  승률        : {win_rate}")
        if carryover_sells:
            lines.append(f"  전일 이월 매도: {', '.join(sorted(carryover_sells.keys()))}  (손익 미집계)")

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
                    # avg_buy_price(잔고API) 우선 사용
                    api_buy_price = max(
                        (int(s.get("avg_buy_price", 0) or 0) for s in sell_list),
                        default=0
                    )
                    effective_buy = api_buy_price if api_buy_price > 0 else avg_buy
                    if effective_buy > 0:
                        pnl = (avg_sell - effective_buy) * total_sell_qty
                        pnl_pct = (avg_sell - effective_buy) / effective_buy * 100
                        result_tag = "✅" if pnl >= 0 else "❌"
                        price_note = "(잔고기준)" if api_buy_price > 0 else ""
                        sell_str = f"매도 {avg_sell:,.0f}원  {'+' if pnl>=0 else ''}{pnl:,.0f}원 ({pnl_pct:+.1f}%)  {result_tag} {price_note}"
                    else:
                        # 전일 이월 포지션
                        sell_str = f"매도 {avg_sell:,.0f}원  [전일 이월 — 손익 미집계] 🔄"
                else:
                    sell_str = "홀딩 중 🔄"

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

    # ── 분석 섹션 ────────────────────────────────────────────────

    def _safe_float(self, v) -> float | None:
        try:
            f = float(v)
            return f if f != 0.0 else None
        except (ValueError, TypeError):
            return None

    def _safe_bool(self, v) -> bool | None:
        s = str(v).lower()
        if s == "true":  return True
        if s == "false": return False
        return None

    def _load_signal_rows(self, target_date: date) -> list[dict]:
        if not self.signal_log_file.exists():
            return []
        rows = []
        with self.signal_log_file.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).date()
                    if ts == target_date:
                        rows.append(r)
                except (ValueError, KeyError):
                    continue
        return rows

    def _pct(self, n: int, total: int) -> str:
        return f"{n/total*100:.1f}%" if total > 0 else "0.0%"

    def _build_signal_analysis(self, target_date: date) -> str:
        rows  = self._load_signal_rows(target_date)
        sep   = "─" * 50
        lines = [sep, "  📊 시그널 분석", sep]

        if not rows:
            lines.append("  signal_log.csv 데이터 없음")
            lines.append(sep)
            return "\n".join(lines)

        total     = len(rows)
        buy_rows  = [r for r in rows if r.get("signal") == "BUY"]
        hold_rows = [r for r in rows if r.get("signal") != "BUY"]

        lines.append(f"  전체 판단: {total:,}건  │  BUY: {len(buy_rows)}건 ({self._pct(len(buy_rows),total)})  │  SKIP: {len(hold_rows)}건")

        # skip_reason 분포
        lines.append("")
        lines.append("  [ skip_reason 분포 ]")
        for reason, cnt in Counter(r.get("skip_reason","") for r in hold_rows).most_common():
            lines.append(f"    {(reason or '(없음)'):<38} {cnt:>4}건  {self._pct(cnt, len(hold_rows))}")

        # 패턴 분포
        lines.append("")
        lines.append("  [ 감지 패턴 ]")
        for pat, cnt in Counter(r.get("detected_patterns","-") for r in rows).most_common():
            bc = sum(1 for r in rows if r.get("detected_patterns")==pat and r.get("signal")=="BUY")
            lines.append(f"    {(pat or '-'):<38} {cnt:>4}건  → BUY {bc}건")

        # V자 분석
        v_rows = [r for r in rows if self._safe_bool(r.get("is_v_rebound")) is True]
        lines.append("")
        lines.append(f"  [ V자 반등 ]  감지 {len(v_rows)}건 / 전체 {total}건 ({self._pct(len(v_rows),total)})")
        if v_rows:
            v_buy = [r for r in v_rows if r.get("signal") == "BUY"]
            ages  = [self._safe_float(r.get("v_low_age")) for r in v_rows]
            ages  = [x for x in ages if x is not None]
            drops = [self._safe_float(r.get("v_drop_pct")) for r in v_rows]
            drops = [x for x in drops if x is not None]
            lines.append(f"    V자 → BUY: {len(v_buy)}건  평균 저점나이: {sum(ages)/len(ages):.1f}봉  평균 낙폭: {sum(drops)/len(drops):+.2f}%")

        # v_low_age > 5 누락 분포
        age_gt5 = sum(1 for r in rows
                      if (a := self._safe_float(r.get("v_low_age"))) is not None and a > 5)
        lines.append(f"  [ v_low_age > 5봉 건수 (현재 탐색 제외) ]  {age_gt5}건 ({self._pct(age_gt5, total)})")

        lines.append(sep)
        return "\n".join(lines)

    def _build_trade_analysis(self, target_date: date) -> str:
        if not self.trade_log_file.exists():
            return ""

        rows = []
        with self.trade_log_file.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).date()
                    if ts == target_date:
                        rows.append(r)
                except (ValueError, KeyError):
                    continue

        sep   = "─" * 50
        lines = [sep, "  📈 매매 분석", sep]

        accepted = [r for r in rows if str(r.get("accepted","")).lower() == "true"]
        buys     = [r for r in accepted if r.get("side") == "BUY"]
        sells    = [r for r in accepted if r.get("side") == "SELL"]

        # BUY-SELL 페어링
        from collections import defaultdict as dd
        sym_buys  = dd(list)
        sym_sells = dd(list)
        for r in accepted:
            (sym_buys if r.get("side")=="BUY" else sym_sells)[r["symbol"]].append(r)

        pairs = []
        for sym, sl in sym_sells.items():
            bl = list(sym_buys.get(sym, []))
            for sell in sl:
                sp  = int(sell.get("price",0) or 0)
                qty = int(sell.get("quantity",0) or 0)
                if sp <= 0: continue

                # 평균매입단가 우선순위:
                # 1) 매도 기록의 avg_buy_price (잔고API 기준)
                # 2) 당일 매수 기록의 price
                avg_buy_price = int(sell.get("avg_buy_price", 0) or 0)
                if avg_buy_price > 0:
                    bp = avg_buy_price
                    buy = bl.pop(0) if bl else None
                elif bl:
                    buy = bl.pop(0)
                    bp  = int(buy.get("price",0) or 0)
                else:
                    continue

                if bp <= 0: continue
                pnl_pct = (sp - bp) / bp * 100
                pairs.append({
                    "symbol":      sym,
                    "pnl_pct":     pnl_pct,
                    "pnl_amount":  (sp - bp) * qty,
                    "win":         pnl_pct > 0,
                    "exit_reason": sell.get("exit_reason",""),
                    "hold_min":    self._safe_float(sell.get("hold_minutes")),
                    "is_v":        self._safe_bool(buy.get("is_v_rebound") if buy else None),
                    "score":       buy.get("entry_score","") if buy else "",
                    "avg_buy_price": bp,
                    "used_api_price": avg_buy_price > 0,
                })

        if not pairs:
            lines.append("  매매 쌍 없음 (미청산 포지션은 다음날 집계됩니다)")
            lines.append(sep)
            return "\n".join(lines)

        wins = [p for p in pairs if p["win"]]
        lines.append(f"  승률: {len(wins)}/{len(pairs)} ({self._pct(len(wins),len(pairs))})")
        lines.append(f"  평균 수익률: {sum(p['pnl_pct'] for p in pairs)/len(pairs):+.2f}%")
        holds = [p["hold_min"] for p in pairs if p["hold_min"]]
        if holds:
            lines.append(f"  평균 보유: {sum(holds)/len(holds):.1f}분")

        # exit_reason별
        lines.append("")
        lines.append("  [ exit_reason별 ]")
        er_grp = {}
        for p in pairs:
            er_grp.setdefault(p["exit_reason"] or "(없음)", []).append(p)
        for er, grp in sorted(er_grp.items(), key=lambda x: -len(x[1])):
            w   = sum(1 for p in grp if p["win"])
            avg = sum(p["pnl_pct"] for p in grp) / len(grp)
            lines.append(f"    {er[:36]:<38} {len(grp)}건  승률 {self._pct(w,len(grp))}  {avg:+.2f}%")

        # 거래 목록
        lines.append("")
        lines.append("  [ 거래 목록 ]")
        for p in pairs:
            mark = "✅" if p["win"] else "❌"
            hold = f"{p['hold_min']:.0f}분" if p["hold_min"] else "?"
            v    = "V" if p["is_v"] else "-"
            lines.append(f"    {mark} {p['symbol']}  {p['pnl_pct']:+.1f}%  {p['pnl_amount']:+,.0f}원  {hold}  [{v}]  {p['exit_reason'][:25]}")

        lines.append(sep)
        return "\n".join(lines)
