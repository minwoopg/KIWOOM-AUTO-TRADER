from __future__ import annotations

"""1분봉 원본 데이터 저장기.

저장 경로:  data/minute_bars/YYYYMMDD/{symbol}.csv
저장 형식:  cntr_tm,open,high,low,close,volume
갱신 방식:  봉이 추가되면 덧붙이기 (중복 제거 후 정렬)

향후 간이 리플레이 백테스트의 입력 데이터로 사용합니다.
"""

import csv
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.models import MinuteBar


class MinuteBarSaver:
    """1분봉 데이터를 날짜/종목별 CSV로 저장합니다."""

    FIELDS = ["cntr_tm", "open", "high", "low", "close", "volume"]

    def __init__(self, base_dir: str = "data/minute_bars") -> None:
        self.base_dir = Path(base_dir)

    def _dir(self, target_date: date) -> Path:
        d = self.base_dir / target_date.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, symbol: str, target_date: date) -> Path:
        return self._dir(target_date) / f"{symbol}.csv"

    def save(self, symbol: str, bars: list[MinuteBar]) -> None:
        """분봉 리스트를 저장합니다. 기존 데이터와 병합하여 중복을 제거합니다."""
        if not bars:
            return

        target_date = datetime.now().date()
        path = self._path(symbol, target_date)

        # 기존 데이터 로드
        existing: dict[str, dict] = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    existing[row["cntr_tm"]] = row

        # 신규 봉 추가
        for bar in bars:
            existing[bar.cntr_tm] = {
                "cntr_tm": bar.cntr_tm,
                "open":    bar.open_price,
                "high":    bar.high_price,
                "low":     bar.low_price,
                "close":   bar.close_price,
                "volume":  bar.volume,
            }

        # 시간순 정렬 후 저장
        sorted_rows = sorted(existing.values(), key=lambda r: r["cntr_tm"])
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS)
            writer.writeheader()
            writer.writerows(sorted_rows)

    @staticmethod
    def load(symbol: str, target_date: date,
             base_dir: str = "data/minute_bars") -> list[dict]:
        """저장된 1분봉을 읽어 반환합니다. 리플레이용."""
        path = Path(base_dir) / target_date.strftime("%Y%m%d") / f"{symbol}.csv"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def list_dates(base_dir: str = "data/minute_bars") -> list[date]:
        """저장된 날짜 목록을 반환합니다."""
        base = Path(base_dir)
        if not base.exists():
            return []
        dates = []
        for d in sorted(base.iterdir()):
            if d.is_dir() and len(d.name) == 8:
                try:
                    dates.append(datetime.strptime(d.name, "%Y%m%d").date())
                except ValueError:
                    continue
        return dates
