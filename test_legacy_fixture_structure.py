# -*- coding: utf-8 -*-
"""
Legacy 기준선 fixture 구조 검증 테스트 (2026-07-27, 리팩터링 1A단계)

⚠️ 이름에서 알 수 있듯 이 테스트는 "구조(structure)"만 검증합니다.
실제 전략 판단을 재현해서 expected_decisions.json과 비교하지
않습니다(GPT 코드리뷰 지적으로 파일명을 legacy_baseline_fixture에서
legacy_fixture_structure로 변경 — "기준선(baseline)"이라는 이름이
"의사결정 재현까지 검증한다"는 오해를 줄 수 있었음).

tests/fixtures/legacy_20260721/README.md의 단계 구분표 참고:
  1A-1 과거 로그·분봉 아카이브        완료
  1A-2 fixture 구조·해시 무결성 검증  완료 (이 테스트)
  1A-3 MinuteAnalyzer 재현            미완료
  1A-4 StrategyRouter 재현            일봉 원본 부족
  1A-5 RiskManager 최종판단 재현      시점별 상태 부족

이 테스트가 확인하는 것: 파일 존재, SHA-256 해시 일치(내용이 조용히
바뀌지 않았는지), cutoff 메타데이터의 정합성(미래 데이터 누출 방지
장치가 실제로 정확한지), 기본 다양성. 확인하지 않는 것: 실제
MinuteAnalyzer/StrategyRouter/RiskManager 재실행 결과 비교.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, ".")

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "legacy_20260721"

# ── 1) fixture 디렉토리와 필수 파일 존재 확인 ──────────────────────
check("1) fixture 디렉토리가 존재함", FIXTURE_DIR.is_dir())
check("   README.md 존재", (FIXTURE_DIR / "README.md").is_file())
check("   manifest.sha256 존재", (FIXTURE_DIR / "manifest.sha256").is_file())
check("   settings.yaml 존재", (FIXTURE_DIR / "settings.yaml").is_file())
check("   end_of_capture_runtime_state.json 존재",
      (FIXTURE_DIR / "end_of_capture_runtime_state.json").is_file())
check("   expected_decisions.json 존재", (FIXTURE_DIR / "expected_decisions.json").is_file())
check("   minute_bars/ 디렉토리 존재", (FIXTURE_DIR / "minute_bars").is_dir())

# GPT 지적: runtime_state.json(구 이름)이 더 이상 존재하지 않아야 함
# — 판단 시점 재현에 쓸 수 없는 파일임을 이름으로도 명확히 했으므로,
# 혼동을 막기 위해 구 파일명이 남아있으면 안 됨.
check("   구 이름(runtime_state.json)은 더 이상 존재하지 않음(이름 변경 확인)",
      not (FIXTURE_DIR / "runtime_state.json").exists())

# ══════════════════════════════════════════════════════════════
# 2부: SHA-256 manifest 실제 검증 (GPT 지적 — 내용 변경 감지)
# ══════════════════════════════════════════════════════════════

manifest_path = FIXTURE_DIR / "manifest.sha256"
manifest_lines = manifest_path.read_text(encoding="utf-8").strip().splitlines()
manifest_entries = {}
for line in manifest_lines:
    digest, rel_path = line.split("  ", 1)
    manifest_entries[rel_path] = digest

check("2) manifest.sha256이 비어있지 않음", len(manifest_entries) > 0)

all_hashes_match = True
for rel_path, expected_digest in manifest_entries.items():
    target = FIXTURE_DIR / rel_path
    if not target.is_file():
        print(f"   manifest에 있는데 파일이 없음: {rel_path}")
        all_hashes_match = False
        continue
    actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        print(f"   해시 불일치: {rel_path} (manifest={expected_digest[:12]}..., "
              f"actual={actual_digest[:12]}...)")
        all_hashes_match = False
check("   manifest에 기록된 모든 파일의 SHA-256이 실제 파일과 일치함"
      "(내용이 조용히 변경되지 않았는지 검증)", all_hashes_match)

# manifest가 실제 존재하는 재현 입력 파일을 빠짐없이 커버하는지
expected_files = {
    "expected_decisions.json", "settings.yaml", "end_of_capture_runtime_state.json",
}
expected_files |= {
    f"minute_bars/{p.name}" for p in (FIXTURE_DIR / "minute_bars").glob("*.csv")
}
manifest_files = set(manifest_entries.keys())
check("   manifest이 모든 재현 입력 파일(README/manifest 자신 제외)을 빠짐없이 커버함",
      expected_files == manifest_files)

# manifest 자신은 검증 대상 목록에 없어야 함(자기 자신을 해시하지 않음)
check("   manifest.sha256 자신은 목록에 포함되지 않음",
      "manifest.sha256" not in manifest_entries)

# ══════════════════════════════════════════════════════════════
# 3부: expected_decisions.json 구조 검증
# ══════════════════════════════════════════════════════════════

with open(FIXTURE_DIR / "expected_decisions.json", encoding="utf-8") as f:
    decisions = json.load(f)

check("3) expected_decisions.json에 정확히 10건 존재", len(decisions) == 10)

REQUIRED_FIELDS = {
    "case_label", "timestamp", "symbol", "regime", "score",
    "final_decision", "order_block_reason", "skip_reason",
    "decision_timestamp", "bar_cutoff_timestamp",
    "expected_bars_at_or_before_cutoff", "risk_context_available",
    "legacy_requested_bar_count", "expected_legacy_input_count",
    "expected_legacy_first_timestamp", "expected_legacy_last_timestamp",
    "legacy_input_contains_prior_date",
}
all_have_required = all(REQUIRED_FIELDS.issubset(d.keys()) for d in decisions)
check("   모든 케이스가 필수 필드(cutoff·legacy 60봉 메타데이터 포함)를 가짐",
      all_have_required)

# GPT 지적: risk_context_available이 임의로 True가 아니라 정확히
# False로 명시되어 있는지(과거 시점별 상태를 복원할 자료가 없다는
# 사실을 숨기지 않고 있는지)
all_risk_context_false = all(d.get("risk_context_available") is False for d in decisions)
check("   모든 케이스의 risk_context_available이 정확히 False"
      "(케이스별 RiskManager 상태 복원 자료가 없음을 숨기지 않음)",
      all_risk_context_false)

# ── 다양성 확인 (기존 검증 유지) ───────────────────────────────────
labels = [d["case_label"] for d in decisions]
check("4) 10개 케이스의 case_label이 모두 고유함", len(set(labels)) == 10)

decisions_by_type = {d["case_label"]: d["final_decision"] for d in decisions}
check("   BUY 케이스가 1건 이상 포함됨",
      any(v == "BUY" for v in decisions_by_type.values()))
check("   BLOCKED 케이스가 2건 이상 포함됨",
      sum(1 for v in decisions_by_type.values() if v == "BLOCKED") >= 2)
check("   HOLD(SKIP) 케이스가 다수 포함됨",
      sum(1 for v in decisions_by_type.values() if v == "HOLD") >= 5)

symbols_covered = {d["symbol"] for d in decisions}
check("   5개 이상 서로 다른 종목이 포함됨", len(symbols_covered) >= 5)

# ══════════════════════════════════════════════════════════════
# 4부: 미래 데이터 누출 방지 메타데이터 검증 (GPT 핵심 지적)
# ══════════════════════════════════════════════════════════════


def count_bars_at_or_before(symbol: str, cutoff: str) -> int:
    path = FIXTURE_DIR / "minute_bars" / f"{symbol}.csv"
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return sum(1 for r in rows if r["cntr_tm"] <= cutoff)


def count_bars_total(symbol: str) -> int:
    path = FIXTURE_DIR / "minute_bars" / f"{symbol}.csv"
    with open(path, encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


all_cutoff_counts_correct = True
any_case_has_future_bars = False
for d in decisions:
    actual_count = count_bars_at_or_before(d["symbol"], d["bar_cutoff_timestamp"])
    expected_count = d["expected_bars_at_or_before_cutoff"]
    if actual_count != expected_count:
        print(f"   {d['case_label']}: cutoff까지 실제 봉 수({actual_count}) != "
              f"expected({expected_count})")
        all_cutoff_counts_correct = False

    total = count_bars_total(d["symbol"])
    if total > actual_count:
        any_case_has_future_bars = True

check("5) 모든 케이스의 expected_bars_at_or_before_cutoff가 실제 분봉 CSV와 정확히 일치함",
      all_cutoff_counts_correct)
check("   최소 하나의 케이스에서 cutoff 이후(미래) 봉이 실제로 존재함을 확인"
      "(분봉 CSV에 미래 데이터가 섞여 있다는 사실 자체를 명시적으로 검증)",
      any_case_has_future_bars)

# ── 각 케이스 종목에 대응하는 분봉 fixture가 실제로 존재하는지 ─────
minute_bars_dir = FIXTURE_DIR / "minute_bars"
missing_bars = [
    d["symbol"] for d in decisions
    if not (minute_bars_dir / f"{d['symbol']}.csv").is_file()
]
check("6) expected_decisions.json의 모든 종목에 대응하는 분봉 CSV가 존재함",
      len(missing_bars) == 0)

# ── 분봉 CSV가 파싱 가능하고 비어있지 않은지 ───────────────────────
all_parseable = True
for csv_file in minute_bars_dir.glob("*.csv"):
    try:
        with open(csv_file, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            all_parseable = False
        required_cols = {"cntr_tm", "open", "high", "low", "close", "volume"}
        if not required_cols.issubset(rows[0].keys() if rows else set()):
            all_parseable = False
    except Exception as exc:
        all_parseable = False
        print(f"   {csv_file.name}: 파싱 실패 - {exc}")
check("7) 모든 분봉 CSV가 정상 파싱되고 필수 컬럼을 가짐", all_parseable)

# ══════════════════════════════════════════════════════════════
# 4-2부: legacy 60봉 입력창 검증 (GPT 2차 지적)
#
# 배경: bar_cutoff_timestamp 이전 "전체" 봉 수만 고정하는 것으로는
# 부족함 — 실거래 코드는 minute_bar_count=60(config/settings.yaml)
# 설정으로 raw_bars[:count] 후 reverse()하므로, 실제 전략 입력은
# "cutoff 이전 봉 중 최신 60개"임. 이 둘을 구분하지 않으면 향후
# 재현 로직이 cutoff 이전 전체 봉을 그대로 넣어 기존 시스템과 다른
# (더 많은) 입력을 사용하게 됨 — 특히 장 초반 케이스는 cutoff
# 이전 전체가 60개 미만이라 전날 오후 봉까지 끌어와야 60개를 채움.
# ══════════════════════════════════════════════════════════════


def compute_legacy_input(symbol: str, cutoff: str, count: int = 60) -> list[dict]:
    bars = None
    path = minute_bars_dir / f"{symbol}.csv"
    with open(path, encoding="utf-8", newline="") as f:
        bars = list(csv.DictReader(f))
    eligible = [b for b in bars if b["cntr_tm"] <= cutoff]
    return eligible[-count:]


all_legacy_window_correct = True
for d in decisions:
    legacy_input = compute_legacy_input(
        d["symbol"], d["bar_cutoff_timestamp"], d.get("legacy_requested_bar_count", 60)
    )
    actual_count = len(legacy_input)
    actual_first = legacy_input[0]["cntr_tm"] if legacy_input else None
    actual_last = legacy_input[-1]["cntr_tm"] if legacy_input else None
    actual_contains_prior = any(
        b["cntr_tm"][:8] != d["bar_cutoff_timestamp"][:8] for b in legacy_input
    )

    mismatches = []
    if actual_count != d.get("expected_legacy_input_count"):
        mismatches.append(f"count {actual_count}!={d.get('expected_legacy_input_count')}")
    if actual_first != d.get("expected_legacy_first_timestamp"):
        mismatches.append(f"first {actual_first}!={d.get('expected_legacy_first_timestamp')}")
    if actual_last != d.get("expected_legacy_last_timestamp"):
        mismatches.append(f"last {actual_last}!={d.get('expected_legacy_last_timestamp')}")
    if actual_contains_prior != d.get("legacy_input_contains_prior_date"):
        mismatches.append(
            f"contains_prior {actual_contains_prior}!={d.get('legacy_input_contains_prior_date')}"
        )

    if mismatches:
        all_legacy_window_correct = False
        print(f"   {d['case_label']}: {', '.join(mismatches)}")

check("7-1) 모든 케이스의 legacy 60봉 입력창(개수/첫/끝 timestamp)이 "
      "실제 분봉 CSV로 재계산한 값과 정확히 일치함", all_legacy_window_correct)

# 최소 하나의 케이스는 실제로 전일 봉을 포함해야 함(문제가 실재한다는 증거)
any_contains_prior = any(d.get("legacy_input_contains_prior_date") for d in decisions)
check("   최소 하나의 케이스에서 legacy 60봉 입력에 전일 봉이 실제로 포함됨"
      "(장 초반 세션지표 오염이 fixture로 실증됨)", any_contains_prior)

# 2026-07-27 (GPT 2차 코드리뷰): 최초 보고에서 "6개 케이스가 전일
# 봉을 포함한다"고 잘못 기록했었음(직접 재집계한 실제 값은 5개) —
# 문서뿐 아니라 테스트로도 정확한 개수를 고정해, 향후 fixture가
# 바뀌어도 이 문서 오류가 재발하지 않도록 함.
prior_date_cases = [d["case_label"] for d in decisions if d["legacy_input_contains_prior_date"]]
check("7-2) 전일 봉을 포함하는 케이스가 정확히 5건 "
      "(최초 보고 시 6건으로 잘못 기록했던 것을 재집계로 정정)",
      len(prior_date_cases) == 5)

# ══════════════════════════════════════════════════════════════
# 4-3부: compute_legacy_input()이 의존하는 전제 검증 (GPT 2차 지적)
#
# eligible[-count:] 계산은 CSV가 timestamp 오름차순이고 중복이
# 없다는 전제에 의존함 — 이 전제 자체를 검증하지 않으면, 파일
# 순서가 바뀌었을 때 조용히 잘못된 입력창을 계산하게 됨.
# ══════════════════════════════════════════════════════════════

all_format_valid = True
all_ascending = True
all_no_duplicates = True
for csv_file in sorted(minute_bars_dir.glob("*.csv")):
    with open(csv_file, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    timestamps = [r["cntr_tm"] for r in rows]

    if not all(len(ts) == 14 and ts.isdigit() for ts in timestamps):
        all_format_valid = False
        print(f"   {csv_file.name}: cntr_tm 형식(14자리 숫자) 위반 발견")

    if not all(prev < curr for prev, curr in zip(timestamps, timestamps[1:])):
        all_ascending = False
        print(f"   {csv_file.name}: timestamp가 오름차순이 아님")

    if len(timestamps) != len(set(timestamps)):
        all_no_duplicates = False
        print(f"   {csv_file.name}: 중복 timestamp 발견")

check("8) 모든 분봉 CSV의 cntr_tm이 14자리 숫자(YYYYMMDDHHMMSS) 형식임",
      all_format_valid)
check("   모든 분봉 CSV가 timestamp 오름차순으로 정렬되어 있음"
      "(compute_legacy_input의 eligible[-60:] 전제 검증)", all_ascending)
check("   모든 분봉 CSV에 중복 timestamp가 없음", all_no_duplicates)

# ══════════════════════════════════════════════════════════════
# 4-4부: legacy_requested_bar_count와 settings.yaml 연결 검증 (GPT 2차 지적)
# ══════════════════════════════════════════════════════════════

# 2026-07-27 (GPT 3차 코드리뷰): 정규식(r"minute_bar_count:\s*(\d+)")
# 대신 프로젝트에 이미 있는 PyYAML의 yaml.safe_load()를 사용 —
# 정규식은 나중에 같은 이름이 주석이나 다른 섹션에 추가되면 잘못된
# 값을 잡을 위험이 있음. 환경변수 placeholder(${...})는 safe_load()
# 단계에서도 단순 문자열로 읽히므로 이 검증 목적에는 문제없음
# (실제로 broker.app_key='${KIWOOM_APP_KEY}'로 안전하게 읽히는 것 확인).
fixture_settings = yaml.safe_load((FIXTURE_DIR / "settings.yaml").read_text(encoding="utf-8"))
fixture_minute_bar_count = fixture_settings.get("market_regime", {}).get("minute_bar_count")
check("9) settings.yaml(market_regime.minute_bar_count)에서 값을 정상 추출함",
      fixture_minute_bar_count is not None)

# 2026-07-27 (GPT 4차 코드리뷰): None 여부만 확인하고 있었음 — 값이
# 있어도 int가 아니거나(예: 문자열 "60"), bool(Python에서 bool은
# int의 하위타입이라 isinstance(True, int)가 True임에 주의), 0
# 이하인 경우까지는 안 잡혔음. JSON의 정수 60과 비교하는 다음 검증
# 에서 우연히 걸릴 수는 있지만, 설정 자체의 유효성을 명시적으로
# 검증하는 게 더 안전함.
valid_minute_bar_count = (
    isinstance(fixture_minute_bar_count, int)
    and not isinstance(fixture_minute_bar_count, bool)
    and fixture_minute_bar_count > 0
)
check("   minute_bar_count가 유효한 값임(int, bool 아님, 0보다 큼)",
      valid_minute_bar_count)

if fixture_minute_bar_count is not None:
    all_match_settings = all(
        d["legacy_requested_bar_count"] == fixture_minute_bar_count for d in decisions
    )
    check(f"   모든 케이스의 legacy_requested_bar_count가 settings.yaml의 "
          f"minute_bar_count({fixture_minute_bar_count})와 일치함 "
          f"(설정이 바뀌었는데 expected가 안 바뀐 경우를 잡기 위함)",
          all_match_settings)

# ══════════════════════════════════════════════════════════════
# 4-5부: decision_timestamp와 bar_cutoff_timestamp 일치 검증 (GPT 2차 지적)
# ══════════════════════════════════════════════════════════════

all_cutoff_derived_correctly = True
for d in decisions:
    decision_dt = datetime.fromisoformat(d["decision_timestamp"])
    expected_cutoff = decision_dt.strftime("%Y%m%d%H%M%S")
    if d["bar_cutoff_timestamp"] != expected_cutoff:
        all_cutoff_derived_correctly = False
        print(f"   {d['case_label']}: decision_timestamp로부터 계산한 cutoff"
              f"({expected_cutoff}) != 저장된 bar_cutoff_timestamp"
              f"({d['bar_cutoff_timestamp']})")
check("10) 모든 케이스의 bar_cutoff_timestamp가 decision_timestamp를 "
      "YYYYMMDDHHMMSS로 변환한 값과 정확히 일치함(수동 수정 실수 방지)",
      all_cutoff_derived_correctly)

# ══════════════════════════════════════════════════════════════
# 5부: runtime_state / settings 민감정보 확인
# ══════════════════════════════════════════════════════════════

with open(FIXTURE_DIR / "end_of_capture_runtime_state.json", encoding="utf-8") as f:
    runtime_state = json.load(f)

SENSITIVE_KEY_PATTERNS = ("account", "token", "password", "secret", "app_key")
has_sensitive = any(
    any(pat in str(k).lower() for pat in SENSITIVE_KEY_PATTERNS)
    for k in runtime_state.keys()
)
check("11) end_of_capture_runtime_state.json이 유효한 JSON", isinstance(runtime_state, dict))
check("    최상위 키에 계좌/토큰류 민감정보로 의심되는 키가 없음", not has_sensitive)
check("    last_order_id_by_symbol(불필요한 운영 메타데이터)이 제거됨",
      "last_order_id_by_symbol" not in runtime_state)

settings_text = (FIXTURE_DIR / "settings.yaml").read_text(encoding="utf-8")
has_env_placeholder = "${" in settings_text
check("12) settings.yaml에 환경변수 참조(${...}) 형태가 있음(실제 값 아님)",
      has_env_placeholder)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
