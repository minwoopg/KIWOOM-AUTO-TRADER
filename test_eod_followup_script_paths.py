# -*- coding: utf-8 -*-
"""2026-10-15 (민우님 10/15 보고서 — "볼린저 분석 실패" 반복, 재현/근본원인 확인):

TradingService._run_eod_followups()의 각 단계는 subprocess.run([sys.executable,
"<스크립트>.py", ...])로 저장소 루트의 분석 스크립트를 실행한다. 실제 원인은
코드 로직이 아니라 **파일명 자체**였다 — `analyze_bb_block_impact.py`를
호출하지만 저장소에는 확장자 없는 `analyze_bb_block_impact`만 있었다
(최초 커밋부터 계속, `.py` 버전은 git 역사에 존재한 적이 없음). 매번
"python3: can't open file '.../analyze_bb_block_impact.py': No such file or
directory"로 즉시 실패했고, 이 1줄짜리 interpreter 메시지가
`self.app_logger.warning(f"...실패:\n{result.stderr}")`의 개행 뒤에 와서
구 slice_log()의 "날짜로 시작하는 줄만 채택" 방식에 걸려 번들에서 통째로
잘려나갔다(2026-10-14 보고서 안정화 1차, 0091 패치로 그 관측성 공백은 이미
고쳤음 — 이번엔 가려져 있던 실제 원인을 고친다).

이 테스트는 같은 종류의 버그(호출 파일명과 실제 파일명이 어긋남)가 7개
EOD 후속 단계 스크립트 중 어디서든 재발하면 즉시 실패하도록, 코드와
파일시스템을 직접 대조한다. 매매 판단 로직은 전혀 건드리지 않음 — 순수
배포 상태(파일 존재) 검증.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, ".")

REPO_ROOT = Path(__file__).resolve().parent
TRADING_SERVICE = REPO_ROOT / "domain" / "service" / "trading_service.py"

# trading_service.py 소스에서 "subprocess.run([sys.executable, "<name>.py", ...])"
# 형태로 호출하는 EOD 후속 단계 스크립트 이름을 그대로 추출한다 — 하드코딩한
# 목록이 아니라 실제 소스를 스캔하므로, 앞으로 단계가 추가/변경돼도 이
# 테스트가 자동으로 그 스크립트까지 검증한다.
_SCRIPT_CALL_RE = re.compile(r'sys\.executable,\s*"([A-Za-z0-9_]+\.py)"')


class EodFollowupScriptPathsTests(unittest.TestCase):
    def test_trading_service_source_references_at_least_the_known_scripts(self):
        # 스캔 자체가 깨지지 않았는지(정규식이 더 이상 아무것도 못 찾는
        # 상태로 조용히 통과해버리는 것을 방지) — 알려진 7개 단계가
        # 여전히 전부 발견되는지 먼저 확인한다.
        source = TRADING_SERVICE.read_text(encoding="utf-8")
        found = set(_SCRIPT_CALL_RE.findall(source))
        expected = {
            "export_daily_bundle.py", "analyze_shadow.py", "analyze_signal_log.py",
            "analyze_trades.py", "analyze_indicators.py", "analyze_bb_block_impact.py",
            "replay_runner.py",
        }
        self.assertTrue(
            expected.issubset(found),
            f"스캔으로 찾은 스크립트 집합이 예상보다 적습니다 — 정규식이 깨졌을 "
            f"가능성: 찾음={sorted(found)}",
        )

    def test_every_subprocess_invoked_eod_script_exists_on_disk(self):
        # 핵심 검증: trading_service.py가 subprocess로 실행을 시도하는
        # 모든 "<name>.py" 스크립트가 실제로 저장소 루트에 그 이름
        # 그대로(확장자 포함) 존재해야 한다. analyze_bb_block_impact.py가
        # 바로 이 검증에서 걸렸던 사례 — 파일은 있었지만 이름이
        # analyze_bb_block_impact(확장자 없음)였다.
        source = TRADING_SERVICE.read_text(encoding="utf-8")
        scripts = sorted(set(_SCRIPT_CALL_RE.findall(source)))
        self.assertTrue(scripts, "EOD 후속 단계 스크립트를 하나도 찾지 못함 — 스캔 실패")
        missing = [s for s in scripts if not (REPO_ROOT / s).is_file()]
        self.assertEqual(
            missing, [],
            f"trading_service.py가 호출하는 스크립트가 저장소에 그 이름으로 없음: "
            f"{missing} — 확장자/이름 불일치로 매일 조용히 실패합니다 "
            f"(stderr가 1줄짜리 interpreter 오류라 구 slice_log()에선 더 가려짐).",
        )


if __name__ == "__main__":
    unittest.main()
