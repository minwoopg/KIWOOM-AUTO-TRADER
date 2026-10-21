# -*- coding: utf-8 -*-
"""resolve_carried_over_touches_today() 검증 (2026-10-21)

이 테스트가 존재하는 이유:

민우님이 다른 LLM에 보낼 검토 요청 문서에서, 2026-10-14에 추가한
`collection_quality` 재시작 교차 확인(run_baseline.csv 조인) 기능이
**정상적인 날도 재시작으로 오탐할 수 있다**고 지적했고, 코드를
추적해 실제로 재현되는 버그임을 확인했습니다.

원인: `run_baseline.csv`에는 종료 시각 필드가 전혀 없어서(파일
스키마에 `started_at`만 있음), "오늘 이전 가장 최근 실행"이라는
사실만으로는 그 실행이 오늘까지 이어졌는지, 아니면 며칠 전에
이미 정상 종료된 채 그냥 "가장 최근" 기록으로만 남아있는지 구분할
수 없었습니다. 그 결과 운영 둘째 날부터는 `carried_over_run_present`
가 거의 항상 True가 되어, "전날 정상 종료 → 오늘 개장 전 정상
시작 → 재시작 없음"인 완전히 정상적인 날도 RESTARTED_PARTIAL류로
오판정했습니다.

수정: `resolve_carried_over_touches_today()`를 추가해, "오늘 자체
시작보다 앞선 활동(signal_log.csv 타임스탬프)이 실제로 있는지"로
좁혀 판정합니다. 10/14에 실제로 재현됐던 "전날 이월 → 당일 장중
교체" 패턴(진짜 이월)은 여전히 잡아내면서, 그냥 "과거 어느 날
실행된 적이 있다"는 사실만으로는 더 이상 오탐하지 않습니다.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import export_daily_bundle as edb
from infra.storage.run_baseline import RUN_BASELINE_FIELDS

KST = timezone(timedelta(hours=9))
TARGET = date(2026, 9, 11)
DAY_COMPACT = "20260911"


class TestResolveCarriedOverTouchesTodayPure(unittest.TestCase):
    """순수 함수 단위 테스트 — 번들 생성 전체를 거치지 않고 직접 호출."""

    def test_no_carried_over_candidate_is_false(self):
        self.assertFalse(
            edb.resolve_carried_over_touches_today(False, [], [])
        )

    def test_carried_over_with_no_same_day_start_is_true(self):
        # 자정을 넘겨 재시작 없이 계속 도는 경우 — 비교 기준(오늘
        # 자체 시작)이 없으므로 있는 그대로 True(기존 동작 유지).
        self.assertTrue(
            edb.resolve_carried_over_touches_today(True, [], [])
        )
        self.assertTrue(
            edb.resolve_carried_over_touches_today(True, [], [datetime(2026, 9, 11, 9, 0, tzinfo=KST)])
        )

    def test_normal_day_with_no_activity_before_todays_start_is_false(self):
        # 오탐 재현 시나리오: 오늘 자체 시작(08:40)이 있고, signal_log
        # 활동은 전부 그 이후(09:00~)뿐 — carried_over는 그냥 며칠
        # 전 과거 기록일 뿐, 오늘과 무관해야 함.
        today_start = datetime(2026, 9, 11, 8, 40, tzinfo=KST)
        activity = [
            datetime(2026, 9, 11, 9, 0, tzinfo=KST),
            datetime(2026, 9, 11, 9, 5, tzinfo=KST),
        ]
        self.assertFalse(
            edb.resolve_carried_over_touches_today(True, [today_start], activity)
        )

    def test_genuine_carryover_with_activity_before_todays_start_is_true(self):
        # 10/14 재현 패턴: 전날 이월 실행이 오늘 09:00부터 활동을
        # 남긴 뒤, 10:18에 새 실행(오늘 자체 시작)으로 교체됨 —
        # 09:00 활동이 10:18 시작보다 앞서므로 진짜 이월로 판정해야 함.
        today_start = datetime(2026, 9, 11, 10, 18, tzinfo=KST)
        activity = [
            datetime(2026, 9, 11, 9, 0, tzinfo=KST),
            datetime(2026, 9, 11, 10, 30, tzinfo=KST),
        ]
        self.assertTrue(
            edb.resolve_carried_over_touches_today(True, [today_start], activity)
        )

    def test_activity_exactly_at_todays_start_is_not_counted_as_before(self):
        # 경계값: 활동 시각이 오늘 시작과 "같으면" 그 활동은 오늘
        # 자체 실행이 만든 것일 수 있으므로 "앞섰다"고 보지 않음
        # (< 비교, <= 아님).
        today_start = datetime(2026, 9, 11, 8, 40, tzinfo=KST)
        activity = [today_start]
        self.assertFalse(
            edb.resolve_carried_over_touches_today(True, [today_start], activity)
        )

    def test_multiple_same_day_starts_uses_the_earliest(self):
        # 오늘 자체 시작이 여러 건이면(흔치 않지만) 가장 이른 시각을
        # 기준으로 비교해야 함 — 더 늦은 시작 시각만 보면 그 사이의
        # 진짜 이월 활동을 놓칠 수 있음.
        starts = [
            datetime(2026, 9, 11, 13, 0, tzinfo=KST),
            datetime(2026, 9, 11, 9, 30, tzinfo=KST),
        ]
        activity_before_earliest = [datetime(2026, 9, 11, 9, 0, tzinfo=KST)]
        self.assertTrue(
            edb.resolve_carried_over_touches_today(True, starts, activity_before_earliest)
        )
        activity_between = [datetime(2026, 9, 11, 10, 0, tzinfo=KST)]
        self.assertFalse(
            edb.resolve_carried_over_touches_today(True, starts, activity_between)
        )


class _BundleTestBase(unittest.TestCase):
    """LOGS_DIR/REPORTS_DIR/EXPORTS_DIR을 임시 디렉터리로 바꿔치기하고
    끝나면 원래대로 복원합니다(test_export_daily_bundle_run_baseline.py의
    동일 패턴 — 테스트 간 격리, 실제 logs/를 건드리지 않음)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.logs_dir = base / "logs"
        self.reports_dir = base / "reports"
        self.exports_dir = base / "exports"
        self.logs_dir.mkdir()
        self.reports_dir.mkdir()

        self._orig_logs = edb.LOGS_DIR
        self._orig_reports = edb.REPORTS_DIR
        self._orig_exports = edb.EXPORTS_DIR
        edb.LOGS_DIR = self.logs_dir
        edb.REPORTS_DIR = self.reports_dir
        edb.EXPORTS_DIR = self.exports_dir

    def tearDown(self):
        edb.LOGS_DIR = self._orig_logs
        edb.REPORTS_DIR = self._orig_reports
        edb.EXPORTS_DIR = self._orig_exports
        self._tmp.cleanup()

    def _write_run_baseline_csv(self, rows: list[dict]) -> None:
        path = self.logs_dir / "run_baseline.csv"
        with path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=RUN_BASELINE_FIELDS)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in RUN_BASELINE_FIELDS})

    def _write_signal_log_csv(self, timestamps: list[str]) -> None:
        path = self.logs_dir / "signal_log.csv"
        with path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=["timestamp", "symbol"])
            writer.writeheader()
            for ts in timestamps:
                writer.writerow({"timestamp": ts, "symbol": "005930"})

    def _write_app_log(self, lines: list[str]) -> None:
        path = self.logs_dir / "app.log"
        with path.open("w", encoding="utf-8") as fp:
            for line in lines:
                fp.write(line + "\n")

    def _build_and_read_quality(self) -> str:
        zip_path = edb.build(TARGET, quiet=True)
        self.assertIsNotNone(zip_path, "build()이 락 충돌 없이 정상적으로 zip을 생성해야 합니다.")
        with zipfile.ZipFile(zip_path) as z:
            return z.read("metadata/collection_quality.txt").decode("utf-8")

    @staticmethod
    def _field(quality: str, key: str) -> str:
        """`"key                    = value"` 형식의 한 줄에서 value만
        뽑아냅니다 — `add()`의 고정폭 패딩(38칸)에 의존하지 않도록."""
        for line in quality.splitlines():
            if line.startswith(key) and "=" in line:
                return line.split("=", 1)[1].strip()
        raise AssertionError(f"{key!r} 필드를 collection_quality.txt에서 찾지 못함:\n{quality}")


