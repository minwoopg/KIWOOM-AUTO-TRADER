"""
스윙 전략 서비스.

15:10~15:20 사이 일봉 MA10 근처 눌림 종목을 매수하고
익일~수일 내 매도 조건 충족 시 청산합니다.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from domain.models import PriceBar
from domain.swing.swing_analyzer import SwingAnalyzer, SwingAnalysis
from domain.swing.swing_strategy import (
    SwingStrategy, SwingPosition, SwingExitReason,
)
from infra.broker.base import BrokerBase
from infra.notify.kakao_notifier import KakaoNotifier


class SwingService:
    """스윙 전략 서비스."""

    def __init__(
        self,
        broker: BrokerBase,
        analyzer: SwingAnalyzer,
        strategy: SwingStrategy,
        notifier: KakaoNotifier,
        settings,
        app_logger: logging.Logger,
    ):
        self.broker    = broker
        self.analyzer  = analyzer
        self.strategy  = strategy
        self.notifier  = notifier
        self.settings  = settings
        self.logger    = app_logger

        self._positions: dict[str, SwingPosition] = {}
        self._watch_log: list[dict] = []

        # 설정 단축
        cfg_entry = settings.swing_entry
        cfg_store = settings.swing_storage
        self._trade_enabled     = cfg_entry.trade_enabled
        self._max_positions     = cfg_entry.max_positions
        self._order_cash        = cfg_entry.order_cash_per_trade
        self._start_time        = cfg_entry.start_time   # "15:10"
        self._end_time          = cfg_entry.end_time     # "15:20"
        self._trades_csv        = Path(cfg_store.trades_csv)
        self._watch_csv         = Path(cfg_store.watch_csv)

        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_csv_headers()

    # ── 메인 루프 ─────────────────────────────────────────────

    def run_entry_scan(self, symbols: list[str]) -> None:
        """
        15:10~15:20 사이 진입 스캔.
        각 종목의 일봉 데이터를 받아 진입 조건 평가.
        """
        now      = datetime.now()
        now_str  = now.strftime("%H:%M")
        mode_tag = "실매수" if self._trade_enabled else "관찰모드"

        if not (self._start_time <= now_str <= self._end_time):
            return

        self.logger.info(
            f"[SWING] 진입 스캔 시작 ({mode_tag}) | "
            f"후보 {len(symbols)}종목"
        )

        candidates: list[SwingAnalysis] = []

        for symbol in symbols:
            try:
                analysis = self._analyze_symbol(symbol)
                if analysis is None:
                    continue

                self.logger.info(f"[SWING_WATCH] {analysis}")

                # 관찰 로그 저장
                self._write_watch_log(analysis)

                if analysis.entry_ok:
                    candidates.append(analysis)
                else:
                    self.logger.info(
                        f"[SWING_SKIP] {symbol} | {analysis.block_reason}"
                    )

            except Exception as e:
                self.logger.warning(f"[SWING] {symbol} 분석 실패: {e}")

        # 점수 높은 순 정렬
        candidates.sort(key=lambda a: a.score, reverse=True)

        # 최대 포지션 수 제한
        slots = self._max_positions - len(self._positions)
        for analysis in candidates[:slots]:
            self._try_entry(analysis)

    def run_exit_check(self, today: date) -> None:
        """
        보유 포지션 매도 조건 체크.
        장 중 주기적으로 호출.
        """
        for symbol, pos in list(self._positions.items()):
            try:
                market_price = self.broker.get_market_price(symbol)
                if market_price is None:
                    continue

                current_price = market_price.current_price

                # 일봉 데이터로 MA5 계산
                daily_bars = self.broker.get_daily_prices(symbol, days=10)
                ma5 = self._calc_ma(daily_bars, 5)

                signal = self.strategy.check_exit(
                    pos=pos,
                    current_price=current_price,
                    today=today,
                    ma5=ma5,
                )

                if signal is None:
                    profit_pct = (
                        (current_price - pos.avg_price) / pos.avg_price * 100
                    )
                    self.logger.debug(
                        f"[SWING_HOLD] {symbol} | "
                        f"수익률 {profit_pct:+.1f}% | "
                        f"고점 {pos.peak_price:,}원 | "
                        f"쿠션 {'O' if pos.cushion_hit else 'X'}"
                    )
                    continue

                # 감시 신호 (매도 없음)
                if signal.sell_ratio == 0:
                    self.logger.warning(
                        f"[SWING_WATCH] {symbol} | {signal.message}"
                    )
                    self.notifier.send(
                        f"⚠️ [스윙 감시] {symbol}\n{signal.message}"
                    )
                    continue

                self._try_exit(pos, current_price, signal)

            except Exception as e:
                self.logger.warning(f"[SWING] {symbol} 매도 체크 실패: {e}")

    # ── 진입/청산 ─────────────────────────────────────────────

    def _try_entry(self, analysis: SwingAnalysis) -> None:
        symbol        = analysis.symbol
        current_price = analysis.current_price
        quantity      = max(1, self._order_cash // current_price)

        self.logger.info(
            f"[SWING_{'BUY' if self._trade_enabled else 'WATCH'}] "
            f"{symbol} | 점수 {analysis.score}/8 | "
            f"수량 {quantity}주 | {analysis.score_detail}"
        )

        if not self._trade_enabled:
            # 관찰 모드 — 주문 없이 기록만
            self.logger.info(
                f"[SWING_WATCH_ENTRY] {symbol} | "
                f"가격 {current_price:,}원 | 수량 {quantity}주 (관찰)"
            )
            return

        # 실매수
        from domain.models import OrderRequest, OrderSide
        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            price=current_price,
        )
        result = self.broker.place_order(order)

        if result.accepted:
            pos = SwingPosition(
                symbol=symbol,
                entry_price=current_price,
                quantity=quantity,
                entry_date=date.today(),
            )
            self._positions[symbol] = pos

            self.logger.info(
                f"[SWING_BUY] {symbol} | "
                f"{current_price:,}원 x {quantity}주 | "
                f"점수 {analysis.score}/8"
            )
            self.notifier.send(
                f"📈 [스윙 매수] {symbol}\n"
                f"가격: {current_price:,}원 | 수량: {quantity}주\n"
                f"점수: {analysis.score}/8 | {analysis.score_detail}"
            )
            self._write_trade_log(
                symbol, "BUY", quantity, current_price,
                reason=f"스윙진입 점수{analysis.score}"
            )
        else:
            self.logger.warning(
                f"[SWING_FAIL] {symbol} | 매수 실패: {result.message}"
            )

    def _try_exit(
        self,
        pos: SwingPosition,
        current_price: int,
        signal,
    ) -> None:
        symbol   = pos.symbol
        sell_qty = max(1, int(pos.quantity * signal.sell_ratio))

        profit_pct = (current_price - pos.avg_price) / pos.avg_price * 100
        profit_amt = (current_price - pos.avg_price) * sell_qty

        self.logger.info(
            f"[SWING_SELL] {symbol} | "
            f"{current_price:,}원 x {sell_qty}주 | "
            f"수익률 {profit_pct:+.1f}% | {signal.message}"
        )

        if not self._trade_enabled:
            self.logger.info(f"[SWING_WATCH_EXIT] {symbol} | (관찰)")
            return

        from domain.models import OrderRequest, OrderSide
        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=sell_qty,
            price=current_price,
        )
        result = self.broker.place_order(order)

        if result.accepted:
            icon = "✅" if profit_amt >= 0 else "❌"
            self.notifier.send(
                f"{icon} [스윙 매도] {symbol}\n"
                f"가격: {current_price:,}원 | 수량: {sell_qty}주\n"
                f"수익률: {profit_pct:+.2f}% | 손익: {profit_amt:+,}원\n"
                f"사유: {signal.message}"
            )
            self._write_trade_log(
                symbol, "SELL", sell_qty, current_price,
                reason=signal.message,
                profit_pct=profit_pct,
                profit_amt=profit_amt,
            )

            # 전량 청산 시 포지션 제거
            if signal.sell_ratio >= 1.0:
                self._positions.pop(symbol, None)
            else:
                pos.quantity -= sell_qty
                # 부분매도 후 평균단가 유지
        else:
            self.logger.warning(
                f"[SWING_SELL_FAIL] {symbol} | 실패: {result.message}"
            )

    # ── 분석 ──────────────────────────────────────────────────

    def _analyze_symbol(
        self, symbol: str
    ) -> Optional[SwingAnalysis]:
        """종목 일봉 데이터 수집 및 스윙 분석."""
        import time

        daily_bars = self.broker.get_daily_prices(symbol, days=260)
        time.sleep(0.3)  # API 과호출 방지

        if not daily_bars or len(daily_bars) < 22:
            self.logger.debug(f"[SWING] {symbol} 일봉 부족")
            return None

        market_price = self.broker.get_market_price(symbol)
        if market_price is None:
            return None

        current_price = market_price.current_price

        # 당일 거래대금 = 당일 봉 종가 × 거래량으로 근사
        # (일봉 API에 거래대금 필드가 없으므로)
        today_bar     = daily_bars[-1] if daily_bars else None
        trading_value = (
            today_bar.close_price * today_bar.volume
            if today_bar else 0
        )

        return self.analyzer.analyze(
            symbol=symbol,
            bars=daily_bars,
            current_price=current_price,
            trading_value=trading_value,
        )

    @staticmethod
    def _calc_ma(bars: list[PriceBar], period: int) -> float:
        """이동평균 계산."""
        closes = [b.close_price for b in bars]
        prev   = closes[:-1]  # 당일 미완성봉 제외
        if len(prev) < period:
            return 0.0
        return sum(prev[-period:]) / period

    # ── CSV 로그 ──────────────────────────────────────────────

    def _ensure_csv_headers(self) -> None:
        if not self._trades_csv.exists():
            with self._trades_csv.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=[
                    "timestamp", "symbol", "side", "quantity", "price",
                    "reason", "profit_pct", "profit_amt",
                ]).writeheader()
        if not self._watch_csv.exists():
            with self._watch_csv.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=[
                    "timestamp", "symbol", "price", "score",
                    "ma10_dist", "ma20_rising", "vol_ratio",
                    "drawdown_52w", "day_rate", "entry_ok", "block_reason",
                    "score_detail",
                ]).writeheader()

    def _write_trade_log(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: int,
        reason: str,
        profit_pct: float = 0.0,
        profit_amt: int = 0,
    ) -> None:
        with self._trades_csv.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=[
                "timestamp", "symbol", "side", "quantity", "price",
                "reason", "profit_pct", "profit_amt",
            ]).writerow({
                "timestamp":  datetime.now().isoformat(),
                "symbol":     symbol,
                "side":       side,
                "quantity":   quantity,
                "price":      price,
                "reason":     reason,
                "profit_pct": round(profit_pct, 4),
                "profit_amt": profit_amt,
            })

    def _write_watch_log(self, a: SwingAnalysis) -> None:
        with self._watch_csv.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=[
                "timestamp", "symbol", "price", "score",
                "ma10_dist", "ma20_rising", "vol_ratio",
                "drawdown_52w", "day_rate", "entry_ok", "block_reason",
                "score_detail",
            ]).writerow({
                "timestamp":    datetime.now().isoformat(),
                "symbol":       a.symbol,
                "price":        a.current_price,
                "score":        a.score,
                "ma10_dist":    round(a.ma10_distance_pct, 2),
                "ma20_rising":  a.ma20_rising,
                "vol_ratio":    round(a.volume_ratio_20d, 2),
                "drawdown_52w": round(a.drawdown_from_52w_pct, 2),
                "day_rate":     round(a.day_rate_pct, 2),
                "entry_ok":     a.entry_ok,
                "block_reason": a.block_reason,
                "score_detail": a.score_detail,
            })
