from __future__ import annotations

"""일일 매매 리포트 생성기.

trades.csv를 읽어서 당일 매매 내역을 분석하고
사람이 읽기 쉬운 리포트를 파일로 저장합니다.

추가 API 호출 없이 로컬 파일만 사용합니다.
"""

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path


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
        """리포트를 생성하고 파일로 저장한 뒤 내용 문자열을 반환합니다.

        Parameters
        ----------
        target_date    : 리포트 대상 날짜 (기본: 오늘)
        regime_summary : 종목별 장세 판단 요약 (선택)
                         예: {'001510': 'SIDEWAYS (RSI 76.4↓)'}
        """
        target_date = target_date or date.today()
        trades = self._load_trades(target_date)
        report = self._build_report(target_date, trades, regime_summary or {})

        # 파일 저장
        report_file = self.report_dir / f"daily_report_{target_date.strftime('%Y%m%d')}.txt"
        report_file.write_text(report, encoding="utf-8")

        return report

    # ── 내부 메서드 ──────────────────────────────────────────────

    def _load_trades(self, target_date: date) -> list[dict]:
        """trades.csv에서 당일 거래 내역만 필터링합니다."""
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
        date_str = target_date.strftime("%Y-%m-%d")
        lines = []

        lines.append(f"{'=' * 45}")
        lines.append(f"  일일 매매 리포트  {date_str}")
        lines.append(f"{'=' * 45}")

        if not trades:
            lines.append("")
            lines.append("  당일 거래 내역이 없습니다.")
            lines.append(f"{'=' * 45}")
            return "\n".join(lines)

        # ── 집계 ──────────────────────────────────────────────────
        accepted_trades = [t for t in trades if t.get("accepted", "").lower() == "true"]
        buy_trades  = [t for t in accepted_trades if t["side"] == "BUY"]
        sell_trades = [t for t in accepted_trades if t["side"] == "SELL"]

        # 종목별 집계
        symbol_stats: dict[str, dict] = defaultdict(lambda: {
            "buy_count": 0, "sell_count": 0,
            "buy_amount": 0, "sell_amount": 0,
        })

        # 주의: trades.csv에 체결가가 없으므로 수량 기반으로만 집계합니다.
        # 실제 손익은 증권사 앱에서 확인하세요.
        buy_qty_total  = sum(int(t["quantity"]) for t in buy_trades)
        sell_qty_total = sum(int(t["quantity"]) for t in sell_trades)

        for t in accepted_trades:
            s = t["symbol"]
            qty = int(t["quantity"])
            if t["side"] == "BUY":
                symbol_stats[s]["buy_count"]  += 1
                symbol_stats[s]["buy_amount"] += qty
            else:
                symbol_stats[s]["sell_count"]  += 1
                symbol_stats[s]["sell_amount"] += qty

        fail_count = len(trades) - len(accepted_trades)

        # ── 매매 요약 ──────────────────────────────────────────────
        lines.append("")
        lines.append("[ 매매 요약 ]")
        lines.append(f"  총 주문 수  : {len(trades)}건")
        lines.append(f"  매수        : {len(buy_trades)}건  ({buy_qty_total}주)")
        lines.append(f"  매도        : {len(sell_trades)}건  ({sell_qty_total}주)")
        lines.append(f"  체결 성공   : {len(accepted_trades)}건 / 실패 : {fail_count}건")

        # ── 종목별 내역 ────────────────────────────────────────────
        lines.append("")
        lines.append("[ 종목별 내역 ]")
        for symbol, stat in sorted(symbol_stats.items()):
            lines.append(
                f"  {symbol}  "
                f"매수 {stat['buy_count']}회({stat['buy_amount']}주) / "
                f"매도 {stat['sell_count']}회({stat['sell_amount']}주)"
            )

        # ── 장세 판단 요약 ─────────────────────────────────────────
        if regime_summary:
            lines.append("")
            lines.append("[ 장세 판단 ]")
            for symbol, regime in sorted(regime_summary.items()):
                lines.append(f"  {symbol}  {regime}")

        # ── 주의사항 ───────────────────────────────────────────────
        lines.append("")
        lines.append("[ 참고 ]")
        lines.append("  실제 손익은 증권사 앱에서 확인하세요.")
        lines.append("  이 리포트는 주문 수량 기준 집계입니다.")
        lines.append(f"{'=' * 45}")

        return "\n".join(lines)
