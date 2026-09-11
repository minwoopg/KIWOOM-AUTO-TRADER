"""2026-09-11: KakaoNotifier.verify_account()/mask_kakao_id() 자체 점검 기능 테스트.

여러 사람이 각자 컴퓨터에서 각자 카카오 토큰으로 이 프로그램을 돌릴 때,
시작 시점에 "지금 이 토큰이 어느 계정 것인지"를 바로 확인하기 위한
기능입니다. 네트워크 호출 없이 requests.get/post를 Mock으로 대체해
결정론적으로 검증합니다. 매매 로직과는 무관합니다.

2026-09-11 GPT reclosure 반영: 계정 확인 + 시작 알림 전송을 trading/
WebSocket startup critical path에서 분리한 백그라운드 스레드 경로
(send_startup_notification_async/_run_startup_check_and_notify)의
(1) 논블로킹 동작, (2) 실패/예외 시 fail-open(시작 알림은 계속 시도),
(3) 닉네임이 app_logger에는 남지 않고 카카오 메시지 본문에만 포함되는지,
(4) 마스킹 표기가 "id ...*****1234" 식으로 중복되지 않는지를 추가로
검증합니다.

Run directly (official regression runner) or with pytest.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from infra.notify.kakao_notifier import (
    KakaoNotifier,
    mask_kakao_id,
    send_startup_notification_async,
    _run_startup_check_and_notify,
)


def _resp(status_code: int, json_data: dict | None = None, text: str = "") -> Mock:
    m = Mock()
    m.status_code = status_code
    m.json.return_value = json_data or {}
    m.text = text
    return m


class TestMaskKakaoId(unittest.TestCase):
    def test_masks_all_but_last_4(self):
        self.assertEqual(mask_kakao_id("123456789"), "*****6789")

    def test_short_id_fully_masked(self):
        self.assertEqual(mask_kakao_id("12"), "**")
        self.assertEqual(mask_kakao_id("1234"), "****")

    def test_empty_or_none_is_empty_string(self):
        self.assertEqual(mask_kakao_id(""), "")
        self.assertEqual(mask_kakao_id(None), "")


class TestVerifyAccount(unittest.TestCase):
    def test_disabled_notifier_returns_none_without_network_call(self):
        notifier = KakaoNotifier(access_token="")
        with patch("infra.notify.kakao_notifier.requests.get") as mock_get:
            self.assertIsNone(notifier.verify_account())
            mock_get.assert_not_called()

    def test_success_with_nickname(self):
        notifier = KakaoNotifier(access_token="tok-a")
        ok = _resp(200, {"id": 987654321, "kakao_account": {"profile": {"nickname": "홍길동"}}})
        with patch("infra.notify.kakao_notifier.requests.get", return_value=ok):
            info = notifier.verify_account()
        self.assertEqual(info, {"id": "987654321", "nickname": "홍길동"})

    def test_success_without_nickname_scope_still_returns_id(self):
        # 닉네임 동의항목이 없거나 비공개면 kakao_account가 아예 없을 수 있음.
        notifier = KakaoNotifier(access_token="tok-a")
        ok = _resp(200, {"id": 111})
        with patch("infra.notify.kakao_notifier.requests.get", return_value=ok):
            info = notifier.verify_account()
        self.assertEqual(info, {"id": "111", "nickname": None})

    def test_expired_token_refreshes_then_retries(self):
        notifier = KakaoNotifier(access_token="old", refresh_token="rt", rest_api_key="key")
        expired = _resp(401, text="expired")
        refreshed_ok = _resp(200, {"id": 42, "kakao_account": {"profile": {"nickname": "새계정"}}})
        refresh_ok = _resp(200, {"access_token": "new-token"})
        with patch("infra.notify.kakao_notifier.requests.get", side_effect=[expired, refreshed_ok]), \
             patch("infra.notify.kakao_notifier.requests.post", return_value=refresh_ok):
            info = notifier.verify_account()
        self.assertEqual(info, {"id": "42", "nickname": "새계정"})
        self.assertEqual(notifier.access_token, "new-token")

    def test_http_error_returns_none_without_raising(self):
        notifier = KakaoNotifier(access_token="tok-a")
        bad = _resp(500, text="server error")
        with patch("infra.notify.kakao_notifier.requests.get", return_value=bad):
            self.assertIsNone(notifier.verify_account())

    def test_network_exception_returns_none_without_raising(self):
        notifier = KakaoNotifier(access_token="tok-a")
        with patch("infra.notify.kakao_notifier.requests.get", side_effect=ConnectionError("no network")):
            self.assertIsNone(notifier.verify_account())

    def test_missing_id_in_response_returns_none(self):
        notifier = KakaoNotifier(access_token="tok-a")
        weird = _resp(200, {})
        with patch("infra.notify.kakao_notifier.requests.get", return_value=weird):
            self.assertIsNone(notifier.verify_account())

    def test_enabled_property_reflects_token_presence(self):
        self.assertTrue(KakaoNotifier(access_token="x").enabled)
        self.assertFalse(KakaoNotifier(access_token="").enabled)


def _join_kakao_startup_thread(timeout: float = 2.0) -> None:
    """테스트가 끝나기 전에 백그라운드 스레드가 완료되도록 기다립니다."""
    for t in threading.enumerate():
        if t.name == "kakao-startup-notify":
            t.join(timeout=timeout)


class TestSendStartupNotificationAsyncIsNonBlocking(unittest.TestCase):
    """P1 reclosure 핵심: 계정 확인이 느려도 trading startup을 지연시키면 안 됨."""

    def tearDown(self):
        _join_kakao_startup_thread()

    def test_returns_immediately_even_if_verify_account_is_slow(self):
        notifier = Mock()
        notifier.enabled = True

        def slow_verify():
            time.sleep(0.3)  # verify_account()가 401→refresh→재조회로 지연되는 상황을 흉내
            return {"id": "123456789", "nickname": "느린계정"}

        notifier.verify_account.side_effect = slow_verify
        logger = Mock()

        started = time.monotonic()
        send_startup_notification_async(notifier, logger, "모의투자", "09:00", [1, 2, 3])
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed, 0.05,
            "verify_account()가 느려도 send_startup_notification_async()는 "
            "즉시 반환해야 trading/WebSocket 시작이 지연되지 않습니다.",
        )
        _join_kakao_startup_thread()
        notifier.send.assert_called_once()  # 백그라운드에서 결국 전송은 됨

    def test_disabled_notifier_does_nothing(self):
        notifier = Mock()
        notifier.enabled = False
        logger = Mock()
        send_startup_notification_async(notifier, logger, "모의투자", "09:00", [1])
        _join_kakao_startup_thread()
        notifier.verify_account.assert_not_called()
        notifier.send.assert_not_called()


class TestRunStartupCheckAndNotify(unittest.TestCase):
    """백그라운드 스레드 본체 로직 — 스레드 없이 동기 호출해 결정론적으로 검증."""

    def test_verify_account_returning_none_still_sends_start_message(self):
        notifier = Mock()
        notifier.enabled = True
        notifier.verify_account.return_value = None
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1, 2, 3])

        notifier.send.assert_called_once()
        sent_text = notifier.send.call_args[0][0]
        self.assertNotIn("알림 수신 계정", sent_text)

    def test_verify_account_raising_exception_still_sends_start_message(self):
        """계정 확인 자체가 예외를 던져도(네트워크 단절 등) fail-open — 시작 알림은 계속 시도."""
        notifier = Mock()
        notifier.enabled = True
        notifier.verify_account.side_effect = RuntimeError("network down")
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1])

        notifier.send.assert_called_once()

    def test_send_exception_does_not_propagate(self):
        """카카오 전송 자체가 실패해도 예외가 밖으로 새면 안 됨(트레이딩 시작에 영향 없어야 함)."""
        notifier = Mock()
        notifier.enabled = True
        notifier.verify_account.return_value = {"id": "123456789", "nickname": "홍길동"}
        notifier.send.side_effect = RuntimeError("kakao down")
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1])  # 예외 없이 반환돼야 함

    def test_nickname_never_appears_in_logs_only_in_message_text(self):
        notifier = Mock()
        notifier.enabled = True
        notifier.verify_account.return_value = {"id": "123456789", "nickname": "홍길동"}
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1, 2, 3])

        logged_texts = [
            str(c.args[0]) for c in (logger.info.call_args_list + logger.warning.call_args_list)
        ]
        for text in logged_texts:
            self.assertNotIn("홍길동", text, "카카오 닉네임은 app.log(→ 일일 번들)에 남으면 안 됩니다.")

        sent_text = notifier.send.call_args[0][0]
        self.assertIn("홍길동", sent_text, "닉네임은 카카오 메시지 본문에는 표시돼야 합니다(사람이 눈으로 확인).")

    def test_masked_id_shown_without_duplicate_ellipsis(self):
        notifier = Mock()
        notifier.enabled = True
        notifier.verify_account.return_value = {"id": "987654321", "nickname": None}
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1])

        sent_text = notifier.send.call_args[0][0]
        self.assertIn("id *****4321", sent_text)
        self.assertNotIn("...*", sent_text)

    def test_disabled_notifier_skips_everything(self):
        notifier = Mock()
        notifier.enabled = False
        logger = Mock()

        _run_startup_check_and_notify(notifier, logger, "모의투자", "09:00", [1])

        notifier.verify_account.assert_not_called()
        notifier.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
