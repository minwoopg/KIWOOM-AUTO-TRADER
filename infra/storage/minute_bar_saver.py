from __future__ import annotations

"""1분봉 원본 데이터 저장기.

저장 경로:  data/minute_bars/YYYYMMDD/{symbol}.csv
저장 형식:  cntr_tm,open,high,low,close,volume
갱신 방식:  봉이 추가되면 덧붙이기 (중복 제거 후 정렬)

향후 간이 리플레이 백테스트의 입력 데이터로 사용합니다.
"""

import csv
from datetime import date, datetime

from utils.time_utils import parse_kst_bar_timestamp
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
        """분봉 리스트를 저장합니다. 기존 데이터와 병합하여 중복을 제거합니다.

        2026-08-04 (GPT 코드리뷰 지적, 재현 확인): 이전 버전은
        target_date를 now_kst().date() 하나로만 정해서, bars 전체를
        같은 오늘 날짜 폴더에 그대로 저장하고 있었음 — 키움 최근
        60봉 응답은 장 초반에 전일 봉과 오늘 봉이 섞여 오는 경우가
        흔한데(예: 09:01에 전일 43개+오늘 17개), 그 60개가 전부
        오늘 날짜 파일 하나에 뒤섞여 저장되고 있었음(재현 확인:
        20260804 15:29 봉과 20260805 09:00 봉을 함께 넣으면 둘 다
        20260805/{symbol}.csv 안에 섞여 들어감).

        이건 세션 지표(1C단계)에서 이미 재현·차단했던 것과 정확히
        같은 유형의 오염이 리플레이용 원본 CSV에는 그대로 남아있던
        것 — 오늘 폴더에 전일 봉이 섞이면 그 리플레이 파일로 계산
        하는 당일 고가·저가·VWAP과 A/B/C/V/PR 패턴의 장 초반 판정이
        전부 왜곡됨.

        수정: 각 봉을 cntr_tm 기준으로 파싱해 실제 날짜별로 그룹핑한
        뒤, 그 날짜의 폴더에 각각 저장 — 예를 들어 60개 중 43개가
        어제 것이면 어제 폴더에 43개, 17개가 오늘 것이면 오늘
        폴더에 17개로 정확히 나뉘어 저장됨. 파싱 불가능한 timestamp
        (형식이 깨진 봉)는 조용히 건너뜀 — 저장 자체를 막지 않되,
        어느 날짜 폴더에도 잘못된 데이터를 넣지 않음.
        """
        if not bars:
            return

        bars_by_date: dict[date, list[MinuteBar]] = {}
        for bar in bars:
            bar_dt = parse_kst_bar_timestamp(bar.cntr_tm)
            if bar_dt is None:
                continue
            bars_by_date.setdefault(bar_dt.date(), []).append(bar)

        for target_date, date_bars in bars_by_date.items():
            self._save_for_date(symbol, date_bars, target_date)

    def _save_for_date(self, symbol: str, bars: list[MinuteBar], target_date: date) -> None:
        """이미 같은 날짜로 필터링된 봉들을 해당 날짜 폴더에 저장합니다."""
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
