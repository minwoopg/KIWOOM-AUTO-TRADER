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

# 2026-08-21 (1P0.8-OBS.2-C): analyze_trades.py와 승/무/패 정의를
# 공유하는 공통 helper. 이 모듈이 임포트될 때 repo root(app/main.py가
# `python -m app.main`으로 실행되며 이미 sys.path에 올려둔 경로)가
# 아직 없는 실행 컨텍스트(예: 이 파일을 단독 스크립트로 직접 실행)
# 대비 방어적으로 추가 — 이미 있으면 중복 삽입되지 않도록 확인.
if "." not in sys.path:
    sys.path.insert(0, ".")
from utils.trade_outcome import classify_outcome, format_win_rate, WIN, LOSS, BREAKEVEN  # noqa: E402


DAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _fifo_match(buy_list: list[dict], sell_list: list[dict]) -> list[dict]:
    """매수 로트를 시간순 FIFO로 소진하며 매도 건별 가중평균 매수가를 계산합니다.

    2026-07-16 수정: 기존엔 종목당 "전체 매수 평균가"를 매도 수량에
    그대로 곱해서 썼음 — 아직 안 팔린 물량의 매수가까지 평균에 섞이는
    문제가 있었음(재진입 허용 이후 부분체결이 흔해지며 실제로 발생).
    analyze_trades.py의 pair_trades()와 동일한 방식으로 통일 — 매도
    수량만큼만 매수 큐에서 정확히 소진해 가중평균을 계산한다.

    Returns: [{"buy_price": 가중평균매수가, "sell_price", "qty", "buy": 첫매수row, "sell": 매도row}, ...]
    """
    buy_queue = [[int(b.get("qty", 0) or 0), int(b.get("price", 0) or 0), b] for b in buy_list]
    matched = []
    for sell in sell_list:
        sell_price = int(sell.get("price", 0) or 0)
        sell_qty   = int(sell.get("qty", 0) or 0)
        if sell_price <= 0 or sell_qty <= 0:
            continue
        remaining, consumed_cost, consumed_qty, first_buy = sell_qty, 0, 0, None
        while remaining > 0 and buy_queue:
            lot = buy_queue[0]
            lot_qty, lot_price, lot_row = lot
            if lot_price <= 0 or lot_qty <= 0:
                buy_queue.pop(0)
                continue
            if first_buy is None:
                first_buy = lot_row
            take = min(remaining, lot_qty)
            consumed_cost += take * lot_price
            consumed_qty  += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= 0:
                buy_queue.pop(0)
        if consumed_qty <= 0 or first_buy is None:
            continue
        matched.append({
            "buy_price": consumed_cost / consumed_qty,
            "sell_price": sell_price,
            "qty": consumed_qty,
            "buy": first_buy,
            "sell": sell,
        })
    return matched


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
        with self.trade_log_file.open(encoding="utf-8-sig") as f:
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
                symbol_buys[t["symbol"]].append({
                    "price": price, "qty": qty, "ts": t["timestamp"],
                    "regime": t.get("market_regime", ""),
                    "entry_score": t.get("entry_score", ""),
                })
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

        # ── 손익 계산 ─────────────────────────────────────────
        # 기준: 당일 실제 체결가 (매수 평균 체결가 vs 매도 평균 체결가)
        # → 종목별 상세 / trade_analysis.py 와 동일한 기준으로 통일 (2026-07-09)
        # 비용(왕복 수수료 0.25% + 세금 0.18% + 슬리피지 0.10% = 0.53%)은
        # 실현 손익에 섞지 않고 별도 줄로 분리 표기한다.
        # 2026-07-15: 실제 계좌 스크린샷(7/15)과 대조한 결과 0.53% 가정이
        # 실제보다 크게 낮았음 — 실제 수수료+세금 427,911원 / 매도총액
        # 47,438,100원 = 0.902%. 스크린샷 1건 기반 보정이라 잠정치이며,
        # 데이터가 더 쌓이면 재보정 필요.
        # 2026-08-07 (1J): 비용을 domain/cost_model.py 단일 출처에서
        # 읽습니다. daily_report는 보수적 상한(Stress)을 쓰고,
        # replay 계열은 Base를 씁니다 — 서로 다른 기준이라는 점을
        # 리포트에 명시해 혼동을 막습니다.
        from domain.cost_model import load_cost_model
        _cm = load_cost_model()
        COST_RATE = _cm.stress_roundtrip_pct / 100.0

        completed_buys = [t for t in buys if t["symbol"] in today_sells]
        holding_buys   = [t for t in buys if t["symbol"] not in today_sells]

        realized_pnl   = 0   # 당일 체결가 기준 gross 손익 (비용 미차감)
        estimated_cost = 0   # 추정 비용 (별도 표기용)
        total_buy_amount  = 0
        total_sell_amount = 0
        wins = losses = breakevens = 0

        symbol_matches: dict[str, list[dict]] = {}
        for sym, sell_list in today_sells.items():
            buy_list = symbol_buys.get(sym, [])
            if not buy_list:
                continue
            matches = _fifo_match(buy_list, sell_list)
            symbol_matches[sym] = matches

            for m in matches:
                gross = (m["sell_price"] - m["buy_price"]) * m["qty"]
                # 2026-08-07 (1J.1): 비용 기준금액을 **진입 원금**으로
                # 통일. 이전엔 매도금액 기준이라 replay의
                # "gross_return_pct - cost_pct"와 의미가 달랐음
                # (매수 100 → 매도 110이면 매도금액 기준 비용은
                # 진입원금 대비 0.99%가 되어 0.90%와 어긋남).
                cost  = int(m["buy_price"] * m["qty"] * COST_RATE)
                realized_pnl   += gross
                estimated_cost += cost
                total_buy_amount  += m["buy_price"] * m["qty"]
                total_sell_amount += m["sell_price"] * m["qty"]
                # 2026-08-21 (1P0.8-OBS.2-C, 8/21 실측 재현): 기존에는
                # 여기가 sell_price >= buy_price(동률도 승)였고,
                # analyze_trades.py는 pnl_pct > 0(동률은 승 아님)이라
                # 서로 달랐습니다(017670 pnl=0 때문에 같은 5건을 두고
                # "3승 2패"/"2승 3패"로 갈림). 이제 두 파일 모두
                # utils/trade_outcome.classify_outcome() 하나만 씁니다.
                outcome = classify_outcome(m["sell_price"] - m["buy_price"])
                if outcome == WIN:
                    wins += 1
                elif outcome == LOSS:
                    losses += 1
                else:
                    breakevens += 1

        realized_pnl      = int(round(realized_pnl))
        total_buy_amount  = int(round(total_buy_amount))
        net_realized_pnl  = realized_pnl - estimated_cost

        win_rate = format_win_rate(wins, losses, breakevens)

        # ── 이월 청산 손익 계산 ───────────────────────────────────
        # 전일 이월 포지션의 매도 손익을 별도 집계 (avg_buy_price=전일 평균단가 사용)
        carryover_pnl = 0
        carryover_details: list[str] = []
        for sym, sell_list in carryover_sells.items():
            for sell in sells:
                if sell["symbol"] != sym or sell["side"] != "SELL":
                    continue
                sell_price = int(sell.get("price", 0) or 0)
                sell_qty   = int(sell.get("quantity", 0) or 0)
                avg_buy_p  = int(sell.get("avg_buy_price", 0) or 0)
                if sell_price <= 0 or sell_qty <= 0 or avg_buy_p <= 0:
                    continue
                gross = (sell_price - avg_buy_p) * sell_qty
                # 이월 포지션은 당일 진입 원금을 확정할 수 없으므로
                # 전일 평균단가 × 수량을 진입 원금 추정치로 사용합니다
                # (리포트에 추정치임을 명시).
                cost  = int(avg_buy_p * sell_qty * COST_RATE)
                net   = gross - cost
                carryover_pnl += net
                rate  = (sell_price - avg_buy_p) / avg_buy_p * 100
                sign  = "+" if net >= 0 else ""
                carryover_details.append(f"    {sym}  {rate:+.1f}%  {sign}{net:,}원")

        # 합계는 "순손익"끼리 더한다 (당일 신규 순손익 + 이월 청산 순손익).
        # carryover_pnl은 위에서 이미 비용을 차감한 net 값.
        total_pnl = net_realized_pnl + carryover_pnl

        # ── 손익 요약 ──────────────────────────────────────────────
        # 기준: 당일 실제 체결가 (종목별 상세 / trade_analysis.py와 동일 기준)
        lines.append("")
        lines.append("[ 💰 손익 요약 ]")
        pnl_sign = "+" if realized_pnl >= 0 else ""
        lines.append(f"  실현 손익 (당일 신규, 주문가 기준 예상) : {pnl_sign}{realized_pnl:>11,}원")
        completed_sell_cnt = len([t for t in sells if t['symbol'] in today_sells])
        lines.append(f"    매수 총액 : {total_buy_amount:>11,}원  (당일 주문가 기준)")
        lines.append(f"    매도 총액 : {total_sell_amount:>11,}원  ({completed_sell_cnt}건)")
        lines.append(f"    승률      : {win_rate}")
        cost_sign = "-" if estimated_cost > 0 else ""
        lines.append(f"  추정 비용 (Stress {COST_RATE*100:.2f}%, 진입 원금 기준) : {cost_sign}{estimated_cost:>11,}원")
        lines.append(f"    ※ 비용 기준: {_cm.describe()} — 이 리포트는 보수적 상한인 Stress를 적용합니다.")
        lines.append("    ※ replay/백테스트 리포트는 Base 기준이므로 수치가 다릅니다(같은 cost_model의 다른 시나리오).")
        lines.append("    ※ 이월 포지션 비용은 전일 평균단가 기준 추정치입니다.")
        net_sign = "+" if net_realized_pnl >= 0 else ""
        lines.append(f"  당일 신규 순손익        : {net_sign}{net_realized_pnl:>11,}원")
        if carryover_sells:
            co_sign = "+" if carryover_pnl >= 0 else ""
            lines.append(f"  이월 청산 손익 (순액)   : {co_sign}{carryover_pnl:>11,}원")
            for d in carryover_details:
                lines.append(d)
        total_sign = "+" if total_pnl >= 0 else ""
        lines.append("  " + "─" * 38)
        lines.append(f"  합계 실현 손익 (순액)   : {total_sign}{total_pnl:>11,}원")
        if holding_buys:
            holding_syms = sorted(set(t['symbol'] for t in holding_buys))
            holding_amt  = sum(int(t.get('price',0))*int(t.get('quantity',0)) for t in holding_buys)
            lines.append(f"  홀딩 매수   : {holding_amt:>11,}원  ({', '.join(holding_syms)})  (미청산)")

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

                matches = symbol_matches.get(sym, [])
                if matches:
                    # 2026-07-16: FIFO 매칭 결과 사용 — 매도된 수량만큼만 정확히
                    # 반영. 기존엔 avg_buy(매수 전체 평균)를 매도 전체수량에
                    # 곱해서, 라벨의 "x{total_buy_qty}주"와 실제 손익 계산에
                    # 쓰인 수량이 달라 오해를 유발했음(7/16: 096770 사례).
                    matched_qty = sum(m["qty"] for m in matches)
                    gross = sum((m["sell_price"] - m["buy_price"]) * m["qty"] for m in matches)
                    avg_sell = sum(m["sell_price"] * m["qty"] for m in matches) / matched_qty
                    matched_avg_buy = sum(m["buy_price"] * m["qty"] for m in matches) / matched_qty
                    pnl = gross
                    pnl_pct = gross / (matched_avg_buy * matched_qty) * 100
                    # 2026-08-21 (1P0.8-OBS.2-C): 위 [손익 요약]의
                    # 승/무/패 집계와 동일한 classify_outcome() 기준 —
                    # 동률(pnl==0)을 ✅로 잘못 표시하지 않음.
                    _outcome = classify_outcome(pnl)
                    result_tag = "✅" if _outcome == WIN else ("➖" if _outcome == BREAKEVEN else "❌")
                    sell_qty_note = f" x{matched_qty}주" if matched_qty != total_buy_qty else ""
                    sell_str = (
                        f"매도 {avg_sell:,.0f}원{sell_qty_note}  "
                        f"{'+' if pnl>=0 else ''}{pnl:,.0f}원 ({pnl_pct:+.1f}%)  {result_tag}"
                    )
                elif sell_list:
                    avg_sell = sum(s["price"]*s["qty"] for s in sell_list) / sum(s["qty"] for s in sell_list)
                    # 전일 이월 포지션 (당일 매수 기록 없음 → 상단 [이월 청산 손익]에서 별도 집계)
                    sell_str = f"매도 {avg_sell:,.0f}원  [전일 이월 — 손익 미집계] 🔄"
                else:
                    sell_str = "홀딩 중 🔄"

                buy_str = f"매수 {avg_buy:,.0f}원 x{total_buy_qty}주" if buy_list else "매수없음"
                if buy_list:
                    entry_regime = buy_list[0].get("regime", "")
                    entry_score  = buy_list[0].get("entry_score", "")
                    tag_bits = [b for b in (entry_regime, f"{entry_score}점" if entry_score else "") if b]
                    if tag_bits:
                        buy_str += f" [매수시점 {'/'.join(tag_bits)}]"
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
            lines.append("[ 🌐 장세 판단 (리포트 생성 시점 기준 — 매수 시점과 다를 수 있음) ]")
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
        with self.signal_log_file.open(encoding="utf-8-sig") as f:
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
        # final_decision 기준으로만 집계 (signal==BUY 기준 제거)
        buy_rows     = [r for r in rows if r.get("final_decision") == "BUY"]
        blocked_rows = [r for r in rows if r.get("final_decision") == "BLOCKED"]
        hold_rows    = [r for r in rows
                        if r.get("final_decision","") not in ("BUY","BLOCKED")]

        lines.append(
            f"  전체 판단: {total:,}건  │  "
            f"BUY: {len(buy_rows)}건 ({self._pct(len(buy_rows),total)})  │  "
            f"BLOCKED: {len(blocked_rows)}건 ({self._pct(len(blocked_rows),total)})  │  "
            f"SKIP: {len(hold_rows)}건"
        )

        # skip_reason 분포
        lines.append("")
        lines.append("  [ skip_reason 분포 ]")
        for reason, cnt in Counter(r.get("skip_reason","") for r in hold_rows).most_common():
            lines.append(f"    {(reason or '(없음)'):<38} {cnt:>4}건  {self._pct(cnt, len(hold_rows))}")

        # 패턴 분포
        lines.append("")
        lines.append("  [ 감지 패턴 ]")
        for pat, cnt in Counter(r.get("detected_patterns","-") for r in rows).most_common():
            bc = sum(1 for r in rows
                     if r.get("detected_patterns")==pat
                     and r.get("final_decision")=="BUY")
            lines.append(f"    {(pat or '-'):<38} {cnt:>4}건  → BUY {bc}건")

        # V자 분석
        v_rows = [r for r in rows if self._safe_bool(r.get("is_v_rebound")) is True]
        lines.append("")
        lines.append(f"  [ V자 반등 ]  감지 {len(v_rows)}건 / 전체 {total}건 ({self._pct(len(v_rows),total)})")
        if v_rows:
            v_buy = [r for r in v_rows if r.get("final_decision") == "BUY"]
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
        with self.trade_log_file.open(encoding="utf-8-sig") as f:
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

                # 평균매입단가: 당일 실제 체결가 기준 (상단 [손익 요약],
                # analyze_trades.py의 pair_trades()와 동일 기준 — 2026-07-09)
                if bl:
                    buy = bl.pop(0)
                    bp  = int(buy.get("price", 0) or 0)
                else:
                    continue

                if bp <= 0: continue
                pnl_pct = (sp - bp) / bp * 100
                pairs.append({
                    "symbol":      sym,
                    "pnl_pct":     pnl_pct,
                    "pnl_amount":  (sp - bp) * qty,
                    # 2026-08-21 (1P0.8-OBS.2-C → closure): "win"
                    # (pnl_pct>0) 필드는 하위호환을 위해 남겨뒀지만,
                    # 이 파일 안에서는 더 이상 쓰이지 않습니다 —
                    # exit_reason별 breakdown도 outcome(wins/(wins+
                    # losses), breakeven 제외) 기준으로 통일했습니다
                    # (세부 breakdown이 여전히 win/len(grp)라 헤드라인과
                    # 다른 승률이 날 수 있었던 문제를 닫음).
                    "outcome":     classify_outcome(pnl_pct),
                    "win":         pnl_pct > 0,
                    "exit_reason": sell.get("exit_reason",""),
                    "hold_min":    self._safe_float(sell.get("hold_minutes")),
                    "is_v":        self._safe_bool(buy.get("is_v_rebound") if buy else None),
                    "score":       buy.get("entry_score","") if buy else "",
                    "avg_buy_price": bp,
                })

        if not pairs:
            lines.append("  매매 쌍 없음 (미청산 포지션은 다음날 집계됩니다)")
            lines.append(sep)
            return "\n".join(lines)

        wins       = [p for p in pairs if p["outcome"] == WIN]
        losses     = [p for p in pairs if p["outcome"] == LOSS]
        breakevens = [p for p in pairs if p["outcome"] == BREAKEVEN]
        # 2026-08-21 (1P0.8-OBS.2-C, 8/21 실측 재현): 위 [손익 요약]
        # 섹션과 반드시 같은 승/무/패 숫자가 나오도록 동일한
        # classify_outcome()/format_win_rate()를 씁니다 — 이전에는
        # 여기가 pnl_pct>0/len(pairs) 기준이라 위 섹션(sell_price>=
        # buy_price 기준)과 동률 매매가 있을 때 서로 다른 승률을
        # 냈습니다(8/21 017670: "3승 2패" vs "2/5(40.0%)").
        lines.append(f"  승률: {format_win_rate(len(wins), len(losses), len(breakevens))}")
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
            # 2026-08-21 (1P0.8-OBS.2-C closure):
            # 헤드라인 승률(위)은 outcome 3분류로 이미 통일했지만, 이
            # exit_reason별 세부 breakdown은 여전히 "win"(pnl_pct>0)
            # 개수를 len(grp)(=BREAKEVEN 포함 전체 건수)로 나누고
            # 있었습니다 — 동률이 섞이면 분모가 달라 헤드라인과 다른
            # 승률이 나옵니다. wins/(wins+losses)로 통일.
            #
            # 2026-08-21 (1P0.8-OBS.2 최종 리뷰): 분모(w+l)가 0인
            # 경우(그룹 전체가 BREAKEVEN) `self._pct(w, w+l)`을 그대로
            # 쓰면 "0.0%"가 나와 정의 불가 상태를 승률 0%로 잘못
            # 표시합니다. `_pct()` 자체는 다른 일반 백분율 계산에도
            # 쓰이므로 전역 의미를 바꾸지 않고, 이 승률 표시 경로만
            # 분모 0일 때 "해당없음"으로 분기합니다.
            w = sum(1 for p in grp if p["outcome"] == WIN)
            l = sum(1 for p in grp if p["outcome"] == LOSS)
            decided = w + l
            win_rate = self._pct(w, decided) if decided > 0 else "해당없음"
            avg = sum(p["pnl_pct"] for p in grp) / len(grp)
            lines.append(f"    {er[:36]:<38} {len(grp)}건  승률 {win_rate}  {avg:+.2f}%")

        # 거래 목록
        lines.append("")
        lines.append("  [ 거래 목록 ]")
        for p in pairs:
            # 2026-08-21 (1P0.8-OBS.2-C): 위 헤드라인 승률과 동일한
            # outcome 기준(동률은 ➖).
            mark = "✅" if p["outcome"] == WIN else ("➖" if p["outcome"] == BREAKEVEN else "❌")
            hold = f"{p['hold_min']:.0f}분" if p["hold_min"] else "?"
            v    = "V" if p["is_v"] else "-"
            lines.append(f"    {mark} {p['symbol']}  {p['pnl_pct']:+.1f}%  {p['pnl_amount']:+,.0f}원  {hold}  [{v}]  {p['exit_reason'][:25]}")

        lines.append(sep)
        return "\n".join(lines)
