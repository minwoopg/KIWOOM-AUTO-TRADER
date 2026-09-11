"""2026-09-11 (GPT reclosure, 2차): 카카오 토큰 lifecycle 안정성 테스트.

세 가지를 검증합니다:
1. TradingService와 시작 알림/계정 확인이 같은 KakaoNotifier 인스턴스를
   공유하는지(별도 인스턴스로 나뉘면 한쪽이 토큰을 갱신해도 다른 쪽은
   예전 토큰을 그대로 들고 있다가 나중에 갱신 실패할 수 있음).
2. client_secret 옵션 지원(카카오 앱의 "Client Secret 사용함" 대응)과
   기존(꺼진 앱) 하위호환.
3. 토큰 갱신의 동시성 안전성(중복 갱신 방지)과, 회전된 토큰이 재시작
   후에도 이어지도록 하는 영속화.

2026-09-11 (GPT reclosure, 3차) 추가 검증:
4. 실제 send()/verify_account() 메서드를 진짜 스레드 두 개로 동시
   호출했을 때도 refresh POST가 정확히 1번만 나가는지(기존 3번 검증은
   _refresh_access_token()을 직접 호출하는 격리 테스트라 이 실제 경로의
   버그를 못 잡았음).
5. 저장된 토큰 상태가 지금 .env의 refresh_token과 같은 계정인지
   (seed_refresh_token_fingerprint로) 확인해서, 다른 사람의 .env로
   바뀌면 옛 상태를 버리고 새 .env 값을 쓰는지.
6. 갱신 실패 로그에 카카오 응답 원문이 남지 않는지.

네트워크 호출 없이 requests.post를 Mock으로 대체해 결정론적으로
검증합니다. 매매 로직과는 무관합니다.

Run directly (official regression runner) or with pytest.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infra.notify.kakao_notifier import (
    KakaoNotifier,
    build_notifier,
    _load_persisted_tokens,
    _fingerprint_token,
    _safe_kakao_error_code,
)


def _resp(status_code: int, json_data: dict | None = None, text: str = "") -> Mock:
    m = Mock()
    m.status_code = status_code
    m.json.return_value = json_data or {}
    m.text = text
    return m


class TestSharedNotifierBetweenTradingServiceAndStartup(unittest.TestCase):
    """P1 BLOCKER: TradingService와 시작 알림이 같은 notifier 인스턴스를 써야 함."""

    def test_trading_service_uses_injected_notifier_instance(self):
        from test_run_once_integration import build_minimal_settings
        from domain.market_regime.classifier import MarketRegimeClassifier
        from domain.risk.risk_manager import RiskManager
        from domain.service.trading_service import TradingService
        from domain.strategy.strategy_router import StrategyRouter
        from infra.broker.mock_broker import MockBroker
        from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
        from infra.storage.state_store import JsonStateStore

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            shared_notifier = KakaoNotifier(access_token="shared-token")

            service = TradingService(
                settings=settings,
                broker=MockBroker(),
                strategy_router=StrategyRouter(settings.strategy),
                regime_classifier=MarketRegimeClassifier(settings.market_regime),
                risk_manager=RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file),
                app_logger=build_app_logger(settings.storage.app_log_file, settings.app.log_level),
                trade_logger=TradeCsvLogger(settings.storage.trade_log_file),
                signal_logger=SignalCsvLogger(settings.storage.signal_log_file),
                state_store=JsonStateStore(settings.storage.state_file),
                notifier=shared_notifier,
            )

            self.assertIs(
                service._notifier, shared_notifier,
                "TradingService는 주입된 notifier를 그대로 써야 합니다 "
                "(내부에서 별도로 build_notifier()를 다시 호출해 새 인스턴스를 "
                "만들면, 토큰이 갱신됐을 때 서로 다른 상태를 갖게 됩니다).",
            )

    def test_trading_service_falls_back_to_build_notifier_when_none(self):
        """notifier를 안 넘기면(기존 호출부/테스트) 기존처럼 자체 생성 — 하위호환."""
        import tempfile as _tempfile
        from test_run_once_integration import build_minimal_settings
        from domain.market_regime.classifier import MarketRegimeClassifier
        from domain.risk.risk_manager import RiskManager
        from domain.service.trading_service import TradingService
        from domain.strategy.strategy_router import StrategyRouter
        from infra.broker.mock_broker import MockBroker
        from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
        from infra.storage.state_store import JsonStateStore

        with _tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            service = TradingService(
                settings=settings,
                broker=MockBroker(),
                strategy_router=StrategyRouter(settings.strategy),
                regime_classifier=MarketRegimeClassifier(settings.market_regime),
                risk_manager=RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file),
                app_logger=build_app_logger(settings.storage.app_log_file, settings.app.log_level),
                trade_logger=TradeCsvLogger(settings.storage.trade_log_file),
                signal_logger=SignalCsvLogger(settings.storage.signal_log_file),
                state_store=JsonStateStore(settings.storage.state_file),
                # notifier 생략
            )
            self.assertIsNotNone(service._notifier)  # 토큰이 없으므로 비활성 notifier가 자체 생성됨
            self.assertFalse(service._notifier.enabled)

    def test_build_trading_service_forwards_notifier(self):
        """app/main.py의 build_trading_service()가 notifier를 그대로 전달하는지."""
        import tempfile as _tempfile
        from app.main import build_trading_service
        from test_run_once_integration import build_minimal_settings
        from infra.broker.mock_broker import MockBroker
        from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
        from infra.storage.state_store import JsonStateStore

        with _tempfile.TemporaryDirectory() as tmpdir:
            settings = build_minimal_settings(tmpdir)
            shared_notifier = KakaoNotifier(access_token="shared-token-2")
            service = build_trading_service(
                settings, MockBroker(),
                build_app_logger(settings.storage.app_log_file, settings.app.log_level),
                TradeCsvLogger(settings.storage.trade_log_file),
                SignalCsvLogger(settings.storage.signal_log_file),
                JsonStateStore(settings.storage.state_file),
                notifier=shared_notifier,
            )
            self.assertIs(service._notifier, shared_notifier)


class TestClientSecretRefresh(unittest.TestCase):
    """2026년 기준 "Client Secret 사용함" 앱 지원 + 기존(꺼진 앱) 하위호환."""

    def test_refresh_includes_client_secret_when_set(self):
        notifier = KakaoNotifier(
            access_token="old", refresh_token="rt", rest_api_key="key",
            client_secret="secret-value",
        )
        ok = _resp(200, {"access_token": "new"})
        with patch("infra.notify.kakao_notifier.requests.post", return_value=ok) as mock_post:
            self.assertTrue(notifier._refresh_access_token("old"))
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_data["client_secret"], "secret-value")

    def test_refresh_omits_client_secret_when_not_set(self):
        """기존 앱(Client Secret 꺼짐)은 이 필드 자체가 요청에 없어야 기존과 동일하게 동작."""
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")
        ok = _resp(200, {"access_token": "new"})
        with patch("infra.notify.kakao_notifier.requests.post", return_value=ok) as mock_post:
            self.assertTrue(notifier._refresh_access_token("old"))
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("client_secret", sent_data)


class TestConcurrentRefreshDoesNotDoubleRefresh(unittest.TestCase):
    """동시성: TradingService와 시작 알림이 같은 notifier를 공유하므로,
    두 스레드가 거의 동시에 401을 만나 동시에 갱신을 시도할 수 있음.
    """

    def test_second_caller_skips_network_call_if_already_refreshed(self):
        """순차 재현 — 락을 얻었을 때 이미 다른 호출이 갱신을 끝냈으면 재요청하지 않음."""
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")
        ok = _resp(200, {"access_token": "new", "refresh_token": "new-rt"})
        with patch("infra.notify.kakao_notifier.requests.post", return_value=ok) as mock_post:
            self.assertTrue(notifier._refresh_access_token("old"))  # 첫 갱신 — 실제로 POST
            self.assertEqual(mock_post.call_count, 1)
            # 두 번째 호출자가 여전히 "old"를 만료된 토큰으로 알고 갱신을 요청
            self.assertTrue(notifier._refresh_access_token("old"))  # 이미 access_token != "old" → 생략
            self.assertEqual(mock_post.call_count, 1, "이미 갱신된 뒤라 두 번째 요청은 네트워크 호출을 하면 안 됩니다")

    def test_two_real_threads_racing_only_refresh_once(self):
        """실제 스레드 두 개가 거의 동시에 401을 만나는 상황을 재현.

        먼저 도착한 스레드가 락을 잡고 네트워크 호출 중(0.05초) 두 번째
        스레드가 뒤이어 락 대기에 들어가도록 살짝의 시작 지연을 둡니다.
        첫 스레드가 끝나 락을 놓으면, 두 번째 스레드는 그 시점에
        self.access_token이 이미 바뀐 걸 보고 네트워크 호출 없이
        성공으로 처리해야 합니다 — 즉 실제 POST는 1번만 나가야 합니다.
        """
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")

        def slow_post(*args, **kwargs):
            time.sleep(0.05)
            return _resp(200, {"access_token": "new", "refresh_token": "new-rt"})

        results = []
        with patch("infra.notify.kakao_notifier.requests.post", side_effect=slow_post) as mock_post:
            def worker():
                results.append(notifier._refresh_access_token("old"))

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            time.sleep(0.01)  # t1이 락을 잡고 네트워크 호출에 들어갈 시간을 줌
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

        self.assertEqual(results, [True, True])
        self.assertEqual(mock_post.call_count, 1, "락으로 직렬화되어 실제 네트워크 갱신 요청은 한 번만 나가야 합니다")
        self.assertEqual(notifier.access_token, "new")


class TestRealSendAndVerifyAccountConcurrencyDoesNotDoubleRefresh(unittest.TestCase):
    """3차 GPT reclosure: 위 TestConcurrentRefreshDoesNotDoubleRefresh는
    _refresh_access_token()을 직접(격리해서) 호출하는 테스트라, 실제
    send()/verify_account()의 401 처리 경로(요청 "전" 토큰 스냅샷 vs
    "후" 재조회)에 있던 진짜 동시성 버그는 잡아내지 못했습니다
    (검토자가 실제로 send()/verify_account()에서 중복 refresh POST 2회를
    재현). 여기서는 실제 send()/verify_account() 메서드를 진짜
    threading.Thread 두 개로 동시에 호출해, 두 스레드 모두 (아직 갱신
    전이라) 같은 만료된 토큰으로 요청을 보내 401을 받는 상황을
    재현합니다 — 실제 네트워크 refresh POST는 정확히 1번만 나가야
    합니다.
    """

    def test_two_real_threads_calling_send_only_refresh_once(self):
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")

        def fake_send_request(text, token):
            if token == "old":
                # 두 스레드 모두 아직 만료된 "old" 토큰으로 요청을 보낼
                # 시간을 벌어줌(첫 스레드가 갱신을 끝내기 전에 두 번째
                # 스레드도 이미 "old"로 요청을 보낸 상태여야 실제 버그가
                # 재현됨).
                time.sleep(0.03)
                return _resp(401)
            return _resp(200)

        def fake_refresh_post(*args, **kwargs):
            time.sleep(0.05)
            return _resp(200, {"access_token": "new", "refresh_token": "new-rt"})

        results = []
        with patch.object(notifier, "_send_request", side_effect=fake_send_request), \
             patch("infra.notify.kakao_notifier.requests.post", side_effect=fake_refresh_post) as mock_refresh_post:
            def worker():
                results.append(notifier.send("hi"))

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            time.sleep(0.01)
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

        self.assertEqual(results, [True, True])
        self.assertEqual(
            mock_refresh_post.call_count, 1,
            "실제 send() 경로에서 두 스레드가 거의 동시에 401을 만나도 "
            "refresh POST는 1번만 나가야 합니다(요청 전 스냅샷을 안 쓰면 "
            "2번 나가는 버그가 재현됨).",
        )
        self.assertEqual(notifier.access_token, "new")

    def test_two_real_threads_calling_verify_account_only_refresh_once(self):
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")

        def fake_user_info_request(token):
            if token == "old":
                time.sleep(0.03)
                return _resp(401)
            return _resp(200, {"id": "1234"})

        def fake_refresh_post(*args, **kwargs):
            time.sleep(0.05)
            return _resp(200, {"access_token": "new", "refresh_token": "new-rt"})

        results = []
        with patch.object(notifier, "_user_info_request", side_effect=fake_user_info_request), \
             patch("infra.notify.kakao_notifier.requests.post", side_effect=fake_refresh_post) as mock_refresh_post:
            def worker():
                results.append(notifier.verify_account())

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            time.sleep(0.01)
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

        self.assertEqual([r["id"] for r in results], ["1234", "1234"])
        self.assertEqual(
            mock_refresh_post.call_count, 1,
            "실제 verify_account() 경로에서도 refresh POST는 1번만 나가야 합니다",
        )
        self.assertEqual(notifier.access_token, "new")


class TestTokenPersistence(unittest.TestCase):
    """P1: 회전된 refresh_token이 재시작 후에도 이어져야 함(.env의 옛 값을 다시 읽지 않도록).

    3차 GPT reclosure: 단, "재시작 후에도 이어짐"은 같은 계정이 정상적으로
    회전된 경우로 한정됩니다 — data/ 폴더를 다른 사람 컴퓨터로 그대로
    복사해가고 그 사람이 자기 .env에 자기 토큰을 넣은 경우(계정 전환)까지
    옛 상태를 이어받으면 안 됩니다(실제 재현된 버그, 아래
    test_build_notifier_discards_* 참고).
    """

    def test_refresh_persists_new_tokens_to_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            seed_fp = _fingerprint_token("old-rt")
            notifier = KakaoNotifier(
                access_token="old", refresh_token="old-rt", rest_api_key="key",
                token_state_file=state_file,
                seed_refresh_token_fingerprint=seed_fp,
            )
            ok = _resp(200, {"access_token": "new", "refresh_token": "new-rt"})
            with patch("infra.notify.kakao_notifier.requests.post", return_value=ok):
                notifier._refresh_access_token("old")

            saved = json.loads(Path(state_file).read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "new")
            self.assertEqual(saved["refresh_token"], "new-rt")
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["seed_refresh_token_fingerprint"], seed_fp)

    def test_build_notifier_uses_persisted_tokens_when_same_account_rotated(self):
        """재시작 시, 같은 계정(.env refresh_token 지문 일치)이면 .env의 옛
        값 대신 상태 파일의 회전된 값을 써야 함 — 정상적인 토큰 회전
        연속성 케이스."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            # .env는 코드가 고치지 않으므로, 재시작해도 이 값은 최초 발급받은
            # (아직 회전 전) refresh_token 그대로입니다.
            env_refresh_token = "stale-env-refresh"
            seed_fp = _fingerprint_token(env_refresh_token)
            Path(state_file).write_text(
                json.dumps({
                    "schema_version": 2,
                    "seed_refresh_token_fingerprint": seed_fp,
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                }),
                encoding="utf-8",
            )
            settings = Mock()
            settings.kakao = Mock(
                access_token="stale-env-access",
                refresh_token=env_refresh_token,
                rest_api_key="key",
                client_secret="",
                token_state_file=state_file,
            )
            notifier = build_notifier(settings)
            self.assertEqual(notifier.access_token, "rotated-access")
            self.assertEqual(notifier.refresh_token, "rotated-refresh")

    def test_build_notifier_discards_persisted_state_when_env_refresh_token_changed(self):
        """P1 (3차 재현/수정): data/ 폴더를 새 컴퓨터로 그대로 복사해갔지만
        그 사람이 자기 .env에 자기(다른) refresh_token을 넣은 경우 —
        지문이 다르므로 이전 사람(민우님)의 저장된 토큰을 쓰면 안 되고
        새 사람의 .env 값을 그대로 써야 함. 이게 안 되면 새 사람
        카카오톡에는 알림이 전혀 가지 않는 문제가 재현됐었습니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            old_seed_fp = _fingerprint_token("original-person-refresh-token")
            Path(state_file).write_text(
                json.dumps({
                    "schema_version": 2,
                    "seed_refresh_token_fingerprint": old_seed_fp,
                    "access_token": "original-person-rotated-access",
                    "refresh_token": "original-person-rotated-refresh",
                }),
                encoding="utf-8",
            )
            settings = Mock()
            settings.kakao = Mock(
                access_token="new-person-env-access",
                refresh_token="new-person-env-refresh",  # 다른 사람의 .env 값 — 지문 불일치
                rest_api_key="key",
                client_secret="",
                token_state_file=state_file,
            )
            notifier = build_notifier(settings)
            self.assertEqual(notifier.access_token, "new-person-env-access")
            self.assertEqual(notifier.refresh_token, "new-person-env-refresh")
            self.assertEqual(
                notifier.seed_refresh_token_fingerprint,
                _fingerprint_token("new-person-env-refresh"),
                "새 사람의 .env 값이 다음 저장의 새 seed가 되어야 합니다",
            )

    def test_build_notifier_ignores_legacy_persisted_state_without_fingerprint(self):
        """구버전(지문 필드 없는) 상태 파일은 신뢰하지 않고 .env 값을 사용
        해야 함 — 업그레이드 직후 첫 실행에 안전한 기본값."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            Path(state_file).write_text(
                json.dumps({"access_token": "rotated-access", "refresh_token": "rotated-refresh"}),
                encoding="utf-8",
            )
            settings = Mock()
            settings.kakao = Mock(
                access_token="env-access",
                refresh_token="env-refresh",
                rest_api_key="key",
                client_secret="",
                token_state_file=state_file,
            )
            notifier = build_notifier(settings)
            self.assertEqual(notifier.access_token, "env-access")
            self.assertEqual(notifier.refresh_token, "env-refresh")

    def test_build_notifier_falls_back_to_settings_when_no_state_file_yet(self):
        """첫 실행(상태 파일 없음)에는 기존처럼 .env 값을 그대로 사용."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "does_not_exist.json")
            settings = Mock()
            settings.kakao = Mock(
                access_token="env-access",
                refresh_token="env-refresh",
                rest_api_key="key",
                client_secret="",
                token_state_file=state_file,
            )
            notifier = build_notifier(settings)
            self.assertEqual(notifier.access_token, "env-access")
            self.assertEqual(notifier.refresh_token, "env-refresh")

    def test_load_persisted_tokens_handles_corrupt_file_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "corrupt.json")
            Path(state_file).write_text("not valid json {{{", encoding="utf-8")
            self.assertIsNone(_load_persisted_tokens(state_file))

    def test_load_persisted_tokens_missing_file_returns_none(self):
        self.assertIsNone(_load_persisted_tokens("/nonexistent/path/kakao_token_state.json"))


class TestNoTokenValuesLogged(unittest.TestCase):
    """토큰 값 자체는 access_token이든 refresh_token이든 로그에 절대 남으면 안 됨."""

    def test_refresh_and_persist_cycle_never_logs_token_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            notifier = KakaoNotifier(
                access_token="OLD_SECRET_TOKEN_VALUE",
                refresh_token="OLD_SECRET_REFRESH_VALUE",
                rest_api_key="key",
                token_state_file=state_file,
            )
            ok = _resp(200, {"access_token": "NEW_SECRET_TOKEN_VALUE", "refresh_token": "NEW_SECRET_REFRESH_VALUE"})
            with patch("infra.notify.kakao_notifier.requests.post", return_value=ok), \
                 patch("infra.notify.kakao_notifier.logger") as mock_logger:
                notifier._refresh_access_token("OLD_SECRET_TOKEN_VALUE")

            all_log_calls = (
                mock_logger.info.call_args_list
                + mock_logger.warning.call_args_list
                + mock_logger.debug.call_args_list
                + mock_logger.error.call_args_list
            )
            logged_text = " ".join(str(c) for c in all_log_calls)
            for secret in (
                "OLD_SECRET_TOKEN_VALUE", "OLD_SECRET_REFRESH_VALUE",
                "NEW_SECRET_TOKEN_VALUE", "NEW_SECRET_REFRESH_VALUE",
            ):
                self.assertNotIn(secret, logged_text)

    def test_build_notifier_never_logs_fingerprint_or_token_values(self):
        """3차 reclosure: seed_refresh_token_fingerprint(SHA-256 지문) 자체도
        로그에 남으면 안 됨 — 지문은 원문 복원이 불가능한 단방향 해시지만,
        검토 요청에서 "지문 자체도 로그에 남기지 말 것"을 명시했습니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "kakao_token_state.json")
            settings = Mock()
            settings.kakao = Mock(
                access_token="ENV_ACCESS_SECRET",
                refresh_token="ENV_REFRESH_SECRET",
                rest_api_key="key",
                client_secret="",
                token_state_file=state_file,
            )
            with patch("infra.notify.kakao_notifier.logger") as mock_logger:
                notifier = build_notifier(settings)

            all_log_calls = (
                mock_logger.info.call_args_list
                + mock_logger.warning.call_args_list
                + mock_logger.debug.call_args_list
                + mock_logger.error.call_args_list
            )
            logged_text = " ".join(str(c) for c in all_log_calls)
            self.assertNotIn("ENV_ACCESS_SECRET", logged_text)
            self.assertNotIn("ENV_REFRESH_SECRET", logged_text)
            self.assertTrue(notifier.seed_refresh_token_fingerprint)
            self.assertNotIn(notifier.seed_refresh_token_fingerprint, logged_text)


