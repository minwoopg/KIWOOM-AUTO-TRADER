# -*- coding: utf-8 -*-
"""2026-10-14 (보고서 안정화 1차): slice_log()가 여러 물리 줄에 걸친
레코드(예외 스택트레이스가 포함된 로그)를 더 이상 잘라내지 않는지
검증합니다.

배경: infra/storage/logger.py의 포매터("%(asctime)s | %(levelname)s |
%(message)s")는 message 자체에 개행이 있으면 그 전체를 한 번에
emit()합니다. trading_service.py의
`self.app_logger.warning(f"...실패:\n{result.stderr}")`가 대표적 예 —
결과물은 맨 첫 물리 줄에만 날짜 접두사가 붙고, 뒤따르는 실제 예외
내용(원인 전체)은 접두사가 없습니다. 예전 slice_log()는 "날짜로
시작하는 줄만 채택"했기 때문에 이 뒤따르는 줄들을 전부 버렸습니다 —
2026-09-18 bb_block 분석 때 "번들만으론 원인을 알 수 없다"고 기록해둔
바로 그 공백입니다.

매매 판단 로직은 전혀 건드리지 않습니다 — 순수 번들 로그 추출 로직
검증입니다.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

import export_daily_bundle as edb

TARGET = date(2026, 10, 13)


class SliceLogMultilineTests(unittest.TestCase):
    def _slice(self, raw_text: str):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "app.log"
            src.write_text(raw_text, encoding="utf-8")
            dst = Path(d) / "out.log"
            return edb.slice_log([src], dst, TARGET)

    def test_exception_traceback_after_tagged_line_is_preserved(self):
        # 실제 trading_service.py의 bb_block 실패 호출 형태를 그대로 재현:
        # self.app_logger.warning(f"[ANALYSIS] 볼린저 차단 영향 분석 실패:\n{result.stderr}")
        raw = (
            "2026-10-13 15:25:24,123 | WARNING | [ANALYSIS] 볼린저 차단 영향 분석 실패:\n"
            'Traceback (most recent call last):\n'
            '  File "analyze_bb_block_impact.py", line 42, in <module>\n'
            "    raise KeyError(\"missing column\")\n"
            "KeyError: 'missing column'\n"
            "2026-10-13 15:25:33,001 | INFO | [ANALYSIS] shadow 관측 분석 완료\n"
        )
        total, kept, records, used = self._slice(raw)
        # 뒤에 이어지는 "shadow 관측 분석 완료"도 그 자체로 allowlist
        # 태그가 있는 별개의 정상 레코드라 독립적으로 채택됨(2건) —
        # 핵심은 이 두 레코드가 서로 섞이지 않는 것.
        self.assertEqual(len(records), 2)
        self.assertIn("볼린저 차단 영향 분석 실패", records[0])
        self.assertIn("Traceback (most recent call last):", records[0])
        self.assertIn("KeyError: 'missing column'", records[0])
        self.assertNotIn("shadow 관측 분석 완료", records[0])
        self.assertIn("shadow 관측 분석 완료", records[1])
        self.assertNotIn("Traceback", records[1])
        # kept는 물리 줄 수 기준(기존 manifest "N줄 / 전체 M줄" 표기와
        # 호환) — 실패 레코드 5개 물리 줄 + shadow 레코드 1줄 = 6.
        self.assertEqual(kept, 6)

    def test_continuation_of_an_unselected_record_is_not_captured(self):
        # 태그가 없는(=채택되지 않는) 레코드의 연속줄은 뒤섞여 들어오면
        # 안 됨 — buffer가 비어 있을 때 연속줄은 버려져야 함.
        raw = (
            "2026-10-13 09:00:01,000 | ERROR | 관계없는 예외 발생\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: 이건 allowlist 태그가 없어서 애초에 채택 안 됨\n"
            "2026-10-13 09:00:02,000 | WARNING | [ANALYSIS] 다른 실패:\n"
            "실제 원인 줄\n"
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(len(records), 1)
        self.assertIn("다른 실패", records[0])
        self.assertIn("실제 원인 줄", records[0])
        self.assertNotIn("관계없는 예외", "".join(records))
        self.assertNotIn("RuntimeError", "".join(records))

    def test_single_line_records_unaffected(self):
        raw = (
            "2026-10-13 09:00:01,000 | INFO | [REPORT] 저장 완료\n"
            "2026-10-13 09:00:02,000 | INFO | [COND] 조건검색 갱신\n"
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(kept, 2)
        self.assertEqual(total, 2)

    def test_other_day_records_excluded_even_with_trailing_continuation(self):
        raw = (
            "2026-10-12 15:25:24,123 | WARNING | [ANALYSIS] 전날 실패:\n"
            "전날 스택트레이스\n"
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(records, [])
        self.assertEqual(kept, 0)

    def test_masking_applies_across_the_whole_joined_record(self):
        raw = (
            '2026-10-13 10:00:00,000 | WARNING | [ANALYSIS] 실패:\n'
            '  appkey="SECRET_EXAMPLE_12345"\n'
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(len(records), 1)
        self.assertNotIn("SECRET_EXAMPLE_12345", records[0])

    def test_record_sort_key_still_uses_leading_timestamp_of_joined_block(self):
        raw = (
            "2026-10-13 12:00:00,000 | WARNING | [ANALYSIS] 두 번째:\n"
            "둘째 원인\n"
            "2026-10-13 09:00:00,000 | WARNING | [ANALYSIS] 첫 번째:\n"
            "첫째 원인\n"
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(len(records), 2)
        self.assertIn("첫 번째", records[0])
        self.assertIn("두 번째", records[1])

    def test_leading_continuation_line_with_no_prior_record_is_dropped_not_crashed(self):
        raw = (
            "줄바꿈 중간에 잘린 것처럼 보이는 선두 줄(레코드 시작 아님)\n"
            "2026-10-13 09:00:00,000 | WARNING | [ANALYSIS] 정상 레코드:\n"
            "정상 원인\n"
        )
        total, kept, records, used = self._slice(raw)
        self.assertEqual(len(records), 1)
        self.assertIn("정상 레코드", records[0])
        self.assertNotIn("잘린 것처럼", "".join(records))


if __name__ == "__main__":
    unittest.main()
