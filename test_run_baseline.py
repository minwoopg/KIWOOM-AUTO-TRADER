# -*- coding: utf-8 -*-
"""2026-09-11 (B01, 개선 체크리스트 0단계): 실행 기준선 기록 테스트.

검증 대상: infra/storage/run_baseline.py
1. git_sha/git_dirty 캡처 — 실제 git 저장소에서 정상 동작, git이 없거나
   저장소가 아닌 경우 시작을 막지 않고 "unknown"/None으로 대체.
2. effective_config_hash — 민감정보(app_key/secret_key/account_number/
   카카오 토큰 등)가 원문으로 절대 포함되지 않음, 같은 설정이면 같은
   해시(결정적), 민감하지 않은 필드가 바뀌면 해시도 바뀜.
3. RunBaselineLogger — 새 파일에 헤더+행을 쓰고, 기존 파일에는 헤더
   중복 없이 이어붙임.
4. TRADE_FIELDS/SIGNAL_FIELDS가 이번 작업으로 변경되지 않았음을 보장
   (회귀 가드 — 기존 CSV 소비자와의 호환성 유지 확인).
5. resolve_run_id_for_timestamp() — timestamp 시간범위 조인 로직.

매매 판단 로직은 전혀 건드리지 않았습니다 — 순수 관측/메타데이터
기록 기능입니다.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, ".")

from infra.storage.run_baseline import (
    RUN_BASELINE_FIELDS,
    RunBaselineLogger,
    capture_git_info,
    capture_run_baseline,
    compute_effective_config_hash,
    load_run_baselines,
    parse_run_timestamp,
    perform_run_baseline_startup,
    redact_settings,
    resolve_run_id_for_timestamp,
    save_effective_config_snapshot,
)
from infra.storage.logger import TRADE_FIELDS, SIGNAL_FIELDS
from test_run_once_integration import build_minimal_settings


class TestGitInfoCapture(unittest.TestCase):

    def test_real_git_repo_returns_sha_and_dirty_flag(self):
        # 이 프로젝트 자체가 git 저장소이므로 실제 동작을 그대로 확인.
        sha, dirty = capture_git_info(".")
        self.assertNotEqual(sha, "unknown", "실제 git 저장소에서는 SHA를 가져와야 합니다.")
        self.assertEqual(len(sha), 40, "git rev-parse HEAD는 40자 SHA를 반환해야 합니다.")
        self.assertIn(dirty, (True, False))

    def test_non_git_directory_falls_back_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sha, dirty = capture_git_info(tmpdir)
            self.assertEqual(sha, "unknown")
            self.assertIsNone(dirty)

    def test_dirty_flag_reflects_uncommitted_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                 "commit", "--allow-empty", "-q", "-m", "init"],
                cwd=tmpdir, check=True,
            )
            sha_clean, dirty_clean = capture_git_info(tmpdir)
            self.assertEqual(dirty_clean, False)

            (Path(tmpdir) / "untracked_but_new.txt").write_text("x")
            # untracked 파일도 --porcelain에 나타나 dirty=True가 되어야 함
            # (아직 커밋되지 않은 변경 전체를 보수적으로 잡기 위함).
            sha_dirty, dirty_flag = capture_git_info(tmpdir)
            self.assertEqual(sha_dirty, sha_clean, "파일 추가만으로는 HEAD SHA가 바뀌지 않아야 합니다.")
            self.assertEqual(dirty_flag, True)


class TestConfigRedactionAndHash(unittest.TestCase):

    def test_sensitive_fields_never_appear_in_redacted_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            # 민감할 수 있는 값을 명확한 문자열로 채워 누출 여부를 검증.
            # Settings/하위 dataclass가 frozen이라 dataclasses.replace()로
            # 새 인스턴스를 만든다.
            settings = dataclasses.replace(
                settings,
                broker=dataclasses.replace(
                    settings.broker,
                    app_key="SECRET_APP_KEY_VALUE",
                    secret_key="SECRET_SECRET_KEY_VALUE",
                    account_number="1234567890",
                ),
                kakao=dataclasses.replace(
                    settings.kakao,
                    access_token="SECRET_KAKAO_ACCESS",
                    refresh_token="SECRET_KAKAO_REFRESH",
                    rest_api_key="SECRET_KAKAO_REST",
                    client_secret="SECRET_KAKAO_CLIENT",
                ),
            )

            redacted = redact_settings(settings)
            import json
            dump = json.dumps(redacted, default=str)

            for secret in [
                "SECRET_APP_KEY_VALUE", "SECRET_SECRET_KEY_VALUE", "1234567890",
                "SECRET_KAKAO_ACCESS", "SECRET_KAKAO_REFRESH",
                "SECRET_KAKAO_REST", "SECRET_KAKAO_CLIENT",
            ]:
                self.assertNotIn(secret, dump, f"민감정보 '{secret}'가 redacted 덤프에 그대로 남아있습니다.")

            self.assertEqual(redacted["broker"]["app_key"], "***REDACTED***")
            self.assertEqual(redacted["kakao"]["refresh_token"], "***REDACTED***")

    def test_sensitive_fields_never_appear_in_hash_input_either(self):
        # 해시 자체는 원문을 복원할 수 없지만, 계산 과정에서 원문
        # 문자열이 그대로 직렬화되지 않는지(=redact가 hash 경로에도
        # 적용되는지)까지 화이트박스로 확인.
        with tempfile.TemporaryDirectory() as tmpdir:
            base_settings = build_minimal_settings(tmpdir)
            settings_a = dataclasses.replace(
                base_settings,
                broker=dataclasses.replace(base_settings.broker, account_number="9999999999"),
            )
            h1 = compute_effective_config_hash(settings_a)

            settings_b = dataclasses.replace(
                base_settings,
                broker=dataclasses.replace(base_settings.broker, account_number="0000000000"),
            )
            h2 = compute_effective_config_hash(settings_b)

            self.assertEqual(
                h1, h2,
                "계좌번호처럼 redact되는 필드만 바뀌면 해시는 동일해야 합니다 "
                "(redact 후 해시 계산이라는 설계가 실제로 적용됐는지 확인).",
            )

    def test_hash_is_deterministic_for_same_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            h1 = compute_effective_config_hash(settings)
            h2 = compute_effective_config_hash(settings)
            self.assertEqual(h1, h2)

    def test_hash_changes_when_non_sensitive_setting_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_settings = build_minimal_settings(tmpdir)
            h1 = compute_effective_config_hash(base_settings)
            changed_settings = dataclasses.replace(
                base_settings,
                strategy=dataclasses.replace(base_settings.strategy, stop_loss_pct=2.5),
            )
            h2 = compute_effective_config_hash(changed_settings)
            self.assertNotEqual(h1, h2, "전략 파라미터가 바뀌면 해시도 바뀌어야 합니다(설정 식별 목적).")


class TestRunBaselineLogger(unittest.TestCase):

    def test_capture_and_log_creates_file_with_header_and_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            baseline = capture_run_baseline(settings, repo_root=".")
            log_file = f"{tmpdir}/run_baseline.csv"

            RunBaselineLogger(log_file).log(baseline)

            rows = load_run_baselines(log_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], baseline.run_id)
            self.assertEqual(rows[0]["git_sha"], baseline.git_sha)
            self.assertEqual(set(rows[0].keys()), set(RUN_BASELINE_FIELDS))

    def test_appending_second_run_does_not_duplicate_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            log_file = f"{tmpdir}/run_baseline.csv"
            logger = RunBaselineLogger(log_file)

            b1 = capture_run_baseline(settings, repo_root=".")
            b2 = capture_run_baseline(settings, repo_root=".")
            logger.log(b1)
            logger.log(b2)

            rows = load_run_baselines(log_file)
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["run_id"], rows[1]["run_id"], "매 실행마다 run_id는 달라야 합니다.")

            text = Path(log_file).read_text(encoding="utf-8")
            self.assertEqual(text.count("run_id"), 1, "헤더가 중복 기록되면 안 됩니다.")

    def test_missing_file_returns_empty_list(self):
        rows = load_run_baselines("/tmp/nonexistent_run_baseline_xyz.csv")
        self.assertEqual(rows, [])


class TestExistingCsvSchemaUnchanged(unittest.TestCase):
    """B01이 기존 trades.csv/signal_log.csv 스키마를 건드리지 않았는지 확인.

    민우님이 지적한 "기존 로그 소비자와의 CSV 형식 호환성" 우려에 대한
    직접적인 회귀 가드 — 이 필드 목록이 실수로라도 바뀌면 이 테스트가
    바로 실패합니다.
    """

    def test_trade_fields_has_no_run_id_column(self):
        self.assertNotIn(
            "run_id", TRADE_FIELDS,
            "trades.csv 스키마에 새 컬럼을 추가하지 않기로 한 설계 결정이 지켜지지 않았습니다.",
        )

    def test_signal_fields_has_no_run_id_column(self):
        self.assertNotIn(
            "run_id", SIGNAL_FIELDS,
            "signal_log.csv 스키마에 새 컬럼을 추가하지 않기로 한 설계 결정이 지켜지지 않았습니다.",
        )


class TestResolveRunIdForTimestamp(unittest.TestCase):

    def test_picks_latest_run_started_before_timestamp(self):
        baselines = [
            {"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"},
            {"run_id": "run-B", "started_at": "2026-09-11T13:00:00+09:00"},
        ]
        # 13:30 거래는 13:00에 시작한 run-B가 만든 것.
        result = resolve_run_id_for_timestamp(baselines, "2026-09-11T13:30:00+09:00")
        self.assertEqual(result, "run-B")

        # 10:00 거래는 09:00에 시작한 run-A가 만든 것(run-B는 아직 시작 전).
        result2 = resolve_run_id_for_timestamp(baselines, "2026-09-11T10:00:00+09:00")
        self.assertEqual(result2, "run-A")

    def test_timestamp_before_any_run_returns_none(self):
        baselines = [{"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"}]
        result = resolve_run_id_for_timestamp(baselines, "2026-09-11T08:00:00+09:00")
        self.assertIsNone(result, "이 관측 기능 도입 이전 거래를 엉뚱한 실행에 잘못 연결하면 안 됩니다.")

    def test_empty_baselines_returns_none(self):
        self.assertIsNone(resolve_run_id_for_timestamp([], "2026-09-11T10:00:00+09:00"))

    # ── 2026-09-11 (B01 보완): GPT가 재현한 두 가지 버그의 회귀 테스트 ──
    # 예전 구현(문자열 사전식 비교)은 아래 두 케이스에서 모두 틀렸습니다.

    def test_naive_kst_log_at_new_run_start_instant_is_not_misattributed_to_previous_run(self):
        """버그(a) 재현: 새 실행이 막 시작한 바로 그 순간의 naive 로그가
        이전 실행으로 잘못 귀속되던 문제.

        run-B가 "2026-09-11T13:00:00+09:00"(aware, RunBaseline.started_at
        형식)에 시작. 바로 그 순간 남은 거래 로그는
        now_kst().replace(tzinfo=None).isoformat() 관례상
        "2026-09-11T13:00:00"(naive, 타임존 표기 없음)로 기록됨.

        예전 문자열 비교: "...13:00:00+09:00" <= "...13:00:00" 는
        (짧은 쪽이 긴 쪽의 접두사이므로) 문자열 비교상 False가 되어
        run-B가 후보에서 빠지고 run-A로 잘못 귀속됐음. 이제는 같은
        순간이라는 datetime 값으로 비교하므로 run-B로 올바르게 연결됨.
        """
        baselines = [
            {"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"},
            {"run_id": "run-B", "started_at": "2026-09-11T13:00:00+09:00"},
        ]
        naive_log_ts_at_same_instant = "2026-09-11T13:00:00"
        result = resolve_run_id_for_timestamp(baselines, naive_log_ts_at_same_instant)
        self.assertEqual(
            result, "run-B",
            "새 실행이 시작한 바로 그 순간의 naive 로그가 이전 실행으로 잘못 귀속되었습니다.",
        )

    def test_aware_utc_offset_log_matches_aware_kst_baseline_at_same_real_instant(self):
        """버그(b) 재현(정정판): GPT가 실제로 재현한 것은 **타임존이 명시된**
        "+00:00"(UTC) 로그와 "+09:00"(KST) 기준선의 비교 오류였습니다
        (naive 값이 아니었습니다 — 최초 보고에서 이 부분을 잘못
        서술했다가, 2026-09-11 2차 검토에서 정정했습니다).

        run-A가 KST 13:00(="+09:00")에 시작. 같은 실제 순간에 UTC
        오프셋을 명시한 로그가 "2026-09-11T04:00:00+00:00"으로 남았다면
        (예: 서버가 UTC 타임존 정보를 포함해 정확히 직렬화하는 경우),
        예전 문자열 비교는 "13:00:00+09:00" vs "04:00:00+00:00"을
        그냥 문자로 비교해 실패했지만, 지금은 둘 다 datetime으로 파싱해
        실제 시각(둘 다 같은 UTC 인스턴트)으로 비교하므로 **정상적으로
        매칭됩니다** — 이 사례는 이번 수정으로 완전히 해결됐습니다.
        """
        baselines = [{"run_id": "run-A", "started_at": "2026-09-11T13:00:00+09:00"}]
        aware_utc_log_at_same_real_instant = "2026-09-11T04:00:00+00:00"
        result = resolve_run_id_for_timestamp(baselines, aware_utc_log_at_same_real_instant)
        self.assertEqual(
            result, "run-A",
            "타임존이 명시된 UTC 로그와 KST 기준선은 이제 실제 시각으로 정확히 비교되어야 합니다.",
        )

    def test_naive_value_that_is_actually_utc_system_clock_remains_a_known_limitation(self):
        """이번 수정의 알려진 한계(위 버그(b)와는 다른 별개 사례):
        타임존 표기가 아예 없는(naive) 로그가 실제로는 UTC 시스템 시계
        값인 경우입니다 — 예: domain/service/trading_service.py의
        `_write_trade_log()`가 쓰는 `datetime.now().isoformat()`이
        UTC로 설정된 서버(AWS 등)에서 남긴 값. run-A가 KST 13:00
        (UTC 04:00)에 시작했고, 이 케이스의 로그는 같은 실제 순간이라도
        "2026-09-11T04:00:00"(오프셋 표기 없는 naive 값)처럼 보입니다.

        parse_run_timestamp()는 naive 입력을 "KST 벽시계 시각"이라고
        **가정**합니다(대다수 로그가 실제로 그렇게 기록되므로) — 이
        가정은 naive 값이 실제로는 UTC인 이 경우까지는 자동으로 고치지
        못합니다. 이는 이 함수의 알려진 한계이자 이번 수정 범위
        밖입니다(_write_trade_log() 자체의 naive 시스템 시계 문제를
        고쳐야 하는 별개 작업, 후속 목록에 기록). "고쳤다"고 주장하지
        않기 위해 이 실패 사례를 회귀 테스트로 그대로 남겨둡니다.
        """
        baselines = [{"run_id": "run-A", "started_at": "2026-09-11T13:00:00+09:00"}]
        naive_value_actually_utc = "2026-09-11T04:00:00"
        result = resolve_run_id_for_timestamp(baselines, naive_value_actually_utc)
        self.assertIsNone(
            result,
            "naive 값이 실제로는 UTC 시스템 시계인 경우는 이번 수정의 알려진 한계입니다 — "
            "parse_run_timestamp()가 naive 입력을 KST로 가정하는 한, 이 로그는 "
            "13:00 KST 시작보다 훨씬 이전 시각(04:00 KST)으로 잘못 해석되어 "
            "run-A보다 앞선다고 판단되므로 매칭되지 않습니다. 이 함수를 쓰는 "
            "쪽(export_daily_bundle.py)이 [RUN_BASELINE]/[CONFIG_SNAPSHOT_MISSING] "
            "로그 태그와 UNRESOLVED 표시로 이런 불일치를 보완해야 합니다.",
        )

    def test_boundary_exact_same_instant_is_inclusive(self):
        """거래 timestamp가 실행 시작과 정확히 같은 순간이면(경계값)
        그 실행에 포함되어야 합니다(<=, 배타적 <이 아님) — 실행이
        시작하자마자 남긴 최초 로그가 자기 자신의 run에 연결되도록.
        """
        baselines = [{"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"}]
        result = resolve_run_id_for_timestamp(baselines, "2026-09-11T09:00:00+09:00")
        self.assertEqual(result, "run-A")

    def test_invalid_timestamp_input_returns_none_without_crashing(self):
        baselines = [{"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"}]
        for bad in ("not-a-timestamp", "", "2026-13-99T99:99:99", "   "):
            with self.subTest(bad=bad):
                self.assertIsNone(resolve_run_id_for_timestamp(baselines, bad))

    def test_invalid_started_at_in_baseline_row_is_skipped_not_crashed(self):
        """일부 행의 started_at이 깨져 있어도(예: 수동 편집 실수) 크래시하지
        않고 그 행만 후보에서 제외해야 합니다 — fail-closed."""
        baselines = [
            {"run_id": "run-broken", "started_at": "garbage"},
            {"run_id": "run-A", "started_at": "2026-09-11T09:00:00+09:00"},
        ]
        result = resolve_run_id_for_timestamp(baselines, "2026-09-11T10:00:00+09:00")
        self.assertEqual(result, "run-A")


class TestParseRunTimestamp(unittest.TestCase):

    def test_naive_input_is_assumed_kst(self):
        dt = parse_run_timestamp("2026-09-11T13:00:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo, "naive 입력도 KST_TZ가 붙어 aware가 되어야 합니다.")
        self.assertEqual(dt.utcoffset().total_seconds(), 9 * 3600)

    def test_aware_input_is_preserved(self):
        dt = parse_run_timestamp("2026-09-11T13:00:00+09:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 9 * 3600)

    def test_none_and_empty_return_none(self):
        self.assertIsNone(parse_run_timestamp(None))
        self.assertIsNone(parse_run_timestamp(""))

    def test_malformed_string_returns_none_not_exception(self):
        for bad in ("완전히 잘못된 값", "2026/09/11", "13:00:00"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_run_timestamp(bad))


class TestSaveEffectiveConfigSnapshot(unittest.TestCase):

    def test_snapshot_file_created_with_expected_content_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            settings = dataclasses.replace(
                settings,
                broker=dataclasses.replace(settings.broker, app_key="SECRET_APP_KEY_VALUE"),
            )
            baseline = capture_run_baseline(settings, repo_root=".")

            snapshot_dir = f"{tmpdir}/run_baseline_configs"
            path = save_effective_config_snapshot(baseline, settings, snapshot_dir)

            self.assertIsNotNone(path)
            self.assertTrue(Path(path).exists())

            import json
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], baseline.run_id)
            self.assertEqual(payload["effective_config_hash"], baseline.effective_config_hash)
            dump = json.dumps(payload, default=str)
            self.assertNotIn("SECRET_APP_KEY_VALUE", dump)
            self.assertEqual(payload["config"]["broker"]["app_key"], "***REDACTED***")

    def test_failure_returns_none_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            baseline = capture_run_baseline(settings, repo_root=".")

            # snapshot_dir의 부모 경로 자리에 파일을 만들어 mkdir이
            # 반드시 실패하게 만듦(디렉터리를 만들 수 없는 상황 재현).
            blocker = Path(tmpdir) / "blocker"
            blocker.write_text("x")
            snapshot_dir = str(blocker / "nested" / "run_baseline_configs")

            result = save_effective_config_snapshot(baseline, settings, snapshot_dir)
            self.assertIsNone(result, "쓰기 실패는 예외를 던지지 않고 None으로 처리해야 합니다.")


class _FakeLogger:
    """app_logger 대역 — .info()/.warning() 호출 내용만 기록합니다."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


