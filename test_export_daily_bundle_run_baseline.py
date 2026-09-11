# -*- coding: utf-8 -*-
"""2026-09-11 (B01 보완): daily bundle의 run_baseline.csv 연결 테스트.

배경: export_daily_bundle.py가 지금까지 run_baseline.csv(B01)를 전혀
수집하지 않아서, 번들만 봐서는 "이 날짜 거래가 어떤 실행/설정으로
나왔는지"를 알 수 없었습니다(민우님/GPT 지적). 이 파일은 그 보완
사항만 검증합니다:

1. 당일 시작한 실행이 run_baseline_YYYYMMDD.csv로 번들에 포함됨.
2. 전날(혹은 그 이전) 시작해 이 날짜까지 이어지고 있을 수 있는 가장
   최근 실행("이월 추정")도 함께 포함되고, MANIFEST에 best-effort임이
   명시됨.
3. redacted config snapshot(JSON)이 존재하면 함께 포함됨.
4. run_baseline.csv 자체가 없으면 MISSING으로 명시됨(자동 대체 없음).
5. 거래/신호의 첫 기록이 포함된 실행의 시작 시각보다 이르면(=조인
   근거가 없으면) UNRESOLVED로 명시됨(자동 보정하지 않음).

매매 판단 로직은 전혀 건드리지 않았습니다 — 순수 관측/번들 구성
기능 검증입니다.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

import export_daily_bundle as edb
from infra.storage.run_baseline import RUN_BASELINE_FIELDS

TARGET = date(2026, 9, 11)
DAY_COMPACT = "20260911"


class _BundleTestBase(unittest.TestCase):
    """LOGS_DIR/REPORTS_DIR/EXPORTS_DIR을 임시 디렉터리로 바꿔치기하고
    끝나면 원래대로 복원합니다(테스트 간 격리, 실제 logs/를 건드리지 않음).
    """

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

    def _build_and_read_manifest(self) -> tuple[Path, str]:
        zip_path = edb.build(TARGET, quiet=True)
        self.assertIsNotNone(zip_path, "build()이 락 충돌 없이 정상적으로 zip을 생성해야 합니다.")
        with zipfile.ZipFile(zip_path) as z:
            manifest = z.read("MANIFEST.txt").decode("utf-8")
        return zip_path, manifest


class TestRunBaselineMissing(_BundleTestBase):

    def test_missing_run_baseline_csv_is_reported_not_silently_skipped(self):
        # run_baseline.csv를 아예 쓰지 않음 — 원본 없는 상태 재현.
        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("run_baseline.csv", manifest)
        self.assertIn("MISSING", manifest)


class TestRunBaselineSameDayIncluded(_BundleTestBase):

    def test_same_day_run_is_included_with_no_carry_over(self):
        self._write_run_baseline_csv([
            {"run_id": "run-today", "started_at": "2026-09-11T09:00:00+09:00",
             "git_sha": "abc123", "git_dirty": "False",
             "effective_config_hash": "hash1", "is_mock": "False",
             "is_paper_trading": "False", "python_version": "3.11.0"},
        ])
        zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("당일 시작 1건", manifest)
        self.assertIn("이월 추정 0건", manifest)
        self.assertNotIn("이월 추정 실행(run_id=", manifest)

        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            self.assertIn(f"raw/run_baseline_{DAY_COMPACT}.csv", names)
            rows = list(csv.DictReader(
                z.read(f"raw/run_baseline_{DAY_COMPACT}.csv").decode("utf-8").splitlines()
            ))
            self.assertEqual([r["run_id"] for r in rows], ["run-today"])


class TestRunBaselineCarriedOver(_BundleTestBase):

    def test_prior_day_run_is_included_as_carried_over_and_labeled_best_effort(self):
        self._write_run_baseline_csv([
            {"run_id": "run-yesterday", "started_at": "2026-09-10T23:50:00+09:00",
             "git_sha": "abc123", "git_dirty": "False",
             "effective_config_hash": "hash1", "is_mock": "False",
             "is_paper_trading": "False", "python_version": "3.11.0"},
        ])
        zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("당일 시작 0건", manifest)
        self.assertIn("이월 추정 1건", manifest)
        self.assertIn("run_id=run-yesterday", manifest)
        self.assertIn("확정된 연결이 아닙니다", manifest)

        with zipfile.ZipFile(zip_path) as z:
            rows = list(csv.DictReader(
                z.read(f"raw/run_baseline_{DAY_COMPACT}.csv").decode("utf-8").splitlines()
            ))
            self.assertEqual([r["run_id"] for r in rows], ["run-yesterday"])

    def test_run_started_further_in_the_past_is_not_pulled_in_when_a_same_day_run_exists(self):
        # 이월 후보는 "당일 이전 중 가장 최근" 1건만 — 오래된 과거 실행이
        # 섞여 들어가면 안 됨.
        self._write_run_baseline_csv([
            {"run_id": "run-old", "started_at": "2026-09-08T09:00:00+09:00"},
            {"run_id": "run-yesterday", "started_at": "2026-09-10T23:50:00+09:00"},
            {"run_id": "run-today", "started_at": "2026-09-11T09:00:00+09:00"},
        ])
        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("당일 시작 1건", manifest)
        self.assertIn("이월 추정 1건", manifest)
        self.assertIn("run-yesterday", manifest)
        self.assertNotIn("run-old", manifest)


class TestRunBaselineConfigSnapshot(_BundleTestBase):

    def test_config_snapshot_json_included_when_present(self):
        self._write_run_baseline_csv([
            {"run_id": "run-today", "started_at": "2026-09-11T09:00:00+09:00"},
        ])
        config_dir = self.logs_dir / "run_baseline_configs"
        config_dir.mkdir()
        (config_dir / "run-today.json").write_text('{"run_id": "run-today"}', encoding="utf-8")

        zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("config snapshot(redacted) 포함: 1/1건", manifest)
        with zipfile.ZipFile(zip_path) as z:
            self.assertIn("raw/run_baseline_config_run-today.json", z.namelist())

    def test_missing_config_snapshot_is_not_treated_as_failure(self):
        self._write_run_baseline_csv([
            {"run_id": "run-today", "started_at": "2026-09-11T09:00:00+09:00"},
        ])
        # run_baseline_configs/ 디렉터리 자체가 없어도(이 실행 시점엔
        # 아직 config snapshot 기능이 없었던 경우 등) 실패로 취급하지 않음.
        zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("config snapshot(redacted) 포함: 0/1건", manifest)
        self.assertIsNotNone(zip_path)


class TestRunBaselineUnresolvedMarker(_BundleTestBase):

    def test_signal_before_earliest_included_run_is_marked_unresolved(self):
        self._write_run_baseline_csv([
            {"run_id": "run-today", "started_at": "2026-09-11T09:10:00+09:00"},
        ])
        # 09:00 신호가 09:10에 시작한 실행보다 앞섬 — 조인 근거 없음.
        self._write_signal_log_csv(["2026-09-11T09:00:00", "2026-09-11T09:30:00"])

        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("UNRESOLVED", manifest)
        self.assertIn("signal_log의 첫 기록", manifest)

    def test_signal_after_run_start_has_no_unresolved_marker(self):
        self._write_run_baseline_csv([
            {"run_id": "run-today", "started_at": "2026-09-11T09:00:00+09:00"},
        ])
        self._write_signal_log_csv(["2026-09-11T09:30:00"])

        _zip_path, manifest = self._build_and_read_manifest()
        self.assertNotIn("UNRESOLVED", manifest)

    # ── 2026-09-11 (B01 v2 재검토, 민우님/GPT 지적): "연결 가능한 실행이
    # 전혀 없는 경우"의 회귀 테스트. 예전에는 이 두 케이스 모두
    # "OK / 해당 실행 없음"으로만 표시되고 UNRESOLVED가 전혀 뜨지
    # 않아서, 실제로 거래/신호가 있는데도 "정상, 그냥 실행이 없었다"
    # 로 오해할 수 있었습니다.

    def test_empty_run_baseline_csv_with_existing_signal_rows_is_marked_unresolved(self):
        # 헤더만 있고 행이 하나도 없는 run_baseline.csv — "기준선 CSV가
        # 비어 있는" 경우를 재현.
        self._write_run_baseline_csv([])
        self._write_signal_log_csv(["2026-09-11T09:00:00"])

        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("이 날짜에 해당하는 실행 없음", manifest)
        self.assertIn("UNRESOLVED", manifest)
        self.assertIn("signal_log에 이 날짜 행이", manifest)

    def test_all_started_at_unparseable_with_existing_trade_rows_is_marked_unresolved(self):
        self._write_run_baseline_csv([
            {"run_id": "run-broken", "started_at": "garbage-not-a-timestamp"},
        ])
        path = self.logs_dir / "trades.csv"
        with path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=["timestamp", "symbol"])
            writer.writeheader()
            writer.writerow({"timestamp": "2026-09-11T09:00:00", "symbol": "005930"})

        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("UNRESOLVED", manifest)
        self.assertIn("trades에 이 날짜 행이", manifest)
        self.assertIn("파싱 실패 1건", manifest)

    def test_no_run_baseline_rows_and_no_trade_signal_rows_has_no_unresolved_marker(self):
        # 실행도 없고 그날 거래/신호도 없으면(예: 장이 열리지 않은 날)
        # UNRESOLVED로 표시할 근거 자체가 없음 — 조용히 "실행 없음"만.
        self._write_run_baseline_csv([])
        _zip_path, manifest = self._build_and_read_manifest()
        self.assertIn("이 날짜에 해당하는 실행 없음", manifest)
        self.assertNotIn("UNRESOLVED", manifest)


if __name__ == "__main__":
    unittest.main()
