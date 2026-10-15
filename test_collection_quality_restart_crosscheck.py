# -*- coding: utf-8 -*-
"""2026-10-14 (보고식 민우님 지적, 재현 확인): build_collection_quality()의
재시작 판정(process_start_count)이 "[COND] watcher.start() 진입" 로그
줄만 세어서, **전날(혹은 그 이전)에 시작해 자정을 넘겨 이어지다가
당일 장중에 교체된 실행**을 놓치는지 검증합니다.

10/14 실제 사례: run_baseline.csv에 전날(10/13) 15:01:54 시작 실행
1건 + 10/14 10:18:57 시작 실행 1건이 둘 다 이 거래일 세션에 걸쳐
있었고(체결조회 관측 기록기의 clean_shutdown 마커도 10:18:44에 남아
있어 장중 재시작이 명백함), signal_log 범위도 09:00~15:15를 모두
만족해서 watcher.start() 로그 기준(starts==1)으로는 COMPLETE로
오판정됐습니다.

이 테스트는 export_daily_bundle.py의 실제 build()가 하는 것과 같은
run_baseline 조인 결과(same_day_run_count, carried_over_run_present)를
직접 넘겨, build_collection_quality()가 이를 교차 확인해 재시작으로
잡아내는지 확인합니다. 매매 판단 로직은 전혀 건드리지 않습니다 —
순수 번들 메타데이터 판정 로직 검증입니다.
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import date

sys.path.insert(0, ".")

import export_daily_bundle as edb

TARGET = date(2026, 10, 14)

# 09:00~15:15 범위를 만족하는 signal_log — watcher.start() 로그만
# 보면 "정상적으로 한 번 기동해서 하루 종일 수집했다"로 보이는 모양.
SIGNAL_ROWS_FULL_COVERAGE = [
    {"timestamp": "2026-10-14T09:00:05.000000"},
    {"timestamp": "2026-10-14T15:19:59.000000"},
]

# 10/14 당일 watcher.start() 로그는 정확히 1회만 보임(교체 시점에
# 새로 시작한 실행의 기동 로그) — 이월 실행 자체의 기동 로그는
# 전날 날짜로 찍혀 있어 이 날짜 슬라이스에는 없음.
LOG_LINES_ONE_START = [
    "2026-10-14 10:18:58,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
]


def _value(quality: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", quality, re.M)
    assert m, f"{key} 필드를 찾을 수 없음:\n{quality}"
    return m.group(1).strip()


class CollectionQualityRestartCrossCheckTests(unittest.TestCase):
    def test_without_run_baseline_data_behaves_as_before(self):
        # same_day_run_count를 넘기지 않으면(None, 기본값) 기존
        # watcher.start() 로그 기준 판정만 쓴다 — 하위 호환.
        quality = edb.build_collection_quality(
            TARGET, LOG_LINES_ONE_START, {}, [], [], SIGNAL_ROWS_FULL_COVERAGE,
        )
        self.assertEqual(_value(quality, "process_start_count"), "1")
        self.assertEqual(_value(quality, "collection_status"), "COMPLETE")
        self.assertEqual(_value(quality, "run_baseline_distinct_run_count"),
                          "N/A(run_baseline 미연동)")

    def test_carryover_run_plus_same_day_run_is_detected_as_restart(self):
        # 10/14 실제 상황 재현: 당일 시작 실행 1건(70d0ec36) +
        # 전날 이월 실행 1건(caecc1d0) — watcher.start() 로그로는
        # starts==1이라 COMPLETE로 보이지만, run_baseline 조인
        # 결과를 교차 확인하면 실제로는 서로 다른 두 실행이 이 날을
        # 나눠 수집한 것이므로 RESTARTED_PARTIAL이어야 한다.
        quality = edb.build_collection_quality(
            TARGET, LOG_LINES_ONE_START, {}, [], [], SIGNAL_ROWS_FULL_COVERAGE,
            same_day_run_count=1,
            carried_over_run_present=True,
        )
        self.assertEqual(_value(quality, "watcher_start_log_count"), "1")
        self.assertEqual(_value(quality, "run_baseline_distinct_run_count"), "2")
        self.assertEqual(_value(quality, "process_start_count"), "2")
        self.assertEqual(_value(quality, "collection_status"), "RESTARTED_PARTIAL")
        self.assertEqual(_value(quality, "full_day_collection"), "False")
        self.assertIn("⚠ watcher.start() 로그는", quality)
        self.assertIn("run_baseline.csv 교차 확인", quality)

    def test_carryover_run_alone_with_no_same_day_start_is_not_flagged(self):
        # 이월 실행이 그대로 하루 종일 돌았을 뿐 교체가 없었던 날
        # (same_day_run_count=0, carried_over_run_present=True)은
        # run_baseline_touch_count=1이라 starts(0 또는 1)보다 크지
        # 않으므로 새 교차 확인 때문에 멀쩡한 날이 재시작으로
        # 잘못 플래그되면 안 된다.
        quality = edb.build_collection_quality(
            TARGET, LOG_LINES_ONE_START, {}, [], [], SIGNAL_ROWS_FULL_COVERAGE,
            same_day_run_count=0,
            carried_over_run_present=True,
        )
        self.assertEqual(_value(quality, "run_baseline_distinct_run_count"), "1")
        self.assertEqual(_value(quality, "process_start_count"), "1")
        self.assertEqual(_value(quality, "collection_status"), "COMPLETE")

    def test_two_same_day_starts_still_detected_without_run_baseline(self):
        # 기존 동작 보존 — watcher.start() 로그만으로 2회 이상 보이면
        # run_baseline 데이터가 없어도 여전히 RESTARTED_PARTIAL.
        two_starts = LOG_LINES_ONE_START + [
            "2026-10-14 13:00:00,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
        ]
        quality = edb.build_collection_quality(
            TARGET, two_starts, {}, [], [], SIGNAL_ROWS_FULL_COVERAGE,
        )
        self.assertEqual(_value(quality, "process_start_count"), "2")
        self.assertEqual(_value(quality, "collection_status"), "RESTARTED_PARTIAL")

    def test_run_baseline_count_lower_than_log_count_defers_to_log_count(self):
        # run_baseline 조인 결과가 (이례적으로) 로그 기준보다 작게
        # 나오면 — 더 보수적인(큰) 쪽을 그대로 유지해야 한다. 즉
        # run_baseline이 낮다고 해서 로그 기반 재시작 판정이 깎이면
        # 안 됨.
        two_starts = LOG_LINES_ONE_START + [
            "2026-10-14 13:00:00,000 | INFO | [COND] watcher.start() 진입 — WebSocket 연결 시작",
        ]
        quality = edb.build_collection_quality(
            TARGET, two_starts, {}, [], [], SIGNAL_ROWS_FULL_COVERAGE,
            same_day_run_count=1,
            carried_over_run_present=False,
        )
        self.assertEqual(_value(quality, "watcher_start_log_count"), "2")
        self.assertEqual(_value(quality, "run_baseline_distinct_run_count"), "1")
        self.assertEqual(_value(quality, "process_start_count"), "2")
        self.assertEqual(_value(quality, "collection_status"), "RESTARTED_PARTIAL")
        # footer의 고정 설명 문구와는 별개로, "이번 호출에서 교차
        # 확인으로 재시작을 추가 발견했다"는 개별 경고 줄은 없어야 함
        # (run_baseline 쪽이 로그 기준보다 작아 아무것도 추가하지 않음).
        self.assertNotIn("⚠ watcher.start() 로그는", quality)


if __name__ == "__main__":
    unittest.main()