class TestPerformRunBaselineStartup(unittest.TestCase):
    """2026-09-11 (B01 v2 재검토): app/main.py에 있던 시작 블록을
    infra/storage/run_baseline.py로 옮긴 perform_run_baseline_startup()의
    단위 테스트. 민우님/GPT가 지적한 "설정 스냅샷 저장 실패가 조용히
    삼켜짐" 문제의 회귀 테스트입니다.
    """

    def test_normal_success_logs_run_baseline_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            settings = dataclasses.replace(
                settings,
                storage=dataclasses.replace(
                    settings.storage, run_baseline_log_file=f"{tmpdir}/logs/run_baseline.csv"
                ),
            )
            logger = _FakeLogger()

            result = perform_run_baseline_startup(settings, logger, repo_root=".")

            self.assertIsNotNone(result)
            self.assertEqual(len(logger.infos), 1)
            self.assertIn("[RUN_BASELINE]", logger.infos[0])
            self.assertEqual(logger.warnings, [], "정상 경로에서는 경고가 남으면 안 됩니다.")
            self.assertTrue(Path(f"{tmpdir}/logs/run_baseline.csv").exists())
            self.assertTrue(
                Path(f"{tmpdir}/logs/run_baseline_configs/{result.run_id}.json").exists()
            )

    def test_config_snapshot_failure_is_logged_but_does_not_block_startup(self):
        """회귀 테스트: save_effective_config_snapshot()이 None을 반환해도
        예전에는 [CONFIG_SNAPSHOT_MISSING] 경고가 전혀 남지 않았습니다.
        run_baseline.csv 기록 자체(매매에 직접 영향 없는 관측 기능이지만
        B01의 핵심 산출물)는 여전히 성공해야 합니다.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            settings = dataclasses.replace(
                settings,
                storage=dataclasses.replace(
                    settings.storage, run_baseline_log_file=f"{tmpdir}/logs/run_baseline.csv"
                ),
            )
            # config snapshot 디렉터리 자리에 파일을 만들어 mkdir이
            # 반드시 실패하게 함(디렉터리를 만들 수 없는 상황 재현).
            Path(f"{tmpdir}/logs").mkdir(parents=True)
            Path(f"{tmpdir}/logs/run_baseline_configs").write_text("blocker")
            logger = _FakeLogger()

            result = perform_run_baseline_startup(settings, logger, repo_root=".")

            self.assertIsNotNone(result, "설정 스냅샷 실패가 run_baseline 기록 자체를 막으면 안 됩니다.")
            self.assertTrue(
                Path(f"{tmpdir}/logs/run_baseline.csv").exists(),
                "run_baseline.csv 기록은 설정 스냅샷과 무관하게 성공해야 합니다.",
            )
            self.assertEqual(len(logger.infos), 1)
            self.assertIn("[RUN_BASELINE]", logger.infos[0])
            self.assertEqual(len(logger.warnings), 1)
            self.assertIn("[CONFIG_SNAPSHOT_MISSING]", logger.warnings[0])
            self.assertIn(result.run_id, logger.warnings[0])

    def test_top_level_failure_logs_run_baseline_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            # run_baseline_log_file의 부모 경로 자리에 파일을 둬서
            # RunBaselineLogger.log()의 디렉터리 생성 자체가 실패하게 함.
            blocker = Path(tmpdir) / "blocker"
            blocker.write_text("x")
            settings = dataclasses.replace(
                settings,
                storage=dataclasses.replace(
                    settings.storage,
                    run_baseline_log_file=str(blocker / "nested" / "run_baseline.csv"),
                ),
            )
            logger = _FakeLogger()

            result = perform_run_baseline_startup(settings, logger, repo_root=".")

            self.assertIsNone(result)
            self.assertEqual(logger.infos, [])
            self.assertEqual(len(logger.warnings), 1)
            self.assertIn("[RUN_BASELINE] 기록 실패", logger.warnings[0])


if __name__ == "__main__":
    unittest.main()
