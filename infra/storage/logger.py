from __future__ import annotations

"""앱 로그와 거래 로그를 관리하는 모듈."""

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


class TradeCsvLogger:
    """주문/체결 결과를 CSV 파일로 남기는 간단한 로거입니다."""

    def __init__(self, file_path: str) -> None:
        """저장 경로를 준비하고 헤더가 없으면 생성합니다."""

        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=["timestamp", "symbol", "side", "quantity", "accepted", "message", "order_id"])
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """거래 로그 한 줄을 CSV 파일에 추가합니다."""

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=["timestamp", "symbol", "side", "quantity", "accepted", "message", "order_id"])
            writer.writerow(row)
