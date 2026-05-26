from __future__ import annotations

"""앱 로그, 거래 로그, 시그널 로그를 관리하는 모듈."""

import csv
import logging
from pathlib import Path
from typing import Any

AppLogger = logging.Logger


def build_app_logger(log_file: str, level: str = "INFO") -> AppLogger:
    """파일 기반 앱 로거를 생성합니다."""

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kiwoom_auto_trader")
    logger.setLevel(level.upper())

    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# ── trades.csv ───────────────────────────────────────────────────────────────
# 기존 필드 + 매수 당시 판단 근거 컨텍스트 필드

TRADE_FIELDS = [
    # 기존 필드
    "timestamp", "symbol", "side", "quantity", "price", "accepted", "message", "order_id",
    # 컨텍스트 필드 (매수 시 기록, 매도 시 exit_reason/hold_minutes 추가)
    "entry_strategy",        # 전략명 (breakout / neutral / hold)
    "market_regime",         # 장세 (BULLISH / NEUTRAL / UNKNOWN)
    "entry_score",           # 점수 (0~8)
    "entry_reason",          # 진입 사유 요약
    "is_v_rebound",          # V자 반등 여부
    "is_pulldown_recovery",  # 눌림목 재상승 여부
    "v_drop_pct",            # 낙폭 (%)
    "v_rise_pct",            # 반등폭 (%)
    "v_low_age",             # 저점 나이 (봉 수)
    "current_vs_vwap_pct",   # 현재가 vs VWAP (%)
    "volume_ratio",          # 반등 구간 거래량 비율
    "bar_amount",            # 현재봉 거래대금 (원)
    "exit_reason",           # 매도 사유 (손절/트레일링/추세꺾임/강제청산 등)
    "hold_minutes",          # 보유 시간 (분)
]


class TradeCsvLogger:
    """주문/체결 결과를 CSV 파일로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """거래 로그 한 줄을 CSV 파일에 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            row.setdefault("price", 0)
            # 컨텍스트 필드 기본값 (매도 행이거나 컨텍스트 없는 경우)
            for field in TRADE_FIELDS[8:]:
                row.setdefault(field, "")
            writer.writerow(row)


# ── signal_log.csv ───────────────────────────────────────────────────────────
# 매수·보류 불문 모든 시그널 판단을 기록 → 파라미터 검증의 근거 데이터

SIGNAL_FIELDS = [
    "timestamp",         # 판단 시각
    "symbol",            # 종목코드
    "price",             # 현재가
    "regime",            # 장세
    "score",             # 점수
    "signal",            # BUY / HOLD / SKIP
    "skip_reason",       # HOLD/SKIP 사유 (전략 reason 그대로)
    "detected_patterns", # 감지된 패턴 목록 (V / PR / A / B / C)
    "is_v_rebound",
    "is_pulldown_recovery",
    "v_drop_pct",
    "v_rise_pct",
    "v_low_age",
    "current_vs_vwap_pct",
    "volume_ratio",
    "bar_amount",
    "ma5_above_ma20",
]


class SignalCsvLogger:
    """모든 시그널 판단 결과를 signal_log.csv에 기록합니다.

    매수된 종목뿐 아니라 탈락된 종목도 기록하여
    추후 파라미터 검증의 근거 데이터로 활용합니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """시그널 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
            for field in SIGNAL_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)