class TestRefreshFailureLoggingIsSafe(unittest.TestCase):
    """갱신/전송/계정확인 실패 로그에 카카오 응답 원문(resp.text)이 그대로
    남지 않아야 함 — 응답 본문에 민감한 값이 섞여 나오지 않는다는 가정에
    기대지 않기 위함(3차 reclosure에서 refresh 실패 로그만 닫았다가, 4차
    reclosure에서 send()/verify_account()의 실패 로그도 같은 문제가 있음이
    재현·지적되어 함께 닫음 — "토큰 값은 어떤 실패 경로에서도 로그에
    남지 않는다"는 계약을 완전히 지키기 위함)."""

    def test_refresh_failure_log_does_not_include_raw_response_body(self):
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")
        bad = _resp(
            400,
            {"error": "invalid_grant", "error_code": "KOE322", "error_description": "SECRET_LEAK_CANARY_TEXT"},
            text='{"error": "invalid_grant", "error_code": "KOE322", "error_description": "SECRET_LEAK_CANARY_TEXT"}',
        )
        with patch("infra.notify.kakao_notifier.requests.post", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertFalse(notifier._refresh_access_token("old"))

        all_log_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.debug.call_args_list
            + mock_logger.error.call_args_list
        )
        logged_text = " ".join(str(c) for c in all_log_calls)
        self.assertNotIn("SECRET_LEAK_CANARY_TEXT", logged_text)
        # 상태코드는 진단을 위해 여전히 남아야 함
        self.assertIn("400", logged_text)

    def test_send_failure_log_does_not_leak_access_token_from_response_body(self):
        """4차 reclosure(재현된 문제): send() 실패 응답 body에 access
        token 문자열이 그대로 echo돼도 로그에 노출되면 안 됨."""
        notifier = KakaoNotifier(access_token="SECRET_ACCESS_TOKEN_VALUE", rest_api_key="key")
        bad = _resp(
            400,
            {"error": "server_error"},
            text='{"error": "server_error", "debug": "server echoed SECRET_ACCESS_TOKEN_VALUE"}',
        )
        with patch.object(notifier, "_send_request", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertFalse(notifier.send("hi"))

        all_log_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.debug.call_args_list
            + mock_logger.error.call_args_list
        )
        logged_text = " ".join(str(c) for c in all_log_calls)
        self.assertNotIn("SECRET_ACCESS_TOKEN_VALUE", logged_text)
        self.assertIn("400", logged_text)

    def test_verify_account_failure_log_does_not_leak_access_token_from_response_body(self):
        """4차 reclosure(재현된 문제): verify_account() 실패 응답 body에
        access token 문자열이 그대로 echo돼도 로그에 노출되면 안 됨."""
        notifier = KakaoNotifier(access_token="SECRET_ACCESS_TOKEN_VALUE", rest_api_key="key")
        bad = _resp(
            400,
            {"error": "server_error"},
            text='{"error": "server_error", "debug": "server echoed SECRET_ACCESS_TOKEN_VALUE"}',
        )
        with patch.object(notifier, "_user_info_request", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertIsNone(notifier.verify_account())

        all_log_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.debug.call_args_list
            + mock_logger.error.call_args_list
        )
        logged_text = " ".join(str(c) for c in all_log_calls)
        self.assertNotIn("SECRET_ACCESS_TOKEN_VALUE", logged_text)
        self.assertIn("400", logged_text)