class TestEndToEndNormalDayNoFalsePositive(_BundleTestBase):
    """오탐 재현 시나리오: 며칠 전 종료된 실행 기록만 있고, 오늘은
    정상적으로 1회 시작해 09:00~15:20까지 수집했을 때 — 더 이상
    RESTARTED_PARTIAL로 오판정되지 않아야 함."""

    def test_old_prior_run_with_clean_single_start_today_is_not_restarted(self):
        self._write_run_baseline_csv([
            # 며칠 전(9/8)에 시작한 실행 — 오늘과는 전혀 무관, 이미
            # 끝났을 가능성이 높지만 run_baseline.csv엔 종료 시각이
            # 없어 그 사실을 직접 알 수는 없음.
            {"run_id": "run-old", "started_at": "2026-09-08T09:00:00+09:00"},
            # 오늘(9/11) 자체의 정상 시작.
            {"run_id": "run-today", "started_at": "2026-09-11T08:40:00+09:00"},
        ])
        # signal_log 활동은 전부 오늘 시작(08:40) 이후뿐.
        self._write_signal_log_csv([
            "2026-09-11T09:00:00",
            "2026-09-11T09:05:00",
            "2026-09-11T15:19:00",
        ])
        self._write_app_log([
            "2026-09-11 08:40:00 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
            "2026-09-11 09:00:30 | INFO | [COND_STATUS] seq900001=1종목 | final=1종목: ['005930']",
        ])
        quality = self._build_and_read_quality()
        self.assertEqual(self._field(quality, "process_start_count"), "1")
        self.assertNotIn("⚠ watcher.start() 로그는", quality)
        self.assertEqual(self._field(quality, "collection_status"), "COMPLETE")

    def test_second_operating_day_with_yesterdays_run_fully_ended_is_not_restarted(self):
        # "둘째 날부터 거의 항상 오탐"했던 바로 그 상황 — 어제 정상
        # 종료, 오늘 개장 전 정상 시작, 장중 재시작 없음.
        self._write_run_baseline_csv([
            {"run_id": "run-yesterday", "started_at": "2026-09-10T08:40:00+09:00"},
            {"run_id": "run-today", "started_at": "2026-09-11T08:40:00+09:00"},
        ])
        self._write_signal_log_csv([
            "2026-09-11T09:00:00",
            "2026-09-11T15:19:00",
        ])
        self._write_app_log([
            "2026-09-11 08:40:00 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
        ])
        quality = self._build_and_read_quality()
        self.assertEqual(self._field(quality, "process_start_count"), "1")
        self.assertEqual(self._field(quality, "collection_status"), "COMPLETE")


class TestEndToEndGenuineCarryoverStillDetected(_BundleTestBase):
    """10/14 재현 패턴(진짜 이월 → 당일 장중 교체)은 이 수정 후에도
    여전히 재시작으로 잡아내야 함 — 회귀 방지."""

    def test_prior_day_run_active_before_todays_replacement_is_still_restarted(self):
        self._write_run_baseline_csv([
            {"run_id": "run-carried-over", "started_at": "2026-09-10T15:01:54+09:00"},
            {"run_id": "run-replacement", "started_at": "2026-09-11T10:18:57+09:00"},
        ])
        # 이월 실행이 09:00부터 활동을 남긴 뒤 10:18에 교체됨.
        self._write_signal_log_csv([
            "2026-09-11T09:00:00",
            "2026-09-11T09:30:00",
            "2026-09-11T10:30:00",
            "2026-09-11T15:19:00",
        ])
        self._write_app_log([
            # 오늘 날짜로 시작하는 watcher.start() 로그는 교체 시점
            # (10:18) 1건뿐 — 이월 실행 자체의 시작 로그는 전날
            # 날짜라 오늘 슬라이스에는 없음(실제 운영과 동일).
            "2026-09-11 10:18:57 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
        ])
        quality = self._build_and_read_quality()
        self.assertIn("⚠ watcher.start() 로그는", quality)
        self.assertEqual(self._field(quality, "process_start_count"), "2")
        self.assertEqual(self._field(quality, "collection_status"), "RESTARTED_PARTIAL")


if __name__ == "__main__":
    unittest.main()