class TestSafeErrorCodeExtractionIsWhitelisted(unittest.TestCase):
    """5차 GPT reclosure: _safe_kakao_error_code()가 JSON의 error_code/error
    필드 값을 검증 없이 그대로 반환하고 있어서, resp.text 원문은 안
    찍혀도 이 필드를 통해 임의 문자열(토큰 값 등)이 그대로 로그에 노출될
    수 있다는 점이 재현·지적됨 — "에러코드만 남긴다"가 아니라 "알려진
    형식/값만 남긴다"로 강화."""

    def test_arbitrary_error_field_value_is_not_leaked_via_refresh_failure_log(self):
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")
        bad = _resp(400, {"error": "SECRET_ACCESS_TOKEN_VALUE"})
        with patch("infra.notify.kakao_notifier.requests.post", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertFalse(notifier._refresh_access_token("old"))
        logged_text = " ".join(
            str(c) for c in mock_logger.warning.call_args_list + mock_logger.info.call_args_list
        )
        self.assertNotIn("SECRET_ACCESS_TOKEN_VALUE", logged_text)

    def test_arbitrary_error_field_value_is_not_leaked_via_send_failure_log(self):
        notifier = KakaoNotifier(access_token="SECRET_ACCESS_TOKEN_VALUE", rest_api_key="key")
        bad = _resp(400, {"error": "SECRET_ACCESS_TOKEN_VALUE"})
        with patch.object(notifier, "_send_request", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertFalse(notifier.send("hi"))
        logged_text = " ".join(
            str(c) for c in mock_logger.warning.call_args_list + mock_logger.info.call_args_list
        )
        self.assertNotIn("SECRET_ACCESS_TOKEN_VALUE", logged_text)

    def test_arbitrary_error_field_value_is_not_leaked_via_verify_account_failure_log(self):
        notifier = KakaoNotifier(access_token="SECRET_ACCESS_TOKEN_VALUE", rest_api_key="key")
        bad = _resp(400, {"error": "SECRET_ACCESS_TOKEN_VALUE"})
        with patch.object(notifier, "_user_info_request", return_value=bad), \
             patch("infra.notify.kakao_notifier.logger") as mock_logger:
            self.assertIsNone(notifier.verify_account())
        logged_text = " ".join(
            str(c) for c in mock_logger.warning.call_args_list + mock_logger.info.call_args_list
        )
        self.assertNotIn("SECRET_ACCESS_TOKEN_VALUE", logged_text)

    def test_arbitrary_error_code_field_value_is_not_leaked(self):
        """error_code 필드도 형식 검증 없이 통과시키면 안 됨(KOE+숫자만 허용)."""
        resp = _resp(400, {"error_code": "SECRET_ACCESS_TOKEN_VALUE"})
        self.assertEqual(_safe_kakao_error_code(resp), "")

    def test_known_kakao_error_code_still_surfaced(self):
        resp = _resp(400, {"error_code": "KOE322"})
        self.assertEqual(_safe_kakao_error_code(resp), "KOE322")

    def test_whitelisted_oauth_error_still_surfaced(self):
        resp = _resp(400, {"error": "invalid_grant"})
        self.assertEqual(_safe_kakao_error_code(resp), "invalid_grant")

    def test_non_whitelisted_oauth_error_string_is_not_surfaced(self):
        resp = _resp(400, {"error": "some_unexpected_error_value"})
        self.assertEqual(_safe_kakao_error_code(resp), "")

    def test_numeric_code_field_still_surfaced(self):
        resp = _resp(400, {"code": -401})
        self.assertEqual(_safe_kakao_error_code(resp), "-401")

    def test_non_integer_code_field_is_not_surfaced(self):
        resp = _resp(400, {"code": "SECRET_ACCESS_TOKEN_VALUE"})
        self.assertEqual(_safe_kakao_error_code(resp), "")


if __name__ == "__main__":
    unittest.main()
